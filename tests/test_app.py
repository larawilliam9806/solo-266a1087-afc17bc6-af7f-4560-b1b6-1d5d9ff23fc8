import json

import pytest

from archive_app import create_app, db


@pytest.fixture()
def app(tmp_path):
    db.configure(None)  # reset any earlier global pin
    application = create_app(
        {
            "TESTING": True,
            "ARCHIVE_DB": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test",
        }
    )
    yield application
    db.configure(None)


@pytest.fixture()
def client(app):
    return app.test_client()


def submission(publish_no="P-1", rows=None):
    if rows is None:
        rows = [
            {
                "line_no": "1",
                "old_name": "北平",
                "new_name": "北京",
                "evidence": [["A-1", "A-2"], ["B-1"]],
            }
        ]
    return {"publish_no": publish_no, "rows": rows}


def post_json(client, payload, url="/publish"):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def csv_rows(response):
    import csv
    import io

    text = response.get_data(as_text=True)
    return list(csv.reader(io.StringIO(text)))


def test_publish_and_download(client):
    resp = post_json(client, submission())
    assert resp.status_code in (200, 201)
    resp = client.get("/download/P-1.csv")
    assert resp.status_code == 200
    rows = csv_rows(resp)
    assert rows[0] == ["行号", "旧名", "今名", "依据档号"]
    assert rows[1] == ["1", "北平", "北京", "A-1;A-2"]


def test_bytes_are_emitted_before_the_whole_table_is_read(client, app):
    """The response yields incrementally: first chunks arrive before the end."""
    N = 1500
    rows = [
        {
            "line_no": str(i),
            "old_name": "o",
            "new_name": "n",
            "evidence": [[f"E{i}"]],
        }
        for i in range(N)
    ]
    post_json(client, submission(publish_no="INC", rows=rows))

    resp = client.get("/download/INC.csv", buffered=False)
    it = iter(resp.response)
    first = next(it)
    second = next(it)
    resp.close()
    assert b"\xe8\xa1\x8c\xe5\x8f\xb7" in first
    assert b"0,o,n,E0" in second
    # Second chunk is one batch of rows, not the whole file.
    assert second.count(b"\r\n") <= 200


def test_first_complete_group_in_registered_order(client):
    post_json(
        client,
        submission(
            rows=[
                {
                    "line_no": "1",
                    "old_name": "x",
                    "new_name": "y",
                    "evidence": [["A-1", "A-2"], ["B-1"], ["C-1"]],
                }
            ]
        ),
    )
    # Incomplete first group, second complete -> second group wins.
    client.post("/withdraw", data={"archive_no": "A-2"})
    rows = csv_rows(client.get("/csv/P-1"))
    assert rows[1][3] == "B-1"

    # First group becomes complete again?  It cannot - withdrawal is permanent.
    # Withdraw the second group too -> third wins.
    client.post("/withdraw", data={"archive_no": "B-1"})
    rows = csv_rows(client.get("/csv/P-1"))
    assert rows[1][3] == "C-1"

    # All groups incomplete -> row omitted.
    client.post("/withdraw", data={"archive_no": "C-1"})
    rows = csv_rows(client.get("/csv/P-1"))
    assert len(rows) == 1


def test_repeated_withdraw_counts_once(client):
    post_json(client, submission())
    first = post_json(client, {"archive_no": "A-1"}, url="/withdraw")
    second = post_json(client, {"archive_no": "A-1"}, url="/withdraw")
    assert first.status_code == 200
    assert second.status_code == 200
    with db.get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM withdrawals").fetchone()[0]
    assert count == 1


def test_publish_rejected_when_any_row_lacks_complete_group(client):
    post_json(client, submission(publish_no="OLD"))
    client.post("/withdraw", data={"archive_no": "A-2"})
    # Row 1 has group B-1 complete; row 2 only references withdrawn A-2.
    resp = post_json(
        client,
        submission(
            publish_no="NEW",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["A-1", "A-2"], ["B-1"]],
                },
                {
                    "line_no": "2",
                    "old_name": "c",
                    "new_name": "d",
                    "evidence": [["A-2"]],
                },
            ],
        ),
    )
    assert resp.status_code >= 400
    assert not db.publication_exists("NEW")


