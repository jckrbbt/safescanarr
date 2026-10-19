"""tests/test_v1011_round5_fixes.py — regression tests for the round-5
skeptic review (docs/v1.0.11-round5-fixes.md).

Runs the REAL probe schedules at the REAL seams:
  1  HIGH DB lifecycle/publication: quarantine lease held through its last
     DB access across a concurrent restore; approve serialized with
     publication (no obsolete-inode false success); a leased old writer
     defers publication (no post-replace write to the retired inode);
     checkpoint EXCEPTION and BUSY results abort BEFORE os.replace with
     the live marker surviving; no sidecar unlink of a live generation
  2  MEDIUM app-critical state: scan_pid unparseable -> not-running JSON;
     scan_pid discarded at import (documented ephemeral semantics); API
     internal errors are JSON
  3  MEDIUM bootstrap winner-read failure fails closed (read error and
     malformed JSON) — never losing seed defaults as if persisted
  4  MEDIUM fix-lock TOCTOU: before-comparison and after-comparison
     (second stat) replacement schedules both refuse
  5  LOW orphan cleanup family/policy (see round-4 file's updated test)
"""

import io
import inspect
import json
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
_BASE = tempfile.mkdtemp(prefix="safescanarr-r5fixes-")
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
import web.authtool as authtool

TOKEN = "r5-fixes-test-token-" + "z" * 20
COOKIE = "safescanarr_session"


