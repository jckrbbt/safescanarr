#!/usr/bin/env python3
"""
safescanarr/scanner.py v0.6

State machine:
  max_confidence < zone_auto_approve  → approved  (auto, no review needed)
  zone_auto_approve ≤ conf < zone_quarantine → pending (review queue)
  zone_quarantine ≤ conf < zone_auto_reject  → quarantined (video moved)
  conf ≥ zone_auto_reject             → rejected

Rejected items are *moved to quarantine* by default; permanent deletion (and
the arr blocklist/re-search flow) only happens when the operator explicitly
sets ``delete_on_reject`` to true in the config.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

# Derive the app directory from this file so the code works from any checkout
# location (and keeps working for existing /opt/safescanarr installs).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import arr
from config import Config as _ConfigClass
from database import Database
from fileops import COPIED_SOURCE_KEPT, MOVED, relocate
from pathutil import sheet_filename, resolve_sheet
import webhook

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
    out_file = output_dir / sheet_filename(video_path)

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
    """Map a max NSFW confidence score onto a review state.

      conf >= zone_auto_reject   → "rejected"
      conf >= zone_quarantine    → "quarantined" (video moved to quarantine)
      conf >= zone_auto_approve  → "pending"     (manual review queue)
      otherwise                  → "approved"    (auto-approved)

    How a "rejected" verdict is *carried out* (permanent delete vs quarantine)
    is controlled by cfg.DELETE_ON_REJECT, not by this function.
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

_SOURCE_RETAINED_WARNED = False


def _warn_source_retained_once(directory: Path) -> None:
    """Log a one-per-scan warning that tells the operator source files remain."""
    global _SOURCE_RETAINED_WARNED
    if _SOURCE_RETAINED_WARNED:
        return
    _SOURCE_RETAINED_WARNED = True
    log.warning(
        "Quarantined content remains in the library because the source file "
        "could not be removed (directory %s is not writable/deletable). "
        "Give the container user write+delete access on the media mount, or "
        "accept that SafeScanarr will keep full-size copies in quarantine.",
        directory,
    )


def action_quarantine(video_path: Path, cfg, db: Database,
                      abs_path: str, nudenet_result: dict) -> tuple[str, str]:
    """Move video to quarantine folder. Returns (new quarantine path, outcome)."""
    q_dir = Path(cfg.QUARANTINE_DIR)
    q_path, outcome = relocate(video_path, q_dir / video_path.name)
    if outcome == MOVED:
        log.warning("QUARANTINED: %s → %s", abs_path, q_path)
    else:
        log.warning("QUARANTINED (COPIED, SOURCE NOT REMOVED): %s → %s", abs_path, q_path)
        _warn_source_retained_once(video_path.parent)
    _send_webhook(cfg, "quarantined", abs_path, nudenet_result)
    return str(q_path), outcome


def action_reject(video_path: Path, cfg, db: Database,
                  abs_path: str, nudenet_result: dict,
                  sheet_path: Path = None,
                  from_quarantine: bool = False) -> tuple[str | None, str, list]:
    """Carry out a reject verdict.

    Safe by default: the video is moved to quarantine and the quarantine path is
    returned. Permanent deletion (plus the arr reject / re-search flow) only
    happens when the operator has explicitly enabled ``delete_on_reject``; in
    that case the video is removed and None is returned.

    Returns (quarantine_path_or_None, outcome, reports) where outcome is one of
    the fileops MOVED / COPIED_SOURCE_KEPT constants and reports is the list of
    arr reject-flow reports (empty if the arr flow did not run).
    """
    from fileops import RETAIN_ERRNOS
    if cfg.DELETE_ON_REJECT:
        target = video_path
        deleted = False
        if target.exists():
            try:
                target.unlink()
                log.warning("REJECTED (deleted): %s", target)
                deleted = True
            except OSError as e:
                if e.errno not in RETAIN_ERRNOS:
                    raise
                log.warning(
                    "REJECTED (delete failed, copying to quarantine): %s: %s",
                    target, e,
                )
        # Fire reject webhook / arr reject only when the source is actually gone.
        if deleted or not target.exists():
            reports = []
            if cfg.ARR_BLOCKLIST_ON_REJECT or cfg.ARR_SEARCH_AFTER_REJECT:
                reports = arr.reject_in_arr(abs_path, cfg, delete_file_in_arr=True)
            _send_webhook(cfg, "rejected", abs_path, nudenet_result, reports=reports)
            return None, MOVED, reports
        else:
            # Source still exists (read-only/cross-device): quarantine-copy it.
            q_dir = Path(cfg.QUARANTINE_DIR)
            q_path, outcome = relocate(video_path, q_dir / video_path.name, verify=True)
            if outcome == COPIED_SOURCE_KEPT:
                log.warning(
                    "REJECTED (quarantined, source retained): %s → %s",
                    abs_path, q_path,
                )
                _warn_source_retained_once(video_path.parent)
            else:
                log.warning(
                    "REJECTED (quarantined, delete_on_reject=false): %s → %s",
                    abs_path, q_path,
                )
            _send_webhook(cfg, "quarantined", abs_path, nudenet_result)
            return str(q_path), outcome, []

    # Quarantine-only (default) mode
    q_dir = Path(cfg.QUARANTINE_DIR)
    q_path, outcome = relocate(video_path, q_dir / video_path.name, verify=True)
    if outcome == COPIED_SOURCE_KEPT:
        log.warning(
            "REJECTED (quarantined, source retained): %s → %s",
            abs_path, q_path,
        )
        _warn_source_retained_once(video_path.parent)
    else:
        log.warning(
            "REJECTED (quarantined, delete_on_reject=false): %s → %s",
            abs_path, q_path,
        )
    _send_webhook(cfg, "quarantined", abs_path, nudenet_result)
    return str(q_path), outcome, []


