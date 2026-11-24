"""SQLite storage layer for the gazetteer (旧版地名对照表) supply service.

The database is the single source of truth.  Every mutation runs in an explicit
``BEGIN IMMEDIATE`` transaction so that a failed save leaves the previous state
(notably the withdrawal register and the prepared supply data) intact.

Besides the raw catalogue, the database holds *prepared supply data*:

* ``export_preps`` marks one row per publication whose selection has been
  precomputed, together with the highest withdrawal id visible at preparation
  time.  Publications written by older releases simply lack a row and are
  prepared lazily on their first download (see :func:`prepare_export`).
* ``export_selection`` stores, per entry, only the archive numbers of the
  selected (first complete) evidence group in registration order.  Downloads
  therefore read just the selected groups and never touch the unused backup
  groups.  A withdrawal only re-checks entries whose selected group contains
  the withdrawn archive number.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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

-- Prepared supply data: one row per publication that can be served from
-- export_selection without scanning the backup evidence groups.
CREATE TABLE IF NOT EXISTS export_preps (
    publish_no   TEXT PRIMARY KEY REFERENCES publications(publish_no) ON DELETE CASCADE,
    cutoff_id    INTEGER NOT NULL,      -- MAX(withdrawals.id) when prepared
    prepared_at  TEXT NOT NULL
);

-- The selected evidence group of each entry, flattened in registration order.
-- Entries without a complete group have no rows here and are omitted from
-- downloads.  Only these rows are read when a prepared publication is served.
CREATE TABLE IF NOT EXISTS export_selection (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_no    TEXT NOT NULL REFERENCES publications(publish_no) ON DELETE CASCADE,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    group_no      INTEGER NOT NULL,     -- selected group, 1-based registration order
    pos           INTEGER NOT NULL,     -- position inside the selected group
    archive_no    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_selection_pub_entry
    ON export_selection(publish_no, entry_id, pos);

-- Supports the targeted withdrawal look-up: which selected groups cite a number.
CREATE INDEX IF NOT EXISTS idx_selection_archive
    ON export_selection(publish_no, archive_no);
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


def _max_withdrawal_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM withdrawals").fetchone()[0]


def _first_complete_group(
    conn: sqlite3.Connection, entry_id: int, withdrawn: set[str]
) -> tuple[int, list[str]] | None:
    """Return ``(group_no, archive_numbers)`` for the entry's first complete
    group in registration order, or ``None`` when every group cites a withdrawn
    archive number.  A group is skipped as soon as one of its numbers is in the
    withdrawal register visible to this transaction.
    """
    rows = conn.execute(
        "SELECT group_no, pos, archive_no FROM evidence "
        "WHERE entry_id = ? ORDER BY group_no, pos",
        (entry_id,),
    ).fetchall()

    group_no: int | None = None
    numbers: list[str] = []
    complete = True
    for row in rows:
        if group_no is None or row["group_no"] != group_no:
            if group_no is not None and complete:
                return group_no, numbers
            group_no = row["group_no"]
            numbers = [row["archive_no"]]
            complete = row["archive_no"] not in withdrawn
        else:
            numbers.append(row["archive_no"])
            if row["archive_no"] in withdrawn:
                complete = False
    if group_no is not None and complete:
        return group_no, numbers
    return None


def publish(rows: list[dict], publish_no: str, source_name: str | None = None) -> None:
    """Store a whole submission atomically, after checking every row has a
    complete evidence group under the withdrawals visible in this transaction.

    The prepared supply data (selected group per row) is written in the same
    transaction, so the first download is served straight from
    ``export_selection`` without ever scanning the backup evidence groups.

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
            selection_payload: list[tuple] = []
            for row in rows:
                cur = conn.execute(
                    "INSERT INTO entries (publish_no, line_no, old_name, new_name) "
                    "VALUES (?, ?, ?, ?)",
                    (publish_no, row["line_no"], row["old_name"], row["new_name"]),
                )
                entry_id = cur.lastrowid
                evidence_payload = []
                for g_idx, group in enumerate(row["evidence"], start=1):
                    for p_idx, archive_no in enumerate(group):
                        evidence_payload.append((entry_id, g_idx, p_idx, archive_no))
                conn.executemany(
                    "INSERT INTO evidence (entry_id, group_no, pos, archive_no) "
                    "VALUES (?, ?, ?, ?)",
                    evidence_payload,
                )
                # Validation above guarantees a complete group exists; pick the
                # first one in registration order (the same rule the CSV uses).
                chosen_group_no, chosen = next(
                    (idx, group)
                    for idx, group in enumerate(row["evidence"], start=1)
                    if all(no not in withdrawn for no in group)
                )
                for pos, archive_no in enumerate(chosen):
                    selection_payload.append(
                        (publish_no, entry_id, chosen_group_no, pos, archive_no)
                    )

            conn.execute(
                "INSERT INTO export_preps (publish_no, cutoff_id, prepared_at) "
                "VALUES (?, ?, ?)",
                (publish_no, _max_withdrawal_id(conn), _now()),
            )
            conn.executemany(
                "INSERT INTO export_selection "
                "(publish_no, entry_id, group_no, pos, archive_no) "
                "VALUES (?, ?, ?, ?, ?)",
                selection_payload,
            )