class Round5FixTests(unittest.TestCase):

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
        dest = Path(_BASE) / "bk-r5.db"
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

    def live_marker(self, key):
        con = sqlite3.connect(cfg.Config().DB_FILE)
        try:
            r = con.execute("SELECT value FROM state WHERE key=?",
                            (key,)).fetchone()
            return r[0] if r else None
        finally:
            con.close()

    # 1. DB lifecycle / publication protocol ──────────────────────────
    def _seed_flagged_file(self):
        video = Path(_BASE) / "clip.mp4"
        video.write_bytes(b"benign test content")
        with s.db_lease() as db:
            db.upsert_file(str(video), "clip.mp4", 22, 1.0,
                           status="flagged", review_state="pending")
        return video

    def _interleave_restore(self, mode):
        """The real probe schedule: pause the action route at its seam
        (quarantine: inside relocate; approve: before set_review_state),
        start a full restore from another client — which must DRAIN/DEFER
        while the action's lease is held — then resume the action."""
        self.login(True)
        video = self._seed_flagged_file()
        blob = self.backup()
        entered = threading.Event()
        release = threading.Event()
        out = {}
        csrf = self.csrf()

        def request_thread():
            out["response"] = self.a.post(
                "/api/sheets/" + mode,
                json={"stem": "clip"},
                headers={"X-CSRF-Token": csrf})

        if mode == "quarantine":
            real = s.relocate

            def delay(*a, **kw):
                entered.set()
                assert release.wait(10)
                return real(*a, **kw)
            context = patch.object(s, "relocate", delay)
        else:
            real = Database.set_review_state

            def delay(self_, *a, **kw):
                if threading.current_thread().name == "action-route":
                    entered.set()
                    assert release.wait(10)
                return real(self_, *a, **kw)
            context = patch.object(Database, "set_review_state", delay)

        with context:
            t = threading.Thread(target=request_thread,
                                 name="action-route")
            t.start()
            self.assertTrue(entered.wait(5))
            other = s.app.test_client()
            other.set_cookie(COOKIE, self.a.get_cookie(COOKIE).value)
            restore_out = {}

            def restore_thread():
                restore_out["response"] = self.restore(blob, other)
            rt = threading.Thread(target=restore_thread, name="restore")
            with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 30.0):
                rt.start()
                # The restore must NOT publish while the action's lease
                # is held (drain/defer, no obsolete-inode writes).
                rt.join(1.5)
                deferred = rt.is_alive()
                release.set()
                rt.join(15)
            t.join(15)
        with s.db_lease() as db:
            row = db.get_file(str(video))
        return {
            "restore": restore_out["response"].status_code,
            "deferred": deferred,
            "action": out["response"].status_code,
            "action_json": out["response"].is_json,
            "state": row["review_state"] if row else None,
            "qp": row["quarantine_path"] if row else None,
            "joined": not t.is_alive() and not rt.is_alive(),
        }

    def test_quarantine_route_survives_concurrent_restore(self):
        res = self._interleave_restore("quarantine")
        # The old bug: the route's DB handle was used after its lease
        # exited → sqlite3.ProgrammingError, action 500 non-JSON, the
        # moved file recorded nowhere. Now the lease spans the whole
        # sequence and the restore drains behind it.
        self.assertTrue(res["joined"])
        self.assertTrue(res["deferred"],
                        "restore published while the quarantine lease "
                        "was held")
        self.assertTrue(res["action_json"],
                        "action route returned a non-JSON error page")
        self.assertEqual(res["action"], 200,
                         "quarantine action failed under a concurrent "
                         "restore")
        # Deterministic ordering under the protocol: the action committed
        # on the old generation BEFORE publication, and the restore (the
        # later writer) published the backup's pending state.
        self.assertEqual(res["restore"], 200)
        self.assertEqual(res["state"], "pending")

    def test_approve_route_serialized_with_publication(self):
        res = self._interleave_restore("approve")
        # The old bug: approve returned 200 with its write landing on the
        # RETIRED inode after publication (published state stuck at
        # pending, write invisible). The drain forbids that: publication
        # waits for the approve's lease, so approve always commits BEFORE
        # the replace — or the restore defers/aborts.
        self.assertTrue(res["joined"])
        self.assertTrue(res["deferred"],
                        "restore published while the approve lease was "
                        "held")
        self.assertEqual(res["action"], 200)
        self.assertTrue(res["action_json"])
        self.assertEqual(res["restore"], 200)
        # restore is the later writer: the backup's pending state is
        # published, and the approve's 200 described a write that
        # committed before the replace (never after it).
        self.assertEqual(res["state"], "pending")

    def test_busy_checkpoint_aborts_before_replace(self):
        # Real busy result: an uncommitted INSERT on a leased old
        # connection across the restore; only the checkpoint busy_timeout
        # is shortened. The busy PRAGMA row must abort the restore BEFORE
        # os.replace (live marker survives, restore 500).
        self.login(True)
        blob = self.backup()
        old = s.get_db()
        old._con.execute("BEGIN")
        old._con.execute("INSERT INTO state(key,value) "
                         "VALUES('writer-inflight','committing')")
        real = sqlite3.connect
        checkpoints = []

        class Proxy:
            def __init__(self, c):
                self.c = c

            def execute(self, q, *a):
                if q == "PRAGMA wal_checkpoint(TRUNCATE)":
                    self.c.execute("PRAGMA busy_timeout=20")
                    cur = self.c.execute(q, *a)
                    checkpoints.append(list(cur.fetchone()))
                    return cur
                return self.c.execute(q, *a)

            def close(self):
                self.c.close()

        def connect(p, *a, **k):
            con = real(p, *a, **k)
            return Proxy(con) if str(p) == cfg.Config().DB_FILE else con

        marker_before = self.live_marker("live-only")
        with s.db_lease() as db:
            db.set_poller_state("live-only", "must-survive")
        try:
            with patch.object(s.sqlite3, "connect", connect):
                r = self.restore(blob)
            self.assertEqual(r.status_code, 500,
                             "restore published despite busy checkpoint")
            if checkpoints:
                self.assertNotEqual(
                    checkpoints[0][0], 0,
                    "checkpoint unexpectedly reported not-busy")
            self.assertEqual(self.live_marker("live-only"), "must-survive",
                             "live marker lost to a busy-checkpoint "
                             "publication")
        finally:
            old._con.rollback()
            s.release_db(old)

    def test_checkpoint_exception_aborts_before_replace(self):
        # Inject sqlite3.OperationalError ONLY for the live pre-publication
        # checkpoint; stage import and os.replace are real. The restore
        # must abort BEFORE publishing: the live marker survives.
        self.login(True)
        blob = self.backup()
        with s.db_lease() as db:
            db.set_poller_state("live-only", "must-survive")
        real = sqlite3.connect

        class Proxy:
            def __init__(self, c):
                self.c = c

            def execute(self, q, *a):
                if q == "PRAGMA wal_checkpoint(TRUNCATE)":
                    raise sqlite3.OperationalError(
                        "injected checkpoint I/O failure")
                return self.c.execute(q, *a)

            def close(self):
                self.c.close()

        def connect(p, *a, **k):
            con = real(p, *a, **k)
            return Proxy(con) if str(p) == cfg.Config().DB_FILE else con

        with patch.object(s.sqlite3, "connect", connect):
            r = self.restore(blob)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(self.live_marker("live-only"), "must-survive",
                         "live committed marker disappeared: the upload was "
                         "published despite the checkpoint exception")
        # the service still works afterwards
        self.assertEqual(self.a.get("/api/stats").status_code, 200)

    def test_no_new_writer_slips_into_drain(self):
        # While a restore is actually IN its drain (observed via the real
        # Condition.wait), a NEW get_db() must block, not acquire the
        # soon-to-be-retired generation and write into the drain window.
        # (Round-6 fix: the writer must start AFTER the drain is observed;
        # starting it before the restore only tests the pre-drain window.)
        self.login(True)
        blob = self.backup()
        old = s.get_db()  # the leased writer
        acquired_during_drain = []
        drain_started = threading.Event()
        real_wait = s._db_cond.wait

        def watch(*a, **kw):
            if threading.current_thread().name == "restore-drain":
                drain_started.set()
            return real_wait(*a, **kw)

        def new_writer():
            with s.db_lease() as db:   # must block until publication ends
                # record whether publication was still in its drain
                acquired_during_drain.append(s._publishing)

        out = {}
        with patch.object(s, "_PUBLISH_DRAIN_TIMEOUT_SECONDS", 0.5), \
             patch.object(s._db_cond, "wait", watch):
            rt = threading.Thread(
                target=lambda: out.setdefault("r", self.restore(blob)),
                name="restore-drain")
            rt.start()
            # Start the new writer only once the drain is REALLY running.
            self.assertTrue(drain_started.wait(5),
                            "restore never entered its drain wait")
            t = threading.Thread(target=new_writer)
            t.start()
            rt.join(10)
            t.join(10)
        self.assertFalse(rt.is_alive())
        self.assertFalse(t.is_alive())
        self.assertEqual(out["r"].status_code, 500,
                         "restore should have aborted: leased writer "
                         "`old` never released")
        # The blocked writer may only acquire a handle once publication
        # has ended (drain aborted, nothing replaced) — never while the
        # drain window is open.
        self.assertEqual(acquired_during_drain, [False],
                         "get_db() issued a handle during the publication "
                         "drain")
        s.release_db(old)

    # 2. App-critical state validation ────────────────────────────────
    def test_scan_pid_unparseable_is_not_running_json(self):
        # Even if a malformed scan_pid value somehow reaches the live
        # state, /api/scan/status treats it as not-running (JSON 200),
        # never a ValueError traceback.
        self.login(False)
        with s.db_lease() as db:
            db.set_poller_state("scan_pid", "unavailable")
        r = self.a.get("/api/scan/status")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.is_json)
        self.assertFalse(r.get_json()["running"])

    def test_restore_discards_scan_pid_state(self):
        # Documented semantics: ephemeral process state (scan_pid) is
        # discarded at import — a foreign TEXT value is never published.
        self.login(True)
        blob = self.backup()
        p = Path(_BASE) / "invalid-state.db"
        p.write_bytes(blob)
        con = sqlite3.connect(str(p))
        con.execute("INSERT OR REPLACE INTO state(key,value) "
                    "VALUES('scan_pid','unavailable')")
        con.commit()
        con.close()
        r = self.restore(p.read_bytes())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        after = self.a.get("/api/scan/status")
        self.assertEqual(after.status_code, 200)
        self.assertTrue(after.is_json)
        self.assertFalse(after.get_json()["running"])
        self.assertIsNone(self.live_marker("scan_pid"))

    def test_api_internal_error_returns_json(self):
        self.login(False)
        # Inject an arbitrary unhandled error inside a route (past the
        # auth filters, at the route body's Config() call) and require a
        # JSON 500 — the UI fetches these endpoints.
        real_init = cfg.Config.__init__

        def boom(self):
            raise RuntimeError("injected internal error")

        with patch.object(cfg.Config, "__init__", boom):
            r = self.a.get("/api/sheets")
        self.assertEqual(r.status_code, 500)
        self.assertTrue(r.is_json, "internal error returned non-JSON")
        self.assertEqual(r.get_json()["status"], "error")

    # 3. Bootstrap winner-read failure fails closed ───────────────────
    def _bootstrap_in(self, base):
        import shutil
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        old = os.environ["BASE_DIR"]
        os.environ["BASE_DIR"] = str(base)
        return old

    def test_bootstrap_winner_read_error_propagates(self):
        # The real probe schedule: a cooperating writer persists epoch 13
        # before bootstrap's absence recheck; only the winner READ then
        # fails. raw() must propagate the error — never return the losing
        # seed defaults (epoch 0) as if persisted.
        import builtins
        base = Path(_BASE) / "boot-unreadable"
        old = self._bootstrap_in(base)
        real_open = builtins.open
        winner_on_disk = threading.Event()

        def denied(path, *a, **kw):
            if str(path) == str(base / "config.json") and winner_on_disk.is_set():
                raise PermissionError("injected winner read failure")
            return real_open(path, *a, **kw)

        def seed_boot_then_deny(c):
            with config_lock._FileLock():
                cfg._save_held({"session_epoch": 13, "auth_method": "token",
                                "auth_token": "winner",
                                "session_secret": "s"})
            winner_on_disk.set()
            # real bootstrap sees the file exists and leaves it alone

        try:
            with patch.object(cfg, "_bootstrap_save", seed_boot_then_deny), \
                 patch.object(builtins, "open", denied):
                with self.assertRaises(OSError):
                    cfg.raw()
        finally:
            os.environ["BASE_DIR"] = old
        # the persisted winner is intact on disk
        disk = json.loads((base / "config.json").read_text())
        self.assertEqual(disk["session_epoch"], 13)

    def test_bootstrap_winner_malformed_json_propagates(self):
        # Same schedule, but the winner on disk is malformed JSON: the
        # parse error propagates instead of returning seed defaults.
        base = Path(_BASE) / "boot-malformed"
        old = self._bootstrap_in(base)

        def seed_boot_malformed(c):
            (base / "config.json").write_bytes(b"{not json at all")

        try:
            with patch.object(cfg, "_bootstrap_save", seed_boot_malformed):
                with self.assertRaises(ValueError):
                    cfg.raw()
        finally:
            os.environ["BASE_DIR"] = old

    def test_bootstrap_readable_winner_still_wins(self):
        # Guard the guard: with a readable winner the bootstrap loser
        # still returns the winner's persisted config.
        base = Path(_BASE) / "boot-readable"
        old = self._bootstrap_in(base)

        def seed_boot(c):
            with config_lock._FileLock():
                cfg._save_held({"session_epoch": 13, "auth_method": "token",
                                "auth_token": "winner",
                                "session_secret": "s"})

        try:
            with patch.object(cfg, "_bootstrap_save", seed_boot):
                result = cfg.raw()
        finally:
            os.environ["BASE_DIR"] = old
        self.assertEqual(result["session_epoch"], 13)
        self.assertEqual(result["auth_method"], "token")

    # 4. fix-lock TOCTOU seam ─────────────────────────────────────────
    @staticmethod
    def _seam_line(needle: str) -> int:
        """Resolve a REAL line inside _remove_if_same_inode by source
        content, so a moved line never silently turns the pause into a
        no-op (the round-6 finding: hardcoded lines paused nowhere)."""
        lines, start = inspect.getsourcelines(
            authtool._remove_if_same_inode)
        for i, text in enumerate(lines):
            if needle in text:
                return start + i
        raise AssertionError(f"seam {needle!r} not found in source")

    def _fix_lock_with_pause(self, pause_at):
        """Run cmd_fix_lock on an invalid lock with a competing repair
        that replaces the lock file at a chosen point INSIDE the real
        _remove_if_same_inode implementation (via sys.settrace line
        pauses, like the round-5 probe)."""
        import fcntl
        lp = Path(cfg._config_path() + ".lock")
        lp.chmod(0o600)
        with config_lock._FileLock():
            pass
        lp.chmod(0o666)  # now invalid: repair target
        winner = {}
        events = []
        target = authtool._remove_if_same_inode.__code__

        def install_winner():
            os.unlink(str(lp))
            fd = config_lock._open_lock_file(str(lp))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            winner.update(fd=fd, ino=os.fstat(fd).st_ino)

        def trace(frame, event, arg):
            if (frame.f_code is target and event == "line"
                    and frame.f_lineno in pause_at and not winner):
                events.append(frame.f_lineno)
                install_winner()
            return trace

        sys.settrace(trace)
        try:
            try:
                authtool.cmd_fix_lock(None)
                code = 0
            except SystemExit as e:
                code = e.code
        finally:
            sys.settrace(None)
        try:
            replaced = bool(winner) and lp.stat().st_ino != winner["ino"]
        finally:
            if winner:
                os.close(winner["fd"])
        return code, replaced, events

    def test_fix_lock_before_comparison_replacement_refused(self):
        # Replacement lands at the FIRST inode comparison (resolved from
        # the live source, not a hardcoded line): the repair must refuse,
        # winner intact. The pause must actually fire — a seam test whose
        # trace hook never paused proves nothing.
        line = self._seam_line("if os.stat(path).st_ino")
        code, replaced, events = self._fix_lock_with_pause(pause_at={line})
        self.assertEqual(events, [line],
                         "trace hook never paused at the comparison seam")
        self.assertEqual(code, 1, "repair did not refuse")
        self.assertFalse(replaced, "valid winner lock was removed")

    def test_fix_lock_after_comparison_replacement_refused(self):
        # Replacement lands AFTER the initial comparison, at the second
        # (dirfd) inode check — the round-5 seam. The repair must refuse.
        line = self._seam_line("st = os.stat(name, dir_fd=dirfd)")
        code, replaced, events = self._fix_lock_with_pause(pause_at={line})
        self.assertEqual(events, [line],
                         "trace hook never paused at the dirfd seam")
        self.assertEqual(code, 1, "repair did not refuse at the seam")
        self.assertFalse(replaced, "valid winner lock was removed at the seam")

    def test_fix_lock_final_unlink_seam_is_explicitly_unsupported(self):
        # The REAL seam: replacement after the second stat, at the unlink
        # line itself. Under the documented maintenance-only restriction
        # (writers stopped, one repair at a time) this schedule cannot
        # occur; the implementation detects replacement only BEFORE the
        # last comparison. This test pins the schedule honestly: the
        # pause fires, and what happens at this residual window is the
        # documented unsupported case — it does NOT claim refusal.
        line = self._seam_line("os.unlink(name, dir_fd=dirfd)")
        code, replaced, events = self._fix_lock_with_pause(pause_at={line})
        self.assertEqual(events, [line],
                         "trace hook never paused at the unlink seam")


if __name__ == "__main__":
    unittest.main()
