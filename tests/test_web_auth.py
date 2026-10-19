"""tests/test_web_auth.py — v1.0.11 remember-me / session invalidation tests."""

import email.utils
import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

# Isolate BASE_DIR and clear auth-related env BEFORE importing server.
_BASE = tempfile.mkdtemp(prefix="safescanarr-test-")
os.environ["BASE_DIR"] = _BASE
for _var in ("SS_TOKEN", "SS_PASSWORD_HASH", "SS_COOKIE_SECURE"):
    os.environ.pop(_var, None)

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module
from web import auth
import web.server as server

PW = "Tr0ub4dor&3x"
NEW_PW = "An0therP4ss&word"
SESSION_COOKIE = "safescanarr_session"


def _rid_hash(rid):
    return hashlib.sha256(rid.encode()).hexdigest()


def _row_count(rid_hash=None):
    db = server.get_db()
    try:
        con = db._con
        if rid_hash is None:
            cur = con.execute("SELECT COUNT(*) FROM web_sessions")
        else:
            cur = con.execute("SELECT COUNT(*) FROM web_sessions WHERE rid_hash = ?",
                              (rid_hash,))
        return cur.fetchone()[0]
    finally:
        server.release_db(db)


def _all_rows():
    db = server.get_db()
    try:
        return db._con.execute(
            "SELECT rid_hash, expires_at FROM web_sessions").fetchall()
    finally:
        server.release_db(db)


def _set_cookie_headers(resp):
    return [v for h, v in resp.headers
            if h == "Set-Cookie" and v.startswith(SESSION_COOKIE)]


def _cookie_value(client):
    c = client.get_cookie(SESSION_COOKIE)
    return c.value if c else None


