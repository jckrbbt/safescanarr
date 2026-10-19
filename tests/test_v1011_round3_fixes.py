"""tests/test_v1011_round3_fixes.py — regression tests for the round-3
skeptic review (docs/v1.0.11-round3-fixes.md).

Covers the execution-ordered fixes:
  1  locked read-modify-write for ALL automatic config changes
     (default-key migration, session-secret generation, first-run
     bootstrap with absence recheck under the lock)
  2  single publication/revocation ordering: deletion after the
     restore recheck stays revoked; a request holding a Database
     reference across a restore keeps working
  3  request-private restore staging: two concurrent restores both
     succeed; a failing restore beside a successful one cannot touch
     the other's stage
  4  hostile-schema handling: uploaded triggers/views are discarded
     (fresh-DDL import route), corrupt uploads fail closed
  5  actionable fail-closed errors: API JSON 500 on lock validation
     failure, authtool clean exit 1 without traceback
"""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASE = tempfile.mkdtemp(prefix="safescanarr-r3fixes-")
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

TOKEN = "r3-fixes-test-token-" + "q" * 20
COOKIE = "safescanarr_session"


class Round3FixTests(unittest.TestCase):

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
        s._throttle.clear()
        s._rate_limit.clear()
        self.a = s.app.test_client()

    def tearDown(self):
        s._reset_db()
        os.environ.pop("SS_TOKEN", None)

    # helpers ──────────────────────────────────────────────────────────
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
        dest = Path(_BASE) / "bk-r3.db"
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
                      data={"file": (io.BytesIO(blob), "bk.db")},
                      headers={"X-CSRF-Token": self.csrf(c)})

    # 1. locked read-modify-write ──────────────────────────────────────
    def test_default_migration_does_not_lose_concurrent_setting(self):
        # Mirror of the round-3 diagnostic: pause the default-key
        # self-heal's WRITE (inside the config lock) while a locked
        # mutation is queued behind it. The mutation must serialize AFTER
        # the self-heal (never interleave) and the completed update must
        # survive — no stale whole-file snapshot can lose it.
        path = Path(cfg._config_path())
        raw = json.loads(path.read_text())
        raw.pop("remember_me_days")
        path.write_text(json.dumps(raw))
        entered = threading.Event()
        release = threading.Event()
        real = cfg.save
        errors = []

        def delayed(c_):
            if threading.current_thread().name == "old-default-reader":
                entered.set()
                if not release.wait(8):
                    errors.append("release-timeout")
            return real(c_)

        def read():
            try:
                cfg.raw()
            except Exception as e:  # pragma: no cover
                errors.append(type(e).__name__)

        def mutator():
            config_lock.mutate(
                lambda c: c.update(web_ui_url="new-setting"))
            mut_done.set()

        with patch.object(cfg, "save", delayed):
            t = threading.Thread(target=read, name="old-default-reader")
            t.start()
            self.assertTrue(entered.wait(5))
            mut_done = threading.Event()
            tm = threading.Thread(target=mutator)
            tm.start()
            # give the mutation every chance to (wrongly) slip in
            deadline = time.time() + 1.0
            while time.time() < deadline and not mut_done.is_set():
                time.sleep(0.05)
            self.assertFalse(mut_done.is_set(),
                             "locked mutation interleaved with the "
                             "default-key self-heal's locked write")
            release.set()
            t.join(8)
            self.assertTrue(mut_done.wait(8))
            tm.join(8)
        self.assertFalse(t.is_alive())
        self.assertFalse(tm.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(cfg.raw()["web_ui_url"], "new-setting",
                         "locked update lost to the default-key self-heal")
        self.assertIn("remember_me_days", cfg.raw())

    def test_session_secret_generation_locked_rmw(self):
        # Empty the stored secret, then pause the secret-generating write
        # (inside the config lock) while a locked config update queues
        # behind it: the update must serialize after and survive, and the
        # persisted secret must match the key actually installed.
        data = cfg.raw()
        data["session_secret"] = ""
        cfg.save(data)
        entered = threading.Event()
        release = threading.Event()
        real = cfg.save
        errors = []

        def delayed(c_):
            if threading.current_thread().name == "secret-thread":
                entered.set()
                if not release.wait(8):
                    errors.append("release-timeout")
            return real(c_)

        def gen():
            try:
                s._init_session_secret()
            except Exception as e:  # pragma: no cover
                errors.append(type(e).__name__)

        def mutator():
            config_lock.mutate(
                lambda c: c.update(web_ui_url="secret-race"))
            mut_done.set()

        with patch.object(cfg, "save", delayed), \
                patch.object(s.config_module, "save", delayed):
            t = threading.Thread(target=gen, name="secret-thread")
            t.start()
            self.assertTrue(entered.wait(5))
            mut_done = threading.Event()
            tm = threading.Thread(target=mutator)
            tm.start()
            deadline = time.time() + 1.0
            while time.time() < deadline and not mut_done.is_set():
                time.sleep(0.05)
            self.assertFalse(mut_done.is_set(),
                             "locked mutation interleaved with the "
                             "session-secret generation write")
            release.set()
            t.join(8)
            self.assertTrue(mut_done.wait(8))
            tm.join(8)
        self.assertFalse(t.is_alive())
        self.assertFalse(tm.is_alive())
        self.assertEqual(errors, [])
        fresh = cfg.raw()
        self.assertEqual(fresh["web_ui_url"], "secret-race",
                         "locked update lost to session-secret generation")
        self.assertTrue(fresh["session_secret"])
        self.assertEqual(s.app.secret_key,
                         __import__("hashlib").sha256(
                             fresh["session_secret"].encode()).hexdigest())

    def test_concurrent_first_run_bootstrap_no_overwrite(self):
        # Two processes bootstrap a not-yet-existing config concurrently:
        # neither may overwrite the other's completed epoch bump with
        # first-run defaults.
        base = Path(_BASE) / "bootstrap-race"
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        code = (
            "import sys, os; sys.path.insert(0, %r); "
            "os.environ['BASE_DIR'] = %r; "
            "import config, config_lock; "
            "config_lock.mutate(lambda c: c.update("
            "auth_method='token', auth_token='boot-race-token', "
            "session_secret='boot-race-secret', "
            "session_epoch=c.get('session_epoch', 0) + 1))"
            % (REPO, str(base)))
        env = os.environ.copy()
        env.pop("SS_TOKEN", None)
        procs = [subprocess.Popen(
                     [sys.executable, "-c", code], env=env,
                     cwd=REPO, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE) for _ in range(4)]
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err.decode())
        data = json.loads((base / "config.json").read_text())
        self.assertEqual(data["session_epoch"], 4,
                         "bootstrap overwrote a concurrent writer's epoch "
                         f"(got {data['session_epoch']})")
        self.assertEqual(data["auth_method"], "token")

    def test_bootstrap_does_not_replace_existing_config(self):
        # A config created between the existence check and the write must
        # survive: bootstrap rechecks absence under the lock.
        base = Path(_BASE) / "bootstrap-existing"
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        env = os.environ.copy()
        env["BASE_DIR"] = str(base)
        code = ("import sys; sys.path.insert(0, %r); import config; "
                "config.raw()" % REPO)
        p = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        first = json.loads((base / "config.json").read_text())
        first["session_epoch"] = 7
        first["auth_method"] = "token"
        first["auth_token"] = "kept-token"
        (base / "config.json").write_text(json.dumps(first))
        # a fresh import bootstraps only if the file is absent; it must
        # not clobber the epoch/credentials written above
        p2 = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True, timeout=30)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        again = json.loads((base / "config.json").read_text())
        self.assertEqual(again["session_epoch"], 7)
        self.assertEqual(again["auth_token"], "kept-token")

    # 2/3. publication ordering + private staging ─────────────────────
    def test_deletion_after_recheck_stays_revoked(self):
        # Log out AFTER the restore's final live-row recheck: the
        # deletion and the publication share _db_lock, so the logout
        # serializes either before the recheck (row dropped -> not
        # seeded) or after publication (row deleted from the live DB).
        # Either way the cookie must end 401.
        old = self.login(True)
        blob = self.backup()
        entered = threading.Event()
        release = threading.Event()
        res = {}

        # Pause right before the final recheck + publication critical
        # section: patch get_web_session on the live DB read performed
        # inside the publication lock.
        from database import Database
        real = Database.get_web_session

        def pause(self_, rid_hash):
            r = real(self_, rid_hash)
            if threading.current_thread().name == "restore-thread" and r is not None:
                entered.set()
                assert release.wait(8)
            return r

        def worker():
            res["response"] = self.restore(blob)

        with patch.object(Database, "get_web_session", pause):
            t = threading.Thread(name="restore-thread", target=worker)
            t.start()
            self.assertTrue(entered.wait(8))
            # logout lands between the recheck-read and the publication
            c = s.app.test_client()
            c.set_cookie(COOKIE, old)
            logout = c.post("/logout", headers={"X-CSRF-Token": self.csrf(c)})
            release.set()
            t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(res["response"].status_code, 200)
        self.assertEqual(self.replay(old), 401,
                         "logout after the recheck was resurrected by "
                         "publication")

    def test_concurrent_request_keeps_db_reference_across_restore(self):
        # Round-5 contract: a request holding a Database lease across a
        # restore blocks PUBLICATION until it exits (no write may land on
        # the retired inode after the replace). The restore defers, then
        # succeeds once the holder releases; the old handle stays usable
        # for reads until then (its inode is never yanked mid-use).
        old = self.login(True)
        blob = self.backup()
        db = s.get_db()
        res = {}
        # prove the old handle still answers while publication waits
        rows = db._con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        self.assertIsInstance(rows, int)

        def worker():
            res["response"] = self.restore(blob)

        t = threading.Thread(target=worker)
        t.start()
        t.join(1.0)
        deferred = t.is_alive()
        s.release_db(db)  # publication may now proceed
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertTrue(deferred,
                        "restore published while a leased holder was "
                        "still active")
        self.assertEqual(res["response"].status_code, 200)
        self.assertEqual(self.replay(old), 200)

    def test_two_concurrent_restores_both_succeed(self):
        # Two simultaneous restores from the SAME operator session (shared
        # client): both must succeed, the operator stays signed in, and no
        # stage files may remain.
        self.login(True)
        blob = self.backup()
        res = {}

        def worker(key):
            res[key] = self.restore(blob)

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start(); t2.start()
        t1.join(30); t2.join(30)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(res["a"].status_code, 200,
                         res["a"].get_data(as_text=True))
        self.assertEqual(res["b"].status_code, 200,
                         res["b"].get_data(as_text=True))
        # Both stages seed the same operator row; whichever publishes last
        # leaves exactly that row live.
        con = sqlite3.connect(cfg.Config().DB_FILE)
        rows = con.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
        con.close()
        self.assertEqual(rows, 1)
        leftovers = [p.name for p in Path(_BASE).glob("*restore*")]
        self.assertEqual(leftovers, [],
                         f"restore stage files left behind: {leftovers}")

    def test_failing_restore_beside_successful_one(self):
        self.login(True)
        good = self.backup()
        # corrupt copy: valid magic header, broken tail
        bad = bytearray(good)
        bad[-64:] = b"\x00" * 64
        bad = bytes(bad)
        res = {}

        from database import Database

        good_enters = threading.Event()
        release = threading.Event()

        real_init = Database.__init__

        def slow_good_init(self_, path):
            real_init(self_, path)
            if threading.current_thread().name == "good-restore":
                good_enters.set()
                assert release.wait(10)

        def worker(blob, key):
            res[key] = self.restore(blob)

        with patch.object(Database, "__init__", slow_good_init):
            tg = threading.Thread(target=worker, args=(good, "good"),
                                  name="good-restore")
            tg.start()
            self.assertTrue(good_enters.wait(10))
            # the failing restore runs (and fails) while the good one is
            # still mid-flight; its cleanup must not touch the good one
            tb = threading.Thread(target=worker, args=(bad, "bad"),
                                  name="bad-restore")
            tb.start()
            tb.join(30)
            release.set()
            tg.join(30)
        self.assertFalse(tg.is_alive())
        self.assertFalse(tb.is_alive())
        self.assertEqual(res["bad"].status_code, 500)
        self.assertEqual(res["good"].status_code, 200,
                         res["good"].get_data(as_text=True))
        leftovers = [p.name for p in Path(_BASE).glob("*restore*")]
        self.assertEqual(leftovers, [],
                         f"restore stage files left behind: {leftovers}")

    # 4. hostile schema / integrity ────────────────────────────────────
    def test_restore_discards_uploaded_triggers_and_views(self):
        self.login(True)
        dest = Path(_BASE) / "hostile.db"
        if dest.exists():
            dest.unlink()
        con = sqlite3.connect(str(dest))
        db = s.get_db()
        try:
            db._con.backup(con)
        finally:
            s.release_db(db)
        con.execute(
            "CREATE TRIGGER resurrect_session AFTER DELETE ON web_sessions "
            "BEGIN INSERT INTO web_sessions "
            "(rid_hash, created_at, expires_at, last_used_at, ip, user_agent) "
            "VALUES (OLD.rid_hash,OLD.created_at,OLD.expires_at,"
            "OLD.last_used_at,OLD.ip,OLD.user_agent); END")
        # quote-wrapped trigger name with an embedded double quote: the
        # old DROP TRIGGER path interpolated identifiers unsafely; the
        # import route never parses them at all.
        con.execute(
            'CREATE TRIGGER "evil""--injection" AFTER INSERT ON files '
            "BEGIN SELECT 1; END")
        con.execute("CREATE VIEW v_injection AS SELECT * FROM web_sessions")
        con.commit()
        con.close()
        r = self.restore(dest.read_bytes())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        pub = sqlite3.connect(cfg.Config().DB_FILE)
        objs = pub.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "OR type='view'").fetchone()[0]
        rows = pub.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
        ok = pub.execute("PRAGMA integrity_check").fetchone()[0]
        pub.close()
        self.assertEqual(objs, 0, "uploaded trigger/view survived restore")
        self.assertEqual(rows, 1, "expected only the operator's row")
        self.assertEqual(ok, "ok")

    def test_restore_rejects_corrupt_database(self):
        self.login(True)
        blob = self.backup()
        bad = bytearray(blob)
        bad[-64:] = b"\x00" * 64
        r = self.restore(bytes(bad))
        self.assertEqual(r.status_code, 500,
                         "corrupt upload must fail the restore")
        # live DB untouched and still healthy
        con = sqlite3.connect(cfg.Config().DB_FILE)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        self.assertEqual(ok, "ok")

    # 5. fail-closed but actionable boundaries ─────────────────────────
    def test_lock_error_boundaries_api_and_cli(self):
        self.login(False)
        lockpath = Path(cfg._config_path() + ".lock")
        lockpath.chmod(0o666)
        try:
            r = self.a.post("/api/config",
                            json={"web_ui_url": "http://example.invalid"},
                            headers={"X-CSRF-Token": self.csrf()})
            self.assertEqual(r.status_code, 500)
            self.assertTrue(r.is_json,
                            "API lock failure must return JSON, got "
                            f"{r.content_type}")
            r2 = self.a.post("/api/auth/revoke-sessions",
                             headers={"X-CSRF-Token": self.csrf()})
            self.assertEqual(r2.status_code, 500)
            self.assertTrue(r2.is_json,
                            f"revoke-sessions lock failure must be JSON, "
                            f"got {r2.content_type}")
            cli = subprocess.run(
                [sys.executable, "-m", "web.authtool", "revoke-sessions"],
                cwd=REPO, env=os.environ.copy(), capture_output=True,
                text=True, timeout=30)
            self.assertEqual(cli.returncode, 1)
            self.assertNotIn("Traceback", cli.stderr,
                             "authtool must not dump a traceback")
            self.assertIn("config lock", cli.stderr)
        finally:
            lockpath.chmod(0o600)

    def test_readonly_old_config_merges_defaults_in_memory(self):
        # An old config missing a new default key on a read-only
        # BASE_DIR: reads succeed with the default merged in memory; no
        # write is attempted on every auth read after the first failure.
        ro = Path(_BASE) / "readonly-r3"
        shutil.rmtree(ro, ignore_errors=True)
        ro.mkdir()
        env = os.environ.copy()
        env["BASE_DIR"] = str(ro)
        env.pop("SS_TOKEN", None)
        code = (
            "import sys, json, os; sys.path.insert(0, %r); "
            "os.environ['BASE_DIR'] = %r; "
            "import config as c; "
            "base = c.raw(); "
            "base.update(auth_method='token', auth_token='ro-token', "
            "session_secret='ro-secret'); "
            "c.save(base); "
            "p = __import__('pathlib').Path(c._config_path()); "
            "d = json.loads(p.read_text()); d.pop('remember_me_days'); "
            "p.write_text(json.dumps(d))"
            % (REPO, str(ro)))
        p = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        cp = ro / "config.json"
        lp = ro / "config.json.lock"
        lp.unlink(missing_ok=True)
        cp.chmod(0o400)
        ro.chmod(0o500)
        try:
            code2 = (
                "import sys, os; sys.path.insert(0, %r); "
                "os.environ['BASE_DIR'] = %r; "
                "import config as c; "
                "d1 = c.raw(); "
                "d2 = c.raw(); d3 = c.raw(); "
                "print('DAYS', d1.get('remember_me_days'), "
                "d2.get('remember_me_days'), d3.get('remember_me_days'))"
                % (REPO, str(ro)))
            p2 = subprocess.run([sys.executable, "-c", code2], env=env,
                                capture_output=True, text=True, timeout=30)
            self.assertEqual(p2.returncode, 0, p2.stderr)
            self.assertIn("DAYS 30 30 30", p2.stdout,
                          "defaults not merged in memory on read-only "
                          "install")
        finally:
            ro.chmod(0o700)
            cp.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
