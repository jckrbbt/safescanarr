#!/usr/bin/env python3
"""
contactgen/scanner.py
Scans watched folders for new or changed video files, generates video contact
sheets via vcsi, and records everything in a SQLite database.

Modes:
  --scan          Walk all watched folders (used by the 8 AM systemd timer)
  --file PATH     Process a single specific file (used by Sonarr/Radarr hooks)
  --list          Print all tracked files
  --reset PATH    Remove a file from the DB so it gets re-processed
"""

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import Config as _ConfigClass
Config = _ConfigClass()
from database import Database

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Config.LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Video file extensions we care about
# ---------------------------------------------------------------------------
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v",
    ".flv", ".webm", ".ts", ".mpg", ".mpeg",
}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def file_fingerprint(path: Path) -> tuple:
    """Return (name, size_bytes, mtime) — the change-detection key."""
    stat = path.stat()
    return path.name, stat.st_size, stat.st_mtime


def scan_folder(folder: Path) -> list:
    """Recursively return video files under *folder*."""
    videos = []
    if not folder.exists():
        log.warning("Watched folder does not exist: %s", folder)
        return videos
    for f in folder.rglob("*"):
        if f.is_file() and is_video(f):
            videos.append(f)
    return videos


def get_vcsi_bin() -> str:
    """
    Return the path to the vcsi executable sitting next to this interpreter
    in the venv bin/ directory — works regardless of $PATH.
    """
    venv_bin = Path(sys.executable).parent
    vcsi_bin = venv_bin / "vcsi"
    if not vcsi_bin.exists():
        log.critical(
            "vcsi not found at %s — install it with: pip install vcsi  "
            "or see https://github.com/amietn/vcsi", vcsi_bin
        )
        sys.exit(1)
    return str(vcsi_bin)


def generate_vcs(video_path: Path, output_dir: Path, db: Database) -> bool:
    """
    Call vcsi to create a contact sheet for *video_path*.
    Returns True on success.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / (video_path.stem + ".jpg")

    cmd = [
        get_vcsi_bin(),
        str(video_path),
        "-t",
        "-g", Config.VCS_GRID,
        "-o", str(out_file),
    ] + Config.VCSI_EXTRA_ARGS

    log.info("Generating contact sheet: %s → %s", video_path.name, out_file)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=Config.VCSI_TIMEOUT_SECONDS,
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


def process_one(video: Path, db: Database, source: str = "manual") -> None:
    """
    Process a single video file — used by --file and by run_scan internally.
    *source* is just a label for the log (e.g. 'sonarr', 'radarr', 'scan').
    """
    if not video.exists():
        log.error("[%s] File not found: %s", source, video)
        return

    if not is_video(video):
        log.warning("[%s] Not a recognised video file, skipping: %s", source, video)
        return

    name, size, mtime = file_fingerprint(video)
    abs_path = str(video.resolve())
    existing = db.get_file(abs_path)

    if existing is None:
        log.info("[%s] NEW  %s", source, abs_path)
        ok = generate_vcs(video, Path(Config.OUTPUT_DIR), db)
        db.upsert_file(abs_path, name, size, mtime, status="ok" if ok else "error")

    else:
        stored_size   = existing["size"]
        stored_mtime  = existing["mtime"]
        stored_status = existing["status"]
        changed = size != stored_size or abs(mtime - stored_mtime) > 1

        if changed:
            log.info(
                "[%s] CHANGED  %s  (size %d→%d, mtime %.0f→%.0f)",
                source, abs_path, stored_size, size, stored_mtime, mtime,
            )
            ok = generate_vcs(video, Path(Config.OUTPUT_DIR), db)
            db.upsert_file(abs_path, name, size, mtime, status="ok" if ok else "error")

        elif stored_status == "error":
            log.info("[%s] RETRY (previous error)  %s", source, abs_path)
            ok = generate_vcs(video, Path(Config.OUTPUT_DIR), db)
            db.upsert_file(abs_path, name, size, mtime, status="ok" if ok else "error")

        else:
            log.info("[%s] UNCHANGED, skipping: %s", source, abs_path)


def run_scan(db: Database) -> None:
    """Full folder scan — called by the 8 AM systemd timer."""
    log.info("=== Full scan started ===")
    counts = {"new": 0, "changed": 0, "retried": 0, "skipped": 0, "error": 0}

    for folder in Config.WATCH_FOLDERS:
        folder = Path(folder)
        log.info("Scanning folder: %s", folder)

        for video in scan_folder(folder):
            name, size, mtime = file_fingerprint(video)
            abs_path = str(video.resolve())
            existing = db.get_file(abs_path)

            if existing is None:
                log.info("[scan] NEW  %s", abs_path)
                ok = generate_vcs(video, Path(Config.OUTPUT_DIR), db)
                db.upsert_file(abs_path, name, size, mtime, status="ok" if ok else "error")
                counts["new" if ok else "error"] += 1

            else:
                stored_size   = existing["size"]
                stored_mtime  = existing["mtime"]
                stored_status = existing["status"]
                changed = size != stored_size or abs(mtime - stored_mtime) > 1

                if changed:
                    log.info(
                        "[scan] CHANGED  %s  (size %d→%d, mtime %.0f→%.0f)",
                        abs_path, stored_size, size, stored_mtime, mtime,
                    )
                    ok = generate_vcs(video, Path(Config.OUTPUT_DIR), db)
                    db.upsert_file(abs_path, name, size, mtime, status="ok" if ok else "error")
                    counts["changed" if ok else "error"] += 1

                elif stored_status == "error":
                    log.info("[scan] RETRY (previous error)  %s", abs_path)
                    ok = generate_vcs(video, Path(Config.OUTPUT_DIR), db)
                    db.upsert_file(abs_path, name, size, mtime, status="ok" if ok else "error")
                    counts["retried" if ok else "error"] += 1

                else:
                    counts["skipped"] += 1

    log.info(
        "=== Full scan complete — new:%d  changed:%d  retried:%d  skipped:%d  errors:%d ===",
        counts["new"], counts["changed"], counts["retried"],
        counts["skipped"], counts["error"],
    )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="contactgen – video contact-sheet generator"
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan all watched folders (8 AM timer mode)",
    )
    parser.add_argument(
        "--file", metavar="PATH",
        help="Process a single file immediately (Sonarr/Radarr hook mode)",
    )
    parser.add_argument(
        "--source", metavar="LABEL", default="manual",
        help="Label for the log when using --file (e.g. sonarr, radarr)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all tracked files in the database",
    )
    parser.add_argument(
        "--reset", metavar="PATH",
        help="Remove a file from the DB so it gets re-processed next time",
    )
    args = parser.parse_args()

    db = Database(Config.DB_FILE)

    if args.list:
        rows = db.list_files()
        print(f"{'Status':<8}  {'Last Modified':<20}  Path")
        print("-" * 80)
        for r in rows:
            ts = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{r['status']:<8}  {ts:<20}  {r['path']}")
        return

    if args.reset:
        db.delete_file(args.reset)
        log.info("Removed from DB: %s", args.reset)
        return

    if args.file:
        process_one(Path(args.file), db, source=args.source)
        return

    # Default / --scan
    run_scan(db)


if __name__ == "__main__":
    main()