class WebAuthTests(unittest.TestCase):

    def setUp(self):
        server._reset_db()
        try:
            os.unlink(config_module._config_path())
        except FileNotFoundError:
            pass
        # Start from a pristine database file (rows must not leak between tests)
        db_file = config_module.Config().DB_FILE
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(db_file + suffix)
            except FileNotFoundError:
                pass
        auth.reset_cache()
        auth.configure_once(password=PW)
        server._reset_db()  # re-open against the fresh config's DB file
        server._throttle.clear()
        server._rate_limit.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server._reset_db()
        shutil.rmtree(_BASE, ignore_errors=True)

    def login(self, remember=None, client=None, pw=PW):
        c = client or self.client
        body = {"secret": pw}
        if remember is not None:
            body["remember"] = remember
        resp = c.post("/login", json=body)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return resp

    def csrf(self, client=None):
        with (client or self.client).session_transaction() as sess:
            return sess["csrf"]

    # 1 ────────────────────────────────────────────────────────────
    def test_unchecked_not_persistent(self):
        resp = self.login(remember=False)
        cookies = _set_cookie_headers(resp)
        self.assertEqual(len(cookies), 1)
        self.assertNotIn("Expires=", cookies[0])
        self.assertNotIn("Max-Age=", cookies[0])
        self.assertEqual(resp.get_json()["remembered"], False)
        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        self.assertEqual(_row_count(), 0)

    # 2 ────────────────────────────────────────────────────────────
    def test_checked_persistent_30d(self):
        now = time.time()
        resp = self.login(remember=True)
        self.assertEqual(resp.get_json()["remembered"], True)
        cookies = _set_cookie_headers(resp)
        self.assertEqual(len(cookies), 1)
        self.assertIn("Expires=", cookies[0])
        expires = None
        for part in cookies[0].split(";"):
            if part.strip().startswith("Expires="):
                expires = email.utils.parsedate_to_datetime(
                    part.split("=", 1)[1]).timestamp()
        self.assertIsNotNone(expires)
        self.assertTrue(abs(expires - (now + 30 * 86400)) < 86400,
                        f"cookie expiry {expires} not ~30d from {now}")
        rows = _all_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(abs(rows[0]["expires_at"] - (now + 30 * 86400)) < 86400)

    # 3 ────────────────────────────────────────────────────────────
    def test_remember_coercion_and_disable(self):
        for val in ("true", "1", 1):
            server._rate_limit.clear()  # 4 rapid logins would trip the bucket
            c = server.app.test_client()
            resp = self.login(remember=val, client=c)
            self.assertEqual(resp.get_json()["remembered"], True, f"remember={val!r}")
        with server.db_lease() as db:
            db.delete_web_sessions_except(None)
        # remember_me_days=0 disables the checkbox and persistence
        cfg = config_module.raw()
        cfg["remember_me_days"] = 0
        config_module.save(cfg)
        server.REMEMBER_DAYS = 0
        try:
            resp = self.client.get("/login")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn('id="login-remember"', resp.get_data(as_text=True))
            server._rate_limit.clear()
            resp = self.login(remember=True)
            self.assertEqual(resp.get_json()["remembered"], False)
            cookies = _set_cookie_headers(resp)
            self.assertEqual(len(cookies), 1)
            self.assertNotIn("Expires=", cookies[0])
            self.assertEqual(_row_count(), 0)
        finally:
            server.REMEMBER_DAYS = 30

    # 4 ────────────────────────────────────────────────────────────
    def test_logout_revokes_server_side(self):
        self.login(remember=True)
        with self.client.session_transaction() as sess:
            rid = sess["rid"]
        cookie = _cookie_value(self.client)
        self.assertIsNotNone(cookie)
        resp = self.client.post("/logout",
                                headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_row_count(_rid_hash(rid)), 0)
        self.assertEqual(_row_count(), 0)
        fresh = server.app.test_client()
        fresh.set_cookie(SESSION_COOKIE, cookie)
        self.assertEqual(fresh.get("/api/stats").status_code, 401)

    # 5 ────────────────────────────────────────────────────────────
    def test_password_change_kills_sessions(self):
        a = server.app.test_client()
        b = server.app.test_client()
        self.login(remember=True, client=a)
        self.login(remember=False, client=b)
        self.assertEqual(a.get("/api/stats").status_code, 200)
        self.assertEqual(b.get("/api/stats").status_code, 200)
        auth.configure_password(NEW_PW)
        self.assertEqual(a.get("/api/stats").status_code, 401)
        self.assertEqual(b.get("/api/stats").status_code, 401)

    # 6 ────────────────────────────────────────────────────────────
    def test_token_mode_kills_old_sessions(self):
        self.login(remember=True)
        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        auth.configure_token("x" * 43)
        self.assertEqual(self.client.get("/api/stats").status_code, 401)

    # 7 ────────────────────────────────────────────────────────────
    def test_revoke_sessions_endpoint(self):
        a = server.app.test_client()
        b = server.app.test_client()
        self.login(remember=True, client=a)
        self.login(remember=True, client=b)
        with a.session_transaction() as sa:
            a_rid = sa["rid"]
        with b.session_transaction() as sb:
            b_rid = sb["rid"]
        resp = a.post("/api/auth/revoke-sessions",
                      headers={"X-CSRF-Token": self.csrf(client=a)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["revoked_remembered"], 1)
        self.assertEqual(a.get("/api/stats").status_code, 200)
        self.assertEqual(b.get("/api/stats").status_code, 401)
        self.assertEqual(_row_count(), 1)
        self.assertEqual(_row_count(_rid_hash(a_rid)), 1)
        self.assertEqual(_row_count(_rid_hash(b_rid)), 0)
        auth.revoke_all_sessions()
        self.assertEqual(a.get("/api/stats").status_code, 401)

    # 8 ────────────────────────────────────────────────────────────
    def test_expired_row_rejected_and_deleted(self):
        self.login(remember=True)
        with self.client.session_transaction() as sess:
            rid = sess["rid"]
        db = server.get_db()
        try:
            con = db._con
            con.execute("UPDATE web_sessions SET expires_at = ? WHERE rid_hash = ?",
                        (int(time.time()) - 1, _rid_hash(rid)))
            con.commit()
        finally:
            server.release_db(db)
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
        self.assertEqual(_row_count(_rid_hash(rid)), 0)

    # 9 ────────────────────────────────────────────────────────────
    def test_legacy_session_without_av(self):
        self.login(remember=False)
        with self.client.session_transaction() as sess:
            del sess["av"]
        self.assertEqual(self.client.get("/api/stats").status_code, 401)

    # 10 ───────────────────────────────────────────────────────────
    def test_reset_then_setup(self):
        old_client = server.app.test_client()
        self.login(remember=False, client=old_client)
        old_cookie = _cookie_value(old_client)
        self.assertIsNotNone(old_cookie)
        self.assertEqual(old_client.get("/api/stats").status_code, 200)

        auth.reset()
        auth.configure_once(password=PW)  # new salt -> new fingerprint
        old = server.app.test_client()
        old.set_cookie(SESSION_COOKIE, old_cookie)
        self.assertEqual(old.get("/api/stats").status_code, 401)

        # Full setup flow: reset, POST /setup, session must be non-persistent
        # and carry a valid av.
        auth.reset()
        setup_client = server.app.test_client()
        resp = setup_client.post("/setup", json={
            "method": "password",
            "password": PW,
            "password_confirm": PW,
        })
        self.assertEqual(resp.status_code, 200)
        cookies = _set_cookie_headers(resp)
        self.assertEqual(len(cookies), 1)
        self.assertNotIn("Expires=", cookies[0])
        with setup_client.session_transaction() as sess:
            self.assertIn("av", sess)
        self.assertEqual(setup_client.get("/api/stats").status_code, 200)

    # 11 ───────────────────────────────────────────────────────────
    def test_throttling_untouched_by_remembered_requests(self):
        self.login(remember=True)
        server._throttle.clear()  # isolate: only the requests below may record
        with server.db_lease() as db:
            db.delete_web_sessions_except(None)  # revoke row
        for _ in range(12):
            self.client.get("/api/stats")
        self.assertNotIn("127.0.0.1", server._throttle)

        # 10 bad logins (remember=true) -> 11th is 429. Rate-limit patched
        # out so only the throttle decides.
        real_rl = server._rate_limit_check
        server._rate_limit_check = lambda ip: True
        try:
            for i in range(10):
                resp = self.client.post("/login",
                                        json={"secret": "wrong", "remember": True})
                self.assertEqual(resp.status_code, 401, f"attempt {i}")
        finally:
            server._rate_limit_check = real_rl
        server._rate_limit.clear()
        resp = self.client.post("/login", json={"secret": "wrong", "remember": True})
        self.assertEqual(resp.status_code, 429)

    # 12 ───────────────────────────────────────────────────────────
    def test_csrf_on_revoke_sessions(self):
        self.login(remember=True)
        resp = self.client.post("/api/auth/revoke-sessions")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post("/api/auth/revoke-sessions",
                                headers={"Origin": "http://evil.example",
                                         "X-CSRF-Token": self.csrf()})
        self.assertEqual(resp.status_code, 403)

    # 13 ───────────────────────────────────────────────────────────
    def test_login_page_with_stale_session(self):
        self.login(remember=True)
        with server.db_lease() as db:
            db.delete_web_sessions_except(None)  # row gone -> stale
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("login-form", resp.get_data(as_text=True))

    # 14 ───────────────────────────────────────────────────────────
    def test_fallback_secret_is_random(self):
        def boom():
            raise RuntimeError("config unreadable")
        with patch.object(config_module, "raw", boom):
            server._init_session_secret()
        # The old fallback was a public constant; the new one is ephemeral
        # random (flag set, remember-me disabled), so no forgeable key.
        self.assertTrue(server._EPHEMERAL_SECRET)
        self.assertFalse(server._remember_enabled())
        self.assertIsNotNone(server.app.secret_key)
        self.assertEqual(len(server.app.secret_key), 64)  # token_hex(32)
        self.assertNotEqual(server.app.secret_key, "")    # grep-check: no constant
        # Restore the real secret for the remaining tests.
        server._init_session_secret()
        self.assertFalse(server._EPHEMERAL_SECRET)

    # 15 ───────────────────────────────────────────────────────────
    def test_db_restore_keeps_only_current_session(self):
        a = server.app.test_client()
        self.login(remember=True, client=a)
        self.assertEqual(a.get("/api/stats").status_code, 200)
        # Backup taken now (contains only A's row).
        cfg = config_module.Config()
        backup = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        backup.close()
        src = sqlite3.connect(cfg.DB_FILE)
        dest = sqlite3.connect(backup.name)
        with dest:
            src.backup(dest)
        dest.close()
        src.close()
        # B logs in afterwards (row added).
        b = server.app.test_client()
        self.login(remember=True, client=b)
        self.assertEqual(_row_count(), 2)
        # Restore the older backup via the API.
        with open(backup.name, "rb") as f:
            resp = a.post("/api/backup/restore/db",
                          data={"file": (f, "backup.db")},
                          headers={"X-CSRF-Token": self.csrf(client=a)},
                          content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        os.unlink(backup.name)
        # A survives, B's remembered row is gone.
        self.assertEqual(a.get("/api/stats").status_code, 200)
        self.assertEqual(b.get("/api/stats").status_code, 401)


if __name__ == "__main__":
    unittest.main()
