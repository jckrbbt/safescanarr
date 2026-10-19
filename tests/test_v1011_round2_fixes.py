"""tests/test_v1011_round2_fixes.py — regression tests for the round-2
skeptic review (docs/v1.0.11-round2-fixes.md).

Each test mirrors a probe scenario from
/home/hermes/.hermes/profiles/skeptic/cache/scratch/probe_v1011_round2.py and
asserts the FIXED (fail-closed) outcome:
  DIRECT_SAVE             stale cfg.save() snapshot cannot roll epoch back
  LOAD_MIGRATION_RACE     config._load() default-key self-heal is locked
  MALICIOUS_TRIGGER       AFTER-DELETE trigger cannot survive restore
  CALLER_RESEED_RACE      logout during restore keeps the old cookie 401
  LOCK validation         symlink / bad mode / replaced inode abort the lock
  SIGNOUT UI              /logout non-2xx must not redirect (node vm test)
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
_BASE = tempfile.mkdtemp(prefix="safescanarr-r2fixes-")
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

TOKEN = "r2-fixes-test-token-" + "q" * 20
COOKIE = "safescanarr_session"


class Round2FixTests(unittest.TestCase):

    def setUp(self):
        s._reset_db()
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
        s.app.config["TESTING"] = True
        s._throttle.clear()
        s._rate_limit.clear()
        self.a = s.app.test_client()

    def tearDown(self):
        s._reset_db()
        os.environ.pop("SS_TOKEN", None)

    # helpers (mirror probe_v1011_round2.py) ──────────────────────────
    def login(self, remember=True, client=None):
        c = client or self.a
        s._rate_limit.clear()
        r = c.post("/login", json={"secret": TOKEN, "remember": remember})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get_cookie(COOKIE).value

    def replay(self, cookie):
        c = s.app.test_client()
        c.set_cookie(COOKIE, cookie)
        return c.get("/api/stats").status_code

    def csrf(self, c=None):
        with (c or self.a).session_transaction() as ss:
            return ss["csrf"]

    def backup(self):
        dest = Path(_BASE) / "bk.db"
        if dest.exists():
            dest.unlink()
        con = sqlite3.connect(str(dest))
        db = s.get_db()
        try:
            db._con.backup(con)
        finally:
            s.release_db(db)
        con.close()
        return dest

    def restore(self, blob, c=None):
        c = c or self.a
        return c.post("/api/backup/restore/db",
                      data={"file": (io.BytesIO(blob), "bk.db")},
                      headers={"X-CSRF-Token": self.csrf(c)})

    # DIRECT_SAVE ─────────────────────────────────────────────────────
    def test_direct_save_cannot_rollback_epoch(self):
        old = self.login(False)
        stale = cfg.raw()
        auth.revoke_all_sessions()
        before = self.replay(old)
        high = cfg.raw()["session_epoch"]
        cfg.save(stale)  # direct unlocked-looking write must still clamp
        after = self.replay(old)
        low = cfg.raw()["session_epoch"]
        self.assertEqual(before, 401)
        self.assertEqual(after, 401, "stale cfg.save() revived a revoked cookie")
        self.assertEqual(low, high, "session_epoch rolled backward")

    # LOAD_MIGRATION_RACE ─────────────────────────────────────────────
    def test_load_migration_race_cannot_rollback_epoch(self):
        # With the default-key self-heal now a locked read-modify-write
        # (read INSIDE the lock), the old read-to-write interleaving is no
        # longer constructible. Assert the new guarantee instead: while the
        # self-heal is paused mid-write, a concurrent revocation serializes
        # behind it on the config lock — it can neither interleave nor be
        # lost to the self-heal's snapshot.
        old = self.login(False)
        path = Path(cfg._config_path())
        raw = json.loads(path.read_text())
        raw.pop("remember_me_days")
        path.write_text(json.dumps(raw))
        entered = threading.Event()
        release = threading.Event()
        real = cfg.save

        def delay(data):
            if threading.current_thread().name == "load-thread":
                entered.set()
                assert release.wait(8)
            return real(data)

        with patch.object(cfg, "save", delay):
            t = threading.Thread(
                name="load-thread",
                target=lambda: cfg.raw())
            t.start()
            self.assertTrue(entered.wait(8))
            revoke_done = threading.Event()

            def revoke():
                auth.revoke_all_sessions()
                revoke_done.set()

            tr = threading.Thread(name="revoke-thread", target=revoke)
            tr.start()
            # give the revoke every chance to (wrongly) slip in
            deadline = time.time() + 1.0
            while time.time() < deadline and not revoke_done.is_set():
                time.sleep(0.05)
            self.assertFalse(revoke_done.is_set(),
                             "revocation interleaved with the locked "
                             "default-key self-heal")
            release.set()
            t.join(8)
            self.assertTrue(revoke_done.wait(8))
            tr.join(8)
        self.assertFalse(t.is_alive())
        self.assertFalse(tr.is_alive())
        self.assertEqual(self.replay(old), 401,
                         "unlocked _load() self-heal revived a revoked cookie")
        fresh = cfg.raw()
        self.assertIn("remember_me_days", fresh)
        self.assertGreaterEqual(fresh["session_epoch"], 1)

    # MALICIOUS_TRIGGER ───────────────────────────────────────────────
    def test_restore_rejects_hostile_trigger_schema(self):
        self.login(True)
        b = s.app.test_client()
        old = self.login(True, b)
        dest = self.backup()
        con = sqlite3.connect(str(dest))
        con.execute(
            "CREATE TRIGGER resurrect_session AFTER DELETE ON web_sessions "
            "BEGIN INSERT INTO web_sessions "
            "(rid_hash, created_at, expires_at, last_used_at, ip, user_agent) "
            "VALUES (OLD.rid_hash,OLD.created_at,OLD.expires_at,"
            "OLD.last_used_at,OLD.ip,OLD.user_agent); END")
        con.commit()
        con.close()
        b.post("/logout", headers={"X-CSRF-Token": self.csrf(b)})
        before = self.replay(old)
        r = self.restore(dest.read_bytes())
        after = self.replay(old)
        self.assertEqual(before, 401)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(after, 401,
                         "hostile trigger resurrected a logged-out session")
        # the published DB must contain no uploaded triggers
        pub = sqlite3.connect(cfg.Config().DB_FILE)
        triggers = pub.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone()[0]
        rows = pub.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
        pub.close()
        self.assertEqual(triggers, 0, "uploaded trigger survived restore")
        self.assertEqual(rows, 1, "expected only the restoring operator's row")

    # CALLER_RESEED_RACE ──────────────────────────────────────────────
    def test_logout_during_restore_stays_revoked(self):
        old = self.login(True)
        blob = self.backup().read_bytes()
        from database import Database
        real = Database.create_web_session
        entered = threading.Event()
        release = threading.Event()
        res = {}

        def pause(self_, *a, **kw):
            if threading.current_thread().name == "restore-thread":
                entered.set()
                assert release.wait(8)
            return real(self_, *a, **kw)

        def worker():
            res["response"] = self.restore(blob)

        with patch.object(Database, "create_web_session", pause):
            t = threading.Thread(name="restore-thread", target=worker)
            t.start()
            self.assertTrue(entered.wait(8))
            c = s.app.test_client()
            c.set_cookie(COOKIE, old)
            logout = c.post("/logout", headers={"X-CSRF-Token": self.csrf(c)})
            before = self.replay(old)
            release.set()
            t.join(8)
        self.assertFalse(t.is_alive())
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(before, 401)
        self.assertEqual(res["response"].status_code, 200)
        self.assertEqual(self.replay(old), 401,
                         "restore recreated a rid logged out during staging")

    # LOCK hardening ──────────────────────────────────────────────────
    def _lock_path(self):
        return Path(cfg._config_path() + ".lock")

    def _fresh_lock_file(self):
        path = self._lock_path()
        if path.exists() or path.is_symlink():
            path.unlink()
        path.write_text("")
        os.chmod(path, 0o600)
        self.addCleanup(self._cleanup_lock)
        return path

    def _cleanup_lock(self):
        path = self._lock_path()
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
        except OSError:
            pass

    def test_lock_rejects_symlink(self):
        path = self._fresh_lock_file()
        victim = path.with_suffix(".victim")
        victim.write_text("")
        self.addCleanup(lambda: victim.unlink(missing_ok=True))
        path.unlink()
        path.symlink_to(victim)
        with self.assertRaises(config_lock.LockValidationError):
            with config_lock._FileLock():
                pass

    def test_lock_rejects_insecure_mode(self):
        path = self._fresh_lock_file()
        os.chmod(path, 0o666)
        with self.assertRaises(config_lock.LockValidationError):
            with config_lock._FileLock():
                pass

    def test_lock_aborts_when_inode_replaced_while_locking(self):
        path = self._fresh_lock_file()
        import fcntl
        real_flock = fcntl.flock

        def replacing_flock(fd, op):
            # swap the file out from under the fd while it waits for flock
            path.unlink()
            path.write_text("")
            os.chmod(path, 0o600)
            return real_flock(fd, op)

        with patch("config_lock.fcntl.flock", replacing_flock):
            with self.assertRaises(config_lock.LockValidationError):
                with config_lock._FileLock():
                    pass

    # SIGNOUT UI (node vm) ────────────────────────────────────────────
    def test_signout_ui_does_not_redirect_on_logout_failure(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        script = Path(REPO) / "tests" / "js" / "signout_vm_test.mjs"
        p = subprocess.run([node, str(script)],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(Path(REPO) / "tests" / "js"))
        self.assertEqual(p.returncode, 0,
                         f"node vm test failed:\n{p.stdout}\n{p.stderr}")
        self.assertIn("ALL OK", p.stdout)


if __name__ == "__main__":
    unittest.main()
