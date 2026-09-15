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
    # Webhook
    "webhook_url":             "",
    "webhook_on_review":       False,
    "webhook_on_quarantine":   True,
    "webhook_on_reject":       True,
    "web_ui_url":              "",   # base URL used to build deep-links in webhook payloads
    # VCS
    "vcs_grid":                "4x4",
    "vcsi_timeout_seconds":    300,
    # Web UI auth (generated on first run if unset; SS_TOKEN env overrides)
    "auth_token":              "",
}

# Env vars that override the corresponding config key for secrets.
_ENV_SECRET_OVERRIDES = (
    ("sonarr_api_key", "SONARR_API_KEY"),
    ("radarr_api_key", "RADARR_API_KEY"),
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


def _load() -> dict:
    """Load config.json, creating it from env/defaults if missing."""
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
        # Fill in any new keys
        changed = False
        for k, v in _DEFAULTS.items():
            if k not in data:
                data[k] = v
                changed = True
        if changed:
            save(data)
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
        "webhook_url":           "",
        "webhook_on_review":     False,
        "webhook_on_quarantine": True,
        "webhook_on_reject":     True,
        "web_ui_url":            os.environ.get("WEB_UI_URL", ""),
        "vcs_grid":              os.environ.get("VCS_GRID", _DEFAULTS["vcs_grid"]),
        "vcs_quality":           80,
        "vcsi_timeout_seconds":  int(os.environ.get("VCSI_TIMEOUT_SECONDS", 300)),
        "auth_token":            "",
    }
    save(cfg)
    return _apply_env_secrets(cfg)


def save(cfg: dict) -> None:
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
