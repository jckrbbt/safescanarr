#!/usr/bin/env python3
"""
entrypoint.py — starts poller, midnight scheduler, and web server.
"""

import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# Derive the app directory from this file so the code works from any checkout
# location (and keeps working for existing /opt/safescanarr installs).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from config import Config

# Configure root logger once — all modules inherit this
root_log = logging.getLogger()
if not root_log.handlers:
    root_log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root_log.addHandler(sh)
log = logging.getLogger(__name__)

SCANNER = str(_APP_DIR / "scanner.py")


def run_poller():
    from poller import poll_sonarr, poll_radarr
    from database import Database

    log.info("Poller thread started")
    while True:
        try:
            cfg = Config()
            db  = Database(cfg.DB_FILE)
            log.info("Poller cycle — sonarr_key_set:%s radarr_key_set:%s interval:%ds",
                     bool(cfg.SONARR_API_KEY), bool(cfg.RADARR_API_KEY),
                     cfg.POLL_INTERVAL_SECONDS)
            poll_sonarr(db)
            poll_radarr(db)
            log.info("Poller cycle complete — sleeping %ds", cfg.POLL_INTERVAL_SECONDS)
            time.sleep(cfg.POLL_INTERVAL_SECONDS)
        except Exception as e:
            log.error("Poller error: %s\n%s", e, traceback.format_exc())
            time.sleep(30)


# Scan hours for each schedule option
SCHEDULE_HOURS = {
    "daily":  [0],
    "twice":  [0, 12],
    "quad":   [0, 6, 12, 18],
    "hourly": list(range(24)),
}

# Guards against a scheduled scan being triggered twice concurrently.
_scan_lock: "threading.Lock" = threading.Lock()
_scan_proc = None


def run_midnight_scheduler():
    global _scan_proc
    log.info("Scan scheduler thread started")
    last_run_hour = -1
    while True:
        try:
            cfg      = Config()
            if not cfg.SCAN_SCHEDULE_ENABLED:
                time.sleep(60)
                continue
            schedule = cfg.SCAN_SCHEDULE
            hours    = SCHEDULE_HOURS.get(schedule, [0])
            now      = datetime.now()

            if now.hour in hours and now.hour != last_run_hour:
                # Mark the hour as handled first so a still-running scan cannot
                # cause a second trigger within the same hour.
                last_run_hour = now.hour
                proc = _scan_proc
                if proc is not None and proc.poll() is None:
                    log.info("Scheduled scan skipped — a scan is still running (pid %s)",
                             proc.pid)
                elif not _scan_lock.acquire(blocking=False):
                    log.info("Scheduled scan skipped — another trigger is already starting")
                else:
                    try:
                        # Non-blocking: a long scan must not stall this loop.
                        _scan_proc = subprocess.Popen(
                            [sys.executable, SCANNER, "--scan"],
                            stdout=open(cfg.LOG_FILE, "a"),
                            stderr=subprocess.STDOUT,
                        )
                        log.info("=== Scheduled scan started (schedule=%s, hour=%d, pid=%d) ===",
                                 schedule, now.hour, _scan_proc.pid)
                    except Exception as e:
                        log.error("Could not start scheduled scan: %s", e)
                    finally:
                        _scan_lock.release()

            # Find next scheduled hour for logging
            future = [h for h in hours if h > now.hour]
            next_h = future[0] if future else hours[0]
            if next_h <= now.hour:
                mins_to_next = (24 - now.hour + next_h) * 60 - now.minute
            else:
                mins_to_next = (next_h - now.hour) * 60 - now.minute
            log.debug("Next scheduled scan in ~%d minutes (hour %d)", mins_to_next, next_h)

        except Exception as e:
            log.error("Scheduler error: %s", e)

        time.sleep(60)  # check every minute


def run_web():
    from web.server import app, init_db
    init_db()   # create/migrate schema once, at startup
    cfg = Config()
    log.info("Web UI starting on %s:%d", cfg.WEB_HOST, cfg.WEB_PORT)
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT, threaded=True, use_reloader=False, debug=False)


if __name__ == "__main__":
    cfg = Config()
    os.makedirs(cfg.BASE_DIR,   exist_ok=True)
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)

    for target, name in [
        (run_poller,            "poller"),
        (run_midnight_scheduler,"scheduler"),
    ]:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()

    run_web()
