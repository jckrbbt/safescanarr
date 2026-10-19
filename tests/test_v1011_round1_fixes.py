"""tests/test_v1011_round1_fixes.py — regression tests for the round-1
skeptic review (docs/v1.0.11-round1-fixes.md).

Each test mirrors a probe scenario from
/home/hermes/.hermes/profiles/skeptic/cache/scratch/probe_v1011.py and asserts
the FIXED (fail-closed) outcome:
  probe test_03 reset+same-token setup      -> old cookie 401
  probe test_04 token A->B->A               -> old cookie 401
  probe test_05 SS_TOKEN unreadable config  -> replay 401 (fail closed)
  probe test_06 config save vs epoch bump   -> epoch not rolled back
  probe test_07 login vs credential change  -> login rejected
  probe test_08 logout DELETE failure       -> 500, row still revocable after
  probe test_10 restore cleanup failure     -> 500, live DB untouched
  probe test_11 restore publication gap    -> no window with stale rows
  probe test_12 ephemeral fallback          -> remember disabled
  probe test_16 in-flight revoke vs CLI     -> in-flight session does NOT survive
  probe test_17 unreadable config import    -> clean exit 0, no crash
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlite3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASE = tempfile.mkdtemp(prefix="safescanarr-fixes-")
os.environ["BASE_DIR"] = _BASE
for _var in ("SS_TOKEN", "SS_PASSWORD_HASH", "SS_COOKIE_SECURE",
             "SS_SESSION_SECRET"):
    os.environ.pop(_var, None)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import config as cfg
import config_lock
from web import auth
import web.server as s

TOKEN = "fixes-test-token-" + "q" * 20
TOKEN_B = "another-test-token-" + "z" * 20
PW = "An0therP4ss&word"
COOKIE = "safescanarr_session"


class Round1FixTests(unittest.TestCase):

    def setUp(self):
        s._reset_db()
        # pristine DB file per test
        db_file = cfg.Config().DB_FILE
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(db_file + suffix)
            except FileNotFoundError:
                pass
        os.environ.pop("SS_TOKEN", None)
        auth.configure_token(TOKEN)
        s._init_session_secret()
        s.REMEMBER_DAYS = 30
        s._throttle.clear()
        s._rate_limit.clear()
        self.a = s.app.test_client()

    def tearDown(self):
        s._reset_db()
        os.environ.pop("SS_TOKEN", None)

    # helpers (mirror probe_v1011.py)
    def login(self, remember=False, client=None, secret=TOKEN):
        c = client or self.a
        s._rate_limit.clear()
        r = c.post("/login", json={"secret": secret, "remember": remember})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r

    def cookie(self, c=None):
        ck = (c or self.a).get_cookie(COOKIE)
        return ck.value if ck else None

    def replay(self, cookie, path="/api/stats"):
        c = s.app.test_client()
        c.set_cookie(COOKIE, cookie)
        return c.get(path).status_code

    def csrf(self, c=None):
        with (c or self.a).session_transaction() as ss:
            return ss["csrf"]

    def backup(self):
        dest = Path(_BASE) / "copy.db"
        if dest.exists():
            dest.unlink()
        con = sqlite3.connect(str(dest))
        db = s.get_db()
        try:
            db._con.backup(con)
        finally:
            s.release_db(db)
        con.close()
        return dest.read_bytes()

    def restore(self, blob, c=None):
        c = c or self.a
        return c.post("/api/backup/restore/db",
                      data={"file": (io.BytesIO(blob), "backup.db")},
                      headers={"X-CSRF-Token": self.csrf(c)})

    def worker(self, fn):
        result = {}
        def run():
            try:
                result["value"] = fn()
            except BaseException as e:  # noqa: BLE001
                result["error"] = repr(e)
        t = threading.Thread(target=run, name="probe-request")
        t.start()
        return t, result

    def finish(self, t, result):
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertNotIn("error", result)
        return result["value"]

    # probe test_03 equivalent ────────────────────────────────────────
    def test_03_reset_setup_same_token_stays_revoked(self):
        self.login(True)
        old = self.cookie()
        auth.reset()
        c = s.app.test_client()
        r = c.post("/setup", json={"method": "token", "token": TOKEN})
        status = self.replay(old)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(status, 401,
                         "reset + same-token setup must not resurrect cookies")
        self.assertGreaterEqual(cfg.raw()["session_epoch"], 1)

    # probe test_04 equivalent ────────────────────────────────────────
    def test_04_token_A_B_A_stays_revoked(self):
        self.login(False)
        old = self.cookie()
        auth.configure_token(TOKEN_B)
        changed = self.replay(old)
        auth.configure_token(TOKEN)
        restored = self.replay(old)
        self.assertEqual([changed, restored], [401, 401],
                         "token A->B->A must not resurrect old cookies")

    # probe test_05 equivalent ────────────────────────────────────────
    def test_05_ss_token_unreadable_config_fails_closed(self):
        os.environ["SS_TOKEN"] = TOKEN
        try:
            self.login(False)
            old = self.cookie()
            with s.db_lease():
                pass  # ensure the shared DB exists before revocation
            auth.revoke_all_sessions()
            revoked = self.replay(old)
            path = Path(cfg._config_path())
            before = path.read_text()
            path.write_text("{invalid json")
            try:
                broken = self.replay(old)
            finally:
                path.write_text(before)
            healthy = self.replay(old)
            self.assertEqual([revoked, broken, healthy], [401, 401, 401],
                             "unreadable config must fail closed, never "
                             "substitute epoch 0")
        finally:
            os.environ.pop("SS_TOKEN", None)

    # probe test_06 equivalent ────────────────────────────────────────
    def test_06_config_save_cannot_rollback_epoch(self):
        # Stale-snapshot rollback: pause the config-save's WRITE while a
        # revoke bumps the epoch. With the cross-process config lock the
        # revoke serializes after the save (never interleaves inside it), and
        # the monotonic clamp guarantees epoch can only move forward — so the
        # revoked cookie stays 401 either way.
        self.login(False)
        old = self.cookie()
        operator = s.app.test_client()
        self.login(False, operator)
        entered = threading.Event()
        release = threading.Event()
        real_save = cfg.save

        def delayed_save(data):
            if threading.current_thread().name == "probe-request":
                entered.set()
                assert release.wait(15)
            return real_save(data)

        op_done = threading.Event()
        op_result = {}

        def op_run():
            op_result["resp"] = operator.post(
                "/api/auth/revoke-sessions",
                headers={"X-CSRF-Token": self.csrf(operator)})
            op_done.set()

        with patch.object(cfg, "save", delayed_save), \
                patch.object(s.config_module, "save", delayed_save):
            t, result = self.worker(lambda: self.a.post(
                "/api/config", json={"web_ui_url": "http://example.invalid"},
                headers={"X-CSRF-Token": self.csrf()}))
            self.assertTrue(entered.wait(10))
            epoch_before = cfg.raw()["session_epoch"]
            t_op = threading.Thread(target=op_run, name="operator")
            t_op.start()
            time.sleep(1.0)  # let the revoke reach (and block on) the lock
            release.set()
            saved = self.finish(t, result)
            self.assertTrue(op_done.wait(15))
            t_op.join(15)
        revived = self.replay(old)
        epoch_after = cfg.raw()["session_epoch"]
        self.assertEqual(op_result["resp"].status_code, 200)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(revoked_status := revived, 401,
                         "stale config save must not revive a revoked cookie")
        self.assertGreaterEqual(epoch_after, epoch_before + 1,
                                "session_epoch must be monotonic across a "
                                "concurrent save + revoke")

    def test_06b_mutate_never_lowers_epoch(self):
        # Deterministic unit probe of the monotonic clamp: even a mutator
        # built from a stale snapshot cannot write a lower generation.
        before = cfg.raw()["session_epoch"]
        cfg_set = {"session_epoch": max(0, before - 5)}
        config_lock.mutate(lambda c: c.update(cfg_set))
        self.assertEqual(cfg.raw()["session_epoch"], before)
        # and explicit bumps still work
        out = config_lock.mutate(
            lambda c: c.__setitem__(
                "session_epoch", int(c.get("session_epoch") or 0) + 1))
        self.assertEqual(out["session_epoch"], before + 1)

    # probe test_07 equivalent ────────────────────────────────────────
    def test_07_login_rejected_on_credential_change_race(self):
        entered = threading.Event()
        release = threading.Event()
        real = auth.verify

        def delayed_verify(secret):
            ok = real(secret)
            if threading.current_thread().name == "probe-request" \
                    and ok is not None:
                entered.set()
                assert release.wait(10)
            return ok

        with patch.object(auth, "verify", delayed_verify):
            t, result = self.worker(
                lambda: self.a.post("/login",
                                    json={"secret": TOKEN, "remember": True}))
            self.assertTrue(entered.wait(10))
            auth.configure_password(PW)
            release.set()
            r = self.finish(t, result)
        status = self.a.get("/api/stats").status_code
        self.assertEqual(r.status_code, 401,
                         "login verified under old credential must be rejected")
        self.assertEqual(status, 401)
        self.assertIsNone(auth.verify(TOKEN))

    # probe test_08 equivalent ────────────────────────────────────────
    def test_08_logout_delete_failure_fails_closed(self):
        self.login(True)
        old = self.cookie()
        db = s.get_db()
        try:
            db._con.execute(
                "CREATE TRIGGER deny_session_delete BEFORE DELETE ON web_sessions "
                "BEGIN SELECT RAISE(FAIL, 'injected storage failure'); END")
            db._con.commit()
            r = self.a.post("/logout", headers={"X-CSRF-Token": self.csrf()})
            db._con.execute("DROP TRIGGER deny_session_delete")
            db._con.commit()
        finally:
            s.release_db(db)
        # storage recovered: row still present, so the operator can retry
        self.assertEqual(r.status_code, 500,
                         "logout must report failure when rid deletion fails")
        # after storage recovers, a retry must succeed and then revoke
        r2 = self.a.post("/logout", headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r2.status_code, 200)
        status = self.replay(old)
        self.assertEqual(status, 401)

    # probe test_10 equivalent ────────────────────────────────────────
    def test_10_restore_sanitization_failure_returns_500(self):
        self.login(True)
        b = s.app.test_client()
        self.login(True, b)
        old = self.cookie(b)
        blob = self.backup()
        b.post("/logout", headers={"X-CSRF-Token": self.csrf(b)})
        before = self.replay(old)
        from database import Database
        with patch.object(Database, "create_web_session",
                          side_effect=sqlite3.OperationalError(
                              "injected disk write failure")):
            r = self.restore(blob)
        after = self.replay(old)
        self.assertEqual(before, 401)
        self.assertEqual(r.status_code, 500,
                         "restore must fail (500) when sanitization fails")
        self.assertEqual(after, 401,
                         "failed restore must leave no live stale rows")

    # probe test_11 equivalent ────────────────────────────────────────
    def test_11_restore_has_no_publication_gap(self):
        self.login(True)
        b = s.app.test_client()
        self.login(True, b)
        old = self.cookie(b)
        blob = self.backup()
        b.post("/logout", headers={"X-CSRF-Token": self.csrf(b)})
        entered = threading.Event()
        release = threading.Event()
        from database import Database
        real = Database.create_web_session

        def delayed_seed(db, *a, **kw):
            if threading.current_thread().name == "probe-request":
                entered.set()
                assert release.wait(10)
            return real(db, *a, **kw)

        before = self.replay(old)
        with patch.object(Database, "create_web_session", delayed_seed):
            t, result = self.worker(lambda: self.restore(blob))
            self.assertTrue(entered.wait(10))
            during = self.replay(old)
            release.set()
            r = self.finish(t, result)
        after = self.replay(old)
        self.assertEqual([before, during, r.status_code, after],
                         [401, 401, 200, 401],
                         "no window may expose revoked rows during restore")

    # probe test_12 equivalent ────────────────────────────────────────
    def test_12_ephemeral_fallback_disables_remember(self):
        data = cfg.raw()
        data["session_secret"] = ""
        cfg.save(data)
        with patch.object(cfg, "save",
                          side_effect=PermissionError("injected read-only config")):
            s._init_session_secret()
        key1 = s.app.secret_key
        enabled = s._remember_enabled()
        r = self.login(True)
        header = r.headers.get("Set-Cookie", "")
        self.assertFalse(enabled)
        self.assertFalse(r.get_json()["remembered"])
        self.assertNotIn("Expires=", header)
        db = s.get_db()
        try:
            self.assertEqual(
                db._con.execute("SELECT COUNT(*) FROM web_sessions")
                .fetchone()[0], 0)
        finally:
            s.release_db(db)
        with patch.object(cfg, "save",
                          side_effect=PermissionError("injected read-only config")):
            s._init_session_secret()
        self.assertNotEqual(key1, s.app.secret_key)
        # restore persistent secret for later tests
        s._init_session_secret()

    # probe test_16 equivalent ────────────────────────────────────────
    def test_16_inflight_revoke_does_not_survive_cli_revoke(self):
        self.login(False)
        entered = threading.Event()
        release = threading.Event()
        db = s.get_db()
        real = db.delete_web_sessions_except
        try:
            def delayed_delete(keep):
                if threading.current_thread().name == "probe-request":
                    entered.set()
                    assert release.wait(10)
                return real(keep)

            with patch.object(db, "delete_web_sessions_except", delayed_delete):
                t, result = self.worker(lambda: self.a.post(
                    "/api/auth/revoke-sessions",
                    headers={"X-CSRF-Token": self.csrf()}))
                self.assertTrue(entered.wait(10))
                epoch_before = cfg.raw()["session_epoch"]
                command = subprocess.run(
                    [sys.executable, "-m", "web.authtool", "revoke-sessions"],
                    cwd=REPO, capture_output=True, text=True, check=True,
                    env=os.environ.copy())
                epoch = cfg.raw()["session_epoch"]
                release.set()
                r = self.finish(t, result)
        finally:
            s.release_db(db)
        status = self.a.get("/api/stats").status_code
        self.assertEqual(epoch, epoch_before + 1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(status, 401,
                         "in-flight revoke must not launder a later CLI revoke")
        self.assertIn("All web sessions", command.stdout)

    # probe test_17 equivalent ────────────────────────────────────────
    def test_17_unreadable_config_import_completes(self):
        code = ("import sys; sys.path.insert(0, %r); import config; "
                "from unittest.mock import patch; "
                "p=patch.object(config,'raw',side_effect="
                "PermissionError('injected unreadable config')); p.start(); "
                "import web.server" % REPO)
        env = os.environ.copy()
        env["BASE_DIR"] = str(Path(_BASE) / "import-probe")
        p = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0,
                         f"import must complete, stderr:\n{p.stderr}")
        self.assertIn("ephemeral random key", p.stderr)

    def test_17b_malformed_json_import_completes(self):
        root = Path(_BASE) / "import-probe-json"
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.json").write_text("{invalid json")
        code = "import sys; sys.path.insert(0, %r); import web.server" % REPO
        env = os.environ.copy()
        env["BASE_DIR"] = str(root)
        p = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0,
                         f"import must complete, stderr:\n{p.stderr}")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_BASE, ignore_errors=True)
