"""
safescanarr/pathutil.py

Shared path helpers.

Path containment is always decided on *resolved* paths so that symlinks and
``..`` segments cannot escape a configured root.
"""

import hashlib
from pathlib import Path


def is_within(path, roots) -> bool:
    """True if *path* resolves to a location inside any of *roots*.

    Uses ``Path.resolve()`` on both sides and ``is_relative_to`` so that
    ``/media/foo/../..`` or a symlink pointing outside a watch folder is
    rejected. Empty/blank roots are ignored.
    """
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in roots or []:
        if not root:
            continue
        try:
            base = Path(root).resolve()
        except (OSError, ValueError):
            continue
        if target == base:
            return True
        if target.is_relative_to(base):
            return True
    return False


def sheet_filename(source_path) -> str:
    """Contact-sheet filename for a source video: ``<stem>_<hash8>.jpg``.

    The hash is derived from the full resolved source path so that two videos
    with the same stem in different folders no longer collide.
    """
    p = Path(source_path)
    digest = hashlib.sha1(str(p.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{p.stem}_{digest}.jpg"


def legacy_sheet_filename(source_path) -> str:
    """Pre-hash-suffix sheet naming, kept so existing installs keep working."""
    return Path(source_path).stem + ".jpg"


def resolve_sheet(output_dir, source_path) -> Path:
    """Return the on-disk sheet for *source_path*.

    Prefers the current (hash-suffixed) name, falls back to the legacy
    ``<stem>.jpg`` name for sheets written by older versions. When neither
    exists, returns the current-name target (used when generating a new sheet).
    """
    out = Path(output_dir)
    current = out / sheet_filename(source_path)
    if current.exists():
        return current
    legacy = out / legacy_sheet_filename(source_path)
    if legacy.exists():
        return legacy
    return current
