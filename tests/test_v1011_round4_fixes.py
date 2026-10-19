"""tests/test_v1011_round4_fixes.py — regression tests for the round-4
skeptic review (docs/v1.0.11-round4-fixes.md).

Covers the execution-ordered ship condition:
  1  HIGH DB lifecycle: per-generation leases (close only on that
     generation's LAST release), balanced acquire/release on every
     production route, WAL sidecar deletion ordered after a successful
     os.replace with an exception path preserving the live DB (incl.
     committed WAL data); _establish_session old-row deletion,
     credential-transition hook, and expiration cleanup under the
     publication lock
  2  HIGH source backup contract: ordinary tables required (views /
     virtual tables / missing core tables rejected), imported row types
     / nullability validated before publication, streamed import
  3  MEDIUM default migration under the lock with setdefault semantics;
     bootstrap loser returns the winner's config; pre-lock-acquisition
     pause interleavings
  4  MEDIUM fix-lock safety: inode revalidation before unlink,
     never unlinking a valid or actively-held lock, competing-repair
  5  LOW crash-stage policy: startup cleanup removes only provably
     orphaned/aged stages with ownership checks
"""

import io
import json
import os
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
_BASE = tempfile.mkdtemp(prefix="safescanarr-r4fixes-")
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
import web.authtool as authtool

TOKEN = "r4-fixes-test-token-" + "q" * 20
COOKIE = "safescanarr_session"


