"""
safescanarr/web/auth.py

Single shared-token authentication for the web UI.

Token resolution (first match wins):
  1. ``SS_TOKEN`` environment variable
  2. ``auth_token`` field in config.json
  3. Generated on first run (secrets.token_urlsafe) and saved to config.json,
     which is written with mode 0600

Fail-closed: if no token can be resolved for any reason, every protected
request is denied instead of being allowed through.
"""

import hmac
import logging
import os
import secrets
import threading

import config as config_module

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cached_token = None


def _generate_and_store() -> str:
    token = secrets.token_urlsafe(32)
    cfg = config_module.raw()
    cfg["auth_token"] = token
    config_module.save(cfg)          # written 0600
    return token


def token() -> str:
    """Return the shared auth token, generating one on first run."""
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    with _lock:
        if _cached_token is not None:
            return _cached_token

        env = (os.environ.get("SS_TOKEN") or "").strip()
        if env:
            _cached_token = env
            return _cached_token

        try:
            stored = (config_module.raw().get("auth_token") or "").strip()
            if stored:
                _cached_token = stored
                return _cached_token
            _cached_token = _generate_and_store()
            log.warning(
                "No auth token configured — generated one and stored it in "
                "config.json (0600). Set SS_TOKEN to manage it via the environment."
            )
        except Exception as e:  # fail closed
            log.error("Could not resolve auth token — denying all requests: %s", e)
            _cached_token = ""
        return _cached_token


def check(supplied: str) -> bool:
    """Constant-time comparison of a supplied token against the configured one."""
    expected = token()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(supplied), expected)


def reset_cache() -> None:
    """Drop the cached token (tests / after an out-of-band config change)."""
    global _cached_token
    _cached_token = None
