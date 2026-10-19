"""
safescanarr/config.py

Config priority (highest to lowest):
  1. Environment variables (secret overrides always win — see below)
  2. config.json in the data directory (written by the UI)
  3. Hardcoded defaults

On first run, if config.json doesn't exist, it is created from env vars / defaults.

Secrets precedence
------------------
The following environment variables *override* whatever is stored in
config.json, so operators can keep secrets out of the file entirely:

  SS_TOKEN          web UI auth token (overrides config field ``auth_token``)
  SONARR_API_KEY    overrides ``sonarr_api_key``
  RADARR_API_KEY    overrides ``radarr_api_key``

All other settings are read from config.json, which is written with mode 0600.
Existing installs are chmod-ed to 0600 on first load (self-heal).

Other environment variables (used when seeding a fresh config.json):
  BASE_DIR, WATCH_FOLDERS, SONARR_URL, RADARR_URL, WEB_UI_URL, VCS_GRID,
  POLL_INTERVAL_SECONDS, VCSI_TIMEOUT_SECONDS, WEB_HOST, WEB_PORT
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_DEFAULTS = {
    "watch_folders":           [],
    "sonarr_url":              "http://localhost:8989",
    "sonarr_api_key":          "",
    "radarr_url":              "http://localhost:7878",
    "radarr_api_key":          "",
    "polling_enabled":         False,
    "poll_interval_seconds":   600,
    "scan_schedule_enabled":   False,
    "scan_schedule":           "daily",
    # NSFW Detection zones
    "detection_profile":       "balanced",  # conservative, balanced, aggressive, custom
    "zone_auto_approve":       0.4,   # below → approved automatically
    "zone_quarantine":         0.5,   # above → quarantined
    "zone_auto_reject":        0.9,   # above → rejected immediately
    "nudenet_frames":          10,
    "nudenet_threshold":       0.1,   # kept for frame-level hit threshold
    # Quarantine
    "quarantine_dir":          "",    # empty = BASE_DIR/quarantine
    "quarantine_auto_reject_days": 0, # 0 = never auto-reject, >0 = reject after N days
    "delete_on_reject":        False, # False = safe quarantine-only reject mode
    "arr_blocklist_on_reject": True,  # mark the original grab as failed
    "arr_search_after_reject": True,  # trigger a manual search if not auto-queued
    # Webhook
    "webhook_url":             "",
    "webhook_on_review":       False,
    "webhook_on_quarantine":   True,
    "webhook_on_reject":       True,
    "web_ui_url":              "",   # base URL used to build deep-links in webhook payloads
    # VCS
    "vcs_grid":                "4x4",
    "vcsi_timeout_seconds":    300,
    # Web UI auth (SS_TOKEN env overrides; SS_PASSWORD_HASH declarative override)
    "auth_token":              "",
    "auth_password_hash":      "",
    "auth_method":             "",
    "session_secret":          "",
    # Sessions: session_epoch bumps sign out every device; remember_me_days
    # controls the "Remember me" lifetime (0 disables it). config.json-only —
    # no env overrides; changes take effect after a server restart.
    "session_epoch":           0,
    "remember_me_days":        30,
}

# Env vars that override the corresponding config key for secrets.
_ENV_SECRET_OVERRIDES = (
    ("sonarr_api_key", "SONARR_API_KEY"),
    ("radarr_api_key", "RADARR_API_KEY"),
    ("auth_password_hash", "SS_PASSWORD_HASH"),
)


def _base_dir() -> str:
    return os.environ.get("BASE_DIR", "/opt/safescanarr/data")


def _output_dir() -> str:
    return os.path.join(_base_dir(), "vcs")


def _config_path() -> str:
    return os.path.join(_base_dir(), "config.json")


def _secure(path: str) -> None:
    """Best-effort chmod 0600 on a file that may hold secrets."""
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        log.warning("Could not set 0600 on %s: %s", path, e)


def _apply_env_secrets(cfg: dict) -> dict:
    """Environment secret overrides take precedence over config.json."""
    for key, env in _ENV_SECRET_OVERRIDES:
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return cfg


# Set when a default-key self-heal write has failed on this filesystem
# (read-only / lockless install). Reads then merge defaults in memory
# without retrying the write on every auth read; a restart re-tries once.
_defaults_persist_broken = False


def _load() -> dict:
    """Load config.json, creating it from env/defaults if missing."""
    global _defaults_persist_broken
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
        # Self-heal permissions on existing installs (may predate 0600 writes)
        _secure(path)
        with open(path) as f:
            data = json.load(f)
        # Remove deprecated keys
        for old_key in ("output_dir", "scan_mode", "nudenet_enabled"):
            data.pop(old_key, None)
        # Fill in any new keys: a LOCKED read-modify-write that merges only
        # the defaults STILL MISSING from the freshly-read locked state, so
        # a concurrent config update can never be lost to a stale
        # whole-file snapshot (the missing set is recomputed under the
        # lock, never captured before it).
        missing = {k: v for k, v in _DEFAULTS.items() if k not in data}
        if missing:
            import config_lock
            if config_lock.held_by_current_thread():
                # Re-entrant call from inside mutate(): the read above was
                # made under the already-held lock, so writing this state
                # back cannot lose a concurrent update.
                data.update(missing)
                _write(data)
                _defaults_persist_broken = False
            elif _defaults_persist_broken:
                data.update(missing)
            else:
                try:
                    def _apply_defaults(c):
                        # setdefault semantics on the FRESH locked dict: a
                        # writer that supplied the key while we waited for
                        # the lock keeps its value.
                        for k, v in _DEFAULTS.items():
                            c.setdefault(k, v)
                        return c
                    data = config_lock.mutate(_apply_defaults)
                    _defaults_persist_broken = False
                except (config_lock.LockValidationError, OSError) as e:
                    # Read-only or lockless install: keep serving (defaults
                    # merged in memory) rather than failing every auth
                    # read. Persistent writes are NOT bypassed — the
                    # on-disk file stays as-is until BASE_DIR is writable
                    # or the lock file is repaired.
                    _defaults_persist_broken = True
                    data.update(missing)
                    log.warning("Could not persist default config keys "
                                "(%s); merged in memory only.", e)
        return _apply_env_secrets(data)

    # First run — seed from env vars
    cfg = {
        "watch_folders": [
            p.strip() for p in os.environ.get("WATCH_FOLDERS", "").split(",") if p.strip()
        ],
        "sonarr_url":            os.environ.get("SONARR_URL",    _DEFAULTS["sonarr_url"]),
        "sonarr_api_key":        os.environ.get("SONARR_API_KEY", _DEFAULTS["sonarr_api_key"]),
        "radarr_url":            os.environ.get("RADARR_URL",    _DEFAULTS["radarr_url"]),
        "radarr_api_key":        os.environ.get("RADARR_API_KEY", _DEFAULTS["radarr_api_key"]),
        "polling_enabled":       False,
        "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL_SECONDS", 600)),
        "scan_schedule_enabled": False,
        "scan_schedule":         "daily",
        "detection_profile":     "balanced",
        "zone_auto_approve":     0.4,
        "zone_quarantine":       0.5,
        "zone_auto_reject":      0.9,
        "nudenet_frames":        10,
        "nudenet_threshold":     0.1,
        "quarantine_dir":        "",
        "quarantine_auto_reject_days": 0,
        "delete_on_reject":      False,
        "arr_blocklist_on_reject": True,
        "arr_search_after_reject": True,
        "webhook_url":           "",
        "webhook_on_review":     False,
        "webhook_on_quarantine": True,
        "webhook_on_reject":     True,
        "web_ui_url":            os.environ.get("WEB_UI_URL", ""),
        "vcs_grid":              os.environ.get("VCS_GRID", _DEFAULTS["vcs_grid"]),
        "vcs_quality":           80,
        "vcsi_timeout_seconds":  int(os.environ.get("VCSI_TIMEOUT_SECONDS", 300)),
        "auth_token":            "",
        "auth_password_hash":    "",
        "auth_method":             "",
        "session_secret":          "",
        "session_epoch":           0,
        "remember_me_days":        30,
    }
    _bootstrap_save(cfg)
    if os.path.exists(_config_path()):
        # Another writer won the first-run race while we were seeding:
        # return THEIR persisted config (epoch, credentials and all), not
        # our losing first-run defaults. A winner we cannot READ is never
        # answered with our never-persisted defaults: propagate the error
        # (auth treats an unreadable config as epoch-unavailable and fails
        # closed) instead of resurrecting losing credentials as if real.
        with open(_config_path()) as f:
            cfg = json.load(f)
    return _apply_env_secrets(cfg)


def _write(cfg: dict) -> None:
    """Plain atomic config.json write (tmp + os.replace + 0600).

    Caller must either hold the config lock or be bootstrap. No epoch clamp:
    that is save()'s job.
    """
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for old_key in ("output_dir", "scan_mode", "nudenet_enabled"):
        cfg.pop(old_key, None)
    data = json.dumps(cfg, indent=2).encode("utf-8")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    # os.open mode is only honoured on creation; enforce on every write.
    _secure(path)


def save(cfg: dict) -> None:
    """Persist config.json under the cross-process config lock.

    This is the ONLY write path (besides first-run _bootstrap_save): the
    lock serialises read-modify-write cycles with credential changes /
    session revocation, and ``session_epoch`` is clamped back up to the
    on-disk value, so a stale snapshot can never roll the generation back
    and revive revoked sessions. Re-entrant: safe to call from inside
    config_lock.mutate().
    """
    import config_lock
    with config_lock._FileLock():
        _save_held(cfg)


def _save_held(cfg: dict) -> None:
    """Write cfg with the config lock already held by this thread.

    Enforces the session_epoch monotonic clamp against the on-disk value.
    """
    disk_epoch = 0
    try:
        with open(_config_path(), "rb") as f:
            disk_epoch = int(json.load(f).get("session_epoch") or 0)
    except (OSError, ValueError):
        pass  # no file yet / unreadable: this write is the recovery
    try:
        epoch = int(cfg.get("session_epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0
    if epoch < disk_epoch:
        cfg["session_epoch"] = disk_epoch
    _write(cfg)


def _bootstrap_save(cfg: dict) -> None:
    """First-run seeding ONLY (no config.json exists yet).

    Runs under the config lock with absence rechecked there: two processes
    racing to first-run-initialize can never overwrite a config another
    writer has already created, and an existing config's epoch is never
    replaced with first-run defaults.
    """
    import config_lock

    if config_lock.held_by_current_thread():
        # Re-entrant path (lock already held): recheck absence right here,
        # then write. No unlocked window between check and write.
        if os.path.exists(_config_path()):
            return
        _save_held(cfg)
        return

    with config_lock._FileLock():
        if os.path.exists(_config_path()):
            return  # another writer won the race; leave theirs in place
        _save_held(cfg)


def raw() -> dict:
    """Return the raw stored config (plus env secret overrides), no derived keys."""
    return _load()


def get() -> dict:
    """Return current config as a plain dict, including derived fields."""
    cfg = _load()
    cfg["output_dir"]     = _output_dir()
    cfg["quarantine_dir"] = _resolve_quarantine_dir(cfg)
    return cfg


def _resolve_quarantine_dir(cfg: dict) -> str:
    d = cfg.get("quarantine_dir", "")
    return d if d else os.path.join(_base_dir(), "quarantine")


class Config:
    def __init__(self):
        cfg = _load()
        self.WATCH_FOLDERS           = cfg["watch_folders"]
        self.OUTPUT_DIR              = _output_dir()
        self.SONARR_URL              = cfg["sonarr_url"]
        self.SONARR_API_KEY          = cfg["sonarr_api_key"]
        self.RADARR_URL              = cfg["radarr_url"]
        self.RADARR_API_KEY          = cfg["radarr_api_key"]
        self.POLLING_ENABLED         = cfg.get("polling_enabled", False)
        self.POLL_INTERVAL_SECONDS   = cfg["poll_interval_seconds"]
        self.SCAN_SCHEDULE_ENABLED   = cfg.get("scan_schedule_enabled", False)
        self.SCAN_SCHEDULE           = cfg.get("scan_schedule", "daily")
        # Zones
        self.ZONE_AUTO_APPROVE       = float(cfg.get("zone_auto_approve", 0.1))
        self.ZONE_QUARANTINE         = float(cfg.get("zone_quarantine", 0.4))
        self.ZONE_AUTO_REJECT        = float(cfg.get("zone_auto_reject", 0.85))
        # NudeNet
        self.NUDENET_FRAMES          = int(cfg.get("nudenet_frames", 10))
        self.NUDENET_THRESHOLD       = float(cfg.get("nudenet_threshold", 0.1))
        # Quarantine
        self.QUARANTINE_DIR          = _resolve_quarantine_dir(cfg)
        self.QUARANTINE_AUTO_REJECT_DAYS = int(cfg.get("quarantine_auto_reject_days", 0))
        # Safe-by-default reject behaviour: move to quarantine unless explicitly
        # opted in to permanent deletion.
        self.DELETE_ON_REJECT        = bool(cfg.get("delete_on_reject", False))
        # Arr reject flow
        self.ARR_BLOCKLIST_ON_REJECT = bool(cfg.get("arr_blocklist_on_reject", True))
        self.ARR_SEARCH_AFTER_REJECT = bool(cfg.get("arr_search_after_reject", True))
        # Webhook
        self.WEBHOOK_URL             = cfg.get("webhook_url", "")
        self.WEBHOOK_ON_REVIEW       = cfg.get("webhook_on_review", False)
        self.WEBHOOK_ON_QUARANTINE   = cfg.get("webhook_on_quarantine", True)
        self.WEBHOOK_ON_REJECT       = cfg.get("webhook_on_reject", True)
        self.WEB_UI_URL              = cfg.get("web_ui_url", "")
        # VCS
        self.VCS_GRID                = cfg["vcs_grid"]
        self.VCSI_EXTRA_ARGS: list[str] = []
        self.VCSI_TIMEOUT_SECONDS    = cfg["vcsi_timeout_seconds"]
        # Auth
        self.AUTH_TOKEN              = os.environ.get("SS_TOKEN") or cfg.get("auth_token", "")
        # System
        self.BASE_DIR                = _base_dir()
        self.DB_FILE                 = os.path.join(self.BASE_DIR, "safescanarr.db")
        self.LOG_FILE                = os.path.join(self.BASE_DIR, "safescanarr.log")
        # Bind to loopback by default; containers/LAN deployments opt in via
        # WEB_HOST=0.0.0.0 (docker-compose sets this).
        self.WEB_HOST                = os.environ.get("WEB_HOST", "127.0.0.1")
        self.WEB_PORT                = int(os.environ.get("WEB_PORT", 8666))

    @staticmethod
    def version() -> str:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "VERSION")) as f:
                return f.read().strip()
        except FileNotFoundError:
            return "unknown"
