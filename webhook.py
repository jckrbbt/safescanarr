#!/usr/bin/env python3
"""Shared webhook helpers for SafeScanarr."""

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

USER_AGENT = 'SafeScanarr/{version} (+https://github.com/jckrbbt/safescanarr)'


def url_host(url: str) -> str:
    """Return the hostname from a URL, or '?' if the URL is invalid."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def build_payload(host: str, message: str, event_label: str, url: str, body: dict) -> bytes:
    """Build a provider-specific webhook payload as JSON-encoded bytes.

    Discord and Slack reject unknown JSON bodies, so send the minimal shape
    they expect; generic providers (ntfy JSON publish, Gotify) get the full
    event body.
    """
    host = (host or "").lower()
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        discord_body: dict = {"content": message}
        if url and (url.startswith("http://") or url.startswith("https://")):
            discord_body["embeds"] = [{"title": event_label, "url": url}]
        return json.dumps(discord_body).encode()
    if host.endswith("hooks.slack.com"):
        return json.dumps({"text": message}).encode()
    return json.dumps(body).encode()


def send(webhook_url: str, payload: bytes, version: str):
    """POST a JSON payload to *webhook_url*.

    Returns ``(ok, detail)``.  The URL/token is never logged.  HTTP errors are
    returned in *detail* with the response body truncated to ~300 characters.
    """
    webhook_url = webhook_url.strip()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT.format(version=version),
        "Accept": "application/json",
    }
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True, ""
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            text = str(raw)
        if len(text) > 300:
            text = text[:300] + "…"
        detail = f"HTTP {e.code}: {text}" if text else f"HTTP {e.code}"
        log.warning("Webhook failed (host=%s): %s", url_host(webhook_url), detail)
        return False, detail
    except Exception as e:
        log.warning("Webhook failed (host=%s): %s", url_host(webhook_url), e)
        return False, str(e)
