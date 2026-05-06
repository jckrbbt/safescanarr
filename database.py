"""
safescanarr/database.py
SQLite wrapper with full state machine support for v0.6.

review_state values:
  pending     — new, awaiting review
  approved    — confirmed clean (by user or auto-approve zone)
  quarantined — NSFW detected; video moved to quarantine folder
  rejected    — confirmed bad; video deleted, sheet deleted
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CREATE_FILES = """
CREATE TABLE IF NOT EXISTS files (
    path              TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    size              INTEGER NOT NULL,
    mtime             REAL NOT NULL,
    status            TEXT NOT NULL DEFAULT 'ok',
    review_state      TEXT NOT NULL DEFAULT 'pending',
    flagged           INTEGER NOT NULL DEFAULT 0,
    flag_reason       TEXT,
    nsfw_confidence   REAL,
    quarantine_path   TEXT,
    state_updated_at  TEXT,
    updated_at        TEXT NOT NULL
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

# Migrations: columns added in v0.6
MIGRATIONS = [
    "ALTER TABLE files ADD COLUMN review_state TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE files ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE files ADD COLUMN flag_reason TEXT",
    "ALTER TABLE files ADD COLUMN nsfw_confidence REAL",
    "ALTER TABLE files ADD COLUMN quarantine_path TEXT",
    "ALTER TABLE files ADD COLUMN state_updated_at TEXT",
]


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute(CREATE_FILES)
        self._con.execute(CREATE_ERRORS)
        self._con.execute(CREATE_STATE)
        self._run_migrations()
        # Migrate existing 'ok' records to 'approved'
        self._con.execute(
            "UPDATE files SET review_state = 'approved' "
            "WHERE review_state = 'pending' AND status = 'ok' "
            "AND updated_at < datetime('now', '-1 minute')"
        )
        self._con.commit()

    def _run_migrations(self) -> None:
        cols = [r[1] for r in self._con.execute("PRAGMA table_info(files)").fetchall()]
        for sql in MIGRATIONS:
            col = sql.split("ADD COLUMN")[1].strip().split()[0]
            if col not in cols:
                try:
                    self._con.execute(sql)
                except Exception:
                    pass
        self._con.commit()

    # ------------------------------------------------------------------
    # Files table
    # ------------------------------------------------------------------

    def get_file(self, path: str) -> Optional[sqlite3.Row]:
        cur = self._con.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        )
        return cur.fetchone()

    def upsert_file(self, path: str, name: str, size: int, mtime: float,
                    status: str = "ok", review_state: str = "pending",
                    flagged: bool = False, flag_reason: str = None,
                    nsfw_confidence: float = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            """
            INSERT INTO files
              (path, name, size, mtime, status, review_state, flagged,
               flag_reason, nsfw_confidence, updated_at, state_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name             = excluded.name,
                size             = excluded.size,
                mtime            = excluded.mtime,
                status           = excluded.status,
                review_state     = excluded.review_state,
                flagged          = excluded.flagged,
                flag_reason      = excluded.flag_reason,
                nsfw_confidence  = excluded.nsfw_confidence,
                updated_at       = excluded.updated_at,
                state_updated_at = excluded.state_updated_at
            """,
            (path, name, size, mtime, status, review_state,
             int(flagged), flag_reason, nsfw_confidence, now, now),
        )
        self._con.commit()

    def set_review_state(self, path: str, state: str,
                         quarantine_path: str = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            """UPDATE files SET review_state = ?, quarantine_path = ?,
               state_updated_at = ? WHERE path = ?""",
            (state, quarantine_path, now, path)
        )
        self._con.commit()

    def delete_file(self, path: str) -> None:
        self._con.execute("DELETE FROM files WHERE path = ?", (path,))
        self._con.commit()

    def list_files(self, review_state: str = None) -> list:
        if review_state:
            cur = self._con.execute(
                "SELECT * FROM files WHERE review_state = ? "
                "ORDER BY nsfw_confidence DESC, updated_at DESC",
                (review_state,)
            )
        else:
            cur = self._con.execute(
                "SELECT * FROM files ORDER BY updated_at DESC"
            )
        return cur.fetchall()

    def find_file_by_stem(self, stem: str) -> Optional[sqlite3.Row]:
        cur = self._con.execute(
            "SELECT * FROM files WHERE name LIKE ?", (stem + ".%",)
        )
        return cur.fetchone()

    def get_stats(self) -> dict:
        cur = self._con.execute(
            """SELECT review_state, COUNT(*) as count
               FROM files GROUP BY review_state"""
        )
        stats = {"pending": 0, "approved": 0, "quarantined": 0, "rejected": 0}
        for row in cur.fetchall():
            stats[row["review_state"]] = row["count"]
        return stats

    def get_stale_quarantined(self, days: int) -> list:
        """Return quarantined files older than *days* days."""
        cur = self._con.execute(
            """SELECT * FROM files WHERE review_state = 'quarantined'
               AND state_updated_at < datetime('now', ?)""",
            (f"-{days} days",)
        )
        return cur.fetchall()

    def clean_missing_files(self, watch_folders: list) -> int:
        rows = self.list_files()
        removed = 0
        for row in rows:
            path = Path(row["path"])
            in_watch = any(row["path"].startswith(f) for f in watch_folders)
            if not path.exists() and row["review_state"] not in ("quarantined",) and not in_watch:
                self.delete_file(row["path"])
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def record_error(self, path: str, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "INSERT INTO errors (path, message, recorded_at) VALUES (?, ?, ?)",
            (path, message, now),
        )
        self._con.commit()

    # ------------------------------------------------------------------
    # Poller state
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Scan state (used by stop-scan feature)
    # ------------------------------------------------------------------

    def set_scan_pid(self, pid: int) -> None:
        self.set_poller_state("scan_pid", str(pid))

    def clear_scan_pid(self) -> None:
        self._con.execute("DELETE FROM state WHERE key = 'scan_pid'")
        self._con.commit()

    def get_scan_pid(self) -> Optional[int]:
        val = self.get_poller_state("scan_pid")
        return int(val) if val else None

    def get_poller_state(self, key: str) -> Optional[str]:
        cur = self._con.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_poller_state(self, key: str, value: str) -> None:
        self._con.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._con.commit()

    def close(self) -> None:
        self._con.close()
