"""
safescanarr/database.py
SQLite wrapper — stores one row per tracked video file,
plus a key/value state table used by the poller,
plus an auto_handled table for Safe Mode audit trail.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CREATE_FILES = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    status      TEXT NOT NULL,
    flagged     INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,
    updated_at  TEXT NOT NULL
);
"""

CREATE_ERRORS = """
CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,
    message     TEXT,
    recorded_at TEXT NOT NULL
);
"""

CREATE_STATE = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

CREATE_AUTO_HANDLED = """
CREATE TABLE IF NOT EXISTS auto_handled (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL,
    name         TEXT NOT NULL,
    sheet_name   TEXT,
    reason       TEXT,
    confidence   REAL,
    handled_at   TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(db_path)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute(CREATE_FILES)
        # Migrate: add flagged columns if they don't exist yet
        cols = [r[1] for r in self._con.execute("PRAGMA table_info(files)").fetchall()]
        if "flagged" not in cols:
            self._con.execute("ALTER TABLE files ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0")
        if "flag_reason" not in cols:
            self._con.execute("ALTER TABLE files ADD COLUMN flag_reason TEXT")
        self._con.execute(CREATE_ERRORS)
        self._con.execute(CREATE_STATE)
        self._con.execute(CREATE_AUTO_HANDLED)
        self._con.commit()

    # ------------------------------------------------------------------
    # Files table
    # ------------------------------------------------------------------

    def get_file(self, path: str) -> Optional[sqlite3.Row]:
        cur = self._con.execute(
            "SELECT path, size, mtime, status FROM files WHERE path = ?", (path,)
        )
        return cur.fetchone()

    def upsert_file(self, path: str, name: str, size: int, mtime: float,
                    status: str = "ok", flagged: bool = False,
                    flag_reason: str = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            """
            INSERT INTO files (path, name, size, mtime, status, flagged, flag_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name        = excluded.name,
                size        = excluded.size,
                mtime       = excluded.mtime,
                status      = excluded.status,
                flagged     = excluded.flagged,
                flag_reason = excluded.flag_reason,
                updated_at  = excluded.updated_at
            """,
            (path, name, size, mtime, status, int(flagged), flag_reason, now),
        )
        self._con.commit()

    def delete_file(self, path: str) -> None:
        self._con.execute("DELETE FROM files WHERE path = ?", (path,))
        self._con.commit()

    def list_files(self) -> list:
        cur = self._con.execute(
            "SELECT path, name, size, mtime, status, flagged, flag_reason, updated_at "
            "FROM files ORDER BY flagged DESC, updated_at DESC"
        )
        return cur.fetchall()

    def find_file_by_stem(self, stem: str) -> Optional[sqlite3.Row]:
        """Find a file record where the filename stem matches."""
        cur = self._con.execute(
            "SELECT path, name, size, mtime, status, flagged, flag_reason FROM files WHERE name LIKE ?",
            (stem + ".%",)
        )
        return cur.fetchone()

    def clean_missing_files(self, watch_folders: list) -> int:
        """
        Remove DB records for files that no longer exist on disk
        OR are outside all current watch folders.
        Returns the number of records removed.
        """
        rows = self.list_files()
        removed = 0
        for row in rows:
            path = Path(row["path"])
            in_watch = any(row["path"].startswith(f) for f in watch_folders)
            if not path.exists() or not in_watch:
                self.delete_file(row["path"])
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # Errors table
    # ------------------------------------------------------------------

    def record_error(self, path: str, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "INSERT INTO errors (path, message, recorded_at) VALUES (?, ?, ?)",
            (path, message, now),
        )
        self._con.commit()

    # ------------------------------------------------------------------
    # State table (poller watermarks)
    # ------------------------------------------------------------------

    def get_poller_state(self, key: str) -> Optional[str]:
        cur = self._con.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else None

    def set_poller_state(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._con.commit()

    # ------------------------------------------------------------------
    # Auto-handled table (Safe Mode audit trail)
    # ------------------------------------------------------------------

    def record_auto_handled(self, path: str, name: str, sheet_name: str,
                             reason: str, confidence: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "INSERT INTO auto_handled (path, name, sheet_name, reason, confidence, handled_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, name, sheet_name, reason, confidence, now),
        )
        self._con.commit()

    def list_auto_handled(self) -> list:
        cur = self._con.execute(
            "SELECT id, path, name, sheet_name, reason, confidence, handled_at "
            "FROM auto_handled ORDER BY handled_at DESC"
        )
        return cur.fetchall()

    def delete_auto_handled(self, record_id: int) -> None:
        self._con.execute("DELETE FROM auto_handled WHERE id = ?", (record_id,))
        self._con.commit()

    # ------------------------------------------------------------------

    def close(self) -> None:
        self._con.close()
