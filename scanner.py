#!/usr/bin/env python3
"""
safescanarr/scanner.py v0.6

State machine:
  max_confidence < zone_auto_approve  → approved  (auto, no review needed)
  zone_auto_approve ≤ conf < zone_quarantine → pending (review queue)
  zone_quarantine ≤ conf < zone_auto_reject  → quarantined (video moved)
  conf ≥ zone_auto_reject             → rejected  (video deleted immediately)
  zone_auto_reject_days = 0           → quarantine skipped, straight to reject
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

# When run as subprocess from entrypoint, stdout is redirected to the log file
# by the parent process. Just log to stdout — no FileHandler needed.
_root = logging.getLogger()
if not _root.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
log = logging.getLogger(__name__)

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
        log.critical("vcsi not found at %s", vcsi_bin)
        sys.exit(1)
    return str(vcsi_bin)


# ---------------------------------------------------------------------------
# VCS generation
# ---------------------------------------------------------------------------

def generate_vcs(video_path: Path, output_dir: Path, cfg, db: Database) -> bool:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / (video_path.stem + ".jpg")

    cmd = [
        get_vcsi_bin(),
        str(video_path),
        "-t",
        "-g", cfg.VCS_GRID,
        "-o", str(out_file),
    ] + list(cfg.VCSI_EXTRA_ARGS)

    log.info("Generating contact sheet: %s", video_path.name)
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
# NSFW analysis
# ---------------------------------------------------------------------------

def analyse_video_file(video_path: Path, cfg) -> dict:
    """Extract frames and run NSFW detection. Returns result dict."""
    try:
        from nudenet_scanner import analyse_video
        return analyse_video(
            str(video_path),
            threshold=cfg.NUDENET_THRESHOLD,
            num_frames=cfg.NUDENET_FRAMES,
        )
    except Exception as e:
        log.error("NSFW analysis error: %s", e)
        return {"flagged": False, "labels": [], "max_conf": 0.0, "error": str(e)}


def determine_state(max_conf: float, cfg) -> str:
    """
    Apply zone thresholds to determine the review state.
      >= zone_auto_reject  → rejected immediately
      >= zone_quarantine   → quarantined (video moved)
      < zone_auto_approve  → approved automatically
      else                 → pending (review queue)
    quarantine_auto_reject_days: 0 = never auto-reject, >0 = reject after N days
    """
    if max_conf >= cfg.ZONE_AUTO_REJECT:
        return "rejected"
    if max_conf >= cfg.ZONE_QUARANTINE:
        return "quarantined"
    if max_conf < cfg.ZONE_AUTO_APPROVE:
        return "approved"
    return "pending"


# ---------------------------------------------------------------------------
# State actions
# ---------------------------------------------------------------------------

def action_quarantine(video_path: Path, cfg, db: Database,
                      abs_path: str, nudenet_result: dict) -> str:
    """Move video to quarantine folder. Returns new quarantine path."""
    q_dir = Path(cfg.QUARANTINE_DIR)
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / video_path.name
    # Avoid name collision
    if q_path.exists():
        stem = video_path.stem
        suffix = video_path.suffix
        q_path = q_dir / f"{stem}_{int(datetime.now().timestamp())}{suffix}"
    shutil.move(str(video_path), str(q_path))
    log.warning("QUARANTINED: %s → %s", abs_path, q_path)
    _send_webhook(cfg, "quarantined", abs_path, nudenet_result)
    return str(q_path)


def action_reject(video_path: Path, cfg, db: Database,
                  abs_path: str, nudenet_result: dict,
                  sheet_path: Path = None,
                  from_quarantine: bool = False) -> None:
    """Delete video permanently. Sheet is retained for audit trail."""
    target = video_path
    if target.exists():
        target.unlink()
        log.warning("REJECTED (deleted): %s", target)

    # Sheet is intentionally kept — hidden in UI until user clicks to reveal

    _send_webhook(cfg, "rejected", abs_path, nudenet_result)
    _blacklist(abs_path, cfg)


def _send_webhook(cfg, event: str, path: str, nudenet_result: dict) -> None:
    if not cfg.WEBHOOK_URL:
        return
    if event == "quarantined" and not cfg.WEBHOOK_ON_QUARANTINE:
        return
    if event == "rejected" and not cfg.WEBHOOK_ON_REJECT:
        return

    labels = [h["label"] for h in (nudenet_result.get("labels") or [])]
    payload = json.dumps({
        "event":      event,
        "path":       path,
        "confidence": nudenet_result.get("max_conf", 0),
        "labels":     labels,
        "timestamp":  datetime.now().isoformat(),
    }).encode()

    try:
        req = urllib.request.Request(
            cfg.WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Webhook sent: %s → %s", event, cfg.WEBHOOK_URL)
    except Exception as e:
        log.warning("Webhook failed: %s", e)


def _blacklist(file_path: str, cfg) -> None:
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
                p = (record.get("data") or {}).get("importedPath", "")
                if p == file_path:
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
            log.warning("Blacklist failed for %s: %s", service, e)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_one(video: Path, db: Database, source: str = "manual") -> None:
    cfg = _ConfigClass()

    if not video.exists():
        log.error("[%s] File not found: %s", source, video)
        return
    if not is_video(video):
        log.warning("[%s] Not a video, skipping: %s", source, video)
        return

    name, size, mtime = file_fingerprint(video)
    abs_path = str(video.resolve())
    existing = db.get_file(abs_path)

    # If file changed (upgrade/replace), reset state before reprocessing
    if existing is not None:
        changed = (size != existing["size"] or abs(mtime - existing["mtime"]) > 1)
        # Skip if unchanged, unless it errored AND is still pending (retry errors in review queue only)
        if not changed:
            if existing["status"] != "error":
                log.debug("[%s] UNCHANGED, skipping: %s", source, abs_path)
                return
            if existing["review_state"] != "pending":
                log.debug("[%s] Error but already handled (state=%s), skipping: %s",
                          source, existing["review_state"], abs_path)
                return
        if changed:
            log.info("[%s] CHANGED (re-processing): %s", source, abs_path)
            # Clear old state so it doesn't appear in multiple tabs
            db.set_review_state(abs_path, "pending")

    log.info("[%s] Processing: %s", source, abs_path)

    # 1. Generate contact sheet
    output_dir = Path(cfg.OUTPUT_DIR)
    ok = generate_vcs(video, output_dir, cfg, db)
    if not ok:
        db.upsert_file(abs_path, name, size, mtime, status="error",
                       review_state="pending")
        return

    sheet_path = output_dir / (video.stem + ".jpg")

    # 2. NSFW analysis on source video frames
    nudenet_result = analyse_video_file(video, cfg)
    max_conf       = nudenet_result.get("max_conf", 0.0)
    flag_reason    = ", ".join(h["label"] for h in nudenet_result.get("labels", []))

    # 3. Determine state from zones
    state = determine_state(max_conf, cfg)
    log.info("[%s] NSFW confidence=%.2f → state=%s", source, max_conf, state)

    quarantine_path = None

    if state == "quarantined":
        quarantine_path = action_quarantine(video, cfg, db, abs_path, nudenet_result)

    elif state == "rejected":
        action_reject(video, cfg, db, abs_path, nudenet_result, sheet_path=sheet_path)
        db.upsert_file(abs_path, name, size, mtime, status="ok",
                       review_state="rejected", flagged=True,
                       flag_reason=flag_reason, nsfw_confidence=max_conf)
        return

    # 4. Save to DB
    db.upsert_file(
        abs_path, name, size, mtime,
        status="ok",
        review_state=state,
        flagged=(max_conf >= cfg.ZONE_QUARANTINE),
        flag_reason=flag_reason or None,
        nsfw_confidence=max_conf if max_conf > 0 else None,
    )
    if quarantine_path:
        db.set_review_state(abs_path, "quarantined", quarantine_path=quarantine_path)


def run_scan(db: Database) -> None:
    cfg = _ConfigClass()
    log.info("=== Full scan started ===")
    counts = {"new": 0, "changed": 0, "skipped": 0, "error": 0}

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
                else:
                    counts["skipped"] += 1

    # Auto-reject stale quarantined items
    if cfg.QUARANTINE_AUTO_REJECT_DAYS > 0:
        stale = db.get_stale_quarantined(cfg.QUARANTINE_AUTO_REJECT_DAYS)
        for row in stale:
            log.info("Auto-rejecting stale quarantine: %s", row["path"])
            q_path = Path(row["quarantine_path"]) if row["quarantine_path"] else None
            sheet  = Path(cfg.OUTPUT_DIR) / (Path(row["path"]).stem + ".jpg")
            if q_path and q_path.exists():
                q_path.unlink()
            if sheet.exists():
                sheet.unlink()
            db.set_review_state(row["path"], "rejected")
            _send_webhook(cfg, "rejected", row["path"], {})

    log.info("=== Scan complete — new:%d changed:%d skipped:%d ===",
             counts["new"], counts["changed"], counts["skipped"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan",   action="store_true")
    parser.add_argument("--file",   metavar="PATH")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--list",   action="store_true")
    parser.add_argument("--reset",  metavar="PATH")
    args = parser.parse_args()

    cfg = _ConfigClass()
    db  = Database(cfg.DB_FILE)

    if args.list:
        rows = db.list_files()
        print(f"{'State':<12} {'Conf':<6} {'Name'}")
        print("-" * 70)
        for r in rows:
            conf = f"{r['nsfw_confidence']:.2f}" if r["nsfw_confidence"] else "-"
            print(f"{r['review_state']:<12} {conf:<6} {r['name']}")
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
