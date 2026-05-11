#!/usr/bin/env python3
"""
safescanarr/web/server.py v0.6
"""

import datetime
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from flask import (
    Flask, after_this_request, jsonify, request,
    send_file, send_from_directory, render_template,
)

sys.path.insert(0, "/opt/safescanarr")
import config as config_module
from config import Config
from database import Database

log = logging.getLogger(__name__)
app = Flask(__name__, template_folder="templates", static_folder="static")
SCANNER = "/opt/safescanarr/scanner.py"


def get_db():
    return Database(Config().DB_FILE)


# ── UI ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", version=Config.version())


@app.route("/api/version")
def api_version():
    return jsonify({"version": Config.version()})


# ── Stats ─────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    return jsonify(get_db().get_stats())


# ── Lifetime stats ───────────────────────────────────────────────
@app.route("/api/stats/full")
def api_stats_full():
    return jsonify(get_db().get_stats_full())


# ── Sheets by state ───────────────────────────────────────────────
@app.route("/api/sheets")
def api_sheets():
    cfg        = Config()
    state      = request.args.get("state", "pending")
    search     = request.args.get("search", "").lower()
    source     = request.args.get("source", "")  # "auto", "user", or "" for all
    output_dir = Path(cfg.OUTPUT_DIR)
    sheets     = []

    rows = get_db().list_files(review_state=state)
    for row in rows:
        stem = Path(row["path"]).stem
        if search and search not in stem.lower() and search not in row["path"].lower():
            continue
        row_source = row["state_source"] if "state_source" in row.keys() else None
        if source and row_source != source:
            continue
        sheet_file = output_dir / (stem + ".jpg")
        sheets.append({
            "stem":            stem,
            "filename":        sheet_file.name,
            "has_sheet":       sheet_file.exists(),
            "source_path":     row["path"],
            "review_state":    row["review_state"],
            "flagged":         bool(row["flagged"]),
            "flag_reason":     row["flag_reason"],
            "nsfw_confidence": row["nsfw_confidence"],
            "quarantine_path": row["quarantine_path"],
            "state_updated_at": row["state_updated_at"],
            "updated_at":      row["updated_at"],
            "size":            row["size"],
            "state_source":    row["state_source"] if "state_source" in row.keys() else None,
        })

    return jsonify(sheets)


@app.route("/api/sheets/image/<path:filename>")
def api_sheet_image(filename):
    return send_from_directory(Config().OUTPUT_DIR, filename)


# ── Sheet actions ─────────────────────────────────────────────────
@app.route("/api/sheets/approve", methods=["POST"])
def api_approve():
    """Approve one or more sheets."""
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem:
            stems = [stem]

    db  = get_db()
    cfg = Config()
    done = []
    for stem in stems:
        row = db.find_file_by_stem(stem)
        if not row:
            continue
        # If currently quarantined, move video back
        if row["review_state"] == "quarantined" and row["quarantine_path"]:
            q_path = Path(row["quarantine_path"])
            orig   = Path(row["path"])
            if q_path.exists():
                orig.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(q_path), str(orig))
                log.info("Restored from quarantine: %s", orig)
        db.set_review_state(row["path"], "approved")
        done.append(stem)
    return jsonify({"status": "ok", "approved": done})


@app.route("/api/sheets/reject", methods=["POST"])
def api_reject():
    """Reject one or more sheets — delete video permanently."""
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem:
            stems = [stem]

    db  = get_db()
    cfg = Config()
    done = []
    for stem in stems:
        row = db.find_file_by_stem(stem)
        if not row:
            continue

        # Delete video — from quarantine or original location
        video_path = Path(row["quarantine_path"] or row["path"])
        if video_path.exists():
            video_path.unlink()
            log.info("Rejected (deleted): %s", video_path)

        # Keep sheet for audit trail — hidden in UI until user clicks to reveal

        # Blacklist
        _blacklist_path(row["path"], cfg)

        db.set_review_state(row["path"], "rejected")
        done.append(stem)
    return jsonify({"status": "ok", "rejected": done})


