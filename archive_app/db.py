"""SQLite storage layer for the gazetteer (旧版地名对照表) supply service.

The database is the single source of truth.  Every mutation runs in an explicit
``BEGIN IMMEDIATE`` transaction so that a failed save leaves the previous state
(notably the withdrawal register) intact.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(os.path.dirname(APP_ROOT), "data", "archive.sqlite3")

_configured_path: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    publish_no   TEXT PRIMARY KEY,
    source_name  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_no    TEXT NOT NULL REFERENCES publications(publish_no) ON DELETE CASCADE,
    line_no       TEXT NOT NULL,
    old_name      TEXT NOT NULL,
    new_name      TEXT NOT NULL,
    UNIQUE (publish_no, line_no)
);

CREATE INDEX IF NOT EXISTS idx_entries_pub ON entries(publish_no, id);

CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    group_no      INTEGER NOT NULL,   -- 1-based order of the evidence group as submitted
    pos           INTEGER NOT NULL,   -- position of the archive number inside its group
    archive_no    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_entry ON evidence(entry_id, group_no, pos);

-- One row per successful withdrawal.  The UNIQUE constraint makes repeated
-- withdrawal of the same archive number count once.  ``id`` also serves as a
-- monotonically increasing cut-off for consistent CSV snapshots.
CREATE TABLE IF NOT EXISTS withdrawals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_no    TEXT NOT NULL UNIQUE,
    withdrawn_at  TEXT NOT NULL
);
"""


class DuplicatePublication(Exception):
    def __init__(self, publish_no: str):
        super().__init__(f"发布号已存在：{publish_no}")
        self.publish_no = publish_no


class IncompleteEvidence(Exception):
    def __init__(self, lines: list[str]):
        super().__init__("存在缺少完整依据档号组的行")
        self.lines = lines


def db_path() -> str:
    return _configured_path or os.environ.get("ARCHIVE_DB") or DEFAULT_DB_PATH


def configure(path: str | None) -> None:
    """Pin the database file for this process."""
    global _configured_path
    _configured_path = path or None


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    _ensure_parent(path)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str | None = None) -> None:
    path = path or db_path()
    conn = connect(path)
    try:
        # WAL lets readers keep streaming while a cataloguer withdraws records.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def get_conn(path: str | None = None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def publish(rows: list[dict], publish_no: str, source_name: str | None = None) -> None:
    """Store a whole submission atomically, after checking every row has a
    complete evidence group under the withdrawals visible in this transaction.

    Raises :class:`IncompleteEvidence` or :class:`DuplicatePublication` without
    writing anything.
    """
    with get_conn() as conn:
        with _write_tx(conn):
            if conn.execute(
                "SELECT 1 FROM publications WHERE publish_no = ?", (publish_no,)
            ).fetchone():
                raise DuplicatePublication(publish_no)

            withdrawn = withdrawn_numbers(conn)
            incomplete = [
                row["line_no"]
                for row in rows
                if not any(
                    all(no not in withdrawn for no in group)
                    for group in row["evidence"]
                )
            ]
            if incomplete:
                raise IncompleteEvidence(incomplete)

            conn.execute(
                "INSERT INTO publications (publish_no, source_name, created_at) VALUES (?, ?, ?)",
                (publish_no, source_name, _now()),
            )
            for row in rows:
                cur = conn.execute(
                    "INSERT INTO entries (publish_no, line_no, old_name, new_name) "
                    "VALUES (?, ?, ?, ?)",
                    (publish_no, row["line_no"], row["old_name"], row["new_name"]),
                )
                entry_id = cur.lastrowid
                payload = []
                for g_idx, group in enumerate(row["evidence"], start=1):
                    for p_idx, archive_no in enumerate(group):
                        payload.append((entry_id, g_idx, p_idx, archive_no))
                conn.executemany(
                    "INSERT INTO evidence (entry_id, group_no, pos, archive_no) "
                    "VALUES (?, ?, ?, ?)",
                    payload,
                )


def withdraw(archive_no: str) -> bool:
    """Register a withdrawal, idempotently.

    Returns ``True`` only when a new withdrawal row was created.  Any database
    error rolls the transaction back, so the previously effective withdrawal
    state is kept unchanged.
    """
    with get_conn() as conn:
        with _write_tx(conn):
            cur = conn.execute(
                "INSERT INTO withdrawals (archive_no, withdrawn_at) VALUES (?, ?) "
                "ON CONFLICT(archive_no) DO NOTHING",
                (archive_no, _now()),
            )
            return cur.rowcount > 0


def publication_exists(publish_no: str, path: str | None = None) -> bool:
    with get_conn(path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM publications WHERE publish_no = ?", (publish_no,)
            ).fetchone()
            is not None
        )


def list_publications(path: str | None = None) -> list[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT p.publish_no, p.source_name, p.created_at, "
            "       COUNT(e.id) AS entry_count "
            "FROM publications p LEFT JOIN entries e ON e.publish_no = p.publish_no "
            "GROUP BY p.publish_no ORDER BY p.created_at DESC, p.publish_no"
        ).fetchall()


def list_withdrawals(path: str | None = None) -> list[sqlite3.Row]:
    with get_conn(path) as conn:
        return conn.execute(
            "SELECT id, archive_no, withdrawn_at FROM withdrawals ORDER BY id"
        ).fetchall()


def withdrawn_numbers(conn: sqlite3.Connection) -> set[str]:
    return {
        r["archive_no"]
        for r in conn.execute("SELECT archive_no FROM withdrawals").fetchall()
    }
