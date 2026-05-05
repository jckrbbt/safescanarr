#!/usr/bin/env python3
"""
safescanarr/scanner.py
Scans watched folders for new or changed video files, generates video contact
sheets via vcsi, runs NudeNet analysis, and records everything in a SQLite DB.

Modes:
  --scan          Walk all watched folders (nightly scan)
  --file PATH     Process a single specific file (poller/hook mode)
  --list          Print all tracked files
  --reset PATH    Remove a file from the DB so it gets re-processed
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/opt/safescanarr")
from config import Config as _ConfigClass
from database import Database

# ---------------------------------------------------------------------------
# Logging — re-read config each run so log path is always current
# ---------------------------------------------------------------------------
_cfg_for_log = _ConfigClass()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_cfg_for_log.LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Video file extensions
# ---------------------------------------------------------------------------
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v",
    ".flv", ".webm", ".ts", ".mpg", ".mpeg",
}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def file_fingerprint(path: Path) -> tuple:
    stat = path.stat()
    return path.name, stat.st_size, stat.st_mtime


def scan_folder(folder: Path) -> list:
    videos = []
    if not folder.exists():
        log.warning("Watched folder does not exist: %s", folder)
        return videos
    for f in folder.rglob("*"):
        if f.is_file() and is_video(f):
            videos.append(f)
    return videos


def get_vcsi_bin() -> str:
    venv_bin = Path(sys.executable).parent
    vcsi_bin = venv_bin / "vcsi"
    if not vcsi_bin.exists():
        log.critical(
            "vcsi not found at %s — install it with: pip install vcsi", vcsi_bin
        )
        sys.exit(1)
    return str(vcsi_bin)


# ---------------------------------------------------------------------------
# VCS generation
# ---------------------------------------------------------------------------

def generate_vcs(video_path: Path, output_dir: Path, cfg, db: Database) -> bool:
    """Generate a contact sheet. Returns True on success."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / (video_path.stem + ".jpg")

    cmd = [
        get_vcsi_bin(),
        str(video_path),
        "-t",
        "-g", cfg.VCS_GRID,
        "-o", str(out_file),
    ] + cfg.VCSI_EXTRA_ARGS

    log.info("Generating contact sheet: %s → %s", video_path.name, out_file)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=cfg.VCSI_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            log.error("vcsi failed for %s:\n%s", video_path, result.stderr)
            db.record_error(str(video_path), result.stderr)
            return False
        log.info("Contact sheet saved: %s", out_file)
        return True
    except subprocess.TimeoutExpired:
        log.error("vcsi timed out for %s", video_path)
        db.record_error(str(video_path), "vcsi timeout")
        return False


# ---------------------------------------------------------------------------
# NudeNet analysis
# ---------------------------------------------------------------------------

def analyse_sheet(sheet_path: Path, cfg) -> dict:
    """
    Run NudeNet on *sheet_path* if enabled in config.
    Returns {"flagged": bool, "labels": [...], "max_conf": float, "error": str|None}
    """
    try:
        from nudenet_scanner import analyse
        return analyse(str(sheet_path), threshold=cfg.NUDENET_THRESHOLD)
    except Exception as e:
        log.error("NudeNet analysis error: %s", e)
        return {"flagged": False, "labels": [], "max_conf": 0.0, "error": str(e)}


def handle_flagged(video_path: Path, sheet_path: Path,
                   nudenet_result: dict, cfg, db: Database) -> None:
    """
    Handle a flagged file according to the current scan mode.
    Review Mode: mark flagged in DB, leave sheet in review queue.
    Safe Mode:   delete source, blacklist in Sonarr/Radarr,
                 move sheet to vcs/auto/, record in auto_handled.
    """
    abs_path   = str(video_path.resolve())
    reason_str = ", ".join(
        f"{h['label']} ({h['confidence']:.0%})" for h in nudenet_result["labels"]
    )
    log.warning("FLAGGED [%s] %s — %s", cfg.SCAN_MODE, abs_path, reason_str)

    if cfg.SCAN_MODE == "safe":
        # Move sheet to auto subfolder before deleting source
        auto_dir = Path(cfg.OUTPUT_DIR) / "auto"
        auto_dir.mkdir(parents=True, exist_ok=True)
        auto_sheet = auto_dir / sheet_path.name
        if sheet_path.exists():
            shutil.move(str(sheet_path), str(auto_sheet))

        # Blacklist in Sonarr and Radarr
        _blacklist(abs_path, cfg)

        # Delete source file
        if video_path.exists():
            video_path.unlink()
            log.info("Safe Mode: deleted source %s", abs_path)

        # Record in audit table
        db.record_auto_handled(
            path       = abs_path,
            name       = video_path.name,
            sheet_name = sheet_path.name,
            reason     = reason_str,
            confidence = nudenet_result["max_conf"],
        )
        # Remove from files table
        db.delete_file(abs_path)

    else:
        # Review Mode — just flag in DB so UI can highlight it
        log.info("Review Mode: flagged in DB, queued for manual review")


