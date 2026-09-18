#!/usr/bin/env python3
"""Tests for the arr reject-flow module (stdlib only)."""

import json
import logging
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arr


def _norm(path: str) -> str:
    return os.path.normpath(path)


class _ArrHandler(BaseHTTPRequestHandler):
    def _respond(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        key = (method, self.path)
        # Also allow lookup without query string as fallback
        alt_key = (method, path)

        server = self.server
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        server.requests.append({
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })

        resp = server.responses.get(key)
        if resp is None:
            resp = server.responses.get(alt_key)
        if resp is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        status = resp.get("status", 200)
        data = resp.get("data")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if data is not None:
            self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        self._respond("GET")

    def do_POST(self):
        self._respond("POST")

    def do_DELETE(self):
        self._respond("DELETE")

    def log_message(self, fmt, *args):
        pass


def _start_server(responses: dict):
    server = HTTPServer(("127.0.0.1", 0), _ArrHandler)
    server.responses = responses
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _Cfg:
    def __init__(self, url: str, sonarr: bool = True, radarr: bool = False,
                 blocklist: bool = True, search: bool = True):
        if sonarr:
            self.SONARR_URL = url
            self.SONARR_API_KEY = "sonarr-key"
        else:
            self.SONARR_URL = ""
            self.SONARR_API_KEY = ""
        if radarr:
            self.RADARR_URL = url
            self.RADARR_API_KEY = "radarr-key"
        else:
            self.RADARR_URL = ""
            self.RADARR_API_KEY = ""
        self.ARR_BLOCKLIST_ON_REJECT = blocklist
        self.ARR_SEARCH_AFTER_REJECT = search


class ArrRejectTests(unittest.TestCase):
    def setUp(self):
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    # t1 Sonarr happy path
    def test_sonarr_happy_path(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {
                "data": [{"id": 1, "path": "/tv/Show"}]
            },
            ("GET", f"/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {
                "data": [{"id": 100}]
            },
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/77"): {"status": 200},
            ("GET", "/api/v3/blocklist?page=1&pageSize=50&sortKey=date&sortDirection=descending&seriesIds=1"): {
                "data": {
                    "records": [
                        {"id": 99, "sourceTitle": "Show.S01E01.1080p"}
                    ]
                }
            },
            ("GET", "/api/v3/config/downloadclient"): {
                "data": {"autoRedownloadFailed": False}
            },
            ("POST", "/api/v3/command"): {"status": 200},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        self.assertEqual(len(reports), 1)
        r = reports[0]
        self.assertEqual(r["service"], "sonarr")
        self.assertTrue(r["matched"])
        self.assertEqual(r["file_id"], 10)
        self.assertTrue(r["file_deleted_in_arr"])
        self.assertEqual(r["grabbed_history_id"], 77)
        self.assertTrue(r["blocklisted"])
        self.assertEqual(r["blocklist_id"], 99)
        self.assertEqual(r["source_title"], "Show.S01E01.1080p")
        self.assertTrue(r["search_triggered"])

        paths = [req["path"] for req in self.server.requests]
        self.assertIn("/api/v3/history/failed/77", paths)
        self.assertTrue(any("seriesId=1" in p for p in paths))
        self.assertFalse(any("blacklist" in p for p in paths))
        self.assertFalse(any(p.startswith("/api/v3/blacklist/") for p in paths))

        failed_post = [req for req in self.server.requests
                       if req["method"] == "POST" and req["path"] == "/api/v3/history/failed/77"]
        self.assertEqual(len(failed_post), 1)
        self.assertEqual(failed_post[0]["headers"].get("content-length"), "0")
        self.assertEqual(failed_post[0]["body"], b"")
        self.assertNotIn("content-type", failed_post[0]["headers"])

    # t2 Radarr happy path
    def test_radarr_happy_path(self):
        file_path = "/movies/Movie (2024)/Movie (2024).mkv"
        responses = {
            ("GET", "/api/v3/movie"): {
                "data": [{"id": 5, "path": "/movies/Movie (2024)"}]
            },
            ("GET", "/api/v3/moviefile?movieId=5"): {
                "data": [{"id": 20, "movieId": 5, "path": _norm(file_path)}]
            },
            ("DELETE", "/api/v3/moviefile/20"): {"status": 200},
            ("GET", "/api/v3/history/movie?movieId=5&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 44, "eventType": 3, "downloadId": "d2",
                         "data": {"fileId": "20", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d2&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 88, "eventType": 1, "downloadId": "d2",
                         "sourceTitle": "Movie.2024.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/88"): {"status": 200},
            ("GET", "/api/v3/blocklist/movie?movieId=5"): {
                "data": {
                    "records": [
                        {"id": 98, "sourceTitle": "Movie.2024.1080p"}
                    ]
                }
            },
            ("GET", "/api/v3/config/downloadclient"): {
                "data": {"autoRedownloadFailed": False}
            },
            ("POST", "/api/v3/command"): {"status": 200},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}", sonarr=False, radarr=True)

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        self.assertEqual(len(reports), 1)
        r = reports[0]
        self.assertEqual(r["service"], "radarr")
        self.assertTrue(r["matched"])
        self.assertEqual(r["movie_id"], 5)
        self.assertEqual(r["file_id"], 20)
        self.assertTrue(r["blocklisted"])

        commands = [req for req in self.server.requests
                    if req["method"] == "POST" and req["path"] == "/api/v3/command"]
        self.assertEqual(len(commands), 1)
        body = json.loads(commands[0]["body"])
        self.assertEqual(body, {"name": "MoviesSearch", "movieIds": [5]})

    # t3 episodefile listing requested WITH seriesId
    def test_episodefile_requires_series_id(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {"data": []},
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": []},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {"data": {"records": []}},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        paths = [req["path"] for req in self.server.requests]
        self.assertTrue(any(p.startswith("/api/v3/episodefile?") and "seriesId=" in p for p in paths))
        self.assertFalse(any(p == "/api/v3/episodefile" for p in paths))

    # t4 import record has no downloadId
    def test_import_record_no_download_id(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/config/downloadclient"): {"data": {"autoRedownloadFailed": False}},
            ("POST", "/api/v3/command"): {"status": 200},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        r = reports[0]
        self.assertFalse(r["blocklisted"])
        self.assertIn("downloadId", r["reason"])
        self.assertIsNone(r["grabbed_history_id"])
        paths = [req["path"] for req in self.server.requests]
        self.assertFalse(any("/api/v3/history/failed" in p for p in paths))
        # Search should still run
        self.assertTrue(any(req["path"] == "/api/v3/command" for req in self.server.requests))

    # t5 no grabbed record
    def test_no_grabbed_record(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {"data": {"records": []}},
            ("GET", "/api/v3/config/downloadclient"): {"data": {"autoRedownloadFailed": False}},
            ("POST", "/api/v3/command"): {"status": 200},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        r = reports[0]
        self.assertFalse(r["blocklisted"])
        self.assertIn("no grabbed history", r["reason"])
        self.assertIsNone(r["grabbed_history_id"])
        self.assertFalse(any("/api/v3/history/failed" in req["path"] for req in self.server.requests))

    # t6 history/failed returns 500
    def test_mark_failed_500(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/77"): {"status": 500},
            ("GET", "/api/v3/config/downloadclient"): {"data": {"autoRedownloadFailed": False}},
            ("POST", "/api/v3/command"): {"status": 200},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        with self.assertLogs(arr.log, level="ERROR") as ctx:
            reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        r = reports[0]
        self.assertFalse(r["blocklisted"])
        self.assertIn("HTTP 500", r["reason"])
        attempts = [req for req in self.server.requests
                    if req["method"] == "POST" and "/api/v3/history/failed" in req["path"]]
        self.assertLessEqual(len(attempts), 2)
        self.assertTrue(any(req["path"] == "/api/v3/command" for req in self.server.requests))
        self.assertTrue(any("HTTP 500" in m for m in ctx.output))

    # t7 history/failed returns 404 -> fallback
    def test_mark_failed_404_fallback(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/77"): {"status": 404},
            ("POST", "/api/v3/history/failed?id=77"): {"status": 200},
            ("GET", "/api/v3/blocklist?page=1&pageSize=50&sortKey=date&sortDirection=descending&seriesIds=1"): {
                "data": {"records": [{"id": 99, "sourceTitle": "Show.S01E01.1080p"}]}
            },
            ("GET", "/api/v3/config/downloadclient"): {"data": {"autoRedownloadFailed": False}},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        self.assertTrue(reports[0]["blocklisted"])
        attempts = [req for req in self.server.requests
                    if req["method"] == "POST" and req["path"] == "/api/v3/history/failed?id=77"]
        self.assertEqual(len(attempts), 1)

    # t8 verification gap
    def test_verification_gap(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/77"): {"status": 200},
            ("GET", "/api/v3/blocklist?page=1&pageSize=50&sortKey=date&sortDirection=descending&seriesIds=1"): {
                "data": {"records": []}
            },
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        with self.assertLogs(arr.log, level="ERROR") as ctx:
            reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)

        r = reports[0]
        self.assertFalse(r["blocklisted"])
        self.assertIn("check the arr log", r["reason"])
        self.assertTrue(any("check the arr log" in m for m in ctx.output))

    # t9 path mismatch resolves via basename fallback; unknown file -> matched=False
    def test_basename_fallback_and_unknown_file(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": "/other/path/Show S01E01.mkv"}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": "/other/path/Show S01E01.mkv"},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/77"): {"status": 200},
            ("GET", "/api/v3/blocklist?page=1&pageSize=50&sortKey=date&sortDirection=descending&seriesIds=1"): {
                "data": {"records": [{"id": 99, "sourceTitle": "Show.S01E01.1080p"}]}
            },
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)
        self.assertTrue(reports[0]["matched"])

        # Unknown file: no series match and parse returns nothing
        self.server.requests.clear()
        self.server.responses = {
            ("GET", "/api/v3/series"): {"data": []},
            ("GET", "/api/v3/parse?title=Unknown.mkv"): {"data": {}},
        }
        reports2 = arr.reject_in_arr("/tv/Unknown.mkv", cfg, delete_file_in_arr=True)
        self.assertFalse(reports2[0]["matched"])
        mutating = [req for req in self.server.requests
                    if req["method"] in ("DELETE", "POST")]
        self.assertEqual(len(mutating), 0)

    # t10 autoRedownloadFailed=true -> no command; false -> exactly one
    def test_auto_redownload_skips_search(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        base = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("POST", "/api/v3/history/failed/77"): {"status": 200},
            ("GET", "/api/v3/blocklist?page=1&pageSize=50&sortKey=date&sortDirection=descending&seriesIds=1"): {
                "data": {"records": [{"id": 99, "sourceTitle": "Show.S01E01.1080p"}]}
            },
        }

        # true
        responses = dict(base)
        responses[("GET", "/api/v3/config/downloadclient")] = {
            "data": {"autoRedownloadFailed": True}
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")
        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)
        self.assertTrue(reports[0]["blocklisted"])
        commands = [req for req in self.server.requests
                    if req["method"] == "POST" and req["path"] == "/api/v3/command"]
        self.assertEqual(len(commands), 0)

        # false
        responses2 = dict(base)
        responses2[("GET", "/api/v3/config/downloadclient")] = {
            "data": {"autoRedownloadFailed": False}
        }
        responses2[("POST", "/api/v3/command")] = {"status": 200}
        self.server.shutdown()
        self.server.server_close()
        self.server = _start_server(responses2)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}")
        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)
        commands = [req for req in self.server.requests
                    if req["method"] == "POST" and req["path"] == "/api/v3/command"]
        self.assertEqual(len(commands), 1)

    # t11 arr_blocklist_on_reject=False -> zero history/failed requests
    def test_blocklist_disabled(self):
        file_path = "/tv/Show/Season 01/Show S01E01.mkv"
        responses = {
            ("GET", "/api/v3/series"): {"data": [{"id": 1, "path": "/tv/Show"}]},
            ("GET", "/api/v3/episodefile?seriesId=1"): {
                "data": [{"id": 10, "seriesId": 1, "path": _norm(file_path)}]
            },
            ("GET", "/api/v3/episode?seriesId=1&episodeFileId=10"): {"data": [{"id": 100}]},
            ("DELETE", "/api/v3/episodefile/10"): {"status": 200},
            ("GET", "/api/v3/history/series?seriesId=1&eventType=3"): {
                "data": {
                    "records": [
                        {"id": 33, "eventType": 3, "downloadId": "d1",
                         "data": {"fileId": "10", "importedPath": _norm(file_path)},
                         "date": "2024-01-01T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/history?downloadId=d1&eventType=1&pageSize=50"): {
                "data": {
                    "records": [
                        {"id": 77, "eventType": 1, "downloadId": "d1",
                         "sourceTitle": "Show.S01E01.1080p", "date": "2023-12-31T00:00:00Z"}
                    ]
                }
            },
            ("GET", "/api/v3/config/downloadclient"): {"data": {"autoRedownloadFailed": False}},
            ("POST", "/api/v3/command"): {"status": 200},
        }
        self.server = _start_server(responses)
        port = self.server.server_address[1]
        cfg = _Cfg(f"http://127.0.0.1:{port}", blocklist=False, search=True)

        reports = arr.reject_in_arr(file_path, cfg, delete_file_in_arr=True)
        self.assertIsNone(reports[0]["blocklisted"])
        self.assertFalse(any("/api/v3/history/failed" in req["path"] for req in self.server.requests))

    # t12 source guard: no URL literal contains 'blacklist'
    def test_no_blacklist_in_url_literals(self):
        import ast

        def _string_literals(src):
            tree = ast.parse(src)
            docstring_nodes = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.body and isinstance(node.body[0], ast.Expr):
                        docstring_nodes.add(node.body[0].value)
                elif isinstance(node, ast.Module):
                    if node.body and isinstance(node.body[0], ast.Expr):
                        docstring_nodes.add(node.body[0].value)
            for node in ast.walk(tree):
                if node in docstring_nodes:
                    continue
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    yield node.value
                elif isinstance(node, ast.Str):  # Python <3.8 compatibility
                    yield node.s

        for name in ["arr.py", "scanner.py", "web/server.py"]:
            path = os.path.join(os.path.dirname(__file__), "..", name)
            with open(path) as f:
                src = f.read()
            for s in _string_literals(src):
                lower = s.lower()
                if "blacklist" in lower and ("/api/" in s or "http" in lower):
                    self.fail(f"URL literal containing 'blacklist' found in {name}: {s!r}")


if __name__ == "__main__":
    unittest.main()