def test_publication_atomic_rollback_on_storage_error(client, app):
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON evidence "
            "WHEN NEW.archive_no = 'BOOM' BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    resp = post_json(
        client,
        submission(
            publish_no="BROKEN",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["BOOM"]],
                }
            ],
        ),
    )
    assert resp.status_code >= 500
    assert not db.publication_exists("BROKEN")


def test_failed_withdraw_keeps_previous_state(client):
    post_json(client, submission())
    client.post("/withdraw", data={"archive_no": "A-1"})
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER fail_withdraw BEFORE INSERT ON withdrawals "
            "WHEN NEW.archive_no = 'B-1' BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    resp = client.post(
        "/withdraw", data=json.dumps({"archive_no": "B-1"}), content_type="application/json"
    )
    assert resp.status_code >= 500
    assert db.list_withdrawals() and [w["archive_no"] for w in db.list_withdrawals()] == ["A-1"]
    # The earlier withdrawal is still effective for the CSV.
    rows = csv_rows(client.get("/download/P-1.csv"))
    assert rows[1][3] == "B-1"  # A-1 withdrawn, second group complete


def test_duplicate_publish_rejected(client):
    assert post_json(client, submission()).status_code == 201
    assert post_json(client, submission()).status_code == 409


def test_json_validation_errors_do_not_store(client):
    bad = submission(rows=[{"line_no": "1", "old_name": "a", "new_name": "b"}])
    assert post_json(client, bad).status_code == 400
    assert not db.publication_exists("P-1")

    bad2 = submission(
        rows=[
            {"line_no": "1", "old_name": "a", "new_name": "b", "evidence": []},
        ]
    )
    assert post_json(client, bad2).status_code == 400
    assert not db.publication_exists("P-1")


def test_snapshot_is_uniform_during_download(client):
    N = 450  # spans several entry batches
    rows = [
        {
            "line_no": str(i),
            "old_name": "a",
            "new_name": "b",
            "evidence": [["A-1"], ["B-1"]],
        }
        for i in range(N)
    ]
    post_json(client, submission(publish_no="P-1", rows=rows))
    resp = client.get("/download/P-1.csv", buffered=False)
    it = iter(resp.response)
    first = next(it)
    assert b"\xe8\xa1\x8c\xe5\x8f\xb7" in first  # header
    # Withdraw while the download is still open.
    client.post("/withdraw", data={"archive_no": "A-1"})
    rest = b"".join(it).decode("utf-8")
    resp.close()
    assert rest.count(",A-1") == N
    assert ",B-1" not in rest
    assert len(rest.strip().splitlines()) == N

    # A download started after the withdrawal reflects it in every row.
    rows_after = csv_rows(client.get("/download/P-1.csv"))
    assert len(rows_after) == N + 1
    assert all(r[3] == "B-1" for r in rows_after[1:])


def test_streaming_uses_bounded_batches(client, app, monkeypatch):
    N = 3000
    rows = [
        {
            "line_no": str(i),
            "old_name": "o",
            "new_name": "n",
            "evidence": [["X", "Y"], [f"{i}-1"]],
        }
        for i in range(N)
    ]
    post_json(client, submission(publish_no="BIG", rows=rows))

    from archive_app import export

    calls = {"fetchall": 0, "fetchmany_sizes": []}
    real_connect = db.connect

    class CursorProxy:
        def __init__(self, inner):
            self.inner = inner

        def fetchmany(self, size=-1):
            calls["fetchmany_sizes"].append(size)
            return self.inner.fetchmany(size)

        def fetchall(self):
            calls["fetchall"] += 1
            return self.inner.fetchall()

        def close(self):
            self.inner.close()

    class ConnProxy:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, args=()):
            cur = self.inner.execute(sql, args)
            if sql is export._ENTRY_BATCH_SQL:
                return CursorProxy(cur)
            return cur

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(export.db, "connect", lambda path=None: ConnProxy(real_connect(path)))

    resp = client.get("/download/BIG.csv", buffered=False)
    chunks = list(resp.response)
    resp.close()
    text = b"".join(chunks).decode("utf-8")
    import csv, io

    parsed = list(csv.reader(io.StringIO(text)))
    assert len(parsed) == N + 1
    assert parsed[1] == ["0", "o", "n", "X;Y"]
    assert calls["fetchall"] == 0
    assert calls["fetchmany_sizes"] and set(calls["fetchmany_sizes"]) == {export.ENTRY_BATCH}
    assert len(chunks) > 1


