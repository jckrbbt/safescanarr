#!/usr/bin/env python3
"""
safescanarr/poller.py
Polls Sonarr and Radarr APIs for recently imported files.
"""

import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/opt/safescanarr")
from config import Config
from database import Database

log = logging.getLogger(__name__)

SCANNER = "/opt/safescanarr/scanner.py"


def is_in_watched_folder(path: str, cfg: Config) -> bool:
    return any(path.startswith(f) for f in cfg.WATCH_FOLDERS)


def trigger_scanner(file_path: str, source: str, cfg: Config) -> None:
    log.info("[%s] Triggering scanner for: %s", source, file_path)
    subprocess.Popen(
        [sys.executable, SCANNER, "--file", file_path, "--source", source],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )


def api_get(url: str, api_key: str):
    req = urllib.request.Request(
        url,
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        log.warning("Could not reach %s: %s", url, e.reason)
        return None
    except Exception as e:
        log.warning("Unexpected error calling %s: %s", url, e)
        return None


def poll_sonarr(db: Database) -> None:
    cfg = Config()
    if not cfg.SONARR_API_KEY:
        return

    base = cfg.SONARR_URL.rstrip("/")
    url  = f"{base}/api/v3/history?pageSize=50&sortKey=date&sortDirection=descending&eventType=3"
    data = api_get(url, cfg.SONARR_API_KEY)
    if data is None:
        return

    records      = data.get("records", [])
    last_seen_id = db.get_poller_state("sonarr_last_id")
    new_last_id  = None
    processed    = 0

    for record in records:
        record_id    = record.get("id", 0)
        episode_file = record.get("episodeFile") or {}
        file_path    = episode_file.get("path") or record.get("sourceTitle", "")

        if not file_path:
            continue

        if new_last_id is None or record_id > new_last_id:
            new_last_id = record_id

        if last_seen_id and record_id <= int(last_seen_id):
            break

        if not is_in_watched_folder(file_path, cfg):
            log.debug("[sonarr] Not in watched folder, skipping: %s", file_path)
            continue

        trigger_scanner(file_path, "sonarr", cfg)
        processed += 1

    if new_last_id:
        db.set_poller_state("sonarr_last_id", str(new_last_id))

    if processed:
        log.info("[sonarr] Triggered scanner for %d new import(s)", processed)


def poll_radarr(db: Database) -> None:
    cfg = Config()
    if not cfg.RADARR_API_KEY:
        return

    base = cfg.RADARR_URL.rstrip("/")
    url  = f"{base}/api/v3/history?pageSize=50&sortKey=date&sortDirection=descending&eventType=3"
    data = api_get(url, cfg.RADARR_API_KEY)
    if data is None:
        return

    records      = data.get("records", [])
    last_seen_id = db.get_poller_state("radarr_last_id")
    new_last_id  = None
    processed    = 0

    for record in records:
        record_id  = record.get("id", 0)
        movie_file = record.get("movieFile") or {}
        file_path  = movie_file.get("path") or record.get("sourceTitle", "")

        if not file_path:
            continue

        if new_last_id is None or record_id > new_last_id:
            new_last_id = record_id

        if last_seen_id and record_id <= int(last_seen_id):
            break

        if not is_in_watched_folder(file_path, cfg):
            log.debug("[radarr] Not in watched folder, skipping: %s", file_path)
            continue

        trigger_scanner(file_path, "radarr", cfg)
        processed += 1

    if new_last_id:
        db.set_poller_state("radarr_last_id", str(new_last_id))

    if processed:
        log.info("[radarr] Triggered scanner for %d new import(s)", processed)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    cfg = Config()
    log.info("Safe Scanarr poller starting — interval: %ds", cfg.POLL_INTERVAL_SECONDS)
    db = Database(cfg.DB_FILE)

    while True:
        cfg = Config()  # reload each cycle
        try:
            poll_sonarr(db)
        except Exception as e:
            log.error("[sonarr] Poll error: %s", e)
        try:
            poll_radarr(db)
        except Exception as e:
            log.error("[radarr] Poll error: %s", e)
        time.sleep(cfg.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
