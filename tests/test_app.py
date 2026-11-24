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
