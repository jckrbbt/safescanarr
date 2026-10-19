#!/usr/bin/env python3
"""
safescanarr/web/server.py v1.0.11

Auth model
----------
Every route except ``/static/*``, ``/login``, ``/setup``, and ``/health``
requires authentication. Two modes are supported:

  * token mode:   shared token via session cookie (POST /login) or
                  ``X-Auth-Token`` / ``Authorization: Bearer ***
  * password mode: password login via session cookie

All state-changing requests are CSRF-protected: same-origin is enforced via
Origin/Referer, and the session CSRF token (header ``X-CSRF-Token``) must be
present. Token-mode clients may also use a valid token header as CSRF proof.

Sessions & "Remember me" (v1.0.11)
----------------------------------
Every session stores an ``av`` fingerprint bound to the current auth
credential and ``session_epoch``; a password/token change, a reset + re-setup,
or an epoch bump signs out every device on their next request.

"Remember me" (login checkbox) creates a 30-day absolute server-side session:
a random ``rid`` token is stored (hashed, sha256) in the ``web_sessions``
table and validated on every request, so remembered devices are revocable.
Revocation paths: sign out (that device), Config → "Sign out other devices"
(POST /api/auth/revoke-sessions, keeps the caller), ``authtool
revoke-sessions`` (signs out everything), credential changes, and restoring a
database backup. ``remember_me_days: 0`` in config.json disables the feature.
Set ``SS_COOKIE_SECURE=1`` behind HTTPS to mark the cookie Secure.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from flask import (
    Flask, after_this_request, jsonify, redirect, request,
    send_file, send_from_directory, render_template, session,
)

# Derive the app directory from this file so the code works from any checkout
# location (and keeps working for existing /opt/safescanarr installs).
_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import arr
import config as config_module
import config_lock
import webhook
from web import auth
from config import Config
from database import Database
from fileops import COPIED_SOURCE_KEPT, MOVED, relocate, looks_deletable
from pathutil import is_within, resolve_sheet

log = logging.getLogger(__name__)
app = Flask(__name__, template_folder="templates", static_folder="static")
SCANNER = str(_APP_DIR / "scanner.py")

# ── Upload / archive limits ───────────────────────────────────────
MAX_UPLOAD_BYTES      = 512 * 1024 * 1024        # request body cap
MAX_ZIP_ENTRIES       = 20000                    # entries per restore archive
MAX_ZIP_UNCOMPRESSED  = 2 * 1024 * 1024 * 1024   # total uncompressed bytes cap

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# ── Session secret (independent of auth token) ────────────────────
_EPHEMERAL_SECRET = False


def _init_session_secret() -> None:
    """Generate/persist a session_secret and set app.secret_key."""
    global _EPHEMERAL_SECRET
    _EPHEMERAL_SECRET = False
    secret = None
    try:
        cfg = config_module.raw()
        secret = cfg.get("session_secret", "")
        if not secret:
            secret = os.environ.get("SS_SESSION_SECRET", "")
        if not secret:
            # Locked read-modify-write: take the config lock BEFORE
            # reading, then merge only the new key into whatever state is
            # current under the lock — a concurrent config change made
            # while the secret is generated can no longer be overwritten
            # by this request's stale whole-file snapshot.
            secret = secrets.token_urlsafe(32)

            def _apply(c):
                if not c.get("session_secret"):
                    c["session_secret"] = secret
            cfg = config_lock.mutate(_apply)
            # Two processes may generate concurrently; the FIRST persisted
            # secret wins — always use what is now on disk.
            secret = cfg["session_secret"]
            log.warning(
                "Generated new session_secret and persisted it to config.json (0600). "
                "Existing sessions will be invalidated once on upgrade to 1.0.5."
            )
        app.secret_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    except Exception as e:  # pragma: no cover - read-only fallback
        # Ephemeral random key: sessions won't survive a restart and
        # remember-me stays disabled (a public constant would let anyone
        # forge cookies).
        _EPHEMERAL_SECRET = True
        log.error("Could not persist session_secret: %s. Using an ephemeral "
                  "random key; sessions will not survive a restart and "
                  "\"Remember me\" is disabled.", e)
        app.secret_key = secrets.token_hex(32)


_init_session_secret()

# ── Session / remember-me configuration ───────────────────────────
app.config["SESSION_COOKIE_NAME"] = "safescanarr_session"
app.config["SESSION_REFRESH_EACH_REQUEST"] = False
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SS_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
)


def _remember_days() -> int:
    """Read remember_me_days from config once at import; clamp 0..365.

    Consistent with _init_session_secret: any config read failure (read-only
    filesystem, malformed JSON, ...) yields 0 (disabled) rather than crashing
    startup, so the ephemeral-secret fallback path actually completes. An
    invalid *value* for the key itself still falls back to 30 (spec B1).
    """
    try:
        cfg = config_module.raw()
    except Exception as e:
        log.warning("Could not read config for remember_me_days (%s); "
                    "remember-me disabled for this run.", e)
        return 0
    try:
        days = int(cfg.get("remember_me_days", 30))
    except (TypeError, ValueError):
        log.warning("Invalid remember_me_days in config.json; using 30.")
        days = 30
    return max(0, min(365, days))


REMEMBER_DAYS = _remember_days()
# 31d lifetime even when remember-me is disabled (0), so PERMANENT_SESSION_LIFETIME
# stays a sane value; _remember_enabled() gates the feature itself.
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(
    days=REMEMBER_DAYS or 31)


def _remember_enabled() -> bool:
    return REMEMBER_DAYS > 0 and not _EPHEMERAL_SECRET

PUBLIC_PATHS = {"/login", "/setup", "/health"}

# ── Login throttling (per-IP in-memory) ───────────────────────────
_throttle_lock = threading.Lock()
_throttle: dict = {}  # ip -> {"fails": int, "until": float}


def _throttle_check(ip: str) -> bool:
    """Return True if the request is allowed, False if locked out."""
    now = time.time()
    with _throttle_lock:
        rec = _throttle.get(ip)
        if not rec:
            return True
        if now < rec["until"]:
            return False
        # Lockout expired; reset counters so future failures start fresh
        if rec["until"] > 0:
            rec["fails"] = 0
            rec["until"] = 0
        return True


def _throttle_record(ip: str, *, success: bool) -> None:
    with _throttle_lock:
        rec = _throttle.get(ip)
        if not rec:
            rec = {"fails": 0, "until": 0}
            _throttle[ip] = rec
        if success:
            rec["fails"] = 0
            rec["until"] = 0
            return
        rec["fails"] += 1
        if rec["fails"] >= 10:
            # Exponential backoff starting at 60s, capped at 3600s
            lockout = min(60 * (2 ** (rec["fails"] - 10)), 3600)
            rec["until"] = time.time() + lockout
            log.warning("Login throttled for %s after %d failures (lockout %ds)", ip, rec["fails"], lockout)


def _throttle_remaining(ip: str) -> float:
    with _throttle_lock:
        rec = _throttle.get(ip)
        if not rec:
            return 0.0
        return max(0.0, rec["until"] - time.time())


# ── Per-IP pre-verify rate limit (token bucket, ~1 req/s average) ──
class _TokenBucket:
    def __init__(self, capacity: float, rate: float):
        self.capacity = capacity
        self.rate = rate
        self.tokens = float(capacity)
        self.last = time.time()

    def allow(self) -> bool:
        now = time.time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


_rate_limit_lock = threading.Lock()
_rate_limit: dict = {}  # ip -> _TokenBucket


def _rate_limit_check(ip: str) -> bool:
    """Return True if this IP has not exceeded the per-second request budget."""
    with _rate_limit_lock:
        bucket = _rate_limit.get(ip)
        if bucket is None:
            bucket = _TokenBucket(capacity=3, rate=1.0)
            _rate_limit[ip] = bucket
        return bucket.allow()


# ── Database (initialised once per process, not per request) ──────
# _db_lock guards: (a) initial creation of the shared Database, (b) restore
# publication — the final live-row recheck + WAL flush + os.replace +
# shared-connection handoff. Every web_sessions row deletion (logout,
# revoke, re-login old-row removal, credential-transition revocation,
# expiration cleanup) and the restore seed/purge also run under this lock,
# so there is no unlocked gap between "the caller's row is still live" and
# the file actually being replaced. Leases are tracked per Database
# generation: a retired generation's connection is closed only when THAT
# generation's last lease exits.
# ponytail: process-wide coordination only; multi-web-process deployments
# would need the same discipline per process (see README trust boundary).
_db = None
_db_lock = threading.RLock()
_db_refs: dict = {}     # id(Database) -> outstanding leases on that generation
_db_cond = threading.Condition(_db_lock)
# True while a restore owns publication — from the start of its writer
# drain until os.replace completes or the attempt aborts. This is the
# window in which get_db() must block (admission gate). Guarded by
# _db_lock; genuinely MODULE state, never a restore-local variable.
_publishing = False
# id()s of every generation retired for publication whose leases have not
# all exited yet (the drain set). It deliberately SURVIVES a timed-out
# attempt: the next publication still cannot bypass a still-active old
# writer on the live file. Cleared only after a successful replace (or a
# test reset). Guarded by _db_lock.
_drained_keys: set = set()
# Serializes publication OWNERSHIP: only one restore at a time may run the
# drain → checkpoint → replace sequence, so a second restore can neither
# overwrite the first's drain targets nor interleave a second replace.
# The owner NEVER holds _db_lock across its waits, so lease holders (and
# logout/revocation row deletions) can always finish — no deadlock.
_publish_lock = threading.RLock()
# Bounded wait for leased writers to finish before publication. A stuck
# writer aborts the restore (preserving the live DB) instead of hanging.
_PUBLISH_DRAIN_TIMEOUT_SECONDS = 30.0


class _Lease:
    """One get_db() reference on a specific Database generation.

    release() closes a RETIRED generation only when that generation's own
    last lease exits — never on the first release, and never against an
    unrelated generation's counter.
    """

    __slots__ = ("db",)

    def __init__(self, db: Database):
        self.db = db

    def release(self) -> None:
        db = self.db
        self.db = None
        if db is None:
            return
        with _db_lock:
            key = id(db)
            n = _db_refs.get(key, 0) - 1
            if n > 0:
                _db_refs[key] = n
            else:
                _db_refs.pop(key, None)
                _drained_keys.discard(key)
                if db is not _db:
                    # This generation's last lease, and it is no longer the
                    # shared Database (restore published / reset): close it.
                    try:
                        db.close()
                    except Exception as e:  # pragma: no cover - best effort
                        log.warning("Error closing retired database: %s", e)
            _db_cond.notify_all()

    def __enter__(self) -> Database:
        return self.db

    def __exit__(self, *exc):
        self.release()
        return False


def get_db() -> Database:
    """Return the process-wide Database, initialising/migrating on first use.

    The returned Database is lease-counted per generation: the caller must
    eventually call release_db() (or use the db_lease() context manager) so
    a concurrent restore's publication cannot close a connection this
    request still uses.
    """
    global _db
    with _db_lock:
        while _publishing:
            # Serialize handle acquisition with restore publication: while a
            # restore owns publication (draining leased writers before
            # os.replace), no new handle may be issued — it could open the
            # soon-to-be-replaced inode and slip a write into the drain
            # window. Waiting here is deadlock-free: the publication owner
            # waits for lease releases via _db_cond (releasing _db_lock
            # while blocked), and it drops _publishing before its final
            # notify — so this wait always resumes.
            _db_cond.wait()
        if _db is None:
            _db = Database(Config().DB_FILE)
        db = _db
        _db_refs[id(db)] = _db_refs.get(id(db), 0) + 1
        return db


def db_lease() -> _Lease:
    """Context manager balancing get_db()/release_db() for request handlers."""
    return _Lease(get_db())


def init_db() -> Database:
    """Explicitly initialise the database (called at startup)."""
    with db_lease() as db:
        _cleanup_orphan_restore_stages()
        return db


def release_db(db: Database) -> None:
    """Drop a get_db() reference; a retired generation's LAST lease closes it."""
    _Lease(db).release()


