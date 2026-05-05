#!/usr/bin/env python3
"""
safescanarr/web/server.py
Flask web server — REST API + static file serving.
"""

import json
import logging
import os
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


# ── UI ────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", version=Config.version())


# ── Version ───────────────────────────────────────────────────────────────────

@app.route("/api/version")
def api_version():
    return jsonify({"version": Config.version()})


# ── Sheets ────────────────────────────────────────────────────────────────────

@app.route("/api/sheets")
def api_sheets():
    cfg        = Config()
    output_dir = Path(cfg.OUTPUT_DIR)
    search     = request.args.get("search", "").lower()
    sheets     = []
    if output_dir.exists():
        for f in sorted(output_dir.glob("*.jpg"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if search and search not in f.stem.lower():
                continue
            db    = get_db()
            match = db.find_file_by_stem(f.stem)
            sheets.append({
                "filename":    f.name,
                "stem":        f.stem,
                "mtime":       f.stat().st_mtime,
                "size":        f.stat().st_size,
                "source_path": match["path"]        if match else None,
                "status":      match["status"]      if match else None,
                "flagged":     bool(match["flagged"]) if match else False,
                "flag_reason": match["flag_reason"] if match else None,
            })
    # Sort flagged to top
    sheets.sort(key=lambda s: (0 if s["flagged"] else 1, -s["mtime"]))
    return jsonify(sheets)


@app.route("/api/sheets/image/<filename>")
def api_sheet_image(filename):
    cfg = Config()
    return send_from_directory(cfg.OUTPUT_DIR, filename)


@app.route("/api/sheets/reviewed", methods=["POST"])
def api_sheet_reviewed():
    stem  = (request.get_json() or {}).get("stem", "")
    cfg   = Config()
    sheet = Path(cfg.OUTPUT_DIR) / (stem + ".jpg")
    if sheet.exists():
        sheet.unlink()
        return jsonify({"status": "ok"})
    return jsonify({"status": "not_found"}), 404


@app.route("/api/sheets/delete-media", methods=["POST"])
def api_sheet_delete_media():
    stem  = (request.get_json() or {}).get("stem", "")
    cfg   = Config()
    db    = get_db()
    match = db.find_file_by_stem(stem)

    if not match:
        return jsonify({"status": "error", "message": "No DB record found"}), 404

    source_path = match["path"]
    results     = {}

    results["sonarr"] = _blacklist_in_arr(source_path, "sonarr", cfg)
    results["radarr"] = _blacklist_in_arr(source_path, "radarr", cfg)

    source = Path(source_path)
    if source.exists():
        source.unlink()
        results["source_deleted"] = True
        log.info("Deleted source file: %s", source_path)
    else:
        results["source_deleted"] = False

    db.delete_file(source_path)

    sheet = Path(cfg.OUTPUT_DIR) / (stem + ".jpg")
    if sheet.exists():
        sheet.unlink()
    results["sheet_deleted"] = True

    return jsonify({"status": "ok", "results": results})


# ── Bulk actions ──────────────────────────────────────────────────────────────

@app.route("/api/sheets/bulk-reviewed", methods=["POST"])
def api_bulk_reviewed():
    stems = (request.get_json() or {}).get("stems", [])
    cfg   = Config()
    done  = []
    for stem in stems:
        sheet = Path(cfg.OUTPUT_DIR) / (stem + ".jpg")
        if sheet.exists():
            sheet.unlink()
            done.append(stem)
    return jsonify({"status": "ok", "reviewed": done})


@app.route("/api/sheets/bulk-delete-media", methods=["POST"])
def api_bulk_delete_media():
    stems   = (request.get_json() or {}).get("stems", [])
    results = []
    cfg     = Config()
    db      = get_db()
    for stem in stems:
        match = db.find_file_by_stem(stem)
        if not match:
            results.append({"stem": stem, "status": "no_record"})
            continue
        source_path = match["path"]
        _blacklist_in_arr(source_path, "sonarr", cfg)
        _blacklist_in_arr(source_path, "radarr", cfg)
        source = Path(source_path)
        if source.exists():
            source.unlink()
        db.delete_file(source_path)
        sheet = Path(cfg.OUTPUT_DIR) / (stem + ".jpg")
        if sheet.exists():
            sheet.unlink()
        results.append({"stem": stem, "status": "ok"})
    return jsonify({"status": "ok", "results": results})


# ── Auto-handled (Safe Mode audit) ────────────────────────────────────────────

@app.route("/api/auto-handled")
def api_auto_handled():
    db   = get_db()
    cfg  = Config()
    rows = db.list_auto_handled()
    result = []
    for r in rows:
        sheet_path = Path(cfg.OUTPUT_DIR) / "auto" / (r["sheet_name"] or "")
        result.append({
            "id":         r["id"],
            "name":       r["name"],
            "path":       r["path"],
            "sheet_name": r["sheet_name"],
            "reason":     r["reason"],
            "confidence": r["confidence"],
            "handled_at": r["handled_at"],
            "has_sheet":  sheet_path.exists() if r["sheet_name"] else False,
        })
    return jsonify(result)


@app.route("/api/auto-handled/<int:record_id>/dismiss", methods=["POST"])
def api_auto_handled_dismiss(record_id):
    db  = get_db()
    cfg = Config()
    # Get sheet name before deleting record
    cur = db._con.execute(
        "SELECT sheet_name FROM auto_handled WHERE id = ?", (record_id,)
    )
    row = cur.fetchone()
    if row and row["sheet_name"]:
        sheet = Path(cfg.OUTPUT_DIR) / "auto" / row["sheet_name"]
        if sheet.exists():
            sheet.unlink()
    db.delete_auto_handled(record_id)
    return jsonify({"status": "ok"})


@app.route("/api/auto-handled/image/<filename>")
def api_auto_handled_image(filename):
    cfg = Config()
    return send_from_directory(str(Path(cfg.OUTPUT_DIR) / "auto"), filename)


# ── Config ────────────────────────────────────────────────────────────────────

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
        existing.pop("output_dir", None)  # never persist this
        existing["poll_interval_seconds"] = int(existing["poll_interval_seconds"])
        existing["vcsi_timeout_seconds"]  = int(existing["vcsi_timeout_seconds"])
        existing["polling_enabled"]       = bool(existing.get("polling_enabled", False))
        existing["scan_mode"]             = existing.get("scan_mode", "review")
        existing["nudenet_threshold"]     = float(existing.get("nudenet_threshold", 0.6))
        existing["scan_schedule"]         = existing.get("scan_schedule", "daily")
        if isinstance(existing["watch_folders"], str):
            existing["watch_folders"] = [
                p.strip() for p in existing["watch_folders"].split(",") if p.strip()
            ]
        config_module.save(existing)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("Failed to save config: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Connection tests ──────────────────────────────────────────────────────────

@app.route("/api/config/test-sonarr", methods=["POST"])
def api_test_sonarr():
    cfg  = Config()
    url  = f"{cfg.SONARR_URL.rstrip('/')}/api/v3/system/status"
    data = _api_get(url, cfg.SONARR_API_KEY)
    if data:
        return jsonify({"status": "ok", "version": data.get("version", "unknown")})
    return jsonify({"status": "error", "message": "Could not connect"}), 502


@app.route("/api/config/test-radarr", methods=["POST"])
def api_test_radarr():
    cfg  = Config()
    url  = f"{cfg.RADARR_URL.rstrip('/')}/api/v3/system/status"
    data = _api_get(url, cfg.RADARR_API_KEY)
    if data:
        return jsonify({"status": "ok", "version": data.get("version", "unknown")})
    return jsonify({"status": "error", "message": "Could not connect"}), 502


# ── Database ──────────────────────────────────────────────────────────────────

@app.route("/api/db/files")
def api_db_files():
    db       = get_db()
    status   = request.args.get("status")
    search   = request.args.get("search", "")
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))

    rows = db.list_files()
    if status:
        rows = [r for r in rows if r["status"] == status]
    if search:
        rows = [r for r in rows if search.lower() in r["path"].lower()]

    total     = len(rows)
    start     = (page - 1) * per_page
    page_rows = rows[start:start + per_page]

    return jsonify({
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "rows":     [dict(r) for r in page_rows],
        "has_flagged": any(r["flagged"] for r in page_rows),
    })


@app.route("/api/db/stats")
def api_db_stats():
    db     = get_db()
    rows   = db.list_files()
    total  = len(rows)
    ok     = sum(1 for r in rows if r["status"] == "ok")
    errors = sum(1 for r in rows if r["status"] == "error")
    return jsonify({"total": total, "ok": ok, "errors": errors})


@app.route("/api/db/clean", methods=["POST"])
def api_db_clean():
    """Remove DB records for files that no longer exist or aren't in watch folders."""
    cfg     = Config()
    db      = get_db()
    removed = db.clean_missing_files(cfg.WATCH_FOLDERS)
    log.info("DB clean: removed %d stale records", removed)
    return jsonify({"status": "ok", "removed": removed})


@app.route("/api/db/requeue", methods=["POST"])
def api_db_requeue():
    """Re-generate the VCS for a specific file path."""
    path = (request.get_json() or {}).get("path", "")
    if not path:
        return jsonify({"status": "error", "message": "No path provided"}), 400

    file_exists = Path(path).exists()
    cfg = Config()
    db  = get_db()

    if not file_exists:
        log.warning("Re-queue: source file not found: %s", path)
        return jsonify({
            "status": "error",
            "message": f"Source file no longer exists: {path}"
        }), 404

    # Remove from DB so scanner treats it as new
    db.delete_file(path)

    subprocess.Popen(
        [sys.executable, SCANNER, "--file", path, "--source", "requeue"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"status": "ok", "message": "Re-queued — check Review shortly"})


# ── Logs ──────────────────────────────────────────────────────────────────────

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

    tail = all_lines[-lines:]
    return jsonify({"lines": [l.rstrip() for l in tail]})


# ── Scan ──────────────────────────────────────────────────────────────────────

@app.route("/api/scan", methods=["POST"])
def api_scan():
    cfg = Config()
    subprocess.Popen(
        [sys.executable, SCANNER, "--scan"],
        stdout=open(cfg.LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"status": "started"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _api_get(url: str, api_key: str):
    req = urllib.request.Request(
        url, headers={"X-Api-Key": api_key, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _blacklist_in_arr(file_path: str, service: str, cfg: Config) -> dict:
    if service == "sonarr":
        base    = cfg.SONARR_URL.rstrip("/")
        api_key = cfg.SONARR_API_KEY
    else:
        base    = cfg.RADARR_URL.rstrip("/")
        api_key = cfg.RADARR_API_KEY

    if not api_key:
        return {"status": "no_api_key"}

    url  = f"{base}/api/v3/history?pageSize=100&sortKey=date&sortDirection=descending&eventType=3"
    data = _api_get(url, api_key)
    if data is None:
        return {"status": "unreachable"}

    history_id = None
    for record in data.get("records", []):
        path = (record.get("data") or {}).get("importedPath", "")
        if path == file_path:
            history_id = record.get("id")
            break

    if not history_id:
        return {"status": "not_found_in_history"}

    try:
        req = urllib.request.Request(
            f"{base}/api/v3/blacklist/{history_id}",
            method="DELETE",
            headers={"X-Api-Key": api_key},
        )
        urllib.request.urlopen(req, timeout=10)
        return {"status": "blacklisted", "history_id": history_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = Config()
    app.run(host=cfg.WEB_HOST, port=cfg.WEB_PORT, threaded=True)
