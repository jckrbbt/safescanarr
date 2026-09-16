"""
safescanarr/web/auth.py

Method-aware authentication for the web UI.

Auth methods:
  - ''         : unconfigured (first-run setup)
  - 'token'    : shared token (legacy / script / Docker mode)
  - 'password' : password hash (browser mode)

Token resolution (first match wins):
  1. ``SS_TOKEN`` environment variable -> implies token mode
  2. ``auth_token`` field in config.json -> implies token mode (legacy migration)
  3. Generated once on first-run setup only if user chooses token mode

Password mode stores a werkzeug pbkdf2:sha256 hash.

Fail-closed: if auth cannot be resolved or verified, access is denied.
"""

import hmac
import logging
import os
import secrets
import threading
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

import config as config_module

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

# pbkdf2:sha256 with 600k iterations (Werkzeug default). Keep this in a constant
# so it can be swapped for argon2id in the future without parsing hashes.
_HASH_METHOD = "pbkdf2:sha256:600000"

# Simple list of the most common human-chosen passwords. These are rejected for
# the password method. Keeping it small means no external dependency and no
# false-positive surprises.
_COMMON_PASSWORDS = {
    "password", "123456", "12345678", "qwerty", "password123",
    "admin", "letmein", "welcome", "monkey", "123456789",
    "12345678", "abc123", "football", "iloveyou", "admin123",
    "trustno1", "sunshine", "princess", "password1", "dragon",
}


def _common_passwords_set() -> set:
    return _COMMON_PASSWORDS


class _AuthState:
    def __init__(self):
        self.method: str = ""  # '', 'token', or 'password'
        self.token_value: Optional[str] = None
        self.password_hash: Optional[str] = None
        self.configured: bool = False


_auth_state = _AuthState()


def _load_state() -> _AuthState:
    """Load or refresh auth state from config/environment."""
    global _auth_state
    state = _AuthState()

    # SS_TOKEN always wins and implies token mode
    env = (os.environ.get("SS_TOKEN") or "").strip()
    if env:
        state.method = "token"
        state.token_value = env
        state.configured = True
        _auth_state = state
        return state

    cfg = config_module.raw()
    method = (cfg.get("auth_method") or "").strip()
    token = (cfg.get("auth_token") or "").strip()
    pwd_hash = (cfg.get("auth_password_hash") or "").strip()

    if method == "password" and pwd_hash:
        state.method = "password"
        state.password_hash = pwd_hash
        state.configured = True
    elif method == "token" and token:
        state.method = "token"
        state.token_value = token
        state.configured = True
    elif token:
        # Legacy migration: an auth_token without an method means this is an
        # older install that used shared-token auth. Adopt token mode and write
        # it back so we don't keep hitting this path.
        state.method = "token"
        state.token_value = token
        state.configured = True
        _save_method(method="token", token=token)
    else:
        state.configured = False

    _auth_state = state
    return state


def _save_method(*, method: str, token: Optional[str] = None,
                  password_hash: Optional[str] = None) -> None:
    """Persist auth method and credentials to config.json (mode 0600)."""
    cfg = config_module.raw()
    cfg["auth_method"] = method
    if method == "token":
        cfg["auth_token"] = token or ""
        cfg["auth_password_hash"] = ""
    elif method == "password":
        cfg["auth_token"] = ""
        cfg["auth_password_hash"] = password_hash or ""
    else:
        cfg["auth_token"] = ""
        cfg["auth_password_hash"] = ""
    config_module.save(cfg)


def is_configured() -> bool:
    """Return True once an auth method has been chosen."""
    with _LOCK:
        return _load_state().configured


def method() -> str:
    """Return current auth method: '', 'token', or 'password'."""
    with _LOCK:
        return _load_state().method


def token() -> str:
    """Return the configured token in token mode. Returns '' otherwise."""
    with _LOCK:
        state = _load_state()
        if state.method == "token":
            return state.token_value or ""
        return ""


def verify(supplied: str) -> bool:
    """Verify a supplied secret against the configured auth method."""
    if not supplied:
        return False
    with _LOCK:
        state = _load_state()
        if state.method == "token":
            expected = state.token_value or ""
            if not expected:
                return False
            return hmac.compare_digest(str(supplied), expected)
        if state.method == "password":
            expected = state.password_hash or ""
            if not expected:
                return False
            try:
                return check_password_hash(expected, str(supplied))
            except Exception:  # pragma: no cover - malformed hash
                return False
        return False


def configure_password(pw: str, confirm: Optional[str] = None) -> None:
    """Set password auth. Raises ValueError on policy violations."""
    if not pw or len(pw) < 10:
        raise ValueError("Password must be at least 10 characters long.")
    if confirm is not None and pw != confirm:
        raise ValueError("Passwords do not match.")
    if pw.lower() in _common_passwords_set():
        raise ValueError("Password is too common; choose a stronger one.")
    # Basic entropy check: at least one digit or symbol + mixed case
    if not any(c.isdigit() or not c.isalnum() for c in pw):
        raise ValueError("Password must contain at least one number or symbol.")
    if not (any(c.islower() for c in pw) and any(c.isupper() for c in pw)):
        raise ValueError("Password must contain both lower and upper case letters.")

    h = generate_password_hash(pw, method=_HASH_METHOD)
    _save_method(method="password", password_hash=h)
    _load_state()


def configure_token(existing: Optional[str] = None) -> str:
    """Set token auth. Returns the configured token (existing or newly generated)."""
    t = existing.strip() if existing else secrets.token_urlsafe(32)
    _save_method(method="token", token=t)
    _load_state()
    return t


def reset() -> None:
    """Clear auth configuration, returning to first-run setup state."""
    _save_method(method="", token="", password_hash="")
    _load_state()


def reset_cache() -> None:
    """Drop cached state (tests / out-of-band config changes)."""
    global _auth_state
    with _LOCK:
        _auth_state = _AuthState()