def _drop_shared_db_locked(close: bool) -> None:
    """Retire the shared Database. Caller holds _db_lock.

    With ``close=True`` (test reset) it waits for THAT generation's in-flight
    users and closes the connection. With ``close=False`` (restore
    publication) it retires the handle without closing it — the last
    lease-holder's release_db() closes it — so replacement can never yank a
    connection another request still holds. The reference counter belongs to
    the retired generation only; it is never carried onto the next one.
    """
    global _db
    db = _db
    _db = None
    if db is None:
        return
    key = id(db)
    _drained_keys.add(key)
    if close:
        # Bounded wait: leaked references (e.g. tests grabbing get_db()
        # without release) must not hang — force-close and forget the
        # counter after a short grace period. _reset_db is test-only;
        # production publication uses close=False and never force-closes.
        deadline = time.monotonic() + 2
        while _db_refs.get(key, 0) > 0 and time.monotonic() < deadline:
            _db_cond.wait(timeout=0.1)
        if _db_refs.get(key, 0) > 0:
            _db_refs.pop(key, None)
    if _db_refs.get(key, 0) == 0:
        _db_refs.pop(key, None)
        _drained_keys.discard(key)
        try:
            db.close()
        except Exception as e:  # pragma: no cover - best effort
            log.warning("Error closing database: %s", e)


def _reset_db() -> None:
    """Drop the shared connection so the next request re-opens the DB file."""
    with _publish_lock, _db_lock:
        _drop_shared_db_locked(close=True)


def _retire_rows(fn):
    """Run a remembered-row deletion under the same lock as restore
    publication, acquiring the Database handle INSIDE the lock so the
    deletion always targets the currently published generation (logout,
    revoke, re-login old-row removal, credential transitions, expiration
    cleanup). Returns fn(db)'s result."""
    with _db_lock:
        db = get_db()
        try:
            return fn(db)
        finally:
            release_db(db)


def _revoke_remembered_rows() -> None:
    """Revocation hook: drop every remembered row (epoch bump already
    invalidates the cookies; this also clears the rows themselves)."""
    _retire_rows(lambda db: db.delete_web_sessions_except(None))


auth.register_revocation_hook(_revoke_remembered_rows)


# ── Auth / CSRF ───────────────────────────────────────────────────
def _supplied_token() -> str:
    tok = request.headers.get("X-Auth-Token", "")
    if tok:
        return tok.strip()
    authz = request.headers.get("Authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return ""


def _origin_ok() -> bool:
    """Reject cross-origin state-changing requests (CSRF defence in depth)."""
    host = (request.host or "").split(":")[0].lower()
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if not value:
            continue
        try:
            src_host = (urllib.parse.urlsplit(value).hostname or "").lower()
        except ValueError:
            return False
        if src_host and src_host != host:
            return False
    return True


def _ensure_csrf() -> str:
    if not session.get("csrf"):
        session["csrf"] = os.urandom(24).hex()
    return session["csrf"]


def _csrf_ok() -> bool:
    # A valid token header in token mode also proves intent (custom headers
    # require a CORS preflight). In password mode the session CSRF path is
    # the only proof.
    if auth.method() == "token" and auth.verify(_supplied_token()) is not None:
        return True
    expected = session.get("csrf")
    if not expected:
        return False
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied and request.form:
        supplied = request.form.get("csrf_token", "")
    return bool(supplied) and supplied == expected


def _deny():
    if request.path.startswith("/api/"):
        return jsonify({"status": "error", "message": "Authentication required"}), 401
    return redirect("/login")


def _rid_hash(rid: str) -> str:
    return hashlib.sha256(rid.encode("utf-8")).hexdigest()


class CredentialsChangedError(Exception):
    """Raised when auth credentials changed between verify and establish."""


def _establish_session(remember: bool, generation_value=None) -> None:
    """Create a fresh authenticated session (clears any previous one).

    ``generation_value`` pins the auth generation the login was verified
    against: if auth changed in between (epoch moved on), the fingerprint is
    computed for the *new* generation only when it still matches — otherwise
    the session is stamped with a stale-credential fingerprint and dies on
    its next request. Callers that verified credentials pass the returned
    generation; None means "use whatever is current" (token-header path).
    """
    old_rid = session.get("rid")
    if old_rid:
        try:
            _retire_rows(lambda db: db.delete_web_session(_rid_hash(old_rid)))
        except Exception as e:
            log.warning("Could not delete old remembered session: %s", e)
    if generation_value is not None:
        current = auth.generation()
        if current is None or current != generation_value:
            # Credential transition happened mid-request: do not mint a
            # session for a credential that is no longer current.
            raise CredentialsChangedError()
    session.clear()
    session["authed"] = True
    session["csrf"] = os.urandom(24).hex()
    session["av"] = auth.session_fingerprint(app.secret_key, generation_value)
    if remember and _remember_enabled():
        try:
            rid = secrets.token_urlsafe(32)
            exp = int(time.time()) + REMEMBER_DAYS * 86400

            def _seed(db):
                # Expiration purge + seeding under the publication lock,
                # like every other web_sessions row operation.
                db.purge_expired_web_sessions(int(time.time()))
                db.create_web_session(_rid_hash(rid), exp,
                                      ip=request.remote_addr,
                                      user_agent=request.headers.get("User-Agent", ""))
            with _db_lock:
                db = get_db()
                try:
                    _seed(db)
                finally:
                    release_db(db)
            session["rid"] = rid
            session["rexp"] = exp
            session.permanent = True
        except Exception as e:
            # Fail safe: continue as a normal browser session.
            log.warning("Remember-me persistence failed (%s); "
                        "continuing as a normal session.", e)


def _session_valid() -> bool:
    """Validate the current session: authed flag, av fingerprint, rid row."""
    if not session.get("authed"):
        return False
    fp = auth.session_fingerprint(app.secret_key)
    if not fp or not hmac.compare_digest(str(session.get("av", "")), fp):
        return False
    rid = session.get("rid")
    if rid:
        if not _remember_enabled():
            return False
        try:
            db = get_db()
            try:
                row = db.get_web_session(_rid_hash(rid))
            finally:
                release_db(db)
            if row is None:
                return False
            now = int(time.time())
            if row["expires_at"] <= now:
                # Expiration cleanup participates in the same
                # coordination as restore publication / logout.
                _retire_rows(lambda d: d.delete_web_session(_rid_hash(rid)))
                return False
            if now - row["last_used_at"] > 3600:
                with _db_lock:
                    db = get_db()
                    try:
                        db.touch_web_session(_rid_hash(rid), now)
                    finally:
                        release_db(db)
        except Exception:
            return False  # fail closed on DB errors
    return True


def _revoke_current_session() -> bool:
    """Sign out this device: delete its remembered row, then clear cookie.

    Returns True when signed out. Returns False with the session *preserved*
    (cookie, CSRF and rid intact) when the remembered row could not be
    deleted — the cookie would otherwise stay replayable server-side while
    the browser believes it signed out; keeping client state lets the user
    retry the logout until revocation really happens.
    """
    rid = session.get("rid")
    if rid:
        # Deletion participates in the same coordination as restore's
        # final recheck + publication (both run under _db_lock), so a
        # logout can never land in the gap between the restore's
        # "caller still live" check and the os.replace that publishes.
        # The handle is captured INSIDE the lock: if a restore published
        # while we waited, this is the new shared Database, not the old
        # inode the delete would silently miss.
        try:
            _retire_rows(lambda db: db.delete_web_session(_rid_hash(rid)))
        except Exception as e:
            log.warning("Could not delete remembered session row: %s", e)
            return False
    session.clear()
    return True


@app.before_request
def _require_auth():
    path = request.path
    if path.startswith("/static/") or path in PUBLIC_PATHS:
        return None

    if not auth.is_configured():
        if path.startswith("/api/"):
            return jsonify({"status": "error", "message": "Setup required"}), 503
        if path != "/setup":
            return redirect("/setup")
        # /setup itself falls through (but it is in PUBLIC_PATHS, so we never get here)
        return None

    if not _session_valid():
        # Clear any stale cookie data before denying/re-authenticating.
        if session:
            session.clear()
        supplied = _supplied_token()
        gen = auth.verify(supplied) if supplied else None
        if gen is not None and auth.method() == "token":
            try:
                _establish_session(remember=False, generation_value=gen)
            except CredentialsChangedError:
                return _deny()
        else:
            return _deny()

    _ensure_csrf()

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _origin_ok():
            log.warning("Rejected cross-origin %s %s", request.method, path)
            return jsonify({"status": "error", "message": "Cross-origin request rejected"}), 403
        if not _csrf_ok():
            return jsonify({"status": "error", "message": "Invalid CSRF token"}), 403
    return None


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"status": "error", "message": "Upload too large"}), 413


