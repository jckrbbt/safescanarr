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
    "watch_folders":          [],
    "sonarr_url":             "http://localhost:8989",
    "sonarr_api_key":         "",
    "radarr_url":             "http://localhost:7878",
    "radarr_api_key":         "",
    "polling_enabled":        False,
    "poll_interval_seconds":  600,
    "scan_mode":              "review",   # "review" or "safe"
    "nudenet_threshold":      0.6,
    "nudenet_frames":         10,
    "scan_schedule_enabled":  False,
    "scan_schedule":          "daily",   # daily, twice, quad, hourly
    "vcs_grid":               "4x4",
    "vcsi_timeout_seconds":   300,
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
        data = {}
        with open(path) as f:
            data = json.load(f)
        # Migrate: remove output_dir if present (now hardcoded)
        data.pop("output_dir", None)
        # Fill in any new keys added since last save
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
        "sonarr_url":            os.environ.get("SONARR_URL",            _DEFAULTS["sonarr_url"]),
        "sonarr_api_key":        os.environ.get("SONARR_API_KEY",        _DEFAULTS["sonarr_api_key"]),
        "radarr_url":            os.environ.get("RADARR_URL",            _DEFAULTS["radarr_url"]),
        "radarr_api_key":        os.environ.get("RADARR_API_KEY",        _DEFAULTS["radarr_api_key"]),
        "polling_enabled":       False,
        "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL_SECONDS", _DEFAULTS["poll_interval_seconds"])),
        "scan_mode":             "review",
        "nudenet_threshold":     0.6,
        "nudenet_frames":        10,
        "scan_schedule_enabled": False,
        "scan_schedule":         "daily",
        "vcs_grid":              os.environ.get("VCS_GRID",              _DEFAULTS["vcs_grid"]),
        "vcsi_timeout_seconds":  int(os.environ.get("VCSI_TIMEOUT_SECONDS", _DEFAULTS["vcsi_timeout_seconds"])),
    }
    save(cfg)
    return cfg


def save(cfg: dict) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Never persist output_dir — it's always derived from BASE_DIR
    cfg.pop("output_dir", None)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def get() -> dict:
    """Return current config as a plain dict, including derived fields."""
    cfg = _load()
    cfg["output_dir"] = _output_dir()   # add for UI/API consumers
    return cfg


class Config:
    """
    Attribute-style access. Re-reads config.json every time this class
    is instantiated so changes from the UI take effect immediately.
    """
    def __init__(self):
        cfg = _load()
        self.WATCH_FOLDERS         = cfg["watch_folders"]
        self.OUTPUT_DIR            = _output_dir()
        self.SONARR_URL            = cfg["sonarr_url"]
        self.SONARR_API_KEY        = cfg["sonarr_api_key"]
        self.RADARR_URL            = cfg["radarr_url"]
        self.RADARR_API_KEY        = cfg["radarr_api_key"]
        self.POLLING_ENABLED       = cfg.get("polling_enabled", False)
        self.POLL_INTERVAL_SECONDS = cfg["poll_interval_seconds"]
        self.SCAN_MODE             = cfg.get("scan_mode", "review")
        self.NUDENET_THRESHOLD     = float(cfg.get("nudenet_threshold", 0.6))
        self.NUDENET_FRAMES        = int(cfg.get("nudenet_frames", 10))
        self.SCAN_SCHEDULE_ENABLED = cfg.get("scan_schedule_enabled", False)
        self.SCAN_SCHEDULE         = cfg.get("scan_schedule", "daily")
        self.VCS_GRID              = cfg["vcs_grid"]
        self.VCSI_TIMEOUT_SECONDS  = cfg["vcsi_timeout_seconds"]
        self.BASE_DIR              = _base_dir()
        self.DB_FILE               = os.path.join(self.BASE_DIR, "safescanarr.db")
        self.LOG_FILE              = os.path.join(self.BASE_DIR, "safescanarr.log")
        self.WEB_HOST              = os.environ.get("WEB_HOST", "0.0.0.0")
        self.WEB_PORT              = int(os.environ.get("WEB_PORT", 8686))
        self.VCSI_EXTRA_ARGS: list[str] = []

    @staticmethod
    def version() -> str:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "VERSION")) as f:
                return f.read().strip()
        except FileNotFoundError:
            return "unknown"
