#!/usr/bin/env python3
"""Tests for the webhook module (stdlib only)."""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webhook


class _Handler204(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.captured_body = self.rfile.read(length)
        self.server.captured_headers = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(204)
        self.end_headers()


class _Handler403(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"blocked by cloudflare")


def _run_server(handler_cls, port=0):
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    server.captured_body = None
    server.captured_headers = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class WebhookTests(unittest.TestCase):
    def test_build_payload_discord_with_url(self):
        payload = webhook.build_payload(
            "discord.com",
            "msg",
            "Label",
            "https://example.com",
            {"event": "test"},
        )
        data = json.loads(payload)
        self.assertEqual(data["content"], "msg")
        self.assertEqual(data["embeds"], [{"title": "Label", "url": "https://example.com"}])

    def test_build_payload_discord_without_url(self):
        payload = webhook.build_payload(
            "discord.com",
            "msg",
            "Label",
            "",
            {"event": "test"},
        )
        data = json.loads(payload)
        self.assertEqual(data["content"], "msg")
        self.assertNotIn("embeds", data)

    def test_build_payload_canary_discordapp(self):
        payload = webhook.build_payload(
            "canary.discordapp.com",
            "msg",
            "Label",
            "http://example.com",
            {"event": "test"},
        )
        data = json.loads(payload)
        self.assertEqual(data["content"], "msg")
        self.assertEqual(data["embeds"], [{"title": "Label", "url": "http://example.com"}])

    def test_build_payload_slack(self):
        payload = webhook.build_payload(
            "hooks.slack.com",
            "msg",
            "Label",
            "https://example.com",
            {"event": "test"},
        )
        data = json.loads(payload)
        self.assertEqual(data, {"text": "msg"})

    def test_build_payload_ntfy(self):
        payload = webhook.build_payload(
            "ntfy.sh",
            "msg",
            "Label",
            "https://example.com",
            {"event": "test", "foo": "bar"},
        )
        data = json.loads(payload)
        self.assertIn("event", data)
        self.assertIn("foo", data)
        self.assertEqual(data["foo"], "bar")

    def test_send_user_agent_and_body(self):
        server = _run_server(_Handler204)
        port = server.server_address[1]
        try:
            ok, detail = webhook.send(
                f"http://127.0.0.1:{port}/webhook",
                json.dumps({"test": "body"}).encode(),
                "1.0.8",
            )
            self.assertTrue(ok)
            self.assertEqual(detail, "")
            ua = server.captured_headers.get("user-agent", "")
            self.assertTrue(ua.startswith("SafeScanarr/"))
            self.assertNotIn("Python-urllib", ua)
            self.assertEqual(server.captured_headers.get("content-type"), "application/json")
            self.assertEqual(json.loads(server.captured_body), {"test": "body"})
        finally:
            server.shutdown()
            server.server_close()

    def test_send_403(self):
        server = _run_server(_Handler403)
        port = server.server_address[1]
        try:
            ok, detail = webhook.send(
                f"http://127.0.0.1:{port}/webhook",
                json.dumps({"test": "body"}).encode(),
                "1.0.8",
            )
            self.assertFalse(ok)
            self.assertIn("403", detail)
            self.assertIn("blocked by cloudflare", detail)
        finally:
            server.shutdown()
            server.server_close()

    def test_send_strips_url(self):
        server = _run_server(_Handler204)
        port = server.server_address[1]
        try:
            ok, detail = webhook.send(
                f"  \n http://127.0.0.1:{port}/webhook \n  ",
                json.dumps({"test": "body"}).encode(),
                "1.0.8",
            )
            self.assertTrue(ok)
            self.assertEqual(detail, "")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
