#!/usr/bin/env python3
"""
entrypoint.py — starts poller, midnight scheduler, and web server.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, "/opt/safescanarr")
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

SCANNER = "/opt/safescanarr/scanner.py"


def run_poller():
    from poller import poll_sonarr, poll_radarr
    from database import Database

    log.info("Poller thread started")
    while True:
        cfg = Config()
        db  = Database(cfg.DB_FILE)
        try:
            poll_sonarr(db)
            poll_radarr(db)
        except Exception as e:
            log.error("Poller error: %s", e)
        # Re-read interval each cycle so UI changes take effect
        time.sleep(Config().POLL_INTERVAL_SECONDS)


def run_midnight_scheduler():
    log.info("Midnight scheduler thread started")
    while True:
        now = datetime.now()
        seconds_until_midnight = (
            (24 - now.hour - 1) * 3600
            + (60 - now.minute - 1) * 60
            + (60 - now.second)
        )
        log.info("Next full scan in %.0f minutes", seconds_until_midnight / 60)
        time.sleep(seconds_until_midnight)
        log.info("=== Midnight scan triggered ===")
        try:
            import subprocess
            subprocess.run([sys.executable, SCANNER, "--scan"], check=False)
        except Exception as e:
            log.error("Midnight scan error: %s", e)
        time.sleep(61)


def run_web():
    from web.server import app
    cfg = Config()
    log.info("Web UI starting on %s:%d", cfg.WEB_HOST, cfg.WEB_PORT)
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT, threaded=True)


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