@app.errorhandler(config_lock.LockValidationError)
def _lock_invalid(e):
    """Expected lock-validation failure: actionable JSON, no traceback."""
    log.error("Config lock validation failed: %s", e)
    return jsonify({"status": "error",
                    "message": f"Configuration change failed: the config lock "
                               f"file is unusable ({e}). Fix the ownership/"
                               f"mode of config.json.lock in BASE_DIR and "
                               f"retry."}), 500


@app.errorhandler(OSError)
def _fs_error(e):
    """Filesystem failure (read-only BASE_DIR, permissions, disk): JSON 500."""
    log.error("Filesystem error during request: %s", e)
    return jsonify({"status": "error",
                    "message": f"Filesystem error: {e}"}), 500


@app.errorhandler(Exception)
def _internal_error(e):
    """Any unhandled error inside a route returns JSON, never an HTML
    traceback page (the UI calls these endpoints with fetch/XHR)."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e  # keep normal 404/405/etc. semantics
    log.exception("Internal server error: %s", e)
    return jsonify({"status": "error", "message": "Internal server error"}), 500


# ── Setup (first-run) ──────────────────────────────────────────────
@app.route("/setup", methods=["GET", "POST"])
def setup_route():
    if auth.is_configured():
        if request.method == "GET":
            return redirect("/login")
        return jsonify({"status": "error", "message": "Already configured"}), 409

    if request.method == "GET":
        return render_template("setup.html", version=Config.version())

    # POST
    if not _origin_ok():
        return jsonify({"status": "error", "message": "Cross-origin request rejected"}), 403

    ip = request.remote_addr or "unknown"
    if not _throttle_check(ip):
        return jsonify({"status": "error", "message": "Too many attempts; try again later."}), 429
    if not _rate_limit_check(ip):
        return jsonify({"status": "error", "message": "Too many requests; slow down."}), 429

    data = request.get_json(silent=True) or {}
    method = (data.get("method") or "").strip().lower()

    # Atomically check-then-configure under auth._LOCK so only the first
    # concurrent /setup request can win; the rest get 409.
    try:
        if method == "password":
            password = (data.get("password") or "").strip()
            confirm = (data.get("password_confirm") or "").strip()
            auth.configure_once(password=password, confirm=confirm)
            generated = None
        elif method == "token":
            client_token = (data.get("token") or "").strip() or None
            generated = auth.configure_once(existing_token=client_token)
        else:
            return jsonify({"status": "error", "message": "Invalid method"}), 400
    except auth.AlreadyConfiguredError:
        return jsonify({"status": "error", "message": "Already configured"}), 409
    except ValueError as e:
        _throttle_record(ip, success=False)
        return jsonify({"status": "error", "message": str(e)}), 400

    _establish_session(remember=False)
    log.info("First-run setup completed via %s method from %s", method, ip)
    return jsonify({"status": "ok", "token": generated if method == "token" else None})


# ── Login ─────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if _session_valid():
            return redirect("/")
        if session:
            session.clear()  # stale session: show the login page
        return render_template("login.html", version=Config.version(),
                               auth_method=auth.method(),
                               remember_days=REMEMBER_DAYS if _remember_enabled() else 0)

    if not _origin_ok():
        return jsonify({"status": "error", "message": "Cross-origin request rejected"}), 403

    ip = request.remote_addr or "unknown"
    if not _throttle_check(ip):
        return jsonify({"status": "error", "message": "Too many attempts; try again later."}), 429
    if not _rate_limit_check(ip):
        return jsonify({"status": "error", "message": "Too many requests; slow down."}), 429

    body = request.get_json(silent=True) or request.form or {}
    supplied = str(body.get("secret", "") or body.get("token", "") or body.get("password", "")).strip()

    generation = auth.verify(supplied)
    if generation is None:
        _throttle_record(ip, success=False)
        log.warning("Failed login attempt from %s", ip)
        return jsonify({"status": "error", "message": "Invalid credentials"}), 401

    _throttle_record(ip, success=True)
    remember = _coerce_bool(body.get("remember", False))
    try:
        _establish_session(remember, generation)
    except CredentialsChangedError:
        # Credentials were rotated while this login was in flight; the
        # supplied secret is no longer current. Fail closed.
        log.warning("Login rejected: credentials changed during login")
        return jsonify({"status": "error", "message": "Credentials changed; sign in again"}), 401
    log.info("Login OK from %s (remember=%s)", ip, remember)
    return jsonify({"status": "ok", "csrf_token": session["csrf"],
                    "remembered": bool(session.get("rid"))})


@app.route("/logout", methods=["POST"])
def logout():
    if not _revoke_current_session():
        # The remembered row could not be deleted; the cookie would still
        # replay. Report failure so the user knows they are NOT signed out.
        return jsonify({"status": "error",
                        "message": "Sign-out failed: could not revoke the "
                                   "remembered session server-side. Try again."}), 500
    return jsonify({"status": "ok"})


@app.route("/api/auth/revoke-sessions", methods=["POST"])
def api_revoke_sessions():
    """Sign out every other device; the caller stays signed in."""
    generation = auth.revoke_all_sessions()
    keep = _rid_hash(session["rid"]) if session.get("rid") else None
    # Same coordination as restore publication + logout: the deletion runs
    # under _db_lock so it cannot interleave with a restore's final recheck;
    # the handle is captured inside the lock so it targets the published
    # database even if a restore completed while we waited.
    try:
        n = _retire_rows(lambda db: db.delete_web_sessions_except(keep))
    except Exception as e:
        log.warning("Could not delete remembered session rows: %s", e)
        n = 0
    # Epoch bump changed the fingerprint; refresh this session's av pinned to
    # the generation this request created — never a later one, so a CLI/global
    # revoke that lands mid-request cannot be laundered by our own write.
    new_av = auth.session_fingerprint(app.secret_key, generation)
    if auth.generation() == generation and new_av:
        session["av"] = new_av
    else:
        # A later revoke superseded ours: this session must not survive.
        session.clear()
    return jsonify({"status": "ok", "revoked_remembered": n})


@app.route("/health")
def health():
    """Unauthenticated liveness probe — deliberately reveals nothing else."""
    return jsonify({"status": "ok"})


# ── UI ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", version=Config.version(),
                           csrf_token=session.get("csrf", ""))


@app.route("/api/version")
def api_version():
    return jsonify({
        "version": Config.version(),
        "delete_on_reject": Config().DELETE_ON_REJECT,
        "auth_method": auth.method(),
        "remember_days": REMEMBER_DAYS,
    })


# ── Stats ─────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    with db_lease() as db:
        return jsonify(db.get_stats())


# ── Lifetime stats ───────────────────────────────────────────────
@app.route("/api/stats/full")
def api_stats_full():
    with db_lease() as db:
        return jsonify(db.get_stats_full())


# ── Sheets by state ───────────────────────────────────────────────
@app.route("/api/sheets")
def api_sheets():
    cfg        = Config()
    state      = request.args.get("state", "pending")
    search     = request.args.get("search", "").lower()
    source     = request.args.get("source", "")  # "auto", "user", or "" for all
    output_dir = Path(cfg.OUTPUT_DIR)
    sheets     = []

    with db_lease() as db:
        rows = db.list_files(review_state=state)
    for row in rows:
        stem = Path(row["path"]).stem
        if search and search not in stem.lower() and search not in row["path"].lower():
            continue
        row_source = row["state_source"] if "state_source" in row.keys() else None
        if source and row_source != source:
            continue
        sheet_file = resolve_sheet(output_dir, row["path"])
        sheets.append({
            "stem":            stem,
            "filename":        sheet_file.name,
            "has_sheet":       sheet_file.exists(),
            "source_path":     row["path"],
            "review_state":    row["review_state"],
            "flagged":         bool(row["flagged"]),
            "flag_reason":     row["flag_reason"],
            "nsfw_confidence": row["nsfw_confidence"],
            "quarantine_path": row["quarantine_path"],
            "state_updated_at": row["state_updated_at"],
            "updated_at":      row["updated_at"],
            "size":            row["size"],
            "state_source":    row["state_source"] if "state_source" in row.keys() else None,
            "source_retained": row["source_retained"] if "source_retained" in row.keys() else False,
        })

    return jsonify(sheets)


@app.route("/api/sheets/image/<path:filename>")
def api_sheet_image(filename):
    return send_from_directory(Config().OUTPUT_DIR, filename)


# ── Sheet actions ─────────────────────────────────────────────────
@app.route("/api/sheets/approve", methods=["POST"])
def api_approve():
    """Approve one or more sheets."""
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem:
            stems = [stem]

    done = []
    failed = []
    with db_lease() as db:
        for stem in stems:
            row = db.find_file_by_stem(stem)
            if not row:
                failed.append((stem, "not found"))
                continue
            # If currently quarantined, move video back
            if row["review_state"] == "quarantined" and row["quarantine_path"]:
                q_path = Path(row["quarantine_path"])
                orig   = Path(row["path"])
                source_retained = ("source_retained" in row.keys() and bool(row["source_retained"]))
                if source_retained and q_path.exists():
                    # Source was never removed; just delete the quarantine copy.
                    try:
                        if q_path.stat().st_size != orig.stat().st_size:
                            failed.append((stem, "quarantine copy size mismatch"))
                            continue
                        q_path.unlink()
                        log.info("Approved source-retained item; removed quarantine copy: %s", q_path)
                    except OSError as e:
                        failed.append((stem, f"could not remove quarantine copy: {e.strerror}"))
                        continue
                    db.set_review_state(row["path"], "approved", quarantine_path=None,
                                        source_retained=False)
                elif q_path.exists():
                    try:
                        relocate(q_path, orig, verify=True)
                        log.info("Restored from quarantine: %s", orig)
                    except OSError as e:
                        errno_name = __import__("errno").errorcode.get(e.errno, "EUNKNOWN")
                        failed.append((stem, f"Could not restore {stem}: {errno_name}"))
                        continue
                db.set_review_state(row["path"], "approved")
            else:
                db.set_review_state(row["path"], "approved")
            done.append(stem)
    if failed:
        return jsonify({"status": "error", "approved": done,
                        "failed": [f for f in failed]}), 409
    return jsonify({"status": "ok", "approved": done})


@app.route("/api/sheets/reject", methods=["POST"])
def api_reject():
    """Reject one or more sheets.

    Safe by default: the video is *moved to quarantine*. Permanent deletion
    (plus the arr blocklist/re-search flow) only happens when the config flag
    ``delete_on_reject`` is explicitly enabled.
    """
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem:
            stems = [stem]

    done = []
    failed = []
    all_reports = []
    warnings = []
    with db_lease() as db:
        cfg = Config()
        for stem in stems:
            row = db.find_file_by_stem(stem)
            if not row:
                failed.append((stem, "not found"))
                continue

            source_path = row["path"]
            video_path  = Path(row["quarantine_path"] or row["path"])

            try:
                if cfg.DELETE_ON_REJECT:
                    deleted = False
                    if video_path.exists():
                        try:
                            video_path.unlink()
                            deleted = True
                            log.warning("Rejected (deleted): %s", video_path)
                        except OSError as e:
                            from fileops import RETAIN_ERRNOS
                            if e.errno not in RETAIN_ERRNOS:
                                raise
                            log.warning("Reject delete failed for %s, copying to quarantine: %s",
                                        source_path, e)
                    if deleted or not video_path.exists():
                        reports = []
                        if cfg.ARR_BLOCKLIST_ON_REJECT or cfg.ARR_SEARCH_AFTER_REJECT:
                            reports = arr.reject_in_arr(source_path, cfg, delete_file_in_arr=True)
                        if reports:
                            db.set_arr_result(source_path, reports)
                            all_reports.extend(reports)
                            for r in reports:
                                if r.get("blocklisted") is False:
                                    service = r.get("service", "arr").capitalize()
                                    warnings.append(f"{service} blocklist failed: {r.get('reason') or 'unknown'}")
                        db.set_review_state(source_path, "rejected")
                        done.append(stem)
                        continue
                    # Could not delete source: fall through to a quarantine copy.
                    q_dir = Path(cfg.QUARANTINE_DIR)
                    q_path, outcome = relocate(video_path, q_dir / video_path.name, verify=True)
                    source_retained = outcome == COPIED_SOURCE_KEPT
                    log.warning("Rejected (quarantined, source retained): %s -> %s",
                                source_path, q_path)
                    db.set_review_state(source_path, "quarantined",
                                        quarantine_path=str(q_path),
                                        source_retained=source_retained)
                else:
                    if row["review_state"] == "quarantined" and row["quarantine_path"]:
                        q_path = Path(row["quarantine_path"])   # already quarantined
                        db.set_review_state(source_path, "quarantined",
                                            quarantine_path=str(q_path))
                    elif video_path.exists():
                        q_dir = Path(cfg.QUARANTINE_DIR)
                        q_path, outcome = relocate(video_path, q_dir / video_path.name, verify=True)
                        source_retained = outcome == COPIED_SOURCE_KEPT
                        if source_retained:
                            log.warning("Rejected (quarantined, source retained): %s -> %s",
                                        source_path, q_path)
                        else:
                            log.warning("Rejected (quarantined, delete_on_reject=false): %s -> %s",
                                        source_path, q_path)
                        db.set_review_state(source_path, "quarantined",
                                            quarantine_path=str(q_path),
                                            source_retained=source_retained)
                    else:
                        q_path = Path(row["quarantine_path"]) if row["quarantine_path"] else None
                        db.set_review_state(source_path, "quarantined",
                                            quarantine_path=str(q_path) if q_path else None)
            except OSError as e:
                errno_name = __import__("errno").errorcode.get(e.errno, "EUNKNOWN")
                failed.append((stem, f"Could not process {stem}: {errno_name}"))
                continue

            done.append(stem)
    response = {
        "status": "ok",
        "rejected": done,
        "mode": "delete" if Config().DELETE_ON_REJECT else "quarantine",
    }
    if all_reports:
        response["arr"] = all_reports
    if warnings:
        response["warnings"] = warnings
    if failed:
        return jsonify({"status": "error", "rejected": done,
                        "failed": [f for f in failed],
                        "arr": all_reports,
                        "warnings": warnings}), 409
    return jsonify(response)


@app.route("/api/sheets/quarantine", methods=["POST"])
def api_quarantine():
    """Manually quarantine a pending item."""
    stem = (request.get_json() or {}).get("stem", "")
    cfg  = Config()
    # ONE lease spans the whole read-relocate-write sequence: a concurrent
    # restore publication drains leased writers, so it can neither retire
    # this connection between the file move and the set_review_state()
    # that records where the file went, nor publish between the read and
    # the move.
    with db_lease() as db:
        row = db.find_file_by_stem(stem)
        if not row:
            return jsonify({"status": "error", "message": "Not found"}), 404

        video_path = Path(row["path"])
        if not video_path.exists():
            return jsonify({"status": "error", "message": "Source file not found"}), 404

        try:
            q_dir  = Path(cfg.QUARANTINE_DIR)
            q_path, outcome = relocate(video_path, q_dir / video_path.name, verify=True)
            source_retained = outcome == COPIED_SOURCE_KEPT
            db.set_review_state(row["path"], "quarantined", quarantine_path=str(q_path),
                                source_retained=source_retained)
            return jsonify({"status": "ok", "quarantine_path": str(q_path)})
        except OSError as e:
            errno_name = __import__("errno").errorcode.get(e.errno, "EUNKNOWN")
            log.warning("Manual quarantine failed for %s: %s", stem, e)
            return jsonify({"status": "error", "message": f"Could not quarantine {stem}: {errno_name}"}), 409


@app.route("/api/sheets/requeue", methods=["POST"])
def api_requeue():
    """Re-process a file — regenerate sheet and re-run analysis."""
    stem = (request.get_json() or {}).get("stem", "")
    cfg  = Config()
    with db_lease() as db:
        row = db.find_file_by_stem(stem)

    path = row["path"] if row else None
    if not path or not Path(path).exists():
        return jsonify({"status": "error", "message": "Source file not found"}), 404

    with db_lease() as db:
        db.delete_file(path)
    subprocess.Popen(
        [sys.executable, SCANNER, "--file", path, "--source", "requeue"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"status": "ok"})


# ── Config ────────────────────────────────────────────────────────
_CONFIG_KEYS = {
    "watch_folders", "sonarr_url", "sonarr_api_key", "radarr_url", "radarr_api_key",
    "polling_enabled", "poll_interval_seconds", "scan_schedule_enabled", "scan_schedule",
    "detection_profile", "zone_auto_approve", "zone_quarantine", "zone_auto_reject",
    "nudenet_frames", "nudenet_threshold", "quarantine_dir",
    "quarantine_auto_reject_days", "delete_on_reject",
    "arr_blocklist_on_reject", "arr_search_after_reject",
    "webhook_url", "webhook_on_review", "webhook_on_quarantine", "webhook_on_reject",
    "web_ui_url", "vcs_grid", "vcsi_timeout_seconds",
}
_CONFIG_BOOLS = {
    "polling_enabled", "scan_schedule_enabled", "delete_on_reject",
    "arr_blocklist_on_reject", "arr_search_after_reject",
    "webhook_on_review", "webhook_on_quarantine", "webhook_on_reject",
}
_CONFIG_INTS = {
    "poll_interval_seconds", "nudenet_frames", "quarantine_auto_reject_days",
    "vcsi_timeout_seconds",
}
_CONFIG_FLOATS = {
    "zone_auto_approve", "zone_quarantine", "zone_auto_reject", "nudenet_threshold",
}
_CONFIG_STRS = {
    "sonarr_url", "sonarr_api_key", "radarr_url", "radarr_api_key", "scan_schedule",
    "detection_profile", "quarantine_dir", "webhook_url", "web_ui_url", "vcs_grid",
}
_SECRET_KEYS = {"sonarr_api_key", "radarr_api_key"}


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _validate_watch_folders(value, errors: list) -> list:
    if isinstance(value, str):
        value = [p.strip() for p in value.split(",") if p.strip()]
    if not isinstance(value, list):
        errors.append("watch_folders must be a list of paths")
        return []
    folders = []
    for item in value:
        if item in (None, ""):
            continue
        if not isinstance(item, str):
            errors.append("watch_folders entries must be strings")
            continue
        p = Path(item.strip())
        if not p.is_absolute():
            errors.append(f"watch folder must be an absolute path: {item}")
            continue
        if not p.is_dir():
            errors.append(f"watch folder does not exist: {item}")
            continue
        folders.append(str(p))
    return folders


def _build_config_update(data: dict):
    """Whitelist + type-check + path-validate an incoming config payload."""
    clean: dict = {}
    errors: list = []
    for key, value in data.items():
        if key not in _CONFIG_KEYS:
            continue                       # ignore unknown / derived / non-writable keys
        if key in _CONFIG_BOOLS:
            clean[key] = _coerce_bool(value)
        elif key in _CONFIG_INTS:
            try:
                clean[key] = int(value)
            except (TypeError, ValueError):
                errors.append(f"{key} must be an integer")
        elif key in _CONFIG_FLOATS:
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
        elif key == "watch_folders":
            clean[key] = _validate_watch_folders(value, errors)
        elif key in _CONFIG_STRS:
            if not isinstance(value, str):
                errors.append(f"{key} must be a string")
                continue
            val = value.strip()
            if key in _SECRET_KEYS and val == "":
                continue                   # blank secret = leave unchanged (UI masks it)
            clean[key] = val

    # Path validation for quarantine_dir
    qdir = clean.get("quarantine_dir")
    if qdir:
        p = Path(qdir)
        if not p.is_absolute():
            errors.append("quarantine_dir must be an absolute path")
        elif p.exists() and not p.is_dir():
            errors.append("quarantine_dir exists and is not a directory")
    return clean, errors


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return config with secrets masked (arr API keys reported as set/not-set)."""
    cfg = config_module.get()
    # Never expose auth secrets or the session secret
    cfg.pop("auth_token", None)
    cfg.pop("auth_password_hash", None)
    cfg.pop("session_secret", None)
    cfg.pop("session_epoch", None)
    # webhook_url is reported as set/not-set only
    cfg["webhook_url_set"] = bool(cfg.pop("webhook_url", ""))
    cfg["sonarr_api_key_set"] = bool(cfg.pop("sonarr_api_key", ""))
    cfg["radarr_api_key_set"] = bool(cfg.pop("radarr_api_key", ""))
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def api_config_save():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"status": "error", "message": "No JSON body"}), 400
    # Security-critical: never accept auth/session secrets through this API.
    for forbidden in ("auth_token", "auth_password_hash", "session_secret",
                      "session_epoch"):
        data.pop(forbidden, None)
    try:
        clean, errors = _build_config_update(data)
        if errors:
            return jsonify({"status": "error", "message": "; ".join(errors)}), 400

        existing = config_lock.mutate(lambda cfg: cfg.update(clean) or cfg)
        return jsonify({"status": "ok"})
    except (config_lock.LockValidationError, OSError):
        # Expected operator-actionable failures: let the dedicated error
        # handlers return the explicit diagnostic (no log traceback spam).
        raise
    except Exception as e:
        log.exception("Failed to save config")
        return jsonify({"status": "error", "message": "Failed to save configuration"}), 500