@app.route("/api/sheets/quarantine", methods=["POST"])
def api_quarantine():
    """Manually quarantine a pending item."""
    stem = (request.get_json() or {}).get("stem", "")
    db   = get_db()
    cfg  = Config()
    row  = db.find_file_by_stem(stem)
    if not row:
        return jsonify({"status": "error", "message": "Not found"}), 404

    video_path = Path(row["path"])
    if not video_path.exists():
        return jsonify({"status": "error", "message": "Source file not found"}), 404

    q_dir  = Path(cfg.QUARANTINE_DIR)
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / video_path.name
    shutil.move(str(video_path), str(q_path))
    db.set_review_state(row["path"], "quarantined", quarantine_path=str(q_path))
    return jsonify({"status": "ok", "quarantine_path": str(q_path)})


@app.route("/api/sheets/requeue", methods=["POST"])
def api_requeue():
    """Re-process a file — regenerate sheet and re-run analysis."""
    stem = (request.get_json() or {}).get("stem", "")
    db   = get_db()
    cfg  = Config()
    row  = db.find_file_by_stem(stem)

    path = row["path"] if row else None
    if not path or not Path(path).exists():
        return jsonify({"status": "error", "message": "Source file not found"}), 404

    db.delete_file(path)
    subprocess.Popen(
        [sys.executable, SCANNER, "--file", path, "--source", "requeue"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"status": "ok"})


# ── Config ────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(config_module.get())


@app.route("/api/config", methods=["POST"])
def api_config_save():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400
    try:
        existing = config_module.get()
        existing.update(data)
        # Remove derived fields
        existing.pop("output_dir", None)
        # Type coercions
        existing["poll_interval_seconds"]      = int(existing.get("poll_interval_seconds", 600))
        existing["vcsi_timeout_seconds"]       = int(existing.get("vcsi_timeout_seconds", 300))
        existing["nudenet_frames"]             = int(existing.get("nudenet_frames", 10))
        existing["nudenet_threshold"]          = float(existing.get("nudenet_threshold", 0.1))
        existing["zone_auto_approve"]          = float(existing.get("zone_auto_approve", 0.1))
        existing["zone_quarantine"]            = float(existing.get("zone_quarantine", 0.4))
        existing["zone_auto_reject"]           = float(existing.get("zone_auto_reject", 0.85))
        existing["quarantine_auto_reject_days"]= int(existing.get("quarantine_auto_reject_days", 0))
        existing["polling_enabled"]            = bool(existing.get("polling_enabled", False))
        existing["scan_schedule_enabled"]      = bool(existing.get("scan_schedule_enabled", False))
        existing["webhook_on_review"]          = bool(existing.get("webhook_on_review", False))
        existing["webhook_on_quarantine"]      = bool(existing.get("webhook_on_quarantine", True))
        existing["webhook_on_reject"]          = bool(existing.get("webhook_on_reject", True))
        if isinstance(existing.get("watch_folders"), str):
            existing["watch_folders"] = [
                p.strip() for p in existing["watch_folders"].split(",") if p.strip()
            ]
        config_module.save(existing)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("Failed to save config: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/config/test-sonarr", methods=["POST"])
def api_test_sonarr():
    cfg  = Config()
    data = _api_get(f"{cfg.SONARR_URL.rstrip('/')}/api/v3/system/status", cfg.SONARR_API_KEY)
    if data:
        return jsonify({"status": "ok", "version": data.get("version", "unknown")})
    return jsonify({"status": "error", "message": "Could not connect"}), 502


@app.route("/api/config/test-radarr", methods=["POST"])
def api_test_radarr():
    cfg  = Config()
    data = _api_get(f"{cfg.RADARR_URL.rstrip('/')}/api/v3/system/status", cfg.RADARR_API_KEY)
    if data:
        return jsonify({"status": "ok", "version": data.get("version", "unknown")})
    return jsonify({"status": "error", "message": "Could not connect"}), 502


