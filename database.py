"""
safescanarr/database.py
SQLite wrapper — stores one row per tracked video file,
plus a key/value state table used by the poller.
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

# Generic key/value store — used by the poller to track watermarks
CREATE_STATE = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(db_path)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute(CREATE_FILES)
        self._con.execute(CREATE_ERRORS)
        self._con.execute(CREATE_STATE)
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
                    status: str = "ok") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            """
            INSERT INTO files (path, name, size, mtime, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name       = excluded.name,
                size       = excluded.size,
                mtime      = excluded.mtime,
                status     = excluded.status,
                updated_at = excluded.updated_at
            """,
            (path, name, size, mtime, status, now),
        )
        self._con.commit()

    def delete_file(self, path: str) -> None:
        self._con.execute("DELETE FROM files WHERE path = ?", (path,))
        self._con.commit()

    def list_files(self) -> list:
        cur = self._con.execute(
            "SELECT path, name, size, mtime, status, updated_at "
            "FROM files ORDER BY updated_at DESC"
        )
        return cur.fetchall()

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

    def close(self) -> None:
        self._con.close()

    def find_file_by_stem(self, stem: str) -> Optional[sqlite3.Row]:
        """Find a file record where the filename stem matches."""
        cur = self._con.execute(
            "SELECT path, name, size, mtime, status FROM files WHERE name LIKE ?",
            (stem + ".%",)
        )
        return cur.fetchone()