@app.route("/api/config/test-sonarr", methods=["POST"])
def api_test_sonarr():
    cfg  = Config()
    data = _api_get(f"{cfg.SONARR_URL.rstrip('/')}/api/v3/system/status", cfg.SONARR_API_KEY)
    if data:
        return jsonify({"status": "ok", "version": data.get("version", "unknown")})
    return jsonify({"status": "error", "message": "Could not connect"}), 502


@app.route("/api/config/test-radarr", methods=["POST"])
def api_test_radarr():
    cfg  = Config()
    data = _api_get(f"{cfg.RADARR_URL.rstrip('/')}/api/v3/system/status", cfg.RADARR_API_KEY)
    if data:
        return jsonify({"status": "ok", "version": data.get("version", "unknown")})
    return jsonify({"status": "error", "message": "Could not connect"}), 502


@app.route("/api/config/test-webhook", methods=["POST"])
def api_test_webhook():
    cfg = Config()
    if not cfg.WEBHOOK_URL:
        return jsonify({"status": "error", "message": "No webhook URL set"}), 400

    message = "Safe Scanarr test notification"
    event_label = "Test notification"
    url = ""
    if cfg.WEB_UI_URL:
        url = f"{cfg.WEB_UI_URL.rstrip('/')}/?tab=quarantined"

    body = {
        "event":     "test",
        "path":      "/mnt/media/Movies/Example (2024)/Example (2024).mp4",
        "title":     "Example (2024)",
        "risk":      0.0,
        "labels":    [],
        "message":   message,
        "timestamp": datetime.datetime.now().isoformat(),
    }
    if url:
        body["url"] = url

    host = webhook.url_host(cfg.WEBHOOK_URL).lower()
    payload = webhook.build_payload(host, message, event_label, url, body)
    ok, detail = webhook.send(cfg.WEBHOOK_URL, payload, Config.version())
    if ok:
        return jsonify({"status": "ok"})

    log.warning("Test webhook failed (host=%s): %s", _url_host(cfg.WEBHOOK_URL), detail)
    return jsonify({"status": "error", "message": detail}), 502


