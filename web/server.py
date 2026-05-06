#!/usr/bin/env python3
"""
safescanarr/web/server.py v0.6
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, render_template

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


# ── Sheets by state ───────────────────────────────────────────────
@app.route("/api/sheets")
def api_sheets():
    cfg        = Config()
    state      = request.args.get("state", "pending")
    search     = request.args.get("search", "").lower()
    output_dir = Path(cfg.OUTPUT_DIR)
    sheets     = []

    rows = get_db().list_files(review_state=state)
    for row in rows:
        stem = Path(row["path"]).stem
        if search and search not in stem.lower() and search not in row["path"].lower():
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
        })

    # Pending: sort flagged/high-confidence first
    if state == "pending":
        sheets.sort(key=lambda s: -(s["nsfw_confidence"] or 0))

    return jsonify(sheets)


@app.route("/api/sheets/image/<filename>")
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

        # Delete sheet
        sheet = Path(cfg.OUTPUT_DIR) / (stem + ".jpg")
        if sheet.exists():
            sheet.unlink()

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
    payload = json.dumps({
        "event": "test",
        "message": "Safe Scanarr webhook test",
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }).encode()
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
    cfg     = Config()
    db      = get_db()
    removed = db.clean_missing_files(cfg.WATCH_FOLDERS)
    return jsonify({"status": "ok", "removed": removed})


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
    subprocess.Popen(
        [sys.executable, SCANNER, "--scan"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"status": "started"})


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


def _blacklist_path(file_path: str, cfg: Config) -> None:
    for service in ("sonarr", "radarr"):
        base    = cfg.SONARR_URL.rstrip("/") if service == "sonarr" else cfg.RADARR_URL.rstrip("/")
        api_key = cfg.SONARR_API_KEY        if service == "sonarr" else cfg.RADARR_API_KEY
        if not api_key:
            continue
        try:
            data = _api_get(
                f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3",
                api_key
            )
            if not data:
                continue
            for record in data.get("records", []):
                p = (record.get("data") or {}).get("importedPath", "")
                if p == file_path:
                    req = urllib.request.Request(
                        f"{base}/api/v3/blacklist/{record['id']}",
                        method="DELETE", headers={"X-Api-Key": api_key}
                    )
                    urllib.request.urlopen(req, timeout=10)
                    break
        except Exception as e:
            log.warning("Blacklist failed for %s: %s", service, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = Config()
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT,
            threaded=True, use_reloader=False)
