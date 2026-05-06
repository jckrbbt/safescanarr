"""
safescanarr/config.py

Config priority (highest to lowest):
  1. config.json in the data directory (written by the UI)
  2. Environment variables
  3. Hardcoded defaults

On first run, if config.json doesn't exist, it is created from env vars / defaults.
"""

import json
import os

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
    "zone_auto_approve":       0.2,   # below → approved automatically
    "zone_quarantine":         0.5,   # above → quarantined
    "zone_auto_reject":        0.9,   # above → rejected immediately
    "nudenet_frames":          10,
    "nudenet_threshold":       0.1,   # kept for frame-level hit threshold
    # Quarantine
    "quarantine_dir":          "",    # empty = BASE_DIR/quarantine
    "quarantine_auto_reject_days": 0, # 0 = never auto-reject, >0 = reject after N days
    # Webhook
    "webhook_url":             "",
    "webhook_on_quarantine":   True,
    "webhook_on_reject":       True,
    # VCS
    "vcs_grid":                "4x4",
    "vcsi_timeout_seconds":    300,
}


def _base_dir() -> str:
    return os.environ.get("BASE_DIR", "/opt/safescanarr/data")


def _output_dir() -> str:
    return os.path.join(_base_dir(), "vcs")


def _config_path() -> str:
    return os.path.join(_base_dir(), "config.json")


def _load() -> dict:
    """Load config.json, creating it from env/defaults if missing."""
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
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
        return data

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
        "zone_auto_approve":     0.2,
        "zone_quarantine":       0.5,
        "zone_auto_reject":      0.9,
        "nudenet_frames":        10,
        "nudenet_threshold":     0.1,
        "quarantine_dir":        "",
        "quarantine_auto_reject_days": 0,
        "webhook_url":           "",
        "webhook_on_quarantine": True,
        "webhook_on_reject":     True,
        "vcs_grid":              os.environ.get("VCS_GRID", _DEFAULTS["vcs_grid"]),
        "vcs_quality":           80,
        "vcsi_timeout_seconds":  int(os.environ.get("VCSI_TIMEOUT_SECONDS", 300)),
    }
    save(cfg)
    return cfg


def save(cfg: dict) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for old_key in ("output_dir", "scan_mode", "nudenet_enabled"):
        cfg.pop(old_key, None)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


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
        # Webhook
        self.WEBHOOK_URL             = cfg.get("webhook_url", "")
        self.WEBHOOK_ON_QUARANTINE   = cfg.get("webhook_on_quarantine", True)
        self.WEBHOOK_ON_REJECT       = cfg.get("webhook_on_reject", True)
        # VCS
        self.VCS_GRID                = cfg["vcs_grid"]
        self.VCSI_EXTRA_ARGS: list[str] = []
        self.VCSI_TIMEOUT_SECONDS    = cfg["vcsi_timeout_seconds"]
        # System
        self.BASE_DIR                = _base_dir()
        self.DB_FILE                 = os.path.join(self.BASE_DIR, "safescanarr.db")
        self.LOG_FILE                = os.path.join(self.BASE_DIR, "safescanarr.log")
        self.WEB_HOST                = os.environ.get("WEB_HOST", "0.0.0.0")
        self.WEB_PORT                = int(os.environ.get("WEB_PORT", 8686))

    @staticmethod
    def version() -> str:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "VERSION")) as f:
                return f.read().strip()
        except FileNotFoundError:
            return "unknown"