def _multi_group_rows():
    return [
        {
            "line_no": "1",
            "old_name": "a",
            "new_name": "b",
            "evidence": [["A-1", "A-2"], ["B-1"], ["C-1"]],
        },
        {
            "line_no": "2",
            "old_name": "c",
            "new_name": "d",
            "evidence": [["D-1"], ["E-1"]],
        },
    ]


def test_new_publication_is_prepared_with_selected_group_only(client):
    """New publications get prepared supply data: only the first complete
    group of every row is stored, backup groups are not duplicated there."""
    post_json(client, submission(publish_no="NP", rows=_multi_group_rows()))
    with db.get_conn() as conn:
        prep = conn.execute(
            "SELECT publish_no FROM export_preps WHERE publish_no = 'NP'"
        ).fetchone()
        assert prep is not None
        selected = conn.execute(
            "SELECT e.line_no, s.group_no, s.pos, s.archive_no "
            "FROM export_selection s JOIN entries e ON e.id = s.entry_id "
            "WHERE s.publish_no = 'NP' ORDER BY e.line_no, s.pos"
        ).fetchall()
        assert [(r["line_no"], r["group_no"], r["archive_no"]) for r in selected] == [
            ("1", 1, "A-1"),
            ("1", 1, "A-2"),
            ("2", 1, "D-1"),
        ]


def test_prepared_download_never_reads_evidence_or_withdrawals(client, monkeypatch):
    from archive_app import export

    post_json(client, submission(publish_no="NP", rows=_multi_group_rows()))

    seen_sql = []
    real_connect = db.connect

    class CursorProxy:
        def __init__(self, inner):
            self.inner = inner

        def fetchmany(self, size=-1):
            return self.inner.fetchmany(size)

        def fetchall(self):
            return self.inner.fetchall()

        def fetchone(self):
            return self.inner.fetchone()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    class ConnProxy:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, args=()):
            seen_sql.append(sql)
            return CursorProxy(self.inner.execute(sql, args))

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(export.db, "connect", lambda path=None: ConnProxy(real_connect(path)))

    rows = csv_rows(client.get("/download/NP.csv"))
    assert rows[1] == ["1", "a", "b", "A-1;A-2"]
    assert rows[2] == ["2", "c", "d", "D-1"]
    joined = " ".join(seen_sql).lower()
    # The prepared path reads the selected groups only; no evidence-table
    # re-evaluation and no withdrawal scans happen during the download.
    assert "evidence" not in joined
    assert "withdrawals" not in joined


