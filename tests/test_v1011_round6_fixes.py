"""tests/test_v1011_round6_fixes.py — regression tests for the round-6
skeptic review (docs/v1.0.11-round6-fixes.md), same change as the
publication-protocol repair.

Schedules (all at the REAL seams — the drain's actual Condition.wait,
real restores, real paired lease acquisition/release):
  1  writer arriving AFTER the drain is observed stays blocked
     (publication admission is truly shared module state)
  2  timeout + immediate retry with the first holder still leased:
     retry also aborts; the old writer's successful write cannot
     disappear onto an obsolete inode (live file never replaced)
  3  two simultaneous restores + an active holder: no publication
     while the holder remains; publication ownership serialized
  4  release needing _db_lock during the drain completes (no deadlock)
  5  logout during the drain: session not resurrected, no deadlock
  6  pid discovery is a SINGLE observation: stable AND transient /proc
     failures preserve everything
  7  recognized-state policy: unparseable sonarr/radarr cursors are
     deliberately reset at import; defensive consumers resynchronize
     (restore + real cursor consumption with benign local responses)
"""

import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASE = tempfile.mkdtemp(prefix="safescanarr-r6fixes-")
os.environ["BASE_DIR"] = _BASE
for _var in ("SS_TOKEN", "SS_PASSWORD_HASH", "SS_COOKIE_SECURE",
             "SS_SESSION_SECRET"):
    os.environ.pop(_var, None)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import config as cfg
import config_lock
from database import Database
from web import auth
import web.server as s

TOKEN = "r6-fixes-test-token-" + "z" * 20
COOKIE = "safescanarr_session"


