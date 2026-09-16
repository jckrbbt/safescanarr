#!/usr/bin/env python3
"""
safescanarr/web/server.py v1.0.5

Auth model
----------
Every route except ``/static/*``, ``/login``, ``/setup``, and ``/health``
requires authentication. Two modes are supported:

  * token mode:   shared token via session cookie (POST /login) or
                  ``X-Auth-Token`` / ``Authorization: Bearer ***``
  * password mode: password login via session cookie

All state-changing requests are CSRF-protected: same-origin is enforced via
Origin/Referer, and the session CSRF token (header ``X-CSRF-Token``) must be
present. Token-mode clients may also use a valid token header as CSRF proof.
"""

import datetime
import hashlib
import json
import logging
import os
import shutil
import sqlite3
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

import config as config_module
from web import auth
from config import Config
from database import Database
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
def _init_session_secret() -> None:
    """Generate/persist a session_secret and set app.secret_key."""
    secret = None
    try:
        cfg = config_module.raw()
        secret = cfg.get("session_secret", "")
        if not secret:
            secret = os.environ.get("SS_SESSION_SECRET", "")
        if not secret:
            secret = __import__("secrets").token_urlsafe(32)
            cfg["session_secret"] = secret
            config_module.save(cfg)
            log.warning(
                "Generated new session_secret and persisted it to config.json (0600). "
                "Existing sessions will be invalidated once on upgrade to 1.0.5."
            )
        app.secret_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    except Exception as e:  # pragma: no cover - read-only fallback
        log.error("Could not persist session_secret: %s. Using deterministic fallback.", e)
        app.secret_key = hashlib.sha256(b"safescanarr-session-fallback").hexdigest()


_init_session_secret()

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
_db = None
_db_lock = threading.Lock()


def get_db() -> Database:
    """Return the process-wide Database, initialising/migrating on first use."""
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = Database(Config().DB_FILE)
    return _db


def init_db() -> Database:
    """Explicitly initialise the database (called at startup)."""
    return get_db()


def _reset_db() -> None:
    """Drop the shared connection so the next request re-opens the DB file."""
    global _db
    with _db_lock:
        if _db is not None:
            try:
                _db.close()
            except Exception as e:  # pragma: no cover - best effort
                log.warning("Error closing database: %s", e)
            _db = None


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
    if auth.method() == "token" and auth.verify(_supplied_token()):
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

    if not session.get("authed"):
        # Token-mode clients may authenticate via header for API calls, but
        # HTML pages still require a session cookie (after /login).
        supplied = _supplied_token()
        if supplied and auth.method() == "token" and auth.verify(supplied):
            session.clear()
            session["authed"] = True
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

    session.clear()
    session["authed"] = True
    session["csrf"] = os.urandom(24).hex()
    log.info("First-run setup completed via %s method from %s", method, ip)
    return jsonify({"status": "ok", "token": generated if method == "token" else None})


# ── Login ─────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("authed"):
            return redirect("/")
        return render_template("login.html", version=Config.version(),
                               auth_method=auth.method())

    if not _origin_ok():
        return jsonify({"status": "error", "message": "Cross-origin request rejected"}), 403

    ip = request.remote_addr or "unknown"
    if not _throttle_check(ip):
        return jsonify({"status": "error", "message": "Too many attempts; try again later."}), 429
    if not _rate_limit_check(ip):
        return jsonify({"status": "error", "message": "Too many requests; slow down."}), 429

    body = request.get_json(silent=True) or request.form or {}
    supplied = str(body.get("secret", "") or body.get("token", "") or body.get("password", "")).strip()

    if not auth.verify(supplied):
        _throttle_record(ip, success=False)
        log.warning("Failed login attempt from %s", ip)
        return jsonify({"status": "error", "message": "Invalid credentials"}), 401

    _throttle_record(ip, success=True)
    session.clear()
    session["authed"] = True
    session["csrf"] = os.urandom(24).hex()
    log.info("Login OK from %s", ip)
    return jsonify({"status": "ok", "csrf_token": session["csrf"]})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "ok"})


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
    })


# ── Stats ─────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    return jsonify(get_db().get_stats())


# ── Lifetime stats ───────────────────────────────────────────────
@app.route("/api/stats/full")
def api_stats_full():
    return jsonify(get_db().get_stats_full())


# ── Sheets by state ───────────────────────────────────────────────
@app.route("/api/sheets")
def api_sheets():
    cfg        = Config()
    state      = request.args.get("state", "pending")
    search     = request.args.get("search", "").lower()
    source     = request.args.get("source", "")  # "auto", "user", or "" for all
    output_dir = Path(cfg.OUTPUT_DIR)
    sheets     = []

    rows = get_db().list_files(review_state=state)
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

    db  = get_db()
    cfg = Config()
    done = []
    for stem in stems:
        row = db.find_file_by_stem(stem)
        if not row:
            continue
        # If currently quarantined, move video back
        if row["review_state"] == "quarantined" and row["quarantine_path"]:
            q_path = Path(row["quarantine_path"])
            orig   = Path(row["path"])
            if q_path.exists():
                orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(q_path), str(orig))
                log.info("Restored from quarantine: %s", orig)
        db.set_review_state(row["path"], "approved")
        done.append(stem)
    return jsonify({"status": "ok", "approved": done})


