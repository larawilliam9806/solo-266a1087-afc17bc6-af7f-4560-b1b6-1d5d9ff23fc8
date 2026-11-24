"""Streaming CSV export.

A download fixes its consistency point from the withdrawal register: when the
response starts, it reads the highest committed withdrawal id (the *cut-off*)
and every query only honours withdrawals at or below it.  Withdrawals registered
while the file is being sent therefore never leak into that file, and the export
never holds a long-lived transaction that could block cataloguers.

Entries are walked in small keyset batches and each batch is written onward
immediately, so memory stays bounded by the batch size rather than by the size
of the table.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator

from . import db

CSV_HEADER = ("行号", "旧名", "今名", "依据档号")
NUM_SEPARATOR = ";"
ENTRY_BATCH = 200

_ENTRY_BATCH_SQL = """
SELECT id, line_no, old_name, new_name
FROM entries
WHERE publish_no = ? AND id > ?
ORDER BY id
LIMIT ?
"""

# Evidence of one batch of entries plus whether each archive number was already
# withdrawn at the cut-off.  Group order is registration order.
_EVIDENCE_SQL = """
SELECT ev.entry_id    AS entry_id,
       ev.group_no    AS group_no,
       ev.pos         AS pos,
       ev.archive_no  AS archive_no,
       CASE WHEN w.id IS NULL THEN 0 ELSE 1 END AS withdrawn
FROM evidence ev
LEFT JOIN withdrawals w
       ON w.archive_no = ev.archive_no AND w.id <= ?
WHERE ev.entry_id IN ({placeholders})
ORDER BY ev.entry_id, ev.group_no, ev.pos
"""


class OpenExport:
    """A download's fixed view of the archive, closed when the response ends."""

    def __init__(self, conn, publish_no: str, cutoff: int, first_batch):
        self.conn = conn
        self.publish_no = publish_no
        self.cutoff = cutoff
        self.first_batch = first_batch
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - defensive
            pass


def _fetch_entry_batch(conn, publish_no: str, after_id: int):
    return conn.execute(
        _ENTRY_BATCH_SQL, (publish_no, after_id, ENTRY_BATCH)
    ).fetchmany(ENTRY_BATCH)


def open_export(publish_no: str) -> OpenExport | None:
    """Fix the withdrawal cut-off and read the first batch.

    Returns ``None`` when the publication is unknown.  Everything, including
    the cut-off, is read before the response sends its first byte.
    """
    conn = db.connect()
    try:
        publication = conn.execute(
            "SELECT publish_no FROM publications WHERE publish_no = ?",
            (publish_no,),
        ).fetchone()
        if publication is None:
            conn.close()
            return None
        cutoff = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM withdrawals"
        ).fetchone()[0]
        first_batch = _fetch_entry_batch(conn, publish_no, 0)
        return OpenExport(conn, publish_no, cutoff, first_batch)
    except BaseException:
        conn.close()
        raise


def _first_groups(conn, entry_ids: list[int], cutoff: int) -> dict[int, list[str]]:
    """Map entry id -> archive numbers of its first complete group.

    Groups come in registration order.  A group is complete when none of its
    archive numbers was withdrawn at or before the cut-off.  Entries without any
    complete group are absent from the result.
    """
    placeholders = ",".join("?" for _ in entry_ids)
    cursor = conn.execute(
        _EVIDENCE_SQL.format(placeholders=placeholders), (cutoff, *entry_ids)
    )

    groups: dict[int, list[str]] = {}
    current_entry = None
    current_group = None
    numbers: list[str] = []
    complete = True
    decided = False

    def close_group() -> None:
        nonlocal decided
        if current_entry is not None and not decided and complete:
            groups[current_entry] = list(numbers)
            decided = True

    while True:
        rows = cursor.fetchmany(ENTRY_BATCH * 8)
        if not rows:
            break
        for row in rows:
            entry_id = row["entry_id"]
            group_no = row["group_no"]
            if entry_id != current_entry:
                close_group()
                current_entry = entry_id
                current_group = group_no
                numbers = []
                complete = True
                decided = False
            elif group_no != current_group:
                close_group()
                current_group = group_no
                numbers = []
                complete = True
            numbers.append(row["archive_no"])
            if row["withdrawn"]:
                complete = False
    close_group()
    return groups


def _format_row(values) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(values)
    return buf.getvalue()


def iter_csv(export: OpenExport) -> Iterator[str]:
    """Yield CSV text batch by batch; never holds more than one entry batch."""
    try:
        yield _format_row(CSV_HEADER)
        batch = export.first_batch
        after_id = 0
        while batch:
            entry_ids = [row["id"] for row in batch]
            groups = _first_groups(export.conn, entry_ids, export.cutoff)
            out = []
            for entry in batch:
                numbers = groups.get(entry["id"])
                if numbers is None:
                    continue
                out.append(
                    _format_row(
                        (
                            entry["line_no"],
                            entry["old_name"],
                            entry["new_name"],
                            NUM_SEPARATOR.join(numbers),
                        )
                    )
                )
            if out:
                yield "".join(out)
            after_id = entry_ids[-1]
            batch = _fetch_entry_batch(export.conn, export.publish_no, after_id)
    finally:
        export.close()