def _refresh_preps_after_withdrawal(
    conn: sqlite3.Connection, archive_no: str
) -> None:
    """Recompute prepared selections after a newly registered withdrawal.

    Only entries whose *currently selected* group cites ``archive_no`` are
    re-checked.  Each one moves to the first complete group in registration
    order (any group citing a withdrawn number is skipped), or loses its
    selection entirely when no complete group remains.  Runs inside the
    caller's transaction, so registration and supply changes commit together.
    """
    withdrawn = withdrawn_numbers(conn)
    cutoff_id = _max_withdrawal_id(conn)
    preps = conn.execute(
        "SELECT publish_no FROM export_preps"
    ).fetchall()

    for prep in preps:
        publish_no = prep["publish_no"]
        affected = conn.execute(
            "SELECT DISTINCT entry_id FROM export_selection "
            "WHERE publish_no = ? AND archive_no = ?",
            (publish_no, archive_no),
        ).fetchall()
        if not affected:
            continue

        new_selection: list[tuple] = []
        for item in affected:
            entry_id = item["entry_id"]
            chosen = _first_complete_group(conn, entry_id, withdrawn)
            conn.execute(
                "DELETE FROM export_selection WHERE entry_id = ?", (entry_id,)
            )
            if chosen is None:
                continue  # no complete group left: the row is omitted later
            group_no, numbers = chosen
            for pos, number in enumerate(numbers):
                new_selection.append(
                    (publish_no, entry_id, group_no, pos, number)
                )
        if new_selection:
            conn.executemany(
                "INSERT INTO export_selection "
                "(publish_no, entry_id, group_no, pos, archive_no) "
                "VALUES (?, ?, ?, ?, ?)",
                new_selection,
            )
        conn.execute(
            "UPDATE export_preps SET cutoff_id = ? WHERE publish_no = ?",
            (cutoff_id, publish_no),
        )


def withdraw(archive_no: str) -> bool:
    """Register a withdrawal, idempotently, and refresh affected supply data.

    The withdrawal row and every affected publication's selection changes are
    written in one transaction.  Any database error rolls the whole
    transaction back, so the previously effective withdrawal state and supply
    data are kept unchanged.  Returns ``True`` only for a newly created
    withdrawal row.
    """
    with get_conn() as conn:
        with _write_tx(conn):
            cur = conn.execute(
                "INSERT INTO withdrawals (archive_no, withdrawn_at) VALUES (?, ?) "
                "ON CONFLICT(archive_no) DO NOTHING",
                (archive_no, _now()),
            )
            created = cur.rowcount > 0
            if created:
                _refresh_preps_after_withdrawal(conn, archive_no)
            return created


def prepare_export(publish_no: str) -> bool:
    """Build prepared supply data for a legacy publication on first download.

    Older publications have no ``export_preps`` row.  The first download calls
    this to organize the selected groups once; the result is persisted, so
    later downloads (and process restarts) reuse it.  Returns ``False``
    without raising when preparation fails -- the caller then serves the file
    with the original rule-based download.
    """
    try:
        with get_conn() as conn:
            with _write_tx(conn):
                if conn.execute(
                    "SELECT 1 FROM export_preps WHERE publish_no = ?",
                    (publish_no,),
                ).fetchone():
                    return True
                if conn.execute(
                    "SELECT 1 FROM publications WHERE publish_no = ?",
                    (publish_no,),
                ).fetchone() is None:
                    return False

                withdrawn = withdrawn_numbers(conn)
                cutoff_id = _max_withdrawal_id(conn)
                after_id = 0
                selection_payload: list[tuple] = []
                while True:
                    entries = conn.execute(
                        "SELECT id FROM entries "
                        "WHERE publish_no = ? AND id > ? ORDER BY id LIMIT ?",
                        (publish_no, after_id, 200),
                    ).fetchall()
                    if not entries:
                        break
                    for entry in entries:
                        chosen = _first_complete_group(conn, entry["id"], withdrawn)
                        if chosen is None:
                            continue
                        group_no, numbers = chosen
                        for pos, number in enumerate(numbers):
                            selection_payload.append(
                                (publish_no, entry["id"], group_no, pos, number)
                            )
                    after_id = entries[-1]["id"]

                conn.execute(
                    "INSERT INTO export_preps (publish_no, cutoff_id, prepared_at) "
                    "VALUES (?, ?, ?)",
                    (publish_no, cutoff_id, _now()),
                )
                if selection_payload:
                    conn.executemany(
                        "INSERT INTO export_selection "
                        "(publish_no, entry_id, group_no, pos, archive_no) "
                        "VALUES (?, ?, ?, ?, ?)",
                        selection_payload,
                    )
        return True
    except Exception:
        logger.exception(
            "为旧版发布 %s 准备供数数据失败，本次改用原规则下载", publish_no
        )
        return False


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