@app.route("/api/sheets/reject", methods=["POST"])
def api_reject():
    """Reject one or more sheets.

    Safe by default: the video is *moved to quarantine*. Permanent deletion
    (plus the arr blacklist/re-search flow) only happens when the config flag
    ``delete_on_reject`` is explicitly enabled.
    """
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem:
            stems = [stem]

    db  = get_db()
    cfg = Config()
    done = []
    for stem in stems:
        row = db.find_file_by_stem(stem)
        if not row:
            continue

        source_path = row["path"]
        video_path  = Path(row["quarantine_path"] or row["path"])

        if cfg.DELETE_ON_REJECT:
            if video_path.exists():
                video_path.unlink()
                log.warning("Rejected (deleted): %s", video_path)
            # Blacklist so the arr stack does not re-import the release
            _blacklist_path(source_path, cfg)
            db.set_review_state(source_path, "rejected")
        else:
            if row["review_state"] == "quarantined" and row["quarantine_path"]:
                q_path = Path(row["quarantine_path"])   # already quarantined
            elif video_path.exists():
                q_dir = Path(cfg.QUARANTINE_DIR)
                q_dir.mkdir(parents=True, exist_ok=True)
                q_path = q_dir / video_path.name
                if q_path.exists():
                    q_path = q_dir / f"{video_path.stem}_{int(datetime.datetime.now().timestamp())}{video_path.suffix}"
                shutil.move(str(video_path), str(q_path))
                log.warning("Rejected (quarantined, delete_on_reject=false): %s -> %s",
                            source_path, q_path)
            else:
                q_path = Path(row["quarantine_path"]) if row["quarantine_path"] else None
            db.set_review_state(source_path, "quarantined",
                                quarantine_path=str(q_path) if q_path else None)

        done.append(stem)
    return jsonify({"status": "ok", "rejected": done,
                    "mode": "delete" if cfg.DELETE_ON_REJECT else "quarantine"})


@app.route("/api/sheets/quarantine", methods=["POST"])
def api_quarantine():
    """Manually quarantine a pending item."""
    stem = (request.get_json() or {}).get("stem", "")
    db   = get_db()
    cfg  = Config()
    row  = db.find_file_by_stem(stem)
    if not row:
        return jsonify({"status": "error", "message": "Not found"}), 404

    video_path = Path(row["path"])
    if not video_path.exists():
        return jsonify({"status": "error", "message": "Source file not found"}), 404

    q_dir  = Path(cfg.QUARANTINE_DIR)
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / video_path.name
    if q_path.exists():
        q_path = q_dir / f"{video_path.stem}_{int(datetime.datetime.now().timestamp())}{video_path.suffix}"
    shutil.move(str(video_path), str(q_path))
    db.set_review_state(row["path"], "quarantined", quarantine_path=str(q_path))
    return jsonify({"status": "ok", "quarantine_path": str(q_path)})