@app.route("/api/config/test-webhook", methods=["POST"])
def api_test_webhook():
    cfg = Config()
    if not cfg.WEBHOOK_URL:
        return jsonify({"status": "error", "message": "No webhook URL set"}), 400
    title = "Example (2024)"
    body = {
        "event":     "test",
        "path":      f"/mnt/media/Movies/{title}/{title}.mp4",
        "title":     title,
        "risk":      0.75,
        "labels":    ["FEMALE_BREAST_EXPOSED"],
        "message":   f"Quarantined: {title} (risk: 75%)",
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
    if cfg.WEB_UI_URL:
        body["url"] = f"{cfg.WEB_UI_URL.rstrip('/')}/?tab=quarantined"
    payload = json.dumps(body).encode()
    try:
        req = urllib.request.Request(
            cfg.WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 502


@app.route("/api/db/clean", methods=["POST"])
def api_db_clean():
    cfg    = Config()
    db     = get_db()
    result = db.clean_missing_files(cfg.WATCH_FOLDERS, output_dir=cfg.OUTPUT_DIR)
    return jsonify({"status": "ok", "removed": result["records"], "sheets": result["sheets"]})


# ── Backup / restore ──────────────────────────────────────────────
def _schedule_cleanup(path: str):
    @after_this_request
    def _cleanup(response):
        try:
            os.unlink(path)
        except OSError:
            pass
        return response


@app.route("/api/backup/db")
def api_backup_db():
    """Download a hot-copy of the SQLite database using sqlite3.backup."""
    cfg = Config()
    db_path = cfg.DB_FILE
    if not os.path.isfile(db_path):
        return jsonify({"status": "error", "message": "Database file not found"}), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        src  = sqlite3.connect(db_path)
        dest = sqlite3.connect(tmp.name)
        with dest:
            src.backup(dest)
        dest.close(); src.close()
    except Exception as e:
        os.unlink(tmp.name)
        return jsonify({"status": "error", "message": str(e)}), 500
    _schedule_cleanup(tmp.name)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"safescanarr-{ts}.db",
                     mimetype="application/octet-stream")


@app.route("/api/backup/vcs")
def api_backup_vcs():
    """Download all VCS thumbnails as a zip archive."""
    cfg = Config()
    out_dir = Path(cfg.OUTPUT_DIR)
    if not out_dir.is_dir():
        return jsonify({"status": "error", "message": "VCS directory not found"}), 404
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        # ZIP_DEFLATED gives marginal gain over JPGs but keeps the archive in a
        # universally readable format; transfer cost matters more than ratio here.
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for jpg in sorted(out_dir.glob("*.jpg")):
                z.write(jpg, arcname=jpg.name)
    except Exception as e:
        os.unlink(tmp.name)
        return jsonify({"status": "error", "message": str(e)}), 500
    _schedule_cleanup(tmp.name)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(tmp.name, as_attachment=True,
                     download_name=f"safescanarr-vcs-{ts}.zip",
                     mimetype="application/zip")


@app.route("/api/backup/restore/db", methods=["POST"])
def api_restore_db():
    """Replace the SQLite database with an uploaded file. Refuses while a scan is running."""
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    cfg = Config()
    db  = get_db()
    if db.get_scan_pid():
        return jsonify({"status": "error", "message": "Stop the running scan before restoring"}), 409
    # Validate SQLite magic header
    head = f.read(16)
    if not head.startswith(b"SQLite format 3"):
        return jsonify({"status": "error", "message": "Not a SQLite database file"}), 400
    db_path  = cfg.DB_FILE
    tmp_path = db_path + ".restore.tmp"
    try:
        with open(tmp_path, "wb") as out:
            out.write(head)
            while True:
                chunk = f.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                out.write(chunk)
        # Drop any stale WAL/journal sidecars so the new DB isn't confused with them
        for suffix in ("-wal", "-shm", "-journal"):
            stale = Path(db_path + suffix)
            if stale.exists():
                stale.unlink()
        os.replace(tmp_path, db_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except OSError: pass
        return jsonify({"status": "error", "message": str(e)}), 500
    return jsonify({"status": "ok"})


@app.route("/api/backup/restore/vcs", methods=["POST"])
def api_restore_vcs():
    """Replace VCS thumbnails with the contents of an uploaded zip archive."""
    f = request.files.get("file")
    if not f:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    cfg = Config()
    out_dir = Path(cfg.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        f.save(tmp_path)
        if not zipfile.is_zipfile(tmp_path):
            return jsonify({"status": "error", "message": "Not a zip archive"}), 400
        # Wipe existing thumbnails so the restore is a true replacement
        for existing in out_dir.glob("*.jpg"):
            existing.unlink()
        extracted = 0
        with zipfile.ZipFile(tmp_path) as z:
            for info in z.infolist():
                # Flatten any subdir structure; reject path-traversal attempts
                name = os.path.basename(info.filename)
                if not name or not name.lower().endswith(".jpg"):
                    continue
                target = out_dir / name
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted += 1
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass
    return jsonify({"status": "ok", "extracted": extracted})


# ── Filesystem browser (used by Watch Folders picker) ──────────────
@app.route("/api/fs/list")
def api_fs_list():
    """List subdirectories at the given path. Hidden dirs are skipped."""
    raw = request.args.get("path", "").strip()
    if not raw:
        # Default starting point: prefer /mnt or /media if present, else /
        for candidate in ("/mnt", "/media", "/"):
            if os.path.isdir(candidate):
                raw = candidate
                break
    path = os.path.abspath(raw)
    if not os.path.isdir(path):
        return jsonify({"status": "error", "message": f"Not a directory: {path}"}), 400
    try:
        entries = []
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    entries.append({"name": name, "path": full})
            except OSError:
                continue
    except PermissionError:
        return jsonify({"status": "error", "message": "Permission denied"}), 403
    except OSError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    parent = os.path.dirname(path) if path not in ("/", "") else None
    return jsonify({"path": path, "parent": parent, "entries": entries})


@app.route("/api/scan/status")
def api_scan_status():
    db  = get_db()
    pid = db.get_scan_pid()
    if pid:
        # Check if process is still running
        import os
        try:
            os.kill(pid, 0)
            return jsonify({"running": True, "pid": pid})
        except OSError:
            db.clear_scan_pid()
    return jsonify({"running": False})


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    db  = get_db()
    pid = db.get_scan_pid()
    if not pid:
        return jsonify({"status": "not_running"})
    # Clear the PID — scanner checks between files and exits gracefully
    # Do NOT send SIGTERM; that kills the process mid-file and closes the log pipe
    db.clear_scan_pid()
    log.info("Scan stop requested — scanner will stop after current file (pid %d)", pid)
    return jsonify({"status": "stopping"})


@app.route("/api/sheets/remove-rejected", methods=["POST"])
def api_remove_rejected():
    """Remove a rejected entry from DB and delete its sheet."""
    data  = request.get_json() or {}
    stems = data.get("stems", [])
    if not stems:
        stem = data.get("stem")
        if stem: stems = [stem]

    db  = get_db()
    cfg = Config()
    done = []
    for stem in stems:
        row = db.find_file_by_stem(stem)
        if not row or row["review_state"] != "rejected":
            continue
        # Delete sheet if exists
        sheet = Path(cfg.OUTPUT_DIR) / (stem + ".jpg")
        if sheet.exists():
            sheet.unlink()
        db.delete_file(row["path"])
        done.append(stem)
    return jsonify({"status": "ok", "removed": done})


# ── Logs ──────────────────────────────────────────────────────────
@app.route("/api/logs")
def api_logs():
    cfg      = Config()
    lines    = int(request.args.get("lines", 200))
    level    = request.args.get("level", "")
    log_path = Path(cfg.LOG_FILE)
    if not log_path.exists():
        return jsonify({"lines": []})
    with open(log_path) as f:
        all_lines = f.readlines()
    if level:
        all_lines = [l for l in all_lines if f"[{level}]" in l]
    return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})


# ── Scan ──────────────────────────────────────────────────────────
@app.route("/api/scan", methods=["POST"])
def api_scan():
    cfg = Config()
    db  = get_db()
    # Check if already running
    existing_pid = db.get_scan_pid()
    if existing_pid:
        import os
        try:
            os.kill(existing_pid, 0)
            return jsonify({"status": "already_running", "pid": existing_pid})
        except OSError:
            db.clear_scan_pid()
    proc = subprocess.Popen(
        [sys.executable, SCANNER, "--scan"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    db.set_scan_pid(proc.pid)
    return jsonify({"status": "started", "pid": proc.pid})


# ── Helpers ───────────────────────────────────────────────────────
def _api_get(url: str, api_key: str):
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _api_post(url: str, api_key: str, body: dict = None):
    """POST to an arr API endpoint."""
    data    = json.dumps(body).encode() if body else b""
    headers = {"X-Api-Key": api_key, "Accept": "application/json", "Content-Type": "application/json"}
    req     = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _api_delete(url: str, api_key: str):
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key}, method="DELETE"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def _blacklist_path(file_path: str, cfg: Config) -> None:
    """Full removal flow: delete from library, blacklist, trigger new search."""
    for service in ("sonarr", "radarr"):
        base    = cfg.SONARR_URL.rstrip("/") if service == "sonarr" else cfg.RADARR_URL.rstrip("/")
        api_key = cfg.SONARR_API_KEY        if service == "sonarr" else cfg.RADARR_API_KEY
        if not api_key:
            continue
        try:
            if service == "sonarr":
                # Find episode file
                ep_files = _api_get(f"{base}/api/v3/episodefile", api_key) or []
                ep_file_id = None
                series_id  = None
                for ef in ep_files:
                    if ef.get("path", "") == file_path:
                        ep_file_id = ef.get("id")
                        series_id  = ef.get("seriesId")
                        break
                if not ep_file_id:
                    continue
                # Get episode IDs
                episodes   = _api_get(f"{base}/api/v3/episode?seriesId={series_id}&episodeFileId={ep_file_id}", api_key) or []
                episode_ids = [e["id"] for e in episodes if "id" in e]
                # Delete from library
                _api_delete(f"{base}/api/v3/episodefile/{ep_file_id}", api_key)
                log.info("Sonarr: deleted episodefile %d", ep_file_id)
                # Blacklist history record
                history = _api_get(f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3", api_key) or {}
                for record in history.get("records", []):
                    if (record.get("data") or {}).get("importedPath", "") == file_path:
                        _api_delete(f"{base}/api/v3/blacklist/{record['id']}", api_key)
                        log.info("Sonarr: blacklisted record %d", record["id"])
                        break
                # Trigger new search
                if episode_ids:
                    _api_post(f"{base}/api/v3/command", api_key, {"name": "EpisodeSearch", "episodeIds": episode_ids})
                    log.info("Sonarr: triggered EpisodeSearch for %s", episode_ids)

            else:  # radarr
                # Find movie file
                movie_files = _api_get(f"{base}/api/v3/moviefile", api_key) or []
                movie_file_id = None
                movie_id      = None
                for mf in movie_files:
                    if mf.get("path", "") == file_path:
                        movie_file_id = mf.get("id")
                        movie_id      = mf.get("movieId")
                        break
                if not movie_file_id:
                    continue
                # Delete from library
                _api_delete(f"{base}/api/v3/moviefile/{movie_file_id}", api_key)
                log.info("Radarr: deleted moviefile %d", movie_file_id)
                # Blacklist history record
                history = _api_get(f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3", api_key) or {}
                for record in history.get("records", []):
                    if (record.get("data") or {}).get("importedPath", "") == file_path:
                        _api_delete(f"{base}/api/v3/blacklist/{record['id']}", api_key)
                        log.info("Radarr: blacklisted record %d", record["id"])
                        break
                # Trigger new search
                if movie_id:
                    _api_post(f"{base}/api/v3/command", api_key, {"name": "MoviesSearch", "movieIds": [movie_id]})
                    log.info("Radarr: triggered MoviesSearch for movie %d", movie_id)

        except Exception as e:
            log.warning("Arr removal failed for %s/%s: %s", service, file_path, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = Config()
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT,
            threaded=True, use_reloader=False)