def _url_host(url: str) -> str:
    """Hostname only — webhook/arr URLs frequently embed secrets."""
    return webhook.url_host(url)


def _send_webhook(cfg, event: str, path: str, nudenet_result: dict,
                  reports: Optional[list] = None) -> None:
    if not cfg.WEBHOOK_URL:
        return
    if event == "review"      and not cfg.WEBHOOK_ON_REVIEW:
        return
    if event == "quarantined" and not cfg.WEBHOOK_ON_QUARANTINE:
        return
    if event == "rejected"    and not cfg.WEBHOOK_ON_REJECT:
        return

    from pathlib import Path as _Path
    labels = [h["label"] for h in (nudenet_result.get("labels") or [])]
    risk   = nudenet_result.get("max_conf", 0) or 0
    title  = _Path(path).stem
    event_label = {
        "review":      "Needs review",
        "quarantined": "Quarantined",
        "rejected":    "Rejected",
    }.get(event, event.capitalize())
    message = f"{event_label}: {title} (risk: {round(risk * 100)}%)"

    # Surface arr blocklist failures in the human-readable message
    if reports:
        for r in reports:
            if r.get("blocklisted") is False:
                message += f" - {r['service'].capitalize()} blocklist FAILED"
                break

    # Deep-link to the relevant tab in the Safe Scanarr UI, if a base URL is configured.
    tab_for_event = {
        "review":      "pending",
        "quarantined": "quarantined",
        "rejected":    "rejected",
    }.get(event)
    url = ""
    if cfg.WEB_UI_URL and tab_for_event:
        url = f"{cfg.WEB_UI_URL.rstrip('/')}/?tab={tab_for_event}"

    body = {
        "event":     event,
        "path":      path,
        "title":     title,
        "risk":      risk,
        "labels":    labels,
        "message":   message,
        "timestamp": datetime.now().isoformat(),
    }
    if url:
        body["url"] = url
    if reports:
        body["arr"] = reports

    host = webhook.url_host(cfg.WEBHOOK_URL).lower()
    payload = webhook.build_payload(host, message, event_label, url, body)
    ok, detail = webhook.send(cfg.WEBHOOK_URL, payload, _ConfigClass.version())
    if ok:
        log.info("Webhook sent: %s → %s", event, _url_host(cfg.WEBHOOK_URL))
        log.debug("Webhook sent: %s → %s", event, cfg.WEBHOOK_URL)
    else:
        log.warning("Webhook failed (host=%s): %s", _url_host(cfg.WEBHOOK_URL), detail)


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

    sheet_path = output_dir / sheet_filename(video)

    # 2. NSFW analysis on source video frames
    nudenet_result = analyse_video_file(video, cfg)
    max_conf       = nudenet_result.get("max_conf", 0.0)
    flag_reason    = ", ".join(h["label"] for h in nudenet_result.get("labels", []))

    # 3. Determine state from zones
    state = determine_state(max_conf, cfg)
    log.info("[%s] NSFW confidence=%.2f → state=%s", source, max_conf, state)

    quarantine_path = None
    source_retained = False
    outcome = MOVED

    try:
        if state == "quarantined":
            quarantine_path, outcome = action_quarantine(
                video, cfg, db, abs_path, nudenet_result
            )

        elif state == "rejected":
            quarantine_path, outcome, reports = action_reject(
                video, cfg, db, abs_path, nudenet_result, sheet_path=sheet_path
            )
            if quarantine_path:
                # Safe mode: the video is now in quarantine, awaiting review.
                source_retained = outcome == COPIED_SOURCE_KEPT
                db.upsert_file(
                    abs_path, name, size, mtime, status="ok",
                    review_state="quarantined", flagged=True,
                    flag_reason=flag_reason, nsfw_confidence=max_conf,
                    source_retained=source_retained
                )
                db.set_review_state(
                    abs_path, "quarantined",
                    quarantine_path=quarantine_path, source="auto",
                    source_retained=source_retained,
                )
            else:
                db.upsert_file(abs_path, name, size, mtime, status="ok",
                               review_state="rejected", flagged=True,
                               flag_reason=flag_reason, nsfw_confidence=max_conf)
                if reports:
                    db.set_arr_result(abs_path, reports)
            return

    except OSError:
        log.exception("Quarantine/reject failed for %s", abs_path)
        db.record_error(abs_path, "quarantine/reject failed")
        db.upsert_file(
            abs_path, name, size, mtime,
            status="error",
            review_state="pending",
            flagged=(max_conf >= cfg.ZONE_QUARANTINE),
            flag_reason=flag_reason or None,
            nsfw_confidence=max_conf if max_conf > 0 else None,
        )
        return

    # 4. Save to DB
    source_retained = outcome == COPIED_SOURCE_KEPT
    db.upsert_file(
        abs_path, name, size, mtime,
        status="ok",
        review_state=state,
        flagged=(max_conf >= cfg.ZONE_QUARANTINE),
        flag_reason=flag_reason or None,
        nsfw_confidence=max_conf if max_conf > 0 else None,
        source_retained=source_retained,
    )
    if quarantine_path:
        db.set_review_state(
            abs_path, "quarantined",
            quarantine_path=quarantine_path, source="auto",
            source_retained=source_retained,
        )

    # Fire review webhook if item landed in pending
    if state == "pending":
        _send_webhook(cfg, "review", abs_path, nudenet_result)


