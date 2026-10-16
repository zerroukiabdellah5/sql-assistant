"""Anonymous visitor PDF reports: open access, namespace and the gates.

The requirement these tests exist to prove:

  * the PDF Report button works for a browser that holds no credential and
    never opens a sign-in;
  * the document is built from the caller's own request and their own
    import - another browser's import is a 404, and no owner data is read;
  * the owner report route is unchanged, the trial still runs, and the
    report never consumes a free attempt.

They run against the real application and assert over HTTP.
"""

import os
import sqlite3

import pytest

import app.config as config

from fastapi.testclient import TestClient

from app.main import app


# ============================================================
# HELPERS
# ============================================================

@pytest.fixture
def stub_provider(monkeypatch):
    """Answer /api/ask without a network call."""

    def fake_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
        database_path=None,
        **_,
    ):
        if database_path:
            return "SELECT * FROM items", "ok"

        return "SELECT * FROM products", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)


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


# ============================================================
# AN ANONYMOUS VISITOR CAN BUILD A REPORT
# ============================================================

def test_anonymous_report_needs_no_credential(anon_client, tmp_path):
    response = anon_client.post(
        "/api/visitor/report",
        json={
            "title": "Visitor report",
            "question": "list products",
            "sql": "SELECT * FROM products",
            "data": [{"name": "Widget"}],
            "count": 1,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:5] == b"%PDF-"


def test_the_report_uses_the_visitors_own_import(
    anon_client, tmp_path
):
    content = _db_bytes(tmp_path)

    created = anon_client.post(
        "/api/visitor/imports",
        files={"file": ("items.db", content, "application/octet-stream")},
    ).json()

    response = anon_client.post(
        "/api/visitor/report",
        json={
            "title": "My import",
            "sql": "SELECT * FROM items",
            "data": [{"id": 1, "label": "alpha"}],
            "count": 1,
            "import_id": created["id"],
        },
    )

    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


def test_a_second_visitor_cannot_report_on_anothers_import(
    meta_paths, app_secret, tmp_path
):
    with TestClient(app) as first, TestClient(app) as second:

        created = first.post(
            "/api/visitor/imports",
            files={
                "file": (
                    "up.db",
                    _db_bytes(tmp_path),
                    "application/octet-stream",
                )
            },
        ).json()

        response = second.post(
            "/api/visitor/report",
            json={
                "title": "Peek",
                "sql": "SELECT 1",
                "import_id": created["id"],
            },
        )

        assert response.status_code == 404


# ============================================================
# THE TRIAL AND THE OWNER SURFACE ARE UNCHANGED
# ============================================================

def test_building_a_report_does_not_spend_a_trial_attempt(
    anon_client, stub_provider
):
    assert anon_client.post(
        "/api/visitor/report",
        json={"title": "R", "sql": "SELECT 1"},
    ).status_code == 200

    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 200


def test_the_owner_report_route_is_still_protected(anon_client):
    assert anon_client.post(
        "/api/report", json={"title": "R", "sql": "SELECT 1"}
    ).status_code == 401


# ============================================================
# FAIL CLOSED
# ============================================================

def test_the_visitor_report_route_fails_closed_without_a_secret(
    meta_paths, no_app_secret
):
    with TestClient(app) as client:

        assert client.post(
            "/api/visitor/report",
            json={"title": "R", "sql": "SELECT 1"},
        ).status_code == 503