def test_legacy_publication_prepared_on_first_download_and_reused_after_restart(tmp_path):
    db_path = str(tmp_path / "legacy.sqlite3")
    db.configure(None)
    app = create_app({"TESTING": True, "ARCHIVE_DB": db_path, "SECRET_KEY": "t"})
    client = app.test_client()
    post_json(
        client,
        submission(
            publish_no="LEG",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["X-1", "X-2"], ["Y-1"]],
                },
                {"line_no": "2", "old_name": "c", "new_name": "d", "evidence": [["Z-1"]]},
            ],
        ),
    )
    # Simulate data written by the old release: catalogue present, no prep data.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM export_preps")
        conn.execute("DELETE FROM export_selection")

    rows = csv_rows(client.get("/download/LEG.csv"))
    assert rows[1] == ["1", "a", "b", "X-1;X-2"]
    assert rows[2] == ["2", "c", "d", "Z-1"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM export_preps").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM export_selection").fetchone()[0] == 3
        prepared_at = conn.execute(
            "SELECT prepared_at FROM export_preps WHERE publish_no = 'LEG'"
        ).fetchone()[0]

    # Restart: a fresh app on the same database reuses the prepared data.
    db.configure(None)
    app2 = create_app({"TESTING": True, "ARCHIVE_DB": db_path, "SECRET_KEY": "t"})
    client2 = app2.test_client()
    rows = csv_rows(client2.get("/download/LEG.csv"))
    assert rows[1] == ["1", "a", "b", "X-1;X-2"]
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT prepared_at FROM export_preps WHERE publish_no = 'LEG'"
            ).fetchone()[0]
            == prepared_at
        )
    db.configure(None)


def test_failed_preparation_falls_back_to_legacy_rule_download(client):
    post_json(
        client,
        submission(
            publish_no="FB",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["A-1"], ["B-1"]],
                }
            ],
        ),
    )
    with db.get_conn() as conn:
        conn.execute("DELETE FROM export_preps")
        conn.execute("DELETE FROM export_selection")
        conn.execute(
            "CREATE TRIGGER fail_prep BEFORE INSERT ON export_preps "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )

    # First download: preparation fails, the original rule-based path serves it.
    rows = csv_rows(client.get("/download/FB.csv"))
    assert rows[1] == ["1", "a", "b", "A-1"]
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM export_preps").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM export_selection").fetchone()[0] == 0

    # The legacy path still honours later withdrawals.
    client.post("/withdraw", data={"archive_no": "A-1"})
    rows = csv_rows(client.get("/download/FB.csv"))
    assert rows[1][3] == "B-1"


def test_withdrawal_rechecks_only_rows_whose_selected_group_cites_it(client):
    post_json(
        client,
        submission(
            publish_no="P1",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["A-1", "A-2"], ["B-1"]],
                },
                {
                    "line_no": "2",
                    "old_name": "c",
                    "new_name": "d",
                    "evidence": [["D-1"], ["E-1"]],
                },
            ],
        ),
    )
    post_json(
        client,
        submission(
            publish_no="P2",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "x",
                    "new_name": "y",
                    "evidence": [["A-1"], ["F-1"]],
                }
            ],
        ),
    )

    resp = client.post("/withdraw", data={"archive_no": "A-1"})
    assert resp.status_code in (200, 302)

    # P1 row 1 moves to its backup group; P1 row 2's selection is untouched.
    with db.get_conn() as conn:
        p1 = {
            r["line_no"]: r["archive_no"]
            for r in conn.execute(
                "SELECT e.line_no AS line_no, s.archive_no AS archive_no "
                "FROM export_selection s JOIN entries e ON e.id = s.entry_id "
                "WHERE s.publish_no = 'P1'"
            )
        }
        assert p1 == {"1": "B-1", "2": "D-1"}
        p2 = conn.execute(
            "SELECT archive_no FROM export_selection WHERE publish_no = 'P2'"
        ).fetchall()
        assert [r["archive_no"] for r in p2] == ["F-1"]

    rows = csv_rows(client.get("/download/P1.csv"))
    assert rows[1] == ["1", "a", "b", "B-1"]
    assert rows[2] == ["2", "c", "d", "D-1"]