def _stop_requested(db: Database) -> bool:
    """Returns True if a stop has been requested (PID cleared externally)."""
    import os
    pid = db.get_scan_pid()
    if pid is None:
        return True  # PID was cleared = stop requested
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


def run_scan(db: Database) -> None:
    """Run a full scan, isolating per-file failures and cleaning up pid state."""
    global _SOURCE_RETAINED_WARNED
    _SOURCE_RETAINED_WARNED = False

    cfg = _ConfigClass()
    log.info("=== Full scan started ===")

    # Register our PID so stop button can find us
    import os
    db.set_scan_pid(os.getpid())

    counts = {"new": 0, "changed": 0, "skipped": 0, "error": 0}
    retained_count = 0

    try:
        for folder in cfg.WATCH_FOLDERS:
            folder = Path(folder)
            log.info("Scanning folder: %s", folder)
            for video in scan_folder(folder):
                # Check for stop signal between files
                if _stop_requested(db):
                    log.info("=== Scan stop requested — stopping after current file ===")
                    log.info("=== Scan stopped — new:%d changed:%d skipped:%d errors:%d ===",
                             counts["new"], counts["changed"], counts["skipped"], counts["error"])
                    return

                abs_path = None
                name, size, mtime = None, None, None
                try:
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
                except Exception:
                    log.exception("Unexpected error processing %s", video)
                    counts["error"] += 1
                    if abs_path is None:
                        try:
                            name, size, mtime = file_fingerprint(video)
                            abs_path = str(video.resolve())
                        except Exception:
                            name, size, mtime = video.name, 0, 0.0
                            abs_path = str(video)
                    db.record_error(abs_path, "unexpected processing error")
                    db.upsert_file(
                        abs_path, name or video.name, size or 0, mtime or 0.0,
                        status="error",
                        review_state="pending",
                        flagged=True,
                    )

        # Auto-reject stale quarantined items (skip source-retained copies)
        if cfg.QUARANTINE_AUTO_REJECT_DAYS > 0:
            stale = db.get_stale_quarantined(cfg.QUARANTINE_AUTO_REJECT_DAYS)
            copy_retained_stale = 0
            for row in stale:
                if "source_retained" in row.keys() and row["source_retained"]:
                    copy_retained_stale += 1
                    log.info(
                        "Skipping stale auto-reject for source-retained copy: %s",
                        row["path"]
                    )
                    continue
                try:
                    if not cfg.DELETE_ON_REJECT:
                        log.info("Leaving stale quarantine in place (delete_on_reject=false): %s",
                                 row["path"])
                        continue
                    log.info("Auto-rejecting stale quarantine: %s", row["path"])
                    q_path = Path(row["quarantine_path"]) if row["quarantine_path"] else None
                    sheet  = resolve_sheet(cfg.OUTPUT_DIR, row["path"])
                    if q_path and q_path.exists():
                        q_path.unlink()
                    if sheet.exists():
                        sheet.unlink()
                    reports = []
                    if cfg.ARR_BLOCKLIST_ON_REJECT or cfg.ARR_SEARCH_AFTER_REJECT:
                        reports = arr.reject_in_arr(row["path"], cfg, delete_file_in_arr=True)
                    db.set_review_state(row["path"], "rejected", source="auto")
                    if reports:
                        db.set_arr_result(row["path"], reports)
                    _send_webhook(cfg, "rejected", row["path"], {}, reports=reports)
                except Exception:
                    log.exception("Stale auto-reject failed for %s", row["path"])
                    counts["error"] += 1
                    db.record_error(row["path"], "stale auto-reject failed")

            retained_count = copy_retained_stale
    finally:
        db.clear_scan_pid()

    if retained_count == 0:
        try:
            retained_count = db._con.execute(
                "SELECT COUNT(*) AS c FROM files WHERE source_retained = 1"
            ).fetchone()["c"]
        except Exception:
            retained_count = 0

    if retained_count > 0:
        log.warning("%d file(s) quarantined by copy; sources left in place", retained_count)
    log.info("=== Scan complete — new:%d changed:%d skipped:%d errors:%d ===",
             counts["new"], counts["changed"], counts["skipped"], counts["error"])


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
