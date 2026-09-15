#!/usr/bin/env python3
"""
safescanarr/scanner.py v0.6

State machine:
  max_confidence < zone_auto_approve  → approved  (auto, no review needed)
  zone_auto_approve ≤ conf < zone_quarantine → pending (review queue)
  zone_quarantine ≤ conf < zone_auto_reject  → quarantined (video moved)
  conf ≥ zone_auto_reject             → rejected

Rejected items are *moved to quarantine* by default; permanent deletion (and
the arr blacklist/re-search flow) only happens when the operator explicitly
sets ``delete_on_reject`` to true in the config.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# Derive the app directory from this file so the code works from any checkout
# location (and keeps working for existing /opt/safescanarr installs).
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from config import Config as _ConfigClass
from database import Database
from pathutil import sheet_filename, resolve_sheet

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
                  from_quarantine: bool = False) -> str | None:
    """Carry out a reject verdict.

    Safe by default: the video is moved to quarantine and the quarantine path is
    returned. Permanent deletion (plus the arr blacklist / re-search flow) only
    happens when the operator has explicitly enabled ``delete_on_reject``; in
    that case the video is removed and None is returned.
    """
    if cfg.DELETE_ON_REJECT:
        target = video_path
        if target.exists():
            target.unlink()
            log.warning("REJECTED (deleted): %s", target)
        # Sheet is intentionally kept — hidden in UI until user clicks to reveal
        _send_webhook(cfg, "rejected", abs_path, nudenet_result)
        _blacklist(abs_path, cfg)
        return None

    # Quarantine-only (default) mode
    q_dir = Path(cfg.QUARANTINE_DIR)
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / video_path.name
    if q_path.exists():
        q_path = q_dir / f"{video_path.stem}_{int(datetime.now().timestamp())}{video_path.suffix}"
    if video_path.exists():
        shutil.move(str(video_path), str(q_path))
        log.warning("REJECTED (quarantined, delete_on_reject=false): %s → %s", abs_path, q_path)
    _send_webhook(cfg, "quarantined", abs_path, nudenet_result)
    return str(q_path)


def _url_host(url: str) -> str:
    """Hostname only — webhook/arr URLs frequently embed secrets."""
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _send_webhook(cfg, event: str, path: str, nudenet_result: dict) -> None:
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
    payload = json.dumps(body).encode()

    try:
        req = urllib.request.Request(
            cfg.WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Webhook sent: %s → %s", event, _url_host(cfg.WEBHOOK_URL))
        log.debug("Webhook sent: %s → %s", event, cfg.WEBHOOK_URL)
    except Exception as e:
        log.warning("Webhook failed (host=%s): %s", _url_host(cfg.WEBHOOK_URL), e)


def _api_request(url: str, api_key: str, method: str = "GET", body: dict = None):
    """Make an API request and return parsed JSON or None on failure."""
    data    = json.dumps(body).encode() if body else None
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        log.warning("Arr API request failed: method=%s host=%s error=%s",
                    method, _url_host(url), e)
        return None


def _blacklist(file_path: str, cfg) -> None:
    """
    Full removal flow for each configured service:
      1. Find the episode/movie file ID by matching path
      2. Delete the file from the library
      3. Blacklist the release to prevent re-download
      4. Trigger a new search for a replacement
    """
    for service in ("sonarr", "radarr"):
        base    = cfg.SONARR_URL.rstrip("/") if service == "sonarr" else cfg.RADARR_URL.rstrip("/")
        api_key = cfg.SONARR_API_KEY        if service == "sonarr" else cfg.RADARR_API_KEY
        if not api_key:
            continue
        try:
            if service == "sonarr":
                _arr_remove_sonarr(base, api_key, file_path)
            else:
                _arr_remove_radarr(base, api_key, file_path)
        except Exception as e:
            log.warning("Arr removal failed for %s: %s", service, e)


def _arr_remove_sonarr(base: str, api_key: str, file_path: str) -> None:
    # Step 1: Find episode file ID matching path
    ep_files = _api_request(f"{base}/api/v3/episodefile", api_key) or []
    ep_file_id  = None
    episode_id  = None
    series_id   = None
    release_group = None

    for ef in ep_files:
        if ef.get("path", "") == file_path:
            ep_file_id    = ef.get("id")
            series_id     = ef.get("seriesId")
            release_group = ef.get("releaseGroup", "")
            # Get episode ID from the file's episodes
            eps = ef.get("episodeFileId") or []
            break

    if not ep_file_id:
        log.warning("Sonarr: no episode file found for %s", file_path)
        return

    # Step 2: Get episode IDs for this file
    episodes = _api_request(
        f"{base}/api/v3/episode?seriesId={series_id}&episodeFileId={ep_file_id}",
        api_key
    ) or []
    episode_ids = [e["id"] for e in episodes if "id" in e]

    # Step 3: Delete the file from Sonarr library
    result = _api_request(f"{base}/api/v3/episodefile/{ep_file_id}", api_key, method="DELETE")
    log.info("Sonarr: deleted episodefile %d", ep_file_id)

    # Step 4: Blacklist via history
    history = _api_request(
        f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3",
        api_key
    ) or {}
    for record in history.get("records", []):
        if (record.get("data") or {}).get("importedPath", "") == file_path:
            _api_request(
                f"{base}/api/v3/blacklist/{record['id']}",
                api_key, method="DELETE"
            )
            log.info("Sonarr: blacklisted history record %d", record["id"])
            break

    # Step 5: Trigger new search
    if episode_ids:
        _api_request(f"{base}/api/v3/command", api_key, method="POST",
                     body={"name": "EpisodeSearch", "episodeIds": episode_ids})
        log.info("Sonarr: triggered EpisodeSearch for episode ids %s", episode_ids)


def _arr_remove_radarr(base: str, api_key: str, file_path: str) -> None:
    # Step 1: Find movie file ID matching path
    movie_files = _api_request(f"{base}/api/v3/moviefile", api_key) or []
    movie_file_id = None
    movie_id      = None

    for mf in movie_files:
        if mf.get("path", "") == file_path:
            movie_file_id = mf.get("id")
            movie_id      = mf.get("movieId")
            break

    if not movie_file_id:
        log.warning("Radarr: no movie file found for %s", file_path)
        return

    # Step 2: Delete the file from Radarr library
    _api_request(f"{base}/api/v3/moviefile/{movie_file_id}", api_key, method="DELETE")
    log.info("Radarr: deleted moviefile %d", movie_file_id)

    # Step 3: Blacklist via history
    history = _api_request(
        f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3",
        api_key
    ) or {}
    for record in history.get("records", []):
        if (record.get("data") or {}).get("importedPath", "") == file_path:
            _api_request(
                f"{base}/api/v3/blacklist/{record['id']}",
                api_key, method="DELETE"
            )
            log.info("Radarr: blacklisted history record %d", record["id"])
            break

    # Step 4: Trigger new search
    if movie_id:
        _api_request(f"{base}/api/v3/command", api_key, method="POST",
                     body={"name": "MoviesSearch", "movieIds": [movie_id]})
        log.info("Radarr: triggered MoviesSearch for movie id %d", movie_id)


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

    if state == "quarantined":
        quarantine_path = action_quarantine(video, cfg, db, abs_path, nudenet_result)

    elif state == "rejected":
        quarantine_path = action_reject(video, cfg, db, abs_path, nudenet_result,
                                        sheet_path=sheet_path)
        if quarantine_path:
            # Safe mode: the video is now in quarantine, awaiting review.
            db.upsert_file(abs_path, name, size, mtime, status="ok",
                           review_state="quarantined", flagged=True,
                           flag_reason=flag_reason, nsfw_confidence=max_conf)
            db.set_review_state(abs_path, "quarantined",
                                quarantine_path=quarantine_path, source="auto")
        else:
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
        db.set_review_state(abs_path, "quarantined", quarantine_path=quarantine_path, source="auto")

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
    cfg = _ConfigClass()
    log.info("=== Full scan started ===")

    # Register our PID so stop button can find us
    import os
    db.set_scan_pid(os.getpid())

    counts = {"new": 0, "changed": 0, "skipped": 0, "error": 0}

    for folder in cfg.WATCH_FOLDERS:
        folder = Path(folder)
        log.info("Scanning folder: %s", folder)
        for video in scan_folder(folder):
            # Check for stop signal between files
            if _stop_requested(db):
                log.info("=== Scan stop requested — stopping after current file ===")
                log.info("=== Scan stopped — new:%d changed:%d skipped:%d ===",
                         counts["new"], counts["changed"], counts["skipped"])
                return

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
            db.set_review_state(row["path"], "rejected", source="auto")
            _send_webhook(cfg, "rejected", row["path"], {})

    db.clear_scan_pid()
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