@app.route("/api/db/clean", methods=["POST"])
def api_db_clean():
    cfg = Config()
    with db_lease() as db:
        result = db.clean_missing_files(cfg.WATCH_FOLDERS, output_dir=cfg.OUTPUT_DIR)
    return jsonify({"status": "ok", "removed": result["records"], "sheets": result["sheets"]})


# ── Backup / restore ──────────────────────────────────────────────
def _schedule_cleanup(path: str):
    @after_this_request
    def _cleanup(response):
        try:
            os.unlink(path)
        except OSError:
            pass
        return response


@app.route("/api/backup/db")
def api_backup_db():
    """Download a hot-copy of the SQLite database using sqlite3.backup."""
    cfg = Config()
    db_path = cfg.DB_FILE
    if not os.path.isfile(db_path):
        return jsonify({"status": "error", "message": "Database file not found"}), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        src  = sqlite3.connect(db_path)
        dest = sqlite3.connect(tmp.name)
        with dest:
            src.backup(dest)
        dest.close(); src.close()
    except Exception as e:
        log.warning("Database backup failed: %s", e)
        os.unlink(tmp.name)
        return jsonify({"status": "error", "message": "Backup failed"}), 500
    _schedule_cleanup(tmp.name)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"safescanarr-{ts}.db",
                     mimetype="application/octet-stream")


@app.route("/api/backup/vcs")
def api_backup_vcs():
    """Download all VCS thumbnails as a zip archive."""
    cfg = Config()
    out_dir = Path(cfg.OUTPUT_DIR)
    if not out_dir.is_dir():
        return jsonify({"status": "error", "message": "VCS directory not found"}), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        # ZIP_DEFLATED gives marginal gain over JPGs but keeps the archive in a
        # universally readable format; transfer cost matters more than ratio here.
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for jpg in sorted(out_dir.glob("*.jpg")):
                z.write(jpg, arcname=jpg.name)
    except Exception as e:
        log.warning("VCS backup failed: %s", e)
        os.unlink(tmp.name)
        return jsonify({"status": "error", "message": "Backup failed"}), 500
    _schedule_cleanup(tmp.name)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"safescanarr-vcs-{ts}.zip",
                     mimetype="application/zip")