class Round4FixTests(unittest.TestCase):

    def setUp(self):
        s._reset_db()
        db_file = cfg.Config().DB_FILE
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(db_file + suffix)
            except FileNotFoundError:
                pass
        for stale in Path(_BASE).glob(".safescanarr.db.restore.*"):
            stale.unlink()
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
        dest = Path(_BASE) / "bk-r4.db"
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

    def modify(self, blob, sql, params=()):
        p = Path(_BASE) / ("src-%d.db" % time.monotonic_ns())
        p.write_bytes(blob)
        con = sqlite3.connect(str(p))
        con.execute(sql, params)
        con.commit()
        con.close()
        return p.read_bytes()

    def assertLiveRows(self, sql, params=()):
        con = sqlite3.connect(cfg.Config().DB_FILE)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    # 1. DB lifecycle protocol ─────────────────────────────────────────
    def test_restore_defers_while_leases_outstanding(self):
        # Round-5 protocol: publication must not replace the live file
        # while leased writers on the current generation are still out
        # (they could write to the retired inode after the replace).
        # The restore defers, and aborts (500) if they never finish.
        self.login(True)
        blob = self.backup()
        old1 = s.get_db()
        old2 = s.get_db()
        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 1.0):
            r = self.restore(blob)
        self.assertEqual(r.status_code, 500,
                         "restore published while leased writers were active")
        # the live database is untouched and still usable
        with s.db_lease() as cur:
            cur._con.execute("SELECT COUNT(*) FROM files").fetchone()
        # once the writers exit, the same restore succeeds
        s.release_db(old1)
        s.release_db(old2)
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # new-generation holder works right away
        with s.db_lease() as new:
            new._con.execute("SELECT COUNT(*) FROM files").fetchone()
        # a leased retired generation closes on ITS last release
        old3 = s.get_db()
        r = self.restore(blob)
        self.assertEqual(r.status_code, 500)  # old3 still leased: deferred
        s.release_db(old3)
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200)

    def test_new_generation_writes_publish_after_old_released(self):
        self.login(True)
        blob = self.backup()
        old = s.get_db()
        s.release_db(old)  # writer done before the restore: no deferral
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200)
        # writes on the NEW generation land in the published file
        with s.db_lease() as new:
            new.set_poller_state("gen-b-marker", "written")
        self.assertEqual(
            self.assertLiveRows(
                "SELECT value FROM state WHERE key='gen-b-marker'")[0][0],
            "written")

    def test_repeated_restores_stay_consistent(self):
        self.login(True)
        blob = self.backup()
        for i in range(3):
            r = self.restore(blob)
            self.assertEqual(r.status_code, 200,
                             f"restore {i} failed: "
                             f"{r.get_data(as_text=True)}")
        with s.db_lease() as db:
            db._con.execute("SELECT COUNT(*) FROM files").fetchone()
        leftovers = [p.name for p in Path(_BASE).glob("*restore*")]
        self.assertEqual(leftovers, [])

    def test_routes_balance_db_leases(self):
        self.login(False)
        base = dict(s._db_refs)
        for _ in range(4):
            r = self.a.get("/api/stats")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(s._db_refs, base,
                         "route leaked a database lease: "
                         f"{base} -> {s._db_refs}")

    def test_final_replace_failure_preserves_live_db_with_wal_data(self):
        # Committed WAL content in the live DB; the final os.replace
        # fails. The live DB must remain usable and keep the marker.
        self.login(True)
        blob = self.backup()
        with s.db_lease() as db:
            db._con.execute("PRAGMA wal_autocheckpoint=0")
            db.set_poller_state("pub-fail-marker", "must-survive")
        real_replace = os.replace

        def fail_publish(src, dst):
            if str(dst) == cfg.Config().DB_FILE:
                raise OSError("injected publication failure")
            return real_replace(src, dst)

        with patch.object(s.os, "replace", fail_publish):
            r = self.restore(blob)
        self.assertEqual(r.status_code, 500)
        con = sqlite3.connect(cfg.Config().DB_FILE)
        try:
            row = con.execute(
                "SELECT value FROM state WHERE key='pub-fail-marker'"
            ).fetchone()
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(row[0] if row else None, "must-survive",
                         "committed WAL data lost to a failed publication")
        self.assertEqual(ok, "ok")
        # the service still works afterwards
        self.assertEqual(self.a.get("/api/stats").status_code, 200)

    def test_establish_session_and_expiry_deletions_participate(self):
        # Re-login's old-row deletion and expiration cleanup run under
        # the publication lock: no leaked leases and correct rows after
        # a restore in between.
        cookie = self.login(True)
        blob = self.backup()
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200)
        # re-login (old remembered row deletion path)
        c = s.app.test_client()
        c.set_cookie(COOKIE, cookie)
        r2 = c.post("/login", json={"secret": TOKEN, "remember": True})
        self.assertEqual(r2.status_code, 200)
        rows = self.assertLiveRows("SELECT COUNT(*) FROM web_sessions")[0][0]
        self.assertEqual(rows, 1)
        # every lease on retired generations was released; the current
        # shared generation may still be leased by no one at rest
        self.assertEqual(sum(s._db_refs.values()), 0,
                         "coordinated deletion path leaked a lease: %r"
                         % (dict(s._db_refs),))

    def test_credential_transition_hook_under_lock(self):
        # The revocation hook (credential transition) deletes rows via
        # the coordinated path and balances its lease.
        self.login(True)
        before = dict(s._db_refs)
        auth.configure_token(TOKEN)  # credential transition, even if equal
        rows = self.assertLiveRows("SELECT COUNT(*) FROM web_sessions")
        self.assertEqual(rows[0][0], 0,
                         "credential transition left remembered rows behind")
        self.assertEqual(s._db_refs, before,
                         "revocation hook leaked a lease: "
                         f"{before} -> {s._db_refs}")

    # 2. Source backup contract ────────────────────────────────────────
    def test_known_good_backup_round_trips_exactly(self):
        self.login(True)
        with s.db_lease() as db:
            for i in range(3):
                db.upsert_file("/r4/%d" % i, "f%d" % i, i + 1, 1234.0,
                               status="flagged", review_state="quarantined")
            con = db._con
            con.execute("INSERT INTO errors(path,message,recorded_at) "
                        "VALUES(?,?,?)", ("/r4/0", "unicode-雪", "2026-10-19"))
            con.execute("INSERT OR REPLACE INTO state(key,value) "
                        "VALUES(?,?)", ("marker", "cursor-雪"))
            con.execute("INSERT OR REPLACE INTO lifetime_stats(key,count) "
                        "VALUES(?,?)", ("custom", 123))
            con.commit()
            original = {t: [tuple(r) for r in con.execute(
                "SELECT * FROM " + t + " ORDER BY 1")]
                for t in s._RESTORE_TABLES}
        blob = self.backup()
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200)
        after = {t: self.assertLiveRows(
            "SELECT * FROM " + t + " ORDER BY 1") for t in s._RESTORE_TABLES}
        self.assertEqual({k: [tuple(r) for r in v] for k, v in after.items()},
                         original)

    def test_missing_core_table_rejected_not_published_empty(self):
        self.login(True)
        blob = self.backup()
        with s.db_lease() as db:
            db.upsert_file("/r4/keep", "keep", 1, 1.0)
        before = self.assertLiveRows("SELECT COUNT(*) FROM files")[0][0]
        self.assertGreater(before, 0)
        missing = self.modify(blob, "DROP TABLE files")
        r = self.restore(missing)
        self.assertEqual(r.status_code, 500,
                         "missing core table must be rejected")
        self.assertEqual(
            self.assertLiveRows("SELECT COUNT(*) FROM files")[0][0], before,
            "live files were wiped by a rejected restore")

    def test_source_view_rejected_never_evaluated(self):
        self.login(True)
        blob = self.backup()
        view = self.modify(blob, "DROP TABLE state")
        view = self.modify(view, "CREATE VIEW state AS SELECT 'marker' AS "
                                 "key, 'from-view' AS value")
        r = self.restore(view)
        self.assertEqual(r.status_code, 500, "view must be rejected")
        row = self.assertLiveRows(
            "SELECT value FROM state WHERE key='marker'")
        self.assertEqual(row, [], "view result was imported")

    def test_virtual_table_rejected(self):
        self.login(True)
        blob = self.backup()
        vt = self.modify(blob, "DROP TABLE lifetime_stats")
        vt = self.modify(vt, "CREATE VIRTUAL TABLE lifetime_stats "
                             "USING fts5(key)")
        r = self.restore(vt)
        self.assertEqual(r.status_code, 500, "virtual table must be rejected")

    def test_blob_in_text_column_rejected(self):
        self.login(True)
        with s.db_lease() as db:
            db.upsert_file("/r4/blob", "blob", 1, 1.0)
        blob = self.backup()
        bad = self.modify(blob, "UPDATE files SET flag_reason=? WHERE 1",
                          (sqlite3.Binary(b"bytes"),))
        r = self.restore(bad)
        self.assertEqual(r.status_code, 500,
                         "BLOB in a TEXT column must be rejected")
        # even after a failed attempt, the API stays JSON
        resp = self.a.get("/api/sheets?state=quarantined")
        self.assertTrue(resp.is_json)

    def test_null_in_notnull_column_rejected(self):
        self.login(True)
        with s.db_lease() as db:
            db.upsert_file("/r4/null", "null", 1, 1.0)
        blob = self.backup()
        # rebuild the source table without the NOT NULL constraint and
        # insert a NULL row the destination schema must reject
        p = Path(_BASE) / "null-src.db"
        p.write_bytes(blob)
        con = sqlite3.connect(str(p))
        con.execute("PRAGMA writable_schema=ON")
        con.execute("CREATE TABLE files_nullpath (path TEXT PRIMARY KEY, "
                    "name TEXT, size INTEGER NOT NULL, mtime REAL NOT NULL, "
                    "status TEXT NOT NULL DEFAULT 'ok', review_state TEXT "
                    "NOT NULL DEFAULT 'pending', flagged INTEGER NOT NULL "
                    "DEFAULT 0, flag_reason TEXT, nsfw_confidence REAL, "
                    "quarantine_path TEXT, source_retained INTEGER NOT NULL "
                    "DEFAULT 0, state_updated_at TEXT, state_source TEXT, "
                    "updated_at TEXT, arr_result TEXT)")
        cols = [r[1] for r in con.execute("PRAGMA table_info(files)")]
        con.execute("INSERT INTO files_nullpath (path,name,size,mtime,"
                    "updated_at) VALUES('/x',NULL,1,1.0,'t')")
        con.execute("DROP TABLE files")
        con.execute("ALTER TABLE files_nullpath RENAME TO files")
        con.commit()
        con.close()
        r = self.restore(p.read_bytes())
        self.assertEqual(r.status_code, 500,
                         "NULL in a NOT NULL column must be rejected")

    def test_import_budget_enforced(self):
        self.login(True)
        blob = self.backup()
        p = Path(_BASE) / "budget.db"
        p.write_bytes(blob)
        con = sqlite3.connect(str(p))
        for i in range(50):
            con.execute("INSERT OR REPLACE INTO lifetime_stats "
                        "(key,count) VALUES(?,?)", ("k%d" % i, 1))
        con.commit()
        con.close()
        dest = sqlite3.connect(":memory:")
        try:
            with patch.object(s, "_IMPORT_MAX_ROWS", 10):
                with self.assertRaises(RuntimeError):
                    s._import_table("file:%s?mode=ro" % p, dest,
                                    "lifetime_stats")
        finally:
            dest.close()
        # small batches path works normally
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200)

    # 3. Default migration / bootstrap under the lock ──────────────────
    def test_stale_missing_default_preserved_before_lock(self):
        # Pause the old reader BEFORE mutate() acquires the lock; a
        # locked writer sets the key meanwhile; on resume the reader
        # must NOT overwrite it with the default.
        p = Path(cfg._config_path())
        d = json.loads(p.read_text())
        d.pop("remember_me_days")
        p.write_text(json.dumps(d))
        entered = threading.Event()
        release = threading.Event()
        real = config_lock.mutate
        errors = []

        def delayed(fn):
            if threading.current_thread().name == "stale-default-reader":
                entered.set()
                assert release.wait(10)
            return real(fn)

        def reader():
            try:
                cfg.raw()
            except Exception as e:
                errors.append(repr(e))

        with patch.object(config_lock, "mutate", delayed):
            t = threading.Thread(target=reader, name="stale-default-reader")
            t.start()
            self.assertTrue(entered.wait(5))
            real(lambda c: c.update(remember_me_days=91))
            release.set()
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(cfg.raw()["remember_me_days"], 91,
                         "locked writer's update lost to default migration")

    def test_bootstrap_loser_returns_winner_config(self):
        base = Path(_BASE) / "boot-winner"
        import shutil
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        winner = {"session_epoch": 13, "auth_method": "token",
                  "auth_token": "w", "session_secret": "s",
                  "web_ui_url": "winner"}
        original_boot = cfg._bootstrap_save

        def seed_then_boot(c):
            with config_lock._FileLock():
                cfg._save_held(dict(winner))
            original_boot(c)

        old = os.environ["BASE_DIR"]
        os.environ["BASE_DIR"] = str(base)
        try:
            with patch.object(cfg, "_bootstrap_save", seed_then_boot):
                returned = cfg.raw()
        finally:
            os.environ["BASE_DIR"] = old
        self.assertEqual(returned["session_epoch"], 13,
                         "bootstrap loser returned first-run defaults")
        self.assertEqual(returned["auth_method"], "token")
        disk = json.loads((base / "config.json").read_text())
        self.assertEqual(disk["session_epoch"], 13)

    # 4. fix-lock safety ───────────────────────────────────────────────
    def test_fix_lock_leaves_valid_held_lock_alone(self):
        lp = Path(cfg._config_path() + ".lock")
        lp.chmod(0o600)
        with config_lock._FileLock():
            ino = lp.stat().st_ino
            proc = subprocess.Popen(
                [sys.executable, "-m", "web.authtool", "fix-lock"],
                cwd=REPO, env=os.environ.copy(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.4)
            blocked = proc.poll() is None
            unchanged = lp.stat().st_ino == ino
        out, err = proc.communicate(timeout=15)
        self.assertTrue(blocked, "repair did not wait for the held lock")
        self.assertTrue(unchanged, "valid held lock was replaced")
        self.assertEqual(proc.returncode, 0)

    def test_fix_lock_competing_repair_keeps_winner(self):
        # Invalid-mode lock; a competing repair replaces the inode with a
        # VALID lock (held by a writer) before our repair's identity
        # revalidation. Our repair must refuse instead of removing the
        # winner.
        lp = Path(cfg._config_path() + ".lock")
        lp.chmod(0o600)
        with config_lock._FileLock():
            pass
        lp.chmod(0o666)  # now invalid
        import fcntl
        real_validate = config_lock._validate_lock_fd
        real_unlink = os.unlink
        winner = {}
        called = threading.Event()

        def validate_then_compete(fd, path):
            if str(path) == str(lp) and not called.is_set():
                called.set()
                # competing repair: replace with a fresh valid lock that a
                # writer immediately holds
                real_unlink(str(lp))
                fd2 = config_lock._open_lock_file(str(lp))
                fcntl.flock(fd2, fcntl.LOCK_EX)
                winner["fd"] = fd2
                winner["ino"] = os.fstat(fd2).st_ino
            return real_validate(fd, path)

        try:
            with patch.object(config_lock, "_validate_lock_fd",
                              validate_then_compete):
                with self.assertRaises(SystemExit) as cm:
                    authtool.cmd_fix_lock(None)
            self.assertEqual(cm.exception.code, 1,
                             "repair should refuse after losing the race")
            self.assertEqual(lp.stat().st_ino, winner["ino"],
                             "valid winner lock was replaced by the loser")
            self.assertEqual(lp.stat().st_mode & 0o777, 0o600)
        finally:
            if "fd" in winner:
                os.close(winner["fd"])

    def test_fix_lock_refuses_actively_held_invalid_lock(self):
        # A mode-invalid lock still held by a writer (validated before the
        # mode changed) must not be unlinked: the repair refuses.
        lp = Path(cfg._config_path() + ".lock")
        lp.chmod(0o600)
        with config_lock._FileLock():
            lp.chmod(0o666)
            ino = lp.stat().st_ino
            with self.assertRaises(SystemExit) as cm:
                authtool.cmd_fix_lock(None)
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(lp.stat().st_ino, ino,
                             "actively held lock was removed")
        lp.chmod(0o600)

    # 5. crash-stage policy ────────────────────────────────────────────
    def test_startup_cleanup_removes_only_orphaned_stages(self):
        # Round-5 policy: a stage is removed only when its owning pid is
        # provably dead — never by age alone while the owner lives, and
        # never when liveness is unknown. The full crash family
        # (.tmp/.tmp.upload/sidecars) is covered.
        # Use the LIVE Config() dir: another test module may have
        # repointed BASE_DIR since import.
        db_dir = Path(cfg.Config().DB_FILE).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        dead = db_dir / (".safescanarr.db.restore.99999999.1.%s.tmp"
                         % ("a" * 16))
        dead.write_bytes(b"x")
        dead_upload = db_dir / (".safescanarr.db.restore.99999999.1.%s"
                                ".tmp.upload" % ("e" * 16))
        dead_upload.write_bytes(b"x")
        dead_wal = db_dir / (".safescanarr.db.restore.99999999.1.%s"
                             ".tmp-wal" % ("f" * 16))
        dead_wal.write_bytes(b"x")
        os.utime(dead, (time.time() - 3600, time.time() - 3600))
        alive_pid = next(p for p in os.listdir("/proc") if p.isdigit()
                         and int(p) != os.getpid())
        aged_live = db_dir / (".safescanarr.db.restore.%s.2.%s.tmp"
                              % (alive_pid, "b" * 16))
        aged_live.write_bytes(b"x")
        # aged beyond any retention window, but the owner is alive
        age = 30 * 86400
        os.utime(aged_live, (time.time() - age, time.time() - age))
        try:
            s._cleanup_orphan_restore_stages()
            self.assertFalse(dead.exists(), "orphaned stage not removed")
            self.assertFalse(dead_upload.exists(),
                             "orphaned upload copy not removed")
            self.assertFalse(dead_wal.exists(),
                             "orphaned stage sidecar not removed")
            self.assertTrue(aged_live.exists(),
                            "stage of a live pid was removed despite age")
        finally:
            try:
                aged_live.unlink()
            except FileNotFoundError:
                pass
        # unknown liveness (/proc unreadable) preserves everything
        dead2 = db_dir / (".safescanarr.db.restore.99999998.4.%s.tmp"
                          % ("g" * 16))
        dead2.write_bytes(b"x")
        with patch.object(s, "_live_pids", lambda: None):
            s._cleanup_orphan_restore_stages()
        self.assertTrue(dead2.exists(),
                        "stage removed while liveness was unknown")
        dead2.unlink()

    def test_no_glob_deletion_during_active_requests(self):
        # A restore in flight beside an old orphan: the restore must not
        # touch the orphan (cleanup is startup-only).
        self.login(True)
        orphan = Path(_BASE) / (".safescanarr.db.restore.99999997.1.%s.tmp"
                                % ("d" * 16))
        orphan.write_bytes(b"x")
        blob = self.backup()
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(orphan.exists(),
                        "restore path deleted a foreign stage file")
        orphan.unlink()


if __name__ == "__main__":
    unittest.main()