def test_backup_group_citing_any_withdrawn_number_is_skipped(client):
    post_json(
        client,
        submission(
            publish_no="SK",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["A-1"], ["B-1", "B-2"], ["C-1"]],
                }
            ],
        ),
    )
    client.post("/withdraw", data={"archive_no": "B-2"})  # backup group tainted early
    client.post("/withdraw", data={"archive_no": "A-1"})  # selected group breaks now
    rows = csv_rows(client.get("/download/SK.csv"))
    # Group 2 is skipped (B-2 already withdrawn); group 3 wins.
    assert rows[1][3] == "C-1"

    # Every group broken -> the row is omitted but the header stays.
    client.post("/withdraw", data={"archive_no": "C-1"})
    rows = csv_rows(client.get("/download/SK.csv"))
    assert len(rows) == 1
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM export_selection WHERE publish_no = 'SK'"
            ).fetchone()[0]
            == 0
        )


def test_withdrawal_and_selection_changes_roll_back_together_on_failure(client):
    post_json(
        client,
        submission(
            publish_no="ATM",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["A-1"], ["B-1"]],
                }
            ],
        ),
    )
    with db.get_conn() as conn:
        conn.execute(
            "CREATE TRIGGER fail_refresh BEFORE DELETE ON export_selection "
            "WHEN OLD.archive_no = 'A-1' BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )

    resp = client.post(
        "/withdraw", data=json.dumps({"archive_no": "A-1"}), content_type="application/json"
    )
    assert resp.status_code >= 500
    with db.get_conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM withdrawals WHERE archive_no = 'A-1'").fetchone()[0]
            == 0
        )
        selected = conn.execute(
            "SELECT archive_no FROM export_selection WHERE publish_no = 'ATM'"
        ).fetchall()
        assert [r["archive_no"] for r in selected] == ["A-1"]

    # The download still serves the pre-withdrawal selection.
    rows = csv_rows(client.get("/download/ATM.csv"))
    assert rows[1][3] == "A-1"


def test_prepared_download_snapshot_held_when_withdrawal_happens_midstream(client):
    N = 450  # spans several entry batches
    rows = [
        {
            "line_no": str(i),
            "old_name": "a",
            "new_name": "b",
            "evidence": [["A-1"], ["B-1"]],
        }
        for i in range(N)
    ]
    post_json(client, submission(publish_no="SNAP", rows=rows))
    assert db.list_withdrawals() == []  # prepared with cut-off 0

    resp = client.get("/download/SNAP.csv", buffered=False)
    it = iter(resp.response)
    first = next(it)
    assert b"\xe8\xa1\x8c\xe5\x8f\xb7" in first  # header
    client.post("/withdraw", data={"archive_no": "A-1"})
    rest = b"".join(it).decode("utf-8")
    resp.close()

    # The in-flight download keeps the selection fixed when fetching started.
    assert rest.count(",A-1") == N
    assert ",B-1" not in rest

    # The next download immediately takes the updated prepared selection.
    rows_after = csv_rows(client.get("/download/SNAP.csv"))
    assert len(rows_after) == N + 1
    assert all(r[3] == "B-1" for r in rows_after[1:])
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM export_selection "
                "WHERE publish_no = 'SNAP' AND archive_no = 'A-1'"
            ).fetchone()[0]
            == 0
        )


def test_repeated_withdrawal_does_not_recompute_selections(client):
    post_json(
        client,
        submission(
            publish_no="IDEM",
            rows=[
                {
                    "line_no": "1",
                    "old_name": "a",
                    "new_name": "b",
                    "evidence": [["A-1"], ["B-1"]],
                }
            ],
        ),
    )
    client.post("/withdraw", data={"archive_no": "A-1"})
    with db.get_conn() as conn:
        prepared_at = conn.execute(
            "SELECT prepared_at FROM export_preps WHERE publish_no = 'IDEM'"
        ).fetchone()[0]
    client.post("/withdraw", data={"archive_no": "A-1"})  # idempotent
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT prepared_at FROM export_preps WHERE publish_no = 'IDEM'"
            ).fetchone()[0]
            == prepared_at
        )
        selected = [
            r["archive_no"]
            for r in conn.execute(
                "SELECT archive_no FROM export_selection WHERE publish_no = 'IDEM'"
            )
        ]
        assert selected == ["B-1"]
