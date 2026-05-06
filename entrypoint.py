#!/usr/bin/env python3
"""
entrypoint.py — starts poller, midnight scheduler, and web server.
"""

import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime

sys.path.insert(0, "/opt/safescanarr")
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

SCANNER = "/opt/safescanarr/scanner.py"


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


def run_midnight_scheduler():
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
                last_run_hour = now.hour
                log.info("=== Scheduled scan triggered (schedule=%s, hour=%d) ===",
                         schedule, now.hour)
                import subprocess
                subprocess.run([sys.executable, SCANNER, "--scan"], check=False)

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
    from web.server import app
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
