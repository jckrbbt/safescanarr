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
import config_lock

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
    "abc123", "football", "iloveyou", "admin123",
    "trustno1", "sunshine", "princess", "password1", "dragon",
    "master",
}


def _common_passwords_set() -> set:
    return _COMMON_PASSWORDS


def _parse_epoch(value) -> int:
    """session_epoch is int-like and non-negative; anything else is invalid."""
    n = int(value or 0)
    if n < 0:
        raise ValueError("negative session_epoch")
    return n


class _AuthState:
    def __init__(self):
        self.method: str = ""  # '', 'token', or 'password'
        self.token_value: Optional[str] = None
        self.password_hash: Optional[str] = None
        # Generation counter; None means "epoch unavailable" (unreadable
        # config) — distinct from an explicit stored/default 0, and always
        # fails closed (no fingerprint is issued).
        self.epoch: Optional[int] = 0
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
        try:
            cfg = config_module.raw()
            state.epoch = _parse_epoch(cfg.get("session_epoch"))
        except (ValueError, TypeError):
            state.epoch = None  # non-numeric epoch: unavailable, fail closed
        except Exception:  # unreadable config (I/O, malformed JSON, ...)
            state.epoch = None
        _auth_state = state
        return state

    cfg = config_module.raw()
    method = (cfg.get("auth_method") or "").strip()
    token = (cfg.get("auth_token") or "").strip()
    pwd_hash = (cfg.get("auth_password_hash") or "").strip()
    try:
        state.epoch = _parse_epoch(cfg.get("session_epoch"))
    except (ValueError, TypeError):
        state.epoch = None

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
                  password_hash: Optional[str] = None,
                  bump_epoch: bool = False) -> None:
    """Persist auth method and credentials to config.json (mode 0600).

    Every credential transition bumps the monotonic session_epoch under the
    cross-process config lock, so returning to an earlier credential text
    (token A→B→A, reset + same-token setup) can never resurrect old cookies.
    """
    def apply(cfg):
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
        if bump_epoch:
            cfg["session_epoch"] = int(cfg.get("session_epoch") or 0) + 1
    config_lock.mutate(apply)


# Called (best effort) whenever a credential transition invalidates every
# session, so remembered web_sessions rows are revoked alongside the epoch
# bump. The web process and authtool each register their own.
_revocation_hooks: list = []


def register_revocation_hook(fn) -> None:
    """Register a callable run after each credential-transition epoch bump."""
    _revocation_hooks.append(fn)


def _run_revocation_hooks() -> None:
    for fn in list(_revocation_hooks):
        try:
            fn()
        except Exception as e:
            log.warning("Revocation hook %r failed: %s", fn, e)


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


def verify(supplied: str) -> Optional[int]:
    """Verify a supplied secret against the configured auth method.

    Returns the session generation (session_epoch) the credential was
    verified against, or None on failure. Callers that establish a session
    from the result must pass that generation to the fingerprint check so a
    credential that was rotated away mid-request cannot mint a session
    stamped with the new credential's fingerprint.
    """
    if not supplied:
        return None
    with _LOCK:
        try:
            state = _load_state()
        except Exception:  # unreadable config: fail closed
            return None
        if state.method == "token":
            expected = state.token_value or ""
            if not expected:
                return None
            return state.epoch if hmac.compare_digest(str(supplied), expected) else None
        if state.method == "password":
            expected = state.password_hash or ""
            if not expected:
                return None
            try:
                return state.epoch if check_password_hash(expected, str(supplied)) else None
            except Exception:  # pragma: no cover - malformed hash
                return None
        return None


def generation() -> Optional[int]:
    """Current session generation (epoch), or None if unavailable."""
    with _LOCK:
        try:
            return _load_state().epoch
        except Exception:  # unreadable config
            return None


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
    _save_method(method="password", password_hash=h, bump_epoch=True)
    _load_state()
    _run_revocation_hooks()


def configure_token(existing: Optional[str] = None) -> str:
    """Set token auth. Returns the configured token (existing or newly generated)."""
    t = existing.strip() if existing else secrets.token_urlsafe(32)
    _save_method(method="token", token=t, bump_epoch=True)
    _load_state()
    _run_revocation_hooks()
    return t


class AlreadyConfiguredError(Exception):
    """Raised when a setup/configuration write is attempted after auth is set."""
    pass


def configure_once(password=None, confirm=None, existing_token=None) -> str:
    """Atomically check that auth is unconfigured, then configure it.

    Returns the token value for token mode. Raises AlreadyConfiguredError if
    another thread/process configured auth in the meantime. Raises ValueError
    for invalid password policy. Only one of ``password`` or ``existing_token``
    should be supplied.
    """
    with _LOCK:
        state = _load_state()
        if state.configured:
            raise AlreadyConfiguredError("Already configured")
        if password is not None:
            configure_password(password, confirm)
            return ""
        if existing_token is not None:
            return configure_token(existing_token)
        return configure_token()


def reset() -> None:
    """Clear auth configuration, returning to first-run setup state.

    Bumps the generation and revokes remembered rows too: a reset followed by
    re-setup with the *same* credential text must still kill old cookies.
    """
    _save_method(method="", token="", password_hash="", bump_epoch=True)
    _load_state()
    _run_revocation_hooks()


def reset_cache() -> None:
    """Drop cached state (tests / out-of-band config changes)."""
    global _auth_state
    with _LOCK:
        _auth_state = _AuthState()


def session_fingerprint(key: str, generation_value: Optional[int] = None) -> str:
    """Bind every session to the current auth credential + session_epoch.

    Returns "" when auth is not configured **or when the session epoch is
    unavailable** (unreadable/unparseable config) — fail closed, never
    substitute epoch 0. Any password/token change, a reset + re-setup, or a
    session_epoch bump changes the fingerprint, which invalidates every
    existing session cookie on the next request.

    ``generation_value`` pins the epoch used for the fingerprint (used by
    login: a credential verified at generation N must only produce a
    fingerprint for N, not whatever generation is current later).
    """
    with _LOCK:
        try:
            state = _load_state()
        except Exception:  # unreadable config: fail closed, no fingerprint
            return ""
        if not state.configured:
            return ""
        epoch = state.epoch if generation_value is None else generation_value
        if epoch is None:
            return ""  # epoch unavailable: fail closed, no fingerprint
        method = state.method
        credential = (state.token_value if method == "token"
                      else state.password_hash) or ""
        msg = f"v1|{method}|{credential}|{epoch}"
        return hmac.new(key.encode(), msg.encode(), "sha256").hexdigest()[:32]


def revoke_all_sessions() -> int:
    """Bump session_epoch, signing out every device. Returns the new epoch."""
    with _LOCK:
        def apply(cfg):
            cfg["session_epoch"] = int(cfg.get("session_epoch") or 0) + 1
        cfg = config_lock.mutate(apply)
        _load_state()
        return int(cfg["session_epoch"])