@app.route("/api/backup/restore/db", methods=["POST"])
def api_restore_db():
    global _publishing  # module-shared admission state, not a local
    """Replace the SQLite database with an uploaded file. Refuses while a scan is running.

    The uploaded file's schema is discarded entirely: no uploaded executable
    schema is published — supported application data is imported into a
    fresh database built from the application's own trusted DDL, the upload
    must contain the core tables as ordinary tables (views and virtual
    tables are rejected, never evaluated), and every imported value is
    validated before publication. All uploaded ``web_sessions``
    content is dropped. Only the restoring operator's row is recreated,
    rechecked against the live database immediately before publication.
    Publication (final recheck + writer drain + WAL flush + ``os.replace`` +
    connection handoff) and every remembered-row deletion (logout, revoke,
    re-login old-row removal, credential transitions, expiration cleanup)
    share the same lock, so there is no gap between the liveness check and
    the file is replaced. Publication is owned by ONE restore at a time
    (publication ownership is serialized); while any attempt is in flight,
    new database handles block, and the file is replaced only after every
    lease on EVERY still-active generation retired against the live file
    — including generations left over by a previously timed-out attempt —
    has exited. A failure at any point — a failed checkpoint (including a
    busy result), a drain timeout, or a failed final os.replace — aborts
    the restore (500) BEFORE the live file is replaced and leaves the live
    database untouched and usable, including its committed WAL data. Each
    request stages into its own private file; one restore can never touch
    another's staging or cleanup.
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    cfg = Config()
    db  = get_db()
    try:
        pid = db.get_scan_pid()
        if pid and _pid_is_scanner(pid):
            return jsonify({"status": "error", "message": "Stop the running scan before restoring"}), 409
    finally:
        release_db(db)
    # Validate SQLite magic header
    head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        return jsonify({"status": "error", "message": "Not a SQLite database file"}), 400
    db_path = cfg.DB_FILE
    # Request-private stage in the DB directory: same filesystem for the
    # atomic os.replace, and no other request can touch this pathname.
    tmp_path = os.path.join(
        os.path.dirname(db_path),
        f".{os.path.basename(db_path)}.restore.{os.getpid()}.{threading.get_ident()}"
         f".{secrets.token_hex(8)}.tmp")
    try:
        with open(tmp_path, "wb") as out:
            out.write(head)
            while True:
                chunk = f.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                out.write(chunk)
        rid = session.get("rid")
        rexp = session.get("rexp")
        _import_backup_to_stage(tmp_path, rid, rexp)
        with _db_lock:
            # Final recheck + publication under the same lock as logout and
            # remembered-row deletion: if the caller signed out (or was
            # revoked) while the restore was staged, the last-moment recheck
            # drops the seeded row before the file goes live.
            live = get_db()
            try:
                recheck = (rid is not None and rexp is not None
                           and int(rexp) > int(time.time())
                           and live.get_web_session(_rid_hash(rid)) is not None)
                if not recheck:
                    src = sqlite3.connect(tmp_path)
                    try:
                        src.execute("DELETE FROM web_sessions")
                        src.commit()
                    finally:
                        src.close()
                else:
                    src = sqlite3.connect(tmp_path)
                    try:
                        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        src.close()
            finally:
                release_db(live)
            # Stage sidecars are request-owned; checkpoint/remove before
            # publishing so only the main file is replaced.
            for suffix in ("-wal", "-shm", "-journal"):
                stale = Path(tmp_path + suffix)
                if stale.exists():
                    stale.unlink()
            # ── Publication protocol (failure-safe, writer-coordinated) ──
            # ONE owner at a time: _publish_lock serializes publication
            # ownership across restores (incl. overlapping requests), so
            # a second restore can neither overwrite the first's drain
            # targets nor interleave its own replace. The owner releases
            # _db_lock for every wait below, so lease holders and row
            # deletions (logout/revoke) can always proceed — no deadlock.
            with _publish_lock:
                # 1. Close the admission window to NEW writers: while
                #    _publishing is set, get_db() blocks, so no request
                #    can open the soon-to-be-replaced inode and slip a
                #    write into the drain below. It is set under _db_lock
                #    (module state) and dropped only after the attempt
                #    fully ends, so the get_db() wait always resumes.
                with _db_lock:
                    _publishing = True
                    # 2. Drain: retire the shared connection WITHOUT
                    #    closing it (holders keep their lease; the
                    #    generation's last release closes it later), then
                    #    wait for every leased writer on EVERY generation
                    #    retired against this live file — including those
                    #    left over from a previously timed-out attempt,
                    #    which stay in _drained_keys until their last
                    #    lease exits, so no later restore can bypass a
                    #    still-active old writer.
                    _drop_shared_db_locked(close=False)
                try:
                    deadline = time.monotonic() + _PUBLISH_DRAIN_TIMEOUT_SECONDS
                    while True:
                        with _db_lock:
                            pending = [k for k in _drained_keys
                                       if _db_refs.get(k, 0) > 0]
                        if not pending:
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError(
                                "leased database writers did not finish "
                                f"within {_PUBLISH_DRAIN_TIMEOUT_SECONDS}s; "
                                "restore aborted to preserve committed data")
                        with _db_lock:
                            _db_cond.wait(timeout=0.1)
                    # 3. Flush the live database's committed WAL content into
                    #    the main file and REQUIRE a successful checkpoint:
                    #    fetch the PRAGMA row and check its busy field (0 =
                    #    completed). Leased writers have drained; a busy row
                    #    here means a foreign connection still holds the WAL
                    #    (e.g. an external read transaction) — abort, never
                    #    publish on top of it.
                    with _db_lock:
                        chk = sqlite3.connect(db_path)
                        try:
                            row = chk.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                            if row is None:
                                raise sqlite3.OperationalError(
                                    "wal_checkpoint(TRUNCATE) returned no result row")
                            busy = int(row[0])
                            if busy != 0:
                                raise sqlite3.OperationalError(
                                    f"wal_checkpoint(TRUNCATE) reported busy={busy}; "
                                    "the live WAL could not be fully checkpointed")
                        finally:
                            chk.close()
                        # 4. Publish. Until os.replace succeeds, nothing
                        #    destructive has happened to the live database
                        #    or its sidecars. Any failure above (busy,
                        #    checkpoint error, drain timeout) aborts BEFORE
                        #    the replace, leaving the live database and its
                        #    committed WAL data untouched and usable. The
                        #    recheck/purge (step 0) and this replace run
                        #    under the SAME _db_lock hold: with _db_lock
                        #    held and no lease pending, a logout cannot
                        #    slip between the remembered-row check and the
                        #    replace.
                        os.replace(tmp_path, db_path)
                        _drained_keys.clear()
                    # 5. Only AFTER a successful replace: remove the OLD
                    #    inode's sidecars. Every lease on the retired
                    #    generations has exited (drain above), so no open
                    #    connection can still operate on the old inode's
                    #    WAL. If step 4 raises, we never reach this and the
                    #    live database keeps its WAL/SHM intact and usable.
                    for suffix in ("-wal", "-shm", "-journal"):
                        stale = Path(db_path + suffix)
                        if stale.exists():
                            try:
                                stale.unlink()
                            except OSError as e:
                                log.warning("Could not remove %s after publication: %s",
                                            stale, e)
                finally:
                    with _db_lock:
                        _publishing = False
                        _db_cond.notify_all()
    except Exception as e:
        log.warning("Database restore failed: %s", e)
        # Request-owned cleanup: only this request's stage file and its
        # sidecars — never another restore's. The LIVE database's sidecars
        # are deliberately untouched on failure.
        for suffix in ("", "-wal", "-shm", "-journal"):
            stale = Path(tmp_path + suffix)
            if stale.exists():
                try: stale.unlink()
                except OSError: pass
        return jsonify({"status": "error", "message": "Restore failed"}), 500
    return jsonify({"status": "ok"})


# Stage files are request-private (pid+thread+random suffix). If the process
# dies mid-restore no finally runs, so a crashed request can leave its stage
# behind; startup removes only provably orphaned ones: family members
# (.tmp/.tmp.upload/sidecars) of DEAD-pid owners. There is no age-based
# fallback — a live pid's stage is never deleted however old, and unknown
# liveness (unreadable /proc) preserves everything. (The former retention
# window constant is kept only so external diagnostics that import it keep
# running; it no longer participates in deletion decisions.)
_RESTORE_STAGE_MAX_AGE_SECONDS = 7 * 86400  # unused for deletion; see above


def _cleanup_orphan_restore_stages() -> None:
    """Remove leftover restore stage files at STARTUP only.

    Conservative by design: only the actual crash-artifact family —
    ``.safescanarr.db.restore.<pid>.<thread>.<random>.tmp`` stage files,
    their ``.tmp.upload`` upload copies, and their ``-wal``/`-shm``/
    ``-journal`` sidecars — is considered, and only when the entry is a
    regular file owned by this uid, lives in the DB directory, and its
    owning pid is PROVABLY DEAD (never mtime alone: a live pid's stage is
    untouchable however old it is). A stage whose pid cannot be determined
    (unparseable name) is left alone; if /proc cannot be read at all,
    nothing is removed (unknown liveness is not death). Liveness comes
    from a SINGLE /proc observation whose failure is "unknown", never a
    fabricated empty pid set. Never called during active requests, so it
    cannot race a live request's stage.
    """
    try:
        db_dir = os.path.dirname(Config().DB_FILE)
        me = os.getpid()
        live = _live_pids()  # ONE result: a pid set, or None if unknown
        if live is None:
            return  # pid discovery failed: preserve everything
        for entry in os.listdir(db_dir):
            name = entry.split(".")
            # .safescanarr.db.restore.<pid>.<thread>.<random>.tmp[.upload]
            # (sidecars append -wal/-shm/-journal AFTER those, so parse
            # from the left and require the family prefix exactly).
            if (len(name) < 6 or name[0] or name[1] != "safescanarr"
                    or name[2] != "db" or name[3] != "restore"
                    or not name[4].isdigit()):
                continue
            base = entry
            for suffix in ("-wal", "-shm", "-journal", ".upload"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            if not base.endswith(".tmp"):
                continue  # not the stage family
            path = os.path.join(db_dir, entry)
            try:
                st = os.stat(path)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_uid != os.geteuid():
                    continue
                pid = int(name[4])
                if pid == me:
                    continue  # our own (paranoia; startup has no stages)
                if pid in live:
                    continue  # owner alive: age is never proof of orphanhood
                os.unlink(path)
            except (OSError, ValueError):
                continue
    except OSError:
        pass


def _live_pids():
    """ONE /proc observation: the set of live pids, or None if /proc
    could not be read (liveness unknown — never a fabricated empty set,
    which would convert every owner into a provably-dead one)."""
    try:
        return {int(p) for p in os.listdir("/proc") if p.isdigit()}
    except OSError:
        return None


# Non-application tables that are copied as-is into the restored database.
# Everything else uploaded (triggers, views, web_sessions content) is
# discarded: the schema comes from trusted DDL only.
_RESTORE_TABLES = ("files", "errors", "state", "lifetime_stats")
_RESTORE_TABLE_COLUMNS = {
    "files": {
        "path", "name", "size", "mtime", "status", "review_state", "flagged",
        "flag_reason", "nsfw_confidence", "quarantine_path", "source_retained",
        "state_updated_at", "state_source", "updated_at", "arr_result",
    },
    "errors": {"id", "path", "message", "recorded_at"},
    "state": {"key", "value"},
    "lifetime_stats": {"key", "count"},
}
# Import budgets: the upload is bounded by the 512 MiB request cap on disk,
# but source expressions could still expand in memory; stream in batches and
# cap total rows/values so a pathological backup fails instead of OOM-ing.
_IMPORT_BATCH_ROWS = 500
_IMPORT_MAX_ROWS = 1_000_000
_IMPORT_MAX_VALUES = 8_000_000

# Column value validators, applied to every imported row BEFORE it is
# inserted into the stage (and thus before publication). Values that do not
# satisfy the trusted schema's application assumptions (e.g. BLOBs in TEXT
# fields, wrong integer shapes, NULLs in NOT NULL columns) reject the whole
# restore instead of publishing data that later breaks the API.

def _v_text(v):
    if v is None or isinstance(v, str):
        return v
    raise ValueError("expected TEXT, got " + type(v).__name__)

def _v_int(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("expected INTEGER, got " + type(v).__name__)
    return v

def _v_real(v):
    if v is None:
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("expected REAL, got " + type(v).__name__)
    return v

# NULL allowed only where the trusted DDL allows it.
_RESTORE_TABLE_VALIDATORS = {
    "files": {
        "path": _v_text, "name": _v_text, "size": _v_int, "mtime": _v_real,
        "status": _v_text, "review_state": _v_text, "flagged": _v_int,
        "flag_reason": _v_text, "nsfw_confidence": _v_real,
        "quarantine_path": _v_text, "source_retained": _v_int,
        "state_updated_at": _v_text, "state_source": _v_text,
        "updated_at": _v_text, "arr_result": _v_text,
        # NOT NULL columns: never None
        "__notnull__": ("path", "name", "size", "mtime", "status",
                        "review_state", "flagged", "source_retained",
                        "updated_at"),
    },
    "errors": {
        "id": _v_int, "path": _v_text, "message": _v_text,
        "recorded_at": _v_text,
        "__notnull__": ("path", "recorded_at"),
    },
    "state": {
        "key": _v_text, "value": _v_text,
        "__notnull__": ("key", "value"),
    },
    "lifetime_stats": {
        "key": _v_text, "count": _v_int,
        "__notnull__": ("key", "count"),
    },
}


def _validate_source_schema(src) -> None:
    """The upload must be a supported application backup: each core table
    must exist as an ORDINARY table (sqlite_master type='table', not a view
    or virtual table). Anything else is rejected instead of being published
    as empty or imported through an evaluated view."""
    for table in _RESTORE_TABLES:
        row = src.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"not a supported SafeScanarr backup: required table "
                f"'{table}' is missing")
        obj_sql = (row[1] or "").lstrip().upper()
        if row[0] != "table" or obj_sql.startswith("CREATE VIRTUAL"):
            raise RuntimeError(
                f"unsupported backup object for '{table}': "
                f"only ordinary tables are imported "
                f"(got {row[0]}"
                f"{', virtual' if obj_sql.startswith('CREATE VIRTUAL') else ''})")


def _import_backup_to_stage(tmp_path: str, rid, rexp) -> None:
    """Build a fresh DB from trusted DDL in *tmp_path* and import the
    uploaded backup's supported application data into it.

    The uploaded file is only ever opened read-only for data extraction;
    no uploaded schema is published (the stage is rebuilt from trusted
    DDL) and source objects must be ordinary tables — uploaded views are
    never evaluated. Integrity, schema shape, and every imported value are
    validated before publication; any failure raises, which aborts the
    restore without touching the live database.
    """
    upload = tmp_path + ".upload"
    os.replace(tmp_path, upload)
    try:
        src = sqlite3.connect(f"file:{upload}?mode=ro", uri=True)
        try:
            # Whole-file integrity gate on the upload before importing.
            ok = src.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"uploaded database failed integrity_check: {ok}")
            # Source-contract gate: ordinary core tables, right shape.
            _validate_source_schema(src)
            for table, cols in _RESTORE_TABLE_COLUMNS.items():
                have = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
                missing = cols - have
                if missing:
                    raise RuntimeError(
                        f"backup table '{table}' is missing columns "
                        f"{sorted(missing)}")
            if rid and rexp and int(rexp) > int(time.time()):
                seed_rid = _rid_hash(rid)
                seed_exp = int(rexp)
            else:
                seed_rid = None
                seed_exp = 0
        finally:
            src.close()
        # Rebuild the stage from trusted DDL (Database.__init__).
        staged = Database(tmp_path)
        try:
            con = staged._con
            con.execute("DELETE FROM web_sessions")
            con.execute("DELETE FROM errors")
            con.execute("DELETE FROM state")
            con.execute("DELETE FROM lifetime_stats")
            con.execute("DELETE FROM files")
            for table in _RESTORE_TABLES:
                _import_table(f"file:{upload}?mode=ro", con, table)
            con.commit()
            if seed_rid:
                staged.create_web_session(seed_rid, seed_exp,
                                          ip=request.remote_addr,
                                          user_agent=request.headers.get("User-Agent", ""))
            # Post-sanitization validation: no uploaded schema may remain
            # (the stage was built from trusted DDL, so assert it), the
            # required tables exist with the right columns, and integrity
            # passes. Migration errors are NOT logged-and-ignored here:
            # Database.__init__ warnings surfaced above are advisory, this
            # is the hard gate.
            leftovers = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "OR type='view'").fetchone()[0]
            if leftovers:
                raise RuntimeError("uploaded schema objects survived import")
            for table, cols in _RESTORE_TABLE_COLUMNS.items():
                have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                if not cols <= have:
                    raise RuntimeError(f"table {table} missing columns {cols - have}")
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"staged database failed integrity_check: {ok}")
            count = con.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
            expected = 1 if seed_rid else 0
            if count != expected:
                raise RuntimeError(
                    f"web_sessions sanitization failed: {count} rows present, "
                    f"expected {expected}")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            staged.close()
    finally:
        # Upload copy is request-owned scratch; always remove it.
        for suffix in ("", "-wal", "-shm", "-journal"):
            stale = Path(upload + suffix)
            if stale.exists():
                try: stale.unlink()
                except OSError: pass


# Recognized ephemeral process-state keys that are DISCARDED at import (not
# published): they describe processes on the uploader's machine, are
# meaningless in this install, and a foreign/malformed value (e.g. TEXT
# "unavailable") must never reach the live API consumers. All other state
# keys are imported under the standard validators.
_DISCARDED_STATE_KEYS = {"scan_pid"}

# Recognized persistent cursor keys with numeric semantics: a non-numeric
# value cannot be published (the poller would crash on int() forever), so
# it is DELIBERATELY RESET to absent at import. Documented resynchronization
# semantics: after a reset the next poll treats every record in the history
# window as new (it re-fires scanner triggers for up to the most recent 50
# imports of each arr — a bounded, idempotent rescan), then stores a fresh
# numeric cursor. Consumers additionally parse defensively (None on a bad
# value), so a foreign value that reaches the live state by another path
# cannot crash the poller either.
_CURSOR_STATE_KEYS = ("sonarr_last_id", "radarr_last_id")


def _valid_cursor(v) -> bool:
    # Numeric string (canonical stored form); int-bearing TEXT like "12"
    # passes, anything a real backup could not have written does not.
    if v is None:
        return True
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


def _import_table(src_uri: str, dest_con, table: str) -> None:
    """Copy *table* from the read-only upload into *dest_con*, streaming in
    batches with explicit row/value budgets. Every value passes the
    application's type/nullability validators BEFORE insertion; a bad row
    aborts the whole restore. Values are bound as parameters (never
    interpolated SQL)."""
    src = sqlite3.connect(src_uri, uri=True)
    try:
        use = sorted(_RESTORE_TABLE_COLUMNS[table])
        validators = _RESTORE_TABLE_VALIDATORS[table]
        notnull = set(validators["__notnull__"])
        q = f"SELECT {', '.join(use)} FROM {table}"
        placeholders = ", ".join("?" for _ in use)
        insert = (f"INSERT INTO {table} ({', '.join(use)}) "
                  f"VALUES ({placeholders})")
        # dest column names come from the trusted DDL, not the upload
        total_rows = 0
        total_values = 0
        batch = []
        for row in src.execute(q):
            if table == "state":
                rec = dict(zip(use, row))
                if rec.get("key") in _DISCARDED_STATE_KEYS:
                    continue  # ephemeral process state: never published
                if (rec.get("key") in _CURSOR_STATE_KEYS
                        and not _valid_cursor(rec.get("value"))):
                    # Recognized numeric cursor with an unrecognizable
                    # value: deliberate documented reset (absent), never a
                    # crash-later publish.
                    continue
            total_rows += 1
            if total_rows > _IMPORT_MAX_ROWS:
                raise RuntimeError(
                    f"backup table '{table}' exceeds the import row budget")
            clean = []
            for col, v in zip(use, row):
                total_values += 1
                if total_values > _IMPORT_MAX_VALUES:
                    raise RuntimeError(
                        f"backup table '{table}' exceeds the import value budget")
                if v is None and col in notnull:
                    raise RuntimeError(
                        f"backup row {total_rows} of '{table}' has NULL in "
                        f"NOT NULL column '{col}'")
                try:
                    clean.append(validators[col](v))
                except ValueError as e:
                    raise RuntimeError(
                        f"backup row {total_rows} of '{table}' has an "
                        f"invalid value for '{col}': {e}") from e
            batch.append(clean)
            if len(batch) >= _IMPORT_BATCH_ROWS:
                dest_con.executemany(insert, batch)
                batch = []
        if batch:
            dest_con.executemany(insert, batch)
    finally:
        src.close()


def _zip_entry_is_safe(name: str) -> bool:
    """Reject absolute paths and any traversal segments in an archive entry."""
    if not name:
        return False
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or normalised.startswith("../"):
        return False
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False
    return True


@app.route("/api/backup/restore/vcs", methods=["POST"])
def api_restore_vcs():
    """Replace VCS thumbnails with the contents of an uploaded zip archive."""
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    cfg = Config()
    out_dir = Path(cfg.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        f.save(tmp_path)
        if not zipfile.is_zipfile(tmp_path):
            return jsonify({"status": "error", "message": "Not a zip archive"}), 400

        with zipfile.ZipFile(tmp_path) as z:
            infos = z.infolist()
            # ── Validate before touching anything on disk (zip-bomb guards) ──
            if len(infos) > MAX_ZIP_ENTRIES:
                return jsonify({"status": "error",
                                "message": "Archive has too many entries"}), 400
            total_uncompressed = 0
            for info in infos:
                if not _zip_entry_is_safe(info.filename):
                    return jsonify({"status": "error",
                                    "message": "Archive contains unsafe paths"}), 400
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_ZIP_UNCOMPRESSED:
                    return jsonify({"status": "error",
                                    "message": "Archive expands to too much data"}), 400

            # Wipe existing thumbnails so the restore is a true replacement
            for existing in out_dir.glob("*.jpg"):
                existing.unlink()

            extracted = 0
            for info in infos:
                # Flatten any subdir structure; only .jpg files are stored
                name = os.path.basename(info.filename)
                if not name or not name.lower().endswith(".jpg"):
                    continue
                target = out_dir / name
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted += 1
    except Exception as e:
        log.warning("VCS restore failed: %s", e)
        return jsonify({"status": "error", "message": "Restore failed"}), 500
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass
    return jsonify({"status": "ok", "extracted": extracted})


# ── Filesystem browser (used by Watch Folders picker) ──────────────
def _browse_roots(cfg: Config) -> list:
    """Resolved directories the file browser is allowed to walk."""
    roots = []
    for folder in cfg.WATCH_FOLDERS or []:
        if not folder:
            continue
        try:
            roots.append(Path(folder).resolve())
        except OSError:
            continue
    if not roots:
        # Onboarding: nothing configured yet, so allow the historical default
        # mount points only (still nothing outside them).
        for candidate in ("/mnt", "/media"):
            p = Path(candidate)
            if p.is_dir():
                roots.append(p.resolve())
    return roots


@app.route("/api/fs/list")
def api_fs_list():
    """List subdirectories, restricted to configured watch-folder roots."""
    cfg  = Config()
    roots = _browse_roots(cfg)
    if not roots:
        return jsonify({"status": "error", "message": "Forbidden"}), 403

    raw = request.args.get("path", "").strip()
    if not raw:
        raw = str(roots[0])
    try:
        path = Path(raw).resolve()
    except (OSError, ValueError):
        return jsonify({"status": "error", "message": "Forbidden"}), 403

    if not is_within(path, roots):
        log.warning("fs/list denied outside watch roots: %s", path)
        return jsonify({"status": "error", "message": "Forbidden"}), 403

    if not path.is_dir():
        return jsonify({"status": "error", "message": "Not a directory"}), 400

    try:
        entries = []
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            full = path / name
            try:
                if full.is_dir():
                    entries.append({"name": name, "path": str(full)})
            except OSError:
                continue
    except PermissionError:
        return jsonify({"status": "error", "message": "Permission denied"}), 403
    except OSError as e:
        log.warning("fs/list error for %s: %s", path, e)
        return jsonify({"status": "error", "message": "Could not list directory"}), 500

    # Never offer a parent above the allowed roots.
    parent_path = path.parent
    parent = str(parent_path) if (path != parent_path and is_within(parent_path, roots)) else None
    return jsonify({"path": str(path), "parent": parent, "entries": entries})


# ── Scan control ──────────────────────────────────────────────────
def _pid_is_scanner(pid: int) -> bool:
    """True only if /proc/<pid>/cmdline still belongs to the scanner.

    Guards against PID reuse: a recycled PID must not be mistaken for a running
    scan.
    """
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return any(b"scanner.py" in part for part in cmdline.split(b"\x00"))


@app.route("/api/scan/status")
def api_scan_status():
    with db_lease() as db:
        pid = db.get_scan_pid()
        if pid:
            if _pid_is_scanner(pid):
                return jsonify({"running": True, "pid": pid})
            log.info("Clearing stale scan pid %d (not the scanner)", pid)
            db.clear_scan_pid()
    return jsonify({"running": False})


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    with db_lease() as db:
        pid = db.get_scan_pid()
        if not pid or not _pid_is_scanner(pid):
            if pid:
                db.clear_scan_pid()
            return jsonify({"status": "not_running"})
        # Clear the PID — scanner checks between files and exits gracefully
        # Do NOT send SIGTERM; that kills the process mid-file and closes the log pipe
        db.clear_scan_pid()
    log.info("Scan stop requested — scanner will stop after current file (pid %d)", pid)
    return jsonify({"status": "stopping"})


@app.route("/api/sheets/remove-rejected", methods=["POST"])
def api_remove_rejected():
    """Remove a rejected entry from DB and delete its sheet."""
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem: stems = [stem]

    done = []
    cfg = Config()
    with db_lease() as db:
        for stem in stems:
            row = db.find_file_by_stem(stem)
            if not row or row["review_state"] != "rejected":
                continue
            # Delete sheet if exists
            sheet = resolve_sheet(cfg.OUTPUT_DIR, row["path"])
            if sheet.exists():
                sheet.unlink()
            db.delete_file(row["path"])
            done.append(stem)
    return jsonify({"status": "ok", "removed": done})


# ── Logs ──────────────────────────────────────────────────────────
@app.route("/api/logs")
def api_logs():
    cfg      = Config()
    lines    = int(request.args.get("lines", 200))
    level    = request.args.get("level", "")
    log_path = Path(cfg.LOG_FILE)
    if not log_path.exists():
        return jsonify({"lines": []})
    with open(log_path) as f:
        all_lines = f.readlines()
    if level:
        all_lines = [l for l in all_lines if f"[{level}]" in l]
    return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})


# ── Scan ──────────────────────────────────────────────────────────
@app.route("/api/scan", methods=["POST"])
def api_scan():
    cfg = Config()
    with db_lease() as db:
        # Check if already running
        existing_pid = db.get_scan_pid()
        if existing_pid:
            if _pid_is_scanner(existing_pid):
                return jsonify({"status": "already_running", "pid": existing_pid})
            db.clear_scan_pid()
    proc = subprocess.Popen(
        [sys.executable, SCANNER, "--scan"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    with db_lease() as db:
        db.set_scan_pid(proc.pid)
    return jsonify({"status": "started", "pid": proc.pid})


# ── Helpers ───────────────────────────────────────────────────────
def _url_host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _api_get(url: str, api_key: str):
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("API GET failed: host=%s error=%s", _url_host(url), e)
        return None


def _api_post(url: str, api_key: str, body: dict = None):
    """POST to an arr API endpoint."""
    data    = json.dumps(body).encode() if body else b""
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"}
    req     = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning("API POST failed: host=%s error=%s", _url_host(url), e)
        return None


def _api_delete(url: str, api_key: str):
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key}, method="DELETE"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("API DELETE failed: host=%s error=%s", _url_host(url), e)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = Config()
    init_db()
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT,
            threaded=True, use_reloader=False)
