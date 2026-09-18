#!/usr/bin/env python3
"""
safescanarr/arr.py v1.0.10

Sonarr/Radarr reject flow:
  1. Resolve the series/movie and file IDs
  2. Optionally delete the file from the arr library
  3. Walk history to find the import record, then its downloadId
  4. Find the grabbed history record for that downloadId
  5. POST /api/v3/history/failed/{grabbedHistoryId} to mark the grab as failed
  6. Verify by reading back the blocklist (Sonarr/Radarr use US English "blocklist")
  7. Trigger a manual search unless the arr already queued an auto re-download

No URL literal in this file contains the string "blacklist".
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import webhook

log = logging.getLogger(__name__)


def _url_host(url: str) -> str:
    return webhook.url_host(url)


def _request(base: str, path: str, api_key: str, method: str = "GET",
             body: Optional[dict] = None, timeout: int = 10) -> tuple:
    """Make an arr API request.

    Returns (status, data, err). status is 0 for transport errors.
    Errors are logged with method, path, host and the first 300 chars of the
    response body. One retry is performed for status 0 or 5xx.
    """
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    data = None
    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/json",
    }
    if method == "POST" and body is None:
        # urllib adds a content-type if data is not None, so keep data None and
        # rely on the explicit Content-Length header for an empty body.
        headers["Content-Length"] = "0"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(data))

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    attempts = 0
    while True:
        attempts += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
                data_out = json.loads(raw) if raw else {}
                return status, data_out, None
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read()
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = str(raw)
            if len(text) > 300:
                text = text[:300] + "…"
            host = _url_host(url)
            log.warning("Arr API request failed: method=%s path=%s host=%s status=%s body=%s",
                        method, path, host, status, text)
            if attempts == 1 and (status == 0 or 500 <= status < 600):
                continue
            return status, None, f"HTTP {status}: {text}" if text else f"HTTP {status}"
        except Exception as e:
            host = _url_host(url)
            log.warning("Arr API request failed: method=%s path=%s host=%s error=%s",
                        method, path, host, e)
            if attempts == 1:
                import time
                time.sleep(2)
                continue
            return 0, None, str(e)


def reject_in_arr(file_path: str, cfg, *, delete_file_in_arr: bool = False) -> list:
    """Run the reject flow against every configured arr service.

    Returns a list of report dicts, one per configured service.
    """
    reports = []
    services = []
    if cfg.SONARR_API_KEY:
        services.append(("sonarr", cfg.SONARR_URL, cfg.SONARR_API_KEY))
    if cfg.RADARR_API_KEY:
        services.append(("radarr", cfg.RADARR_URL, cfg.RADARR_API_KEY))

    if not services:
        log.info("No arr services configured; skipping arr reject flow")
        return reports

    for service, base, api_key in services:
        report = _reject_in_service(service, base, api_key, file_path, cfg,
                                    delete_file_in_arr=delete_file_in_arr)
        reports.append(report)
    return reports


def _reject_in_service(service: str, base: str, api_key: str, file_path: str,
                       cfg, *, delete_file_in_arr: bool) -> dict:
    base = base.rstrip("/")
    norm_path = os.path.normpath(file_path)
    basename = Path(norm_path).name

    report = {
        "service": service,
        "matched": False,
        "file_deleted_in_arr": False,
        "grabbed_history_id": None,
        "blocklisted": None,
        "blocklist_id": None,
        "source_title": None,
        "search_triggered": False,
        "reason": None,
    }

    if service == "sonarr":
        series_id, series_path = _resolve_series(base, api_key, norm_path)
        if series_id is None:
            log.warning("Sonarr: no series found for %s", file_path)
            report["reason"] = "no series found"
            return report
        report["series_id"] = series_id

        file_id = _resolve_sonarr_file_id(base, api_key, series_id, norm_path, basename)
        if file_id is None:
            log.warning("Sonarr: no episode file found for %s", file_path)
            report["reason"] = "no episode file found"
            return report
        report["file_id"] = file_id
        report["matched"] = True

        status, episodes, _ = _request(
            base, f"/api/v3/episode?seriesId={series_id}&episodeFileId={file_id}",
            api_key
        )
        episode_ids = [e["id"] for e in (episodes or []) if "id" in e]

        if delete_file_in_arr:
            del_status, _, _ = _request(
                base, f"/api/v3/episodefile/{file_id}", api_key, method="DELETE"
            )
            if 200 <= del_status < 300:
                report["file_deleted_in_arr"] = True
                log.info("Sonarr: deleted episodefile %s", file_id)
            elif del_status == 404:
                log.warning("Sonarr: episodefile %s already gone", file_id)
            else:
                log.error("Sonarr: failed to delete episodefile %s (HTTP %s)",
                          file_id, del_status)
                report["reason"] = f"delete file HTTP {del_status}"
                return report

        # Find import history record (eventType=3)
        status, history, _ = _request(
            base,
            f"/api/v3/history/series?seriesId={series_id}&eventType=3",
            api_key,
        )
        import_record = _find_newest_import_record(
            history, file_id, norm_path, basename, service
        )
        if import_record is None:
            report["blocklisted"] = False
            report["reason"] = "no import history record"
            log.error("Sonarr: no import history record for %s", file_path)
            return report

        download_id = import_record.get("downloadId", "")
        if not download_id:
            report["blocklisted"] = False
            report["reason"] = "import has no downloadId (manually imported)"
            log.warning("Sonarr: import record has no downloadId for %s", file_path)
            _maybe_search_sonarr(base, api_key, series_id, episode_ids, cfg, report)
            return report

        # Find grabbed history record (eventType=1)
        grabbed_id, source_title = _find_grabbed_record(
            base, api_key, download_id, service
        )
        if grabbed_id is None:
            report["blocklisted"] = False
            report["reason"] = "no grabbed history for downloadId"
            log.warning("Sonarr: no grabbed history for downloadId %s", download_id)
            _maybe_search_sonarr(base, api_key, series_id, episode_ids, cfg, report)
            return report

        report["grabbed_history_id"] = grabbed_id
        report["source_title"] = source_title

        if not cfg.ARR_BLOCKLIST_ON_REJECT:
            log.info("Sonarr: arr_blocklist_on_reject disabled; skipping blocklist")
            _maybe_search_sonarr(base, api_key, series_id, episode_ids, cfg, report)
            return report

        mark_ok = _mark_grab_as_failed(base, api_key, grabbed_id, service, report)

        if mark_ok:
            _verify_blocklist(base, api_key, service, series_id=series_id,
                              movie_id=None, source_title=source_title,
                              file_path=file_path, report=report)

        _maybe_search_sonarr(base, api_key, series_id, episode_ids, cfg, report)
        return report

    else:  # radarr
        movie_id, movie_path = _resolve_movie(base, api_key, norm_path)
        if movie_id is None:
            log.warning("Radarr: no movie found for %s", file_path)
            report["reason"] = "no movie found"
            return report
        report["movie_id"] = movie_id

        file_id = _resolve_radarr_file_id(base, api_key, movie_id, norm_path, basename)
        if file_id is None:
            log.warning("Radarr: no movie file found for %s", file_path)
            report["reason"] = "no movie file found"
            return report
        report["file_id"] = file_id
        report["matched"] = True

        if delete_file_in_arr:
            del_status, _, _ = _request(
                base, f"/api/v3/moviefile/{file_id}", api_key, method="DELETE"
            )
            if 200 <= del_status < 300:
                report["file_deleted_in_arr"] = True
                log.info("Radarr: deleted moviefile %s", file_id)
            elif del_status == 404:
                log.warning("Radarr: moviefile %s already gone", file_id)
            else:
                log.error("Radarr: failed to delete moviefile %s (HTTP %s)",
                          file_id, del_status)
                report["reason"] = f"delete file HTTP {del_status}"
                return report

        status, history, _ = _request(
            base,
            f"/api/v3/history/movie?movieId={movie_id}&eventType=3",
            api_key,
        )
        import_record = _find_newest_import_record(
            history, file_id, norm_path, basename, service
        )
        if import_record is None:
            report["blocklisted"] = False
            report["reason"] = "no import history record"
            log.error("Radarr: no import history record for %s", file_path)
            return report

        download_id = import_record.get("downloadId", "")
        if not download_id:
            report["blocklisted"] = False
            report["reason"] = "import has no downloadId (manually imported)"
            log.warning("Radarr: import record has no downloadId for %s", file_path)
            _maybe_search_radarr(base, api_key, movie_id, cfg, report)
            return report

        grabbed_id, source_title = _find_grabbed_record(
            base, api_key, download_id, service
        )
        if grabbed_id is None:
            report["blocklisted"] = False
            report["reason"] = "no grabbed history for downloadId"
            log.warning("Radarr: no grabbed history for downloadId %s", download_id)
            _maybe_search_radarr(base, api_key, movie_id, cfg, report)
            return report

        report["grabbed_history_id"] = grabbed_id
        report["source_title"] = source_title

        if not cfg.ARR_BLOCKLIST_ON_REJECT:
            log.info("Radarr: arr_blocklist_on_reject disabled; skipping blocklist")
            _maybe_search_radarr(base, api_key, movie_id, cfg, report)
            return report

        mark_ok = _mark_grab_as_failed(base, api_key, grabbed_id, service, report)

        if mark_ok:
            _verify_blocklist(base, api_key, service, series_id=None,
                              movie_id=movie_id, source_title=source_title,
                              file_path=file_path, report=report)

        _maybe_search_radarr(base, api_key, movie_id, cfg, report)
        return report


def _resolve_series(base: str, api_key: str, file_path: str):
    """Return (series_id, series_path) for the longest matching series path."""
    status, series_list, _ = _request(base, "/api/v3/series", api_key)
    if not series_list:
        return None, None

    best_id = None
    best_path = None
    for s in series_list:
        path = s.get("path", "")
        if not path:
            continue
        norm = os.path.normpath(path)
        if file_path.startswith(norm + os.sep) or file_path == norm:
            if best_path is None or len(norm) > len(best_path):
                best_id = s.get("id")
                best_path = norm

    if best_id is not None:
        return best_id, best_path

    # Fallback: parse by basename
    basename = Path(file_path).name
    status, parsed, _ = _request(
        base, f"/api/v3/parse?title={urllib.parse.quote(basename)}", api_key
    )
    series = (parsed or {}).get("series")
    if series:
        return series.get("id"), series.get("path")
    return None, None


def _resolve_movie(base: str, api_key: str, file_path: str):
    """Return (movie_id, movie_path) for the longest matching movie path."""
    status, movies, _ = _request(base, "/api/v3/movie", api_key)
    if not movies:
        return None, None

    best_id = None
    best_path = None
    for m in movies:
        path = m.get("path", "")
        if not path:
            continue
        norm = os.path.normpath(path)
        if file_path.startswith(norm + os.sep) or file_path == norm:
            if best_path is None or len(norm) > len(best_path):
                best_id = m.get("id")
                best_path = norm

    if best_id is not None:
        return best_id, best_path

    basename = Path(file_path).name
    status, parsed, _ = _request(
        base, f"/api/v3/parse?title={urllib.parse.quote(basename)}", api_key
    )
    movie = (parsed or {}).get("movie")
    if movie:
        return movie.get("id"), movie.get("path")
    return None, None


def _resolve_sonarr_file_id(base: str, api_key: str, series_id, file_path: str,
                            basename: str):
    status, files, _ = _request(
        base, f"/api/v3/episodefile?seriesId={series_id}", api_key
    )
    if not files:
        return None
    for ef in files:
        if os.path.normpath(ef.get("path", "")) == file_path:
            return ef.get("id")
    for ef in files:
        if Path(ef.get("path", "")).name == basename:
            return ef.get("id")
    return None


def _resolve_radarr_file_id(base: str, api_key: str, movie_id, file_path: str,
                            basename: str):
    status, files, _ = _request(
        base, f"/api/v3/moviefile?movieId={movie_id}", api_key
    )
    if not files:
        return None
    for mf in files:
        if os.path.normpath(mf.get("path", "")) == file_path:
            return mf.get("id")
    for mf in files:
        if Path(mf.get("path", "")).name == basename:
            return mf.get("id")
    return None


def _find_newest_import_record(history, file_id, file_path: str, basename: str,
                               service: str):
    """Find the newest import record (eventType=3) matching the file."""
    records = history
    if isinstance(history, dict):
        records = history.get("records", [])
    target_id = str(file_id)
    candidates = []
    for rec in records:
        if rec.get("eventType") != 3:
            continue
        data = rec.get("data") or {}
        if str(data.get("fileId", "")) == target_id:
            candidates.append(rec)
            continue
        imported_path = data.get("importedPath", "")
        if imported_path and os.path.normpath(imported_path) == file_path:
            candidates.append(rec)
            continue
        if imported_path and Path(imported_path).name == basename:
            candidates.append(rec)
            continue
        # Sonarr sometimes lacks importedPath on the import record
        if service == "sonarr":
            source_title = rec.get("sourceTitle", "")
            if source_title and Path(source_title).name == basename:
                candidates.append(rec)

    if not candidates:
        return None
    # Newest first — history endpoints may already be sorted, but be explicit
    candidates.sort(key=lambda r: r.get("date", ""), reverse=True)
    return candidates[0]


def _find_grabbed_record(base: str, api_key: str, download_id: str,
                         service: str) -> tuple:
    """Return (grabbed_history_id, source_title) for a downloadId."""
    status, history, _ = _request(
        base,
        f"/api/v3/history?downloadId={urllib.parse.quote(download_id)}"
        f"&eventType=1&pageSize=50",
        api_key,
    )
    records = history
    if isinstance(history, dict):
        records = history.get("records", [])
    grabs = [r for r in records if r.get("eventType") == 1]
    if not grabs:
        return None, None
    grabs.sort(key=lambda r: r.get("date", ""), reverse=True)
    rec = grabs[0]
    return rec.get("id"), rec.get("sourceTitle", "")


def _mark_grab_as_failed(base: str, api_key: str, grabbed_history_id,
                         service: str, report: dict) -> bool:
    """POST /api/v3/history/failed/{id} (empty body). Retry once on 404/405."""
    path = f"/api/v3/history/failed/{grabbed_history_id}"
    status, _, err = _request(base, path, api_key, method="POST")
    if status in (200, 201, 202):
        log.info("%s: marked grab %s as failed", service.capitalize(),
                 grabbed_history_id)
        return True
    if status in (404, 405):
        fallback = f"/api/v3/history/failed?id={grabbed_history_id}"
        status, _, err = _request(base, fallback, api_key, method="POST")
        if status in (200, 201, 202):
            log.info("%s: marked grab %s as failed via fallback",
                     service.capitalize(), grabbed_history_id)
            return True
        report["blocklisted"] = False
        report["reason"] = f"mark-as-failed fallback HTTP {status}"
        log.error("%s: mark-as-failed fallback HTTP %s for grab %s",
                  service.capitalize(), status, grabbed_history_id)
        return False

    report["blocklisted"] = False
    report["reason"] = f"mark-as-failed HTTP {status}"
    log.error("%s: mark-as-failed HTTP %s for grab %s",
              service.capitalize(), status, grabbed_history_id)
    return False


def _verify_blocklist(base: str, api_key: str, service: str, *, series_id,
                      movie_id, source_title: str, file_path: str,
                      report: dict) -> None:
    """Read back the blocklist to confirm the mark-as-failed created a row."""
    if service == "sonarr":
        path = (
            f"/api/v3/blocklist?page=1&pageSize=50&sortKey=date"
            f"&sortDirection=descending&seriesIds={series_id}"
        )
    else:
        path = f"/api/v3/blocklist/movie?movieId={movie_id}"

    status, blocklist, _ = _request(base, path, api_key)
    if status != 200:
        report["blocklisted"] = False
        report["reason"] = f"blocklist verification HTTP {status}"
        log.error("%s: blocklist verification HTTP %s for %s",
                  service.capitalize(), status, file_path)
        return

    records = blocklist
    if isinstance(blocklist, dict):
        records = blocklist.get("records", [])

    target = (source_title or "").casefold()
    for entry in records:
        entry_source = entry.get("sourceTitle", "")
        if entry_source and entry_source.casefold() == target:
            report["blocklisted"] = True
            report["blocklist_id"] = entry.get("id")
            log.info("%s: verified blocklist row %s for %s",
                     service.capitalize(), entry.get("id"), file_path)
            return

    report["blocklisted"] = False
    report["reason"] = "arr accepted mark-as-failed but no blocklist row appeared - check the arr log"
    log.error("%s: mark-as-failed succeeded but no blocklist row appeared for %s - check the arr log",
              service.capitalize(), file_path)


def _maybe_search_sonarr(base: str, api_key: str, series_id, episode_ids: list,
                         cfg, report: dict) -> None:
    if not cfg.ARR_SEARCH_AFTER_REJECT:
        return
    if not episode_ids:
        return

    # If autoRedownloadFailed is enabled the arr already queued a search.
    status, config_data, _ = _request(base, "/api/v3/config/downloadclient", api_key)
    if status == 200 and config_data:
        if config_data.get("autoRedownloadFailed"):
            log.info("Sonarr: autoRedownloadFailed enabled; skipping manual search")
            return

    status, _, err = _request(
        base, "/api/v3/command", api_key, method="POST",
        body={"name": "EpisodeSearch", "episodeIds": episode_ids}
    )
    if status in (200, 201, 202):
        report["search_triggered"] = True
        log.info("Sonarr: triggered EpisodeSearch for episode ids %s", episode_ids)
    else:
        log.warning("Sonarr: EpisodeSearch failed: %s", err or f"HTTP {status}")


def _maybe_search_radarr(base: str, api_key: str, movie_id, cfg, report: dict) -> None:
    if not cfg.ARR_SEARCH_AFTER_REJECT:
        return

    status, config_data, _ = _request(base, "/api/v3/config/downloadclient", api_key)
    if status == 200 and config_data:
        if config_data.get("autoRedownloadFailed"):
            log.info("Radarr: autoRedownloadFailed enabled; skipping manual search")
            return

    status, _, err = _request(
        base, "/api/v3/command", api_key, method="POST",
        body={"name": "MoviesSearch", "movieIds": [movie_id]}
    )
    if status in (200, 201, 202):
        report["search_triggered"] = True
        log.info("Radarr: triggered MoviesSearch for movie id %s", movie_id)
    else:
        log.warning("Radarr: MoviesSearch failed: %s", err or f"HTTP {status}")