def _blacklist(file_path: str, cfg) -> None:
    """Try to blacklist in both Sonarr and Radarr (best effort)."""
    for service in ("sonarr", "radarr"):
        base    = cfg.SONARR_URL.rstrip("/") if service == "sonarr" else cfg.RADARR_URL.rstrip("/")
        api_key = cfg.SONARR_API_KEY        if service == "sonarr" else cfg.RADARR_API_KEY
        if not api_key:
            continue
        try:
            url = f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3"
            req = urllib.request.Request(url, headers={"X-Api-Key": api_key, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            history_id = None
            for record in data.get("records", []):
                path = (record.get("data") or {}).get("importedPath", "")
                if path == file_path:
                    history_id = record.get("id")
                    break
            if history_id:
                req2 = urllib.request.Request(
                    f"{base}/api/v3/blacklist/{history_id}",
                    method="DELETE",
                    headers={"X-Api-Key": api_key},
                )
                urllib.request.urlopen(req2, timeout=10)
                log.info("Blacklisted in %s (history id %s)", service, history_id)
        except Exception as e:
            log.warning("Blacklist failed for %s/%s: %s", service, file_path, e)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_one(video: Path, db: Database, source: str = "manual") -> None:
    """Process a single video file."""
    cfg = _ConfigClass()  # fresh config each call

    if not video.exists():
        log.error("[%s] File not found: %s", source, video)
        return
    if not is_video(video):
        log.warning("[%s] Not a recognised video file, skipping: %s", source, video)
        return

    name, size, mtime = file_fingerprint(video)
    abs_path = str(video.resolve())
    existing = db.get_file(abs_path)

    def _do_process(label: str):
        log.info("[%s] %s  %s", source, label, abs_path)
        output_dir = Path(cfg.OUTPUT_DIR)
        ok = generate_vcs(video, output_dir, cfg, db)
        if not ok:
            db.upsert_file(abs_path, name, size, mtime, status="error")
            return

        sheet_path    = output_dir / (video.stem + ".jpg")
        nudenet_result = analyse_sheet(sheet_path, cfg)
        flagged       = nudenet_result["flagged"]
        flag_reason   = (
            ", ".join(h["label"] for h in nudenet_result["labels"])
            if flagged else None
        )

        if flagged:
            handle_flagged(video, sheet_path, nudenet_result, cfg, db)
            if cfg.SCAN_MODE == "safe":
                return  # source deleted, no DB record needed

        db.upsert_file(
            abs_path, name, size, mtime,
            status="ok",
            flagged=flagged,
            flag_reason=flag_reason,
        )

    if existing is None:
        _do_process("NEW")
    else:
        stored_size   = existing["size"]
        stored_mtime  = existing["mtime"]
        stored_status = existing["status"]
        changed = size != stored_size or abs(mtime - stored_mtime) > 1

        if changed:
            _do_process("CHANGED")
        elif stored_status == "error":
            _do_process("RETRY")
        else:
            log.debug("[%s] UNCHANGED, skipping: %s", source, abs_path)


def run_scan(db: Database) -> None:
    """Full folder scan."""
    cfg = _ConfigClass()
    log.info("=== Full scan started ===")
    counts = {"new": 0, "changed": 0, "retried": 0, "skipped": 0, "error": 0}

    for folder in cfg.WATCH_FOLDERS:
        folder = Path(folder)
        log.info("Scanning folder: %s", folder)
        for video in scan_folder(folder):
            name, size, mtime = file_fingerprint(video)
            abs_path = str(video.resolve())
            existing = db.get_file(abs_path)

            if existing is None:
                process_one(video, db, "scan")
                counts["new"] += 1
            else:
                changed = (size != existing["size"] or
                           abs(mtime - existing["mtime"]) > 1)
                if changed:
                    process_one(video, db, "scan")
                    counts["changed"] += 1
                elif existing["status"] == "error":
                    process_one(video, db, "scan")
                    counts["retried"] += 1
                else:
                    counts["skipped"] += 1

    log.info(
        "=== Full scan complete — new:%d changed:%d retried:%d skipped:%d ===",
        counts["new"], counts["changed"], counts["retried"], counts["skipped"],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="safescanarr — scanner")
    parser.add_argument("--scan",   action="store_true", help="Full folder scan")
    parser.add_argument("--file",   metavar="PATH",      help="Process one file")
    parser.add_argument("--source", metavar="LABEL",     default="manual")
    parser.add_argument("--list",   action="store_true", help="List tracked files")
    parser.add_argument("--reset",  metavar="PATH",      help="Remove file from DB")
    args = parser.parse_args()

    cfg = _ConfigClass()
    db  = Database(cfg.DB_FILE)

    if args.list:
        rows = db.list_files()
        print(f"{'Status':<8}  {'Flagged':<8}  {'Last Modified':<20}  Path")
        print("-" * 90)
        for r in rows:
            ts = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            flag = "⚠ YES" if r["flagged"] else ""
            print(f"{r['status']:<8}  {flag:<8}  {ts:<20}  {r['path']}")
        return

    if args.reset:
        db.delete_file(args.reset)
        log.info("Removed from DB: %s", args.reset)
        return

    if args.file:
        process_one(Path(args.file), db, source=args.source)
        return

    run_scan(db)


if __name__ == "__main__":
    main()
