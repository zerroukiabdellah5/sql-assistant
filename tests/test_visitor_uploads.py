"""Anonymous visitor uploads: isolation, ownership and the unchanged gates.

The requirement these tests exist to prove:

  * any visitor can upload Excel/CSV/SQLite and query it with no owner
    sign-in and no App Access Code;
  * each visitor reaches only their own uploads - another browser cannot
    list, read, query or delete them;
  * the owner/admin endpoints are untouched, the trial still runs six
    questions then refuses, and the upload limits and read-only SQL
    rules are unchanged.

They are written against the real application (no hand-written route
list): the visitor routes are exercised over HTTP and the files are
inspected on disk. A claim that "anonymous upload works" that is not
backed by an assertion here does not belong in the report.
"""

import os
import sqlite3

import pytest

from fastapi.testclient import TestClient

import app.config as config
import app.uploads as uploads
import app.visitor as visitor

from app.main import app


# ============================================================
# HELPERS
# ============================================================

def _db_bytes(tmp_path, table="items"):
    """A minimal valid SQLite database, as upload bytes."""

    path = os.path.join(str(tmp_path), table + ".db")

    connection = sqlite3.connect(path)
    connection.execute(
        f"CREATE TABLE {table} (id INTEGER, label TEXT)"
    )
    connection.execute(
        f"INSERT INTO {table} VALUES (1, 'alpha')"
    )
    connection.commit()
    connection.close()

    with open(path, "rb") as handle:
        return handle.read()


def _csv_bytes():
    return b"id,name\n1,alpha\n2,beta\n"


def _xlsx_bytes():
    """A minimal valid .xlsx workbook, as upload bytes."""

    import io

    from openpyxl import Workbook

    stream = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales Data"
    sheet.append(["id", "amount", "note"])
    sheet.append([1, 12.5, "one"])
    sheet.append([2, 30, "two"])
    workbook.save(stream)

    return stream.getvalue()


def _upload(client, content, filename="up.db"):
    """Post an import to the VISITOR route (no credential)."""

    return client.post(
        "/api/visitor/imports",
        files={
            "file": (filename, content, "application/octet-stream")
        },
    )


@pytest.fixture
def stub_provider(monkeypatch):
    """Answer /api/ask without a network call, recording the db path."""

    seen = []

    def fake_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
        database_path=None,
        **_,
    ):
        seen.append(database_path)

        # An imported database has an "items" table; the default store
        # database has a "products" one. Answer whichever was selected
        # so a no-import question is a real 200, not a schema error.
        if database_path:
            return "SELECT * FROM items", "ok"

        return "SELECT * FROM products", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)

    return seen


# ============================================================
# AN ANONYMOUS VISITOR CAN UPLOAD, LIST, SELECT AND QUERY
# ============================================================

def test_anonymous_upload_succeeds_and_mints_an_identity(
    anon_client, tmp_path
):
    """No credential at all: the upload is accepted, and answered."""

    response = _upload(anon_client, _db_bytes(tmp_path))

    assert response.status_code == 200

    body = response.json()

    assert body["kind"] == "database"
    assert body["tables"] == ["items"]
    assert body["id"]

    # The identity travels in an HttpOnly cookie the browser cannot read.
    assert anon_client.cookies.get(config.VISITOR_COOKIE_NAME)


def test_anonymous_upload_does_not_disclose_a_server_path(
    anon_client, tmp_path
):
    """The visitor response omits the filesystem layout the owner one has."""

    body = _upload(anon_client, _db_bytes(tmp_path)).json()

    assert "path" not in body


def test_an_anonymous_visitor_lists_only_their_own_import(
    anon_client, tmp_path
):
    created = _upload(anon_client, _db_bytes(tmp_path)).json()

    listed = anon_client.get("/api/visitor/imports").json()["imports"]

    assert [item["id"] for item in listed] == [created["id"]]
    assert "path" not in listed[0]


def test_an_anonymous_visitor_queries_their_own_upload(
    anon_client, tmp_path, stub_provider
):
    created = _upload(anon_client, _db_bytes(tmp_path)).json()

    generated = anon_client.post(
        "/api/ask",
        json={"prompt": "list items", "import_id": created["id"]},
    )

    assert generated.status_code == 200

    response = anon_client.post(
        "/api/run",
        json={
            "sql": generated.json()["sql"],
            "import_id": created["id"],
            "generation_ticket": generated.json().get("generation_ticket"),
        },
    )

    assert response.status_code == 200

    body = response.json()

    assert body["success"] is True
    assert body["data"] == [{"id": 1, "label": "alpha"}]

    # It was run against the visitor's uploaded database, not store.db.
    assert stub_provider and stub_provider[0] is not None


def test_a_csv_upload_is_queryable_too(anon_client, tmp_path, stub_provider):
    """The requirement names Excel/CSV/SQLite; CSV is the cheap proof."""

    created = _upload(
        anon_client, _csv_bytes(), filename="items.csv"
    ).json()

    assert created["kind"] == "csv"

    response = anon_client.post(
        "/api/ask",
        json={"prompt": "list", "import_id": created["id"]},
    )

    assert response.status_code == 200


