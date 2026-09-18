"""
safescanarr/database.py
SQLite wrapper with full state machine support for v0.6.

review_state values:
  pending     — new, awaiting review
  approved    — confirmed clean (by user or auto-approve zone)
  quarantined — NSFW detected; video moved to quarantine folder
  rejected    — confirmed bad; video deleted, sheet deleted
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pathutil import is_within, resolve_sheet

log = logging.getLogger(__name__)


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
    source_retained   INTEGER NOT NULL DEFAULT 0,
    state_updated_at  TEXT,
    state_source      TEXT,
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

CREATE_LIFETIME = """
CREATE TABLE IF NOT EXISTS lifetime_stats (
    key   TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
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
    "ALTER TABLE files ADD COLUMN state_source TEXT",
    "ALTER TABLE files ADD COLUMN source_retained INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE files ADD COLUMN arr_result TEXT",
]


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute("PRAGMA busy_timeout=10000;")
        self._con.execute(CREATE_FILES)
        self._con.execute(CREATE_ERRORS)
        self._con.execute(CREATE_STATE)
        self._con.execute(CREATE_LIFETIME)
        self._run_migrations()
        # Migrate existing 'ok' records to 'approved'
        self._con.execute(
            "UPDATE files SET review_state = 'approved' "
            "WHERE review_state = 'pending' AND status = 'ok' "
            "AND updated_at < datetime('now', '-1 minute')"
        )
        self._con.commit()
        self._seed_lifetime_if_needed()

    def _run_migrations(self) -> None:
        cols = [r[1] for r in self._con.execute("PRAGMA table_info(files)").fetchall()]
        for sql in MIGRATIONS:
            col = sql.split("ADD COLUMN")[1].strip().split()[0]
            if col in cols:
                continue
            try:
                self._con.execute(sql)
            except sqlite3.OperationalError as e:
                # Two processes racing to migrate is harmless; anything else
                # (lock contention, corrupt schema) is worth surfacing.
                if "duplicate column" not in str(e).lower():
                    log.warning("Migration for column %s failed: %s", col, e)
            except sqlite3.DatabaseError as e:
                log.warning("Migration for column %s failed: %s", col, e)
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
                    flagged: bool = False, flag_reason: Optional[str] = None,
                    nsfw_confidence: Optional[float] = None,
                    state_source: str = "auto",
                    source_retained: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        is_new = self.get_file(path) is None
        self._con.execute(
            """
            INSERT INTO files
              (path, name, size, mtime, status, review_state, flagged,
               flag_reason, nsfw_confidence, state_source, source_retained,
               updated_at, state_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name             = excluded.name,
                size             = excluded.size,
                mtime            = excluded.mtime,
                status           = excluded.status,
                review_state     = excluded.review_state,
                flagged          = excluded.flagged,
                flag_reason      = excluded.flag_reason,
                nsfw_confidence  = excluded.nsfw_confidence,
                state_source     = excluded.state_source,
                source_retained  = excluded.source_retained,
                updated_at       = excluded.updated_at,
                state_updated_at = excluded.state_updated_at
            """,
            (path, name, size, mtime, status, review_state,
             int(flagged), flag_reason, nsfw_confidence, state_source,
             int(source_retained), now, now),
        )
        self._con.commit()
        if is_new:
            self._bump_lifetime("total_scanned")
            if flagged:
                self._bump_lifetime("total_flagged")
            if review_state in ("approved", "quarantined", "rejected"):
                self._bump_lifetime(f"{review_state}_{state_source or 'auto'}")

    def set_review_state(self, path: str, state: str,
                         quarantine_path: Optional[str] = None,
                         source: str = "user",
                         source_retained: Optional[bool] = None) -> None:
        prev_row   = self.get_file(path)
        prev_state = prev_row["review_state"] if prev_row else None
        now = datetime.now(timezone.utc).isoformat()
        if source_retained is None:
            self._con.execute(
                """UPDATE files SET review_state = ?, quarantine_path = ?,
                   state_updated_at = ?, state_source = ? WHERE path = ?""",
                (state, quarantine_path, now, source, path)
            )
        else:
            self._con.execute(
                """UPDATE files SET review_state = ?, quarantine_path = ?,
                   source_retained = ?, state_updated_at = ?, state_source = ?
                   WHERE path = ?""",
                (state, quarantine_path, int(source_retained), now, source, path)
            )
        self._con.commit()
        if state != prev_state and state in ("approved", "quarantined", "rejected"):
            self._bump_lifetime(f"{state}_{source or 'user'}")

    def delete_file(self, path: str) -> None:
        self._con.execute("DELETE FROM files WHERE path = ?", (path,))
        self._con.commit()

    def set_arr_result(self, path: str, reports: list) -> None:
        """Persist the arr reject-flow report dicts as JSON."""
        self._con.execute(
            "UPDATE files SET arr_result = ? WHERE path = ?",
            (json.dumps(reports), path)
        )
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
        """Find a file by its stem (filename without extension).

        LIKE wildcards in *stem* are escaped so a stem containing ``%`` or ``_``
        cannot match unintended rows. When several rows match, the most recently
        updated one is used and the ambiguity is logged.
        """
        escaped = (
            stem.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
        )
        cur = self._con.execute(
            "SELECT * FROM files WHERE name LIKE ? ESCAPE '\\' "
            "ORDER BY updated_at DESC LIMIT 10",
            (escaped + ".%",),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            log.warning("Ambiguous stem %r matched %d files — using most recent (%s)",
                        stem, len(rows), rows[0]["path"])
        return rows[0]

    def get_stats(self) -> dict:
        cur = self._con.execute(
            """SELECT review_state, COUNT(*) as count
               FROM files GROUP BY review_state"""
        )
        stats = {"pending": 0, "approved": 0, "quarantined": 0, "rejected": 0}
        for row in cur.fetchall():
            stats[row["review_state"]] = row["count"]
        return stats

    def get_stats_full(self) -> dict:
        """Return comprehensive lifetime stats.

        Approved/quarantined/rejected counts and total scanned/flagged come from
        cumulative lifetime counters that survive record cleanup. Pending stays
        as a current-snapshot count since it's a transitory state.
        """
        stats = {}
        lifetime = self.get_lifetime_stats()

        # Pending = current snapshot (transitory state, not lifetime-meaningful)
        cur = self._con.execute(
            "SELECT COUNT(*) as count FROM files WHERE review_state = 'pending'"
        )
        pending_now = cur.fetchone()["count"]

        approved_total    = lifetime.get("approved_auto", 0)    + lifetime.get("approved_user", 0)
        quarantined_total = lifetime.get("quarantined_auto", 0) + lifetime.get("quarantined_user", 0)
        rejected_total    = lifetime.get("rejected_auto", 0)    + lifetime.get("rejected_user", 0)

        stats["by_state"] = {
            "pending":     pending_now,
            "approved":    approved_total,
            "quarantined": quarantined_total,
            "rejected":    rejected_total,
        }
        stats["total"] = lifetime.get("total_scanned", pending_now + approved_total + quarantined_total + rejected_total)

        stats["breakdown"] = {
            "approved_auto":    lifetime.get("approved_auto", 0),
            "approved_user":    lifetime.get("approved_user", 0),
            "quarantined_auto": lifetime.get("quarantined_auto", 0),
            "quarantined_user": lifetime.get("quarantined_user", 0),
            "rejected_auto":    lifetime.get("rejected_auto", 0),
            "rejected_user":    lifetime.get("rejected_user", 0),
        }

        stats["total_flagged"] = lifetime.get("total_flagged", 0)

        # Average risk score on flagged items
        cur = self._con.execute(
            "SELECT AVG(nsfw_confidence) as avg FROM files WHERE flagged = 1 AND nsfw_confidence IS NOT NULL"
        )
        row = cur.fetchone()
        stats["avg_risk_flagged"] = round(float(row["avg"]), 3) if row["avg"] else 0.0

        return stats

    def get_stale_quarantined(self, days: int) -> list:
        """Return quarantined files older than *days* days."""
        cur = self._con.execute(
            """SELECT * FROM files WHERE review_state = 'quarantined'
               AND state_updated_at < datetime('now', ?)""",
            (f"-{days} days",)
        )
        return cur.fetchall()

    def clean_missing_files(self, watch_folders: list, output_dir: str = None) -> dict:
        """
        Remove DB records that are stale or orphaned. A record is cleaned if:
          - the source file no longer exists on disk, OR
          - the path is no longer inside any watch folder, OR
          - the VCS sheet is missing (orphan record showing "No Sheet" in the UI).
        For non-rejected items, the VCS sheet is deleted alongside the record.
        Quarantined files are skipped (source was intentionally moved, not missing).
        Returns dict with counts of records and sheets removed.
        """
        rows    = self.list_files()
        records = 0
        sheets  = 0

        for row in rows:
            path     = Path(row["path"])
            in_watch = is_within(row["path"], watch_folders)
            state    = row["review_state"]

            # Skip quarantined — source was intentionally moved, not missing
            if state == "quarantined":
                continue

            sheet_file    = resolve_sheet(output_dir, row["path"]) if output_dir else None
            sheet_missing = sheet_file is not None and not sheet_file.exists()

            if not path.exists() or not in_watch or sheet_missing:
                # Delete VCS sheet alongside the record (rejected sheets are kept as audit trail)
                if state != "rejected" and sheet_file is not None and sheet_file.exists():
                    sheet_file.unlink()
                    sheets += 1

                self.delete_file(row["path"])
                records += 1

        return {"records": records, "sheets": sheets}

    # ------------------------------------------------------------------
    # Lifetime stats counters
    # ------------------------------------------------------------------

    def _bump_lifetime(self, key: str, n: int = 1) -> None:
        self._con.execute(
            "INSERT INTO lifetime_stats (key, count) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET count = count + excluded.count",
            (key, n),
        )
        self._con.commit()

    def _seed_lifetime(self, key: str, count: int) -> None:
        """Seed a lifetime counter only if it has never been set.

        INSERT OR IGNORE makes this atomic, so a concurrent process (or a
        second Database open) can never clobber an existing counter.
        """
        self._con.execute(
            "INSERT OR IGNORE INTO lifetime_stats (key, count) VALUES (?, ?)",
            (key, count),
        )
        self._con.commit()

    def get_lifetime_stats(self) -> dict:
        cur = self._con.execute("SELECT key, count FROM lifetime_stats")
        return {r["key"]: r["count"] for r in cur.fetchall()}

    def _seed_lifetime_if_needed(self) -> None:
        # One-time backfill so existing installations don't start at zero.
        if self.get_poller_state("lifetime_seeded") == "1":
            return
        cur = self._con.execute("SELECT COUNT(*) as c FROM files")
        self._seed_lifetime("total_scanned", cur.fetchone()["c"])
        cur = self._con.execute("SELECT COUNT(*) as c FROM files WHERE flagged = 1")
        self._seed_lifetime("total_flagged", cur.fetchone()["c"])
        cur = self._con.execute(
            "SELECT review_state, state_source, COUNT(*) as c FROM files "
            "WHERE review_state IN ('approved', 'quarantined', 'rejected') "
            "GROUP BY review_state, state_source"
        )
        for r in cur.fetchall():
            source = r["state_source"] or "auto"
            self._seed_lifetime(f"{r['review_state']}_{source}", r["c"])
        self.set_poller_state("lifetime_seeded", "1")

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
