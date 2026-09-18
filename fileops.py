"""
safescanarr/fileops.py

Filesystem helpers for safely moving / copying videos between the library
and the quarantine folder.  Stdlib only; no app imports.
"""

import errno
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

MOVED = "moved"
COPIED_SOURCE_KEPT = "copied_source_kept"

# Errnos that mean the source could not be removed.  After a successful copy,
# these are treated as "source retained" rather than a fatal failure.
RETAIN_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}

# Errnos for which os.rename() and friends can fall back to a copy+unlink.
FALLBACK_ERRNOS = {errno.EXDEV, errno.EACCES, errno.EPERM, errno.EROFS, errno.EBUSY}


def unique_target(dst: Path) -> Path:
    """Return a path that does not collide with an existing file.

    Never clobbers an existing file: if ``dst`` exists, append a timestamp,
    then try _1 .. _999 suffixes if even the timestamped name exists.
    """
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    ts = int(time.time())
    candidate = dst.parent / f"{stem}_{ts}{suffix}"
    if not candidate.exists():
        return candidate
    for i in range(1, 1000):
        candidate = dst.parent / f"{stem}_{ts}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    # Should be impossible in practice, but raise a clear error if it happens.
    raise OSError(errno.EEXIST, f"could not find unique target for {dst}")


def looks_deletable(directory: Path) -> bool:
    """Advisory check: True if the directory appears writable/deletable."""
    return os.access(directory, os.W_OK | os.X_OK)


def _src_size(src: Path) -> int:
    """Return the current size of ``src``; raise a clear error on failure."""
    try:
        return src.stat().st_size
    except OSError as e:
        raise OSError(errno.EIO, f"cannot stat source {src}: {e}")


def relocate(src: Path, dst: Path, *, verify: bool = True) -> tuple[Path, str]:
    """Move ``src`` to ``dst`` with robust cross-device / read-only handling.

    Behaviour:
      1. Ensures ``dst.parent`` exists (OSError propagates).
      2. Picks a non-colliding final destination with :func:`unique_target`.
      3. Attempts a fast ``os.rename(src, dst)``.
      4. If that fails with EXDEV/EACCES/EPERM/EROFS/EBUSY, falls back to an
         atomic copy via ``.sspart`` temporary file and ``os.replace``.
      5. Verifies the copied size matches the source if ``verify`` is True.
      6. Best-effort removes the source.  If removal fails with EACCES/EPERM/
         EROFS, the destination is still complete and the returned outcome is
         :data:`COPIED_SOURCE_KEPT`; other unlink errors are logged but not
         raised (the caller already has the quarantined copy).

    Contract: on success, the returned path is complete and valid.  On raise,
    the source is unchanged and nothing exists at the final destination name.
    """
    if not isinstance(src, Path):
        src = Path(src)
    if not isinstance(dst, Path):
        dst = Path(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = unique_target(dst)

    src_size = _src_size(src)

    # Quick same-device move.
    try:
        os.rename(src, dst)
        return dst, MOVED
    except OSError as e:
        if e.errno not in FALLBACK_ERRNOS:
            raise

    # Check free space before copying.
    try:
        free = shutil.disk_usage(dst.parent).free
    except OSError as e:
        raise OSError(errno.EIO, f"cannot check free space on {dst.parent}: {e}")
    if free < src_size * 1.1:
        raise OSError(
            errno.ENOSPC,
            f"insufficient free space in {dst.parent}: need {src_size * 1.1:.0f} bytes, "
            f"have {free} bytes",
        )

    # Copy to a temp file beside the destination, then atomically replace.
    tmp = dst.with_name(dst.name + ".sspart")
    try:
        shutil.copy2(str(src), str(tmp))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    try:
        if verify and tmp.stat().st_size != src_size:
            copied_size = tmp.stat().st_size
            tmp.unlink(missing_ok=True)
            raise OSError(errno.EIO, f"short copy: {copied_size} != {src_size}")

        os.replace(tmp, dst)

        try:
            os.unlink(src)
        except FileNotFoundError:
            return dst, MOVED
        except OSError as e:
            log.warning(
                "relocate copied source but cannot remove %s (errno %d: %s); "
                "source retained in library",
                src, e.errno, e.strerror,
            )
            # On a *copy* fallback, if the source is not removed we report the
            # retained state regardless of the specific errno.  EACCES/EPERM/
            # EROFS are expected; anything else is a surprise but the copy is
            # already complete so we still mark it retained instead of failing.
            return dst, COPIED_SOURCE_KEPT
        return dst, MOVED
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
