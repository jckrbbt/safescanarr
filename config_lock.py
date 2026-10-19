"""safescanarr/config_lock.py — interprocess-safe config.json mutation.

Every read-modify-write of config.json must go through mutate(), which holds
an exclusive flock across read + mutate + write. This serialises the web
process with ``python -m web.authtool`` (and concurrent web threads) so two
writers can never clobber each other's updates — in v1.0.11 a stale config
snapshot rolled ``session_epoch`` back and revived revoked sessions.

config.save() itself takes this lock, so there is no unlocked write path
left; the lock is re-entrant per thread (mutate() may call raw()/save()
inside itself). ``os.replace`` inside the write already prevents torn JSON;
the lock prevents lost updates. session_epoch can never be lowered.

Trust boundary: the lock is advisory and only as trustworthy as BASE_DIR.
An attacker with write access to BASE_DIR can unlink/replace config.json
itself, so they already own the config; the hardening below (O_NOFOLLOW,
mode check, inode re-verification) stops silent lock-file substitution, not
a hostile BASE_DIR owner. BASE_DIR must not be writable by untrusted local
users.
"""

import errno
import fcntl
import os
import stat
import threading

import config as config_module


def _lock_path() -> str:
    return config_module._config_path() + ".lock"


# flock() is per-fd and would self-deadlock on nested acquisition by the
# same thread (mutate() -> raw() -> save()); track per-thread ownership.
_tls = threading.local()


class LockValidationError(RuntimeError):
    """The lock file failed validation (symlink / bad mode / replaced inode).

    The config write is aborted rather than proceeding on a lock an attacker
    may hold or have substituted.
    """


def _validate_lock_fd(fd: int, path: str) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise LockValidationError(f"{path}: not a regular file")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise LockValidationError(
            f"{path}: owned by uid {st.st_uid}, expected {os.geteuid()}")
    if st.st_mode & 0o022:
        raise LockValidationError(
            f"{path}: mode {oct(st.st_mode & 0o777)} is group/other writable")


def _open_lock_file(path: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK) or (
                hasattr(os, "O_NOFOLLOW") and e.errno == errno.EPERM):
            raise LockValidationError(f"{path}: symlink lock file rejected") from e
        raise
    try:
        _validate_lock_fd(fd, path)
    except Exception:
        os.close(fd)
        raise
    return fd


def _inode_matches(fd: int, path: str) -> bool:
    """True if fd still refers to the file currently at *path*."""
    try:
        return os.fstat(fd).st_ino == os.stat(path).st_ino
    except OSError:
        return False


class _FileLock:
    """Exclusive advisory lock next to config.json (flock, per-fd).

    Re-entrant within a thread; cross-process and cross-thread exclusion
    still comes from flock() on the shared file.
    """

    def __enter__(self):
        depth = getattr(_tls, "depth", 0)
        if depth > 0:
            _tls.depth = depth + 1
            return self
        path = _lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = _open_lock_file(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # The path may have been unlinked/replaced while we waited for
            # flock; our lock would then guard a dead inode and a concurrent
            # opener could take the "same" lock on the new file.
            if not _inode_matches(fd, path):
                raise LockValidationError(
                    f"{path}: lock file replaced while locking")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._fd = fd
        _tls.depth = 1
        return self

    def __exit__(self, *exc):
        _tls.depth -= 1
        if _tls.depth > 0:
            return False
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
        return False


def held_by_current_thread() -> bool:
    return getattr(_tls, "depth", 0) > 0


def mutate(mutator) -> dict:
    """Atomically read-modify-write config.json under the cross-process lock.

    ``mutator(cfg)`` edits the freshly-read dict in place. After the mutator
    runs, ``session_epoch`` is clamped back up to the value current when the
    lock was taken, so an ordinary (or stale) config save can never write a
    lower generation. Returns the persisted config dict.

    ponytail: one global lock for all config writes — per-key or per-section
    locks if write latency ever matters.
    """
    with _FileLock():
        cfg = config_module.raw()
        epoch_before = int(cfg.get("session_epoch") or 0)
        mutator(cfg)
        try:
            epoch_after = int(cfg.get("session_epoch") or 0)
        except (TypeError, ValueError):
            epoch_after = epoch_before
        if epoch_after < epoch_before:
            cfg["session_epoch"] = epoch_before
        # Route through save() so the write is the same locked+clamped
        # primitive everywhere (re-entrant lock: no self-deadlock).
        config_module.save(cfg)
        return cfg