class Round6FixTests(unittest.TestCase):

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

    def csrf(self, c=None):
        with (c or self.a).session_transaction() as ss:
            return ss["csrf"]

    def backup(self):
        dest = Path(_BASE) / "bk-r6.db"
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

    def marker(self, key):
        con = sqlite3.connect(cfg.Config().DB_FILE)
        try:
            r = con.execute("SELECT value FROM state WHERE key=?",
                            (key,)).fetchone()
            return r[0] if r else None
        finally:
            con.close()

    def _drain_watch(self, thread_name):
        """Patch _db_cond.wait so the test observes the REAL drain of the
        restore running on thread *thread_name* (and only that thread)."""
        entered = threading.Event()
        real_wait = s._db_cond.wait

        def watch(*a, **kw):
            if threading.current_thread().name == thread_name:
                entered.set()
            return real_wait(*a, **kw)
        return entered, watch

    # 1. admission is shared state: writer arriving after the drain ────
    def test_writer_after_drain_observed_stays_blocked(self):
        self.login(True)
        blob = self.backup()
        old = s.get_db()  # balanced leased writer
        out = {}
        entered, watch = self._drain_watch("restore-drain")

        def run_restore():
            out["r"] = self.restore(blob)

        acquired = []
        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 15), \
             patch.object(s._db_cond, "wait", watch):
            rt = threading.Thread(target=run_restore, name="restore-drain")
            rt.start()
            self.assertTrue(entered.wait(5),
                            "restore never entered its drain wait")
            # The admission flag is genuinely SHARED module state now.
            self.assertTrue(s._publishing,
                            "publication flag not visible as module state")
            new_same_file = threading.Thread(
                target=lambda: acquired.append(s.get_db()))
            new_same_file.start()
            new_same_file.join(0.7)
            self.assertTrue(new_same_file.is_alive(),
                            "get_db() admitted a new writer during the "
                            "publication drain")
            self.assertEqual(acquired, [])
            # Release the holder: the drain finishes, publication runs,
            # and only then may the blocked writer acquire.
            s.release_db(old)
            rt.join(15)
            new_same_file.join(10)
        self.assertFalse(rt.is_alive())
        self.assertFalse(new_same_file.is_alive())
        self.assertEqual(out["r"].status_code, 200)
        self.assertEqual(len(acquired), 1)
        self.assertIsNot(acquired[0], old,
                         "post-publication handle is the retired "
                         "generation")
        s.release_db(acquired[0])

    # 2. timeout + immediate retry, first holder still leased ─────────
    def test_timeout_then_retry_with_holder_still_leased(self):
        self.login(True)
        blob = self.backup()
        db_file = cfg.Config().DB_FILE
        inode_before = os.stat(db_file).st_ino
        old = s.get_db()
        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 0.15):
            first = self.restore(blob)
            # Immediate retry while the first holder is STILL leased.
            second = self.restore(blob)
        self.assertEqual(first.status_code, 500)
        self.assertEqual(second.status_code, 500,
                         "retry published while the still-leased "
                         "generation from the timed-out attempt was "
                         "active")
        # The old writer remains usable, and because the live file was
        # never replaced its successful write is VISIBLE on the published
        # file — the disappearing-write failure mode is a publication
        # that strands this writer on an obsolete inode.
        old.set_poller_state("after-second-restore", "held-writer")
        self.assertEqual(os.stat(db_file).st_ino, inode_before,
                         "live file was replaced while an old "
                         "generation was still leased")
        self.assertEqual(self.marker("after-second-restore"),
                         "held-writer",
                         "the held writer's successful write disappeared "
                         "(published onto an inode it cannot see)")
        s.release_db(old)
        # After the holder finally releases, publication works again.
        third = self.restore(blob)
        self.assertEqual(third.status_code, 200)

    # 3. two simultaneous restores + an active holder ─────────────────
    def test_two_simultaneous_restores_with_active_holder(self):
        self.login(True)
        cookie = self.login()  # refresh
        other = s.app.test_client()
        other.set_cookie(COOKIE, cookie)
        blob = self.backup()
        db_file = cfg.Config().DB_FILE
        inode_before = os.stat(db_file).st_ino
        old = s.get_db()
        out = {}
        entered, watch = self._drain_watch("restore1")

        def first_restore():
            out["first"] = self.restore(blob)

        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 2), \
             patch.object(s._db_cond, "wait", watch):
            t1 = threading.Thread(target=first_restore, name="restore1")
            t1.start()
            self.assertTrue(entered.wait(5),
                            "first restore never entered its drain")
            # Second restore while the first is paused IN its drain and
            # the original holder is still leased.
            second = self.restore(blob, other)
            t1.join(10)
        self.assertFalse(t1.is_alive())
        # Neither restore may publish over the still-active holder.
        self.assertEqual(second.status_code, 500,
                         "second restore published while the original "
                         "holder was still leased")
        self.assertEqual(out["first"].status_code, 500,
                         "first restore published while the original "
                         "holder was still leased")
        old.set_poller_state("after-two-drains", "held-writer")
        self.assertEqual(os.stat(db_file).st_ino, inode_before,
                         "live file was replaced during overlapping "
                         "restores with an active holder")
        self.assertEqual(self.marker("after-two-drains"), "held-writer",
                         "the held writer's successful write disappeared "
                         "(published onto an inode it cannot see)")
        s.release_db(old)

    # 4. release needing _db_lock during the drain (no deadlock) ──────
    def test_release_needing_db_lock_during_drain(self):
        self.login(True)
        blob = self.backup()
        old = s.get_db()
        out = {}
        entered, watch = self._drain_watch("restore-drain")

        def run_restore():
            out["r"] = self.restore(blob)

        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 15), \
             patch.object(s._db_cond, "wait", watch):
            rt = threading.Thread(target=run_restore, name="restore-drain")
            rt.start()
            self.assertTrue(entered.wait(5))
            self.assertTrue(rt.is_alive())
            # release_db needs _db_lock, which the drain's wait has
            # released — this must neither deadlock nor be starved.
            s.release_db(old)
            rt.join(15)
        self.assertFalse(rt.is_alive())
        self.assertEqual(out["r"].status_code, 200,
                         "restore failed after the holder released "
                         "during the drain")

    # 5. logout during the drain ───────────────────────────────────────
    def test_logout_during_drain_no_resurrection(self):
        cookie = self.login(True)
        other = s.app.test_client()
        other.set_cookie(COOKIE, cookie)
        blob = self.backup()
        old = s.get_db()
        out = {}
        entered, watch = self._drain_watch("restore-drain")

        def run_restore():
            out["r"] = self.restore(blob)

        logout_out = {}

        def run_logout():
            logout_out["r"] = other.post(
                "/logout", headers={"X-CSRF-Token": self.csrf(other)})

        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 15), \
             patch.object(s._db_cond, "wait", watch):
            rt = threading.Thread(target=run_restore, name="restore-drain")
            rt.start()
            self.assertTrue(entered.wait(5))
            lt = threading.Thread(target=run_logout)
            lt.start()
            lt.join(1.0)
            # The logout cannot delete the row mid-drain (it needs a
            # handle; admission is closed) — and it must not deadlock.
            self.assertTrue(lt.is_alive(),
                            "logout completed INSIDE the drain window")
            s.release_db(old)
            rt.join(15)
            lt.join(10)
        self.assertFalse(rt.is_alive())
        self.assertFalse(lt.is_alive())
        self.assertEqual(out["r"].status_code, 200)
        self.assertEqual(logout_out["r"].status_code, 200,
                         "logout deadlocked or failed across publication")
        # Not resurrected: the remembered row is gone from the published
        # file and the logged-out cookie no longer authenticates.
        con = sqlite3.connect(cfg.Config().DB_FILE)
        try:
            n = con.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 0, "a signed-out remembered row was "
                               "resurrected by the restore")
        self.assertEqual(other.get("/api/stats").status_code, 401)

    # 6. single pid-discovery observation ─────────────────────────────
    def test_proc_failure_single_observation_preserves_everything(self):
        import subprocess
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            db_dir = Path(cfg.Config().DB_FILE).parent
            db_dir.mkdir(parents=True, exist_ok=True)
            stage = db_dir / (f".safescanarr.db.restore.{child.pid}.1."
                              + "a" * 16 + ".tmp.upload")
            stage.write_bytes(b"benign stage")
            real = os.listdir
            calls = []

            def stable_denied(path):
                if str(path) == "/proc":
                    raise PermissionError("injected /proc unavailable")
                return real(path)

            with patch.object(s.os, "listdir", stable_denied):
                s._cleanup_orphan_restore_stages()
            self.assertTrue(stage.exists(),
                            "stage removed while /proc was unreadable")

            def transient(path):
                if str(path) == "/proc":
                    calls.append(1)
                    if len(calls) == 1:
                        raise PermissionError("transient /proc failure")
                return real(path)

            with patch.object(s.os, "listdir", transient):
                s._cleanup_orphan_restore_stages()
            self.assertTrue(stage.exists(),
                            "stage removed after a transient /proc read "
                            "failure was converted into an empty pid set")
            self.assertLessEqual(len(calls), 1,
                                 "cleanup used more than one /proc "
                                 "observation")
            self.assertTrue(child.poll() is None)
        finally:
            child.terminate()
            child.wait()

    # 7. recognized-state cursor policy + real consumption ────────────
    def _cursor_backup(self, sonarr_val, radarr_val):
        blob = self.backup()
        p = Path(_BASE) / "cursor-state.db"
        p.write_bytes(blob)
        con = sqlite3.connect(str(p))
        con.executemany(
            "INSERT OR REPLACE INTO state(key,value) VALUES(?,?)",
            [("sonarr_last_id", sonarr_val),
             ("radarr_last_id", radarr_val)])
        con.commit()
        con.close()
        return p.read_bytes()

    def test_unparseable_cursors_reset_at_import_and_consumed(self):
        import poller
        self.login(True)
        blob = self._cursor_backup("unavailable", "unavailable")
        r = self.restore(blob)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # Deliberate documented reset: neither cursor was published.
        self.assertIsNone(self.marker("sonarr_last_id"))
        self.assertIsNone(self.marker("radarr_last_id"))
        # Real consumption: a benign mocked history response is polled
        # with the REAL poll functions; the cursors resynchronize.
        config_lock.mutate(lambda v: v.update(
            sonarr_api_key="benign-local-key",
            radarr_api_key="benign-local-key",
            watch_folders=["/benign"]))
        triggered = []
        with patch.object(poller, "api_get",
                          lambda *a, **kw: {"records": [
                              {"id": 7, "data": {"importedPath":
                                                 "/benign/clip.mp4"}}]}), \
             patch.object(poller, "trigger_scanner",
                          lambda path, source, c: triggered.append(
                              (path, source))):
            outcomes = {}
            with s.db_lease() as db:
                for name in ("sonarr", "radarr"):
                    try:
                        getattr(poller, "poll_" + name)(db)
                        outcomes[name] = "ok"
                    except Exception as e:
                        outcomes[name] = f"{type(e).__name__}: {e}"
        self.assertEqual(outcomes, {"sonarr": "ok", "radarr": "ok"})
        self.assertEqual(len(triggered), 2)
        self.assertEqual(self.marker("sonarr_last_id"), "7")
        self.assertEqual(self.marker("radarr_last_id"), "7")

    def test_defensive_consumer_survives_live_foreign_cursor(self):
        # The same foreign value reaching the LIVE state by another path
        # (not via restore) must degrade to a rescan, never a crash.
        import poller
        self.login(True)
        with s.db_lease() as db:
            db.set_poller_state("sonarr_last_id", "unavailable")
        config_lock.mutate(lambda v: v.update(
            sonarr_api_key="benign-local-key", watch_folders=["/benign"]))
        with patch.object(poller, "api_get",
                          lambda *a, **kw: {"records": [
                              {"id": 3, "data": {"importedPath":
                                                 "/benign/clip.mp4"}}]}), \
             patch.object(poller, "trigger_scanner",
                          lambda path, source, c: None):
            with s.db_lease() as db:
                poller.poll_sonarr(db)  # must not raise
        self.assertEqual(self.marker("sonarr_last_id"), "3")


if __name__ == "__main__":
    unittest.main()