@app.route("/api/sheets/requeue", methods=["POST"])
def api_requeue():
    """Re-process a file — regenerate sheet and re-run analysis."""
    stem = (request.get_json() or {}).get("stem", "")
    db   = get_db()
    cfg  = Config()
    row  = db.find_file_by_stem(stem)

    path = row["path"] if row else None
    if not path or not Path(path).exists():
        return jsonify({"status": "error", "message": "Source file not found"}), 404

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
    "quarantine_auto_reject_days", "delete_on_reject", "webhook_url",
    "webhook_on_review", "webhook_on_quarantine", "webhook_on_reject", "web_ui_url",
    "vcs_grid", "vcsi_timeout_seconds",
}
_CONFIG_BOOLS = {
    "polling_enabled", "scan_schedule_enabled", "delete_on_reject",
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
    for forbidden in ("auth_token", "auth_password_hash", "session_secret"):
        data.pop(forbidden, None)
    try:
        clean, errors = _build_config_update(data)
        if errors:
            return jsonify({"status": "error", "message": "; ".join(errors)}), 400

        existing = config_module.raw()
        existing.update(clean)
        config_module.save(existing)
        return jsonify({"status": "ok"})
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
    title = "Example (2024)"
    body = {
        "event":     "test",
        "path":      f"/mnt/media/Movies/{title}/{title}.mp4",
        "title":     title,
        "risk":      0.75,
        "labels":    ["FEMALE_BREAST_EXPOSED"],
        "message":   f"Quarantined: {title} (risk: 75%)",
        "timestamp": datetime.datetime.now().isoformat(),
    }
    if cfg.WEB_UI_URL:
        body["url"] = f"{cfg.WEB_UI_URL.rstrip('/')}/?tab=quarantined"
    payload = json.dumps(body).encode()
    try:
        req = urllib.request.Request(
            cfg.WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.warning("Test webhook failed (host=%s): %s", _url_host(cfg.WEBHOOK_URL), e)
        return jsonify({"status": "error", "message": "Webhook delivery failed"}), 502


@app.route("/api/db/clean", methods=["POST"])
def api_db_clean():
    cfg    = Config()
    db     = get_db()
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
    """Replace the SQLite database with an uploaded file. Refuses while a scan is running."""
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    cfg = Config()
    db  = get_db()
    pid = db.get_scan_pid()
    if pid and _pid_is_scanner(pid):
        return jsonify({"status": "error", "message": "Stop the running scan before restoring"}), 409
    # Validate SQLite magic header
    head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        return jsonify({"status": "error", "message": "Not a SQLite database file"}), 400
    db_path  = cfg.DB_FILE
    tmp_path = db_path + ".restore.tmp"
    try:
        with open(tmp_path, "wb") as out:
            out.write(head)
            while True:
                chunk = f.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                out.write(chunk)
        # Drop any stale WAL/journal sidecars so the new DB isn't confused with them
        for suffix in ("-wal", "-shm", "-journal"):
            stale = Path(db_path + suffix)
            if stale.exists():
                stale.unlink()
        os.replace(tmp_path, db_path)
    except Exception as e:
        log.warning("Database restore failed: %s", e)
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except OSError: pass
        return jsonify({"status": "error", "message": "Restore failed"}), 500
    # The shared connection still points at the old inode.
    _reset_db()
    return jsonify({"status": "ok"})


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
    db  = get_db()
    pid = db.get_scan_pid()
    if pid:
        if _pid_is_scanner(pid):
            return jsonify({"running": True, "pid": pid})
        log.info("Clearing stale scan pid %d (not the scanner)", pid)
        db.clear_scan_pid()
    return jsonify({"running": False})


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    db  = get_db()
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

    db  = get_db()
    cfg = Config()
    done = []
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
    db  = get_db()
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


def _blacklist_path(file_path: str, cfg: Config) -> None:
    """Full removal flow: delete from library, blacklist, trigger new search."""
    for service in ("sonarr", "radarr"):
        base    = cfg.SONARR_URL.rstrip("/") if service == "sonarr" else cfg.RADARR_URL.rstrip("/")
        api_key = cfg.SONARR_API_KEY        if service == "sonarr" else cfg.RADARR_API_KEY
        if not api_key:
            continue
        try:
            if service == "sonarr":
                # Find episode file
                ep_files = _api_get(f"{base}/api/v3/episodefile", api_key) or []
                ep_file_id = None
                series_id  = None
                for ef in ep_files:
                    if ef.get("path", "") == file_path:
                        ep_file_id = ef.get("id")
                        series_id  = ef.get("seriesId")
                        break
                if not ep_file_id:
                    continue
                # Get episode IDs
                episodes   = _api_get(f"{base}/api/v3/episode?seriesId={series_id}&episodeFileId={ep_file_id}", api_key) or []
                episode_ids = [e["id"] for e in episodes if "id" in e]
                # Delete from library
                _api_delete(f"{base}/api/v3/episodefile/{ep_file_id}", api_key)
                log.info("Sonarr: deleted episodefile %d", ep_file_id)
                # Blacklist history record
                history = _api_get(f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3", api_key) or {}
                for record in history.get("records", []):
                    if (record.get("data") or {}).get("importedPath", "") == file_path:
                        _api_delete(f"{base}/api/v3/blacklist/{record['id']}", api_key)
                        log.info("Sonarr: blacklisted record %d", record["id"])
                        break
                # Trigger new search
                if episode_ids:
                    _api_post(f"{base}/api/v3/command", api_key, {"name": "EpisodeSearch", "episodeIds": episode_ids})
                    log.info("Sonarr: triggered EpisodeSearch for %s", episode_ids)

            else:  # radarr
                # Find movie file
                movie_files = _api_get(f"{base}/api/v3/moviefile", api_key) or []
                movie_file_id = None
                movie_id      = None
                for mf in movie_files:
                    if mf.get("path", "") == file_path:
                        movie_file_id = mf.get("id")
                        movie_id      = mf.get("movieId")
                        break
                if not movie_file_id:
                    continue
                # Delete from library
                _api_delete(f"{base}/api/v3/moviefile/{movie_file_id}", api_key)
                log.info("Radarr: deleted moviefile %d", movie_file_id)
                # Blacklist history record
                history = _api_get(f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3", api_key) or {}
                for record in history.get("records", []):
                    if (record.get("data") or {}).get("importedPath", "") == file_path:
                        _api_delete(f"{base}/api/v3/blacklist/{record['id']}", api_key)
                        log.info("Radarr: blacklisted record %d", record["id"])
                        break
                # Trigger new search
                if movie_id:
                    _api_post(f"{base}/api/v3/command", api_key, {"name": "MoviesSearch", "movieIds": [movie_id]})
                    log.info("Radarr: triggered MoviesSearch for movie %d", movie_id)

        except Exception as e:
            log.warning("Arr removal failed for %s/%s: %s", service, file_path, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = Config()
    init_db()
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT,
            threaded=True, use_reloader=False)