def test_an_excel_upload_is_listed_described_and_queryable(
    anon_client, tmp_path, monkeypatch
):
    """The requirement names Excel explicitly: upload, schema, query."""

    created = _upload(
        anon_client, _xlsx_bytes(), filename="sales.xlsx"
    ).json()

    assert created["kind"] == "excel"
    assert created["tables"] == ["Sales_Data"]

    # Listed alongside the visitor's other imports, without a path.
    listed = anon_client.get("/api/visitor/imports").json()["imports"]

    assert [item["id"] for item in listed] == [created["id"]]
    assert "path" not in listed[0]

    # Described from the converted sandbox database, not the default one.
    described = anon_client.get(
        "/api/visitor/schema", params={"import_id": created["id"]}
    )

    assert described.status_code == 200
    assert "Sales_Data" in described.json()["schema"]

    # Queried against the converted workbook's actual rows.
    seen = []

    def fake_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
        database_path=None,
        **_,
    ):
        seen.append(database_path)
        return "SELECT id, note FROM Sales_Data ORDER BY id", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)

    generated = anon_client.post(
        "/api/ask",
        json={"prompt": "list sales", "import_id": created["id"]},
    )

    assert generated.status_code == 200

    response = anon_client.post(
        "/api/run",
        json={
            "sql": generated.json()["sql"],
            "import_id": created["id"],
            "generation_ticket": generated.json().get("generation_ticket"),
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == [
        {"id": 1, "note": "one"},
        {"id": 2, "note": "two"},
    ]
    assert seen and seen[0] is not None


def test_the_file_lands_in_the_visitor_namespace(
    anon_client, tmp_path
):
    """The shared namespace stays empty; the visitor's own does not."""

    _upload(anon_client, _db_bytes(tmp_path))

    assert uploads.list_imports() == []

    root = os.path.join(config.UPLOAD_DIR, "visitors")

    assert os.path.isdir(root)
    assert any(os.scandir(root))


# ============================================================
# ONE VISITOR CANNOT REACH ANOTHER'S DATA
# ============================================================

def test_a_second_visitor_sees_nothing(meta_paths, app_secret, tmp_path):
    with TestClient(app) as first, TestClient(app) as second:

        created = _upload(first, _db_bytes(tmp_path)).json()

        # The second browser has its own, empty namespace.
        assert second.get(
            "/api/visitor/imports"
        ).json()["imports"] == []

        # It cannot describe, query or delete the first visitor's file.
        assert second.get(
            "/api/visitor/schema", params={"import_id": created["id"]}
        ).status_code == 404

        assert second.delete(
            "/api/visitor/imports/" + created["id"]
        ).status_code == 404

        # The first visitor's file is untouched by all of that.
        assert first.get(
            "/api/visitor/schema", params={"import_id": created["id"]}
        ).status_code == 200


def test_a_second_visitor_cannot_query_anothers_import(
    meta_paths, app_secret, tmp_path, stub_provider
):
    with TestClient(app) as first, TestClient(app) as second:

        created = _upload(first, _db_bytes(tmp_path)).json()

        response = second.post(
            "/api/ask",
            json={"prompt": "x", "import_id": created["id"]},
        )

        assert response.status_code == 404

        # The unknown import was refused, so the provider was never
        # reached and the default database was never substituted.
        assert stub_provider == []


def test_a_guessed_import_id_cannot_escape_the_namespace(
    anon_client, tmp_path
):
    """A well-formed id that is not the caller's resolves to 404."""

    _upload(anon_client, _db_bytes(tmp_path))

    assert anon_client.get(
        "/api/visitor/schema", params={"import_id": "deadbeef0000"}
    ).status_code == 404


def test_a_malformed_import_id_is_rejected(anon_client):
    for bad in ("../etc/passwd", "..", "a" * 65, "has space"):

        assert anon_client.get(
            "/api/visitor/schema", params={"import_id": bad}
        ).status_code in (400, 404)


# ============================================================
# THE TRIAL IS INDEPENDENT OF UPLOADS
# ============================================================

def test_no_credentials_are_needed_to_upload(anon_client, tmp_path):
    """The whole point: the trial allowance does not gate an upload."""

    assert anon_client.cookies.get(config.VISITOR_COOKIE_NAME) is None

    assert _upload(
        anon_client, _db_bytes(tmp_path)
    ).status_code == 200


def test_uploading_does_not_spend_a_trial_attempt(
    anon_client, tmp_path, stub_provider
):
    _upload(anon_client, _db_bytes(tmp_path))

    # All six free questions are still available after an upload.
    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 200


def test_uploads_still_work_after_the_trial_is_exhausted(
    anon_client, tmp_path, stub_provider
):
    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 200

    refused = anon_client.post("/api/ask", json={"prompt": "x"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "FREE_TRIAL_EXHAUSTED"

    # The exhausted visitor can still import.
    assert _upload(
        anon_client, _db_bytes(tmp_path)
    ).status_code == 200


def test_the_seventh_question_is_refused_even_with_an_import_selected(
    anon_client, tmp_path, stub_provider
):
    created = _upload(anon_client, _db_bytes(tmp_path)).json()
    payload = {"prompt": "x", "import_id": created["id"]}

    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json=payload
        ).status_code == 200

    refused = anon_client.post("/api/ask", json=payload)

    assert refused.status_code == 403
    assert refused.json()["detail"] == "FREE_TRIAL_EXHAUSTED"


def test_the_access_code_still_unlocks_ask(anon_client, stub_provider):
    assert anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    ).status_code == 200

    response = anon_client.post("/api/ask", json={"prompt": "x"})

    assert response.status_code == 200


# ============================================================
# THE OWNER PATHS ARE UNCHANGED
# ============================================================

def test_the_owner_upload_route_is_still_protected(anon_client, tmp_path):
    """The visitor route exists; the owner one did not move."""

    assert anon_client.post(
        "/api/upload",
        files={
            "file": ("x.db", _db_bytes(tmp_path), "application/octet-stream")
        },
    ).status_code == 401

    assert anon_client.get("/api/imports").status_code == 401


def test_an_owner_can_query_a_shared_import_as_before(
    authed_client, tmp_path, stub_provider
):
    created = authed_client.post(
        "/api/upload",
        files={
            "file": ("x.db", _db_bytes(tmp_path), "application/octet-stream")
        },
    ).json()

    response = authed_client.post(
        "/api/ask",
        json={"prompt": "x", "import_id": created["id"]},
    )

    assert response.status_code == 200
    assert stub_provider and stub_provider[0] is not None


def test_an_owner_can_query_their_own_visitor_namespace_import(
    meta_paths, app_secret, tmp_path, stub_provider
):
    """The fallback: an owner whose browser uploaded via the visitor route.

    The owner's session resolves shared imports first; when the id is
    not there it falls back to the owner's own visitor namespace, so the
    panel's selected import keeps working for a signed-in user.
    """

    with TestClient(app) as client:

        assert client.post(
            "/api/auth/login", json={"password": app_secret}
        ).status_code == 200

        created = _upload(client, _db_bytes(tmp_path)).json()

        assert uploads.list_imports() == []

        response = client.post(
            "/api/ask",
            json={"prompt": "x", "import_id": created["id"]},
        )

        assert response.status_code == 200


# ============================================================
# UPLOAD LIMITS AND READ-ONLY SQL ARE UNCHANGED
# ============================================================

def test_a_visitor_upload_is_rate_limited(
    meta_paths, app_secret, tmp_path
):
    """The same ceiling as the owner route, keyed by the visitor cookie."""

    limit = config.UPLOAD_RATE_LIMIT_REQUESTS

    with TestClient(app) as client:

        content = _db_bytes(tmp_path)

        created = [
            _upload(client, content, filename=f"up{index}.db").json()["id"]
            for index in range(limit)
        ]

        assert len(set(created)) == limit

        blocked = _upload(client, content, filename="over.db")

        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


def test_an_unsupported_extension_is_refused(anon_client):
    response = _upload(
        anon_client, b"nope", filename="notes.txt"
    )

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]


def test_an_oversized_upload_is_refused(anon_client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)

    response = _upload(
        anon_client, b"x" * (2 * 1024 * 1024), filename="big.csv"
    )

    assert response.status_code == 400
    assert "too large" in response.json()["detail"].lower()


def test_write_sql_is_still_blocked_for_a_visitor_import(
    anon_client, tmp_path, monkeypatch
):
    created = _upload(anon_client, _db_bytes(tmp_path)).json()

    def fake_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
        database_path=None,
        **_,
    ):
        return "UPDATE items SET label = 'x'", "write"

    monkeypatch.setattr("app.main.ask_active", fake_ask)

    response = anon_client.post(
        "/api/ask",
        json={"prompt": "update", "import_id": created["id"]},
    )

    assert response.status_code == 400


# ============================================================
# FAIL CLOSED AND LEAK-FREE
# ============================================================

def test_the_visitor_routes_fail_closed_without_a_signing_secret(
    meta_paths, no_app_secret, tmp_path
):
    """No secret to sign with means no cookie is issued: 503, not 200."""

    with TestClient(app) as client:

        assert client.get("/api/visitor/imports").status_code == 503

        assert _upload(
            client, _db_bytes(tmp_path)
        ).status_code == 503


def test_an_unsigned_or_forged_visitor_cookie_is_ignored(
    meta_paths, app_secret
):
    """A forged cookie is nothing: the caller becomes a fresh visitor."""

    with TestClient(app) as client:

        client.cookies.set(
            config.VISITOR_COOKIE_NAME, "vv1.forged.token.value"
        )

        assert client.get(
            "/api/visitor/imports"
        ).json()["imports"] == []


def test_the_visitor_id_is_never_echoed_in_a_response(
    anon_client, tmp_path
):
    response = _upload(anon_client, _db_bytes(tmp_path))

    token = anon_client.cookies.get(config.VISITOR_COOKIE_NAME)
    visitor_id = visitor.verify_visitor_token(token)

    assert visitor_id
    assert visitor_id not in response.text
