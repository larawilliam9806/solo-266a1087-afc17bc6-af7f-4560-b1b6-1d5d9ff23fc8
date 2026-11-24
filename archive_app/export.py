"""Streaming CSV export.

Two serving paths share one row format:

* **Prepared path (the normal one).**  A publication's selected evidence
  groups are stored in ``export_selection`` -- only the single group actually
  cited per row, never the unused backup groups.  New publications get this
  data while being published; legacy publications are prepared lazily on their
  first download (and reuse the prepared data after a restart).  The export
  reads ``entries`` and ``export_selection`` in small keyset batches.
* **Legacy path.**  When a legacy publication cannot be prepared (the
  organizing step fails), the file is produced the old way: every evidence
  group is joined against the withdrawal register below a fixed cut-off and
  the first complete group in registration order is chosen.

A download fixes its consistency point *when data fetching starts* and holds
it for the whole file.  The streaming connection opens an explicit read
transaction, so under WAL its snapshot never changes: withdrawals registered
while the file is being sent cannot alter this file, while the next download
immediately sees the updated selections.  No long-lived write lock is held, so
cataloguers keep working concurrently.

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

# Prepared path: selected groups only.  Row order inside a group is the
# registration order stored in export_selection.
_SELECTION_SQL = """
SELECT entry_id, pos, archive_no
FROM export_selection
WHERE publish_no = ? AND entry_id IN ({placeholders})
ORDER BY entry_id, pos
"""

# Legacy path: all evidence groups plus whether each archive number was
# already withdrawn at the cut-off.  Group order is registration order.
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

    def __init__(self, conn, publish_no: str, prepared: bool, cutoff: int = 0):
        self.conn = conn
        self.publish_no = publish_no
        self.prepared = prepared
        self.cutoff = cutoff  # legacy path only: withdrawal snapshot boundary
        self.first_batch = None
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        conn = self.conn
        try:
            # End the read transaction that pinned this download's snapshot.
            if conn.in_transaction:
                conn.execute("COMMIT")
        except Exception:  # pragma: no cover - defensive
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass


def _fetch_entry_batch(conn, publish_no: str, after_id: int):
    return conn.execute(
        _ENTRY_BATCH_SQL, (publish_no, after_id, ENTRY_BATCH)
    ).fetchmany(ENTRY_BATCH)


def open_export(publish_no: str) -> OpenExport | None:
    """Fix the download's consistency point and read the first batch.

    Returns ``None`` when the publication is unknown.  Legacy publications are
    prepared once here; if preparation fails, the original rule-based path is
    used.  Everything, including the snapshot, is settled before the response
    sends its first byte.
    """
    # A publication written by an older release has no prepared supply data.
    # Preparing it is a short separate write transaction; a failure leaves the
    # database untouched and we fall back to the legacy rule-based download.
    # Decide the serving path from the preparation outcome itself, so a
    # concurrent download that wins the preparation race is still served by
    # the prepared path.
    prepared = _is_prepared(publish_no)
    if not prepared:
        prepared = db.prepare_export(publish_no)

    conn = db.connect()
    try:
        # Hold one read transaction (WAL snapshot) for the whole download, so
        # withdrawals committed mid-stream never change this file's selection.
        conn.execute("BEGIN")
        publication = conn.execute(
            "SELECT publish_no FROM publications WHERE publish_no = ?",
            (publish_no,),
        ).fetchone()
        if publication is None:
            conn.execute("ROLLBACK")
            conn.close()
            return None

        if not prepared:
            # Another request may have finished preparing meanwhile.
            prepared = (
                conn.execute(
                    "SELECT 1 FROM export_preps WHERE publish_no = ?", (publish_no,)
                ).fetchone()
                is not None
            )

        opened = OpenExport(conn, publish_no, prepared=prepared)
        if not prepared:
            opened.cutoff = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM withdrawals"
            ).fetchone()[0]
        opened.first_batch = _fetch_entry_batch(conn, publish_no, 0)
        return opened
    except BaseException:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.close()
        raise


def _is_prepared(publish_no: str) -> bool:
    with db.get_conn() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM export_preps WHERE publish_no = ?", (publish_no,)
            ).fetchone()
            is not None
        )


def _selected_groups(conn, publish_no: str, entry_ids: list[int]) -> dict[int, list[str]]:
    """Prepared path: map entry id -> the archive numbers of its selected group.

    Only the stored (selected) groups are read; backup groups are never
    touched.  Entries with no complete group have no selection rows and are
    absent from the result.
    """
    placeholders = ",".join("?" for _ in entry_ids)
    cursor = conn.execute(
        _SELECTION_SQL.format(placeholders=placeholders),
        (publish_no, *entry_ids),
    )

    groups: dict[int, list[str]] = {}
    while True:
        rows = cursor.fetchmany(ENTRY_BATCH * 8)
        if not rows:
            break
        for row in rows:
            groups.setdefault(row["entry_id"], []).append(row["archive_no"])
    return groups


def _first_groups(conn, entry_ids: list[int], cutoff: int) -> dict[int, list[str]]:
    """Legacy path: map entry id -> archive numbers of its first complete group.

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
            if export.prepared:
                groups = _selected_groups(export.conn, export.publish_no, entry_ids)
            else:
                groups = _first_groups(export.conn, entry_ids, export.cutoff)
            out = []
            for entry in batch:
                numbers = groups.get(entry["id"])
                if not numbers:
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
