import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from app.main import app


def make_db_bytes():
    descriptor, path = tempfile.mkstemp(suffix=".db")
    os.close(descriptor)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE items (id INTEGER, label TEXT)")
    connection.execute("INSERT INTO items VALUES (1, 'alpha')")
    connection.commit()
    connection.close()
    with open(path, "rb") as handle:
        content = handle.read()
    os.remove(path)
    return content


def test_health(meta_paths):
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_version(meta_paths):
    with TestClient(app) as client:
        response = client.get("/api/version")
        body = response.json()
        assert body["database_exists"] is True
        assert "gemini_configured" in body


def test_schema(meta_paths):
    with TestClient(app) as client:
        response = client.get("/api/schema")
        assert response.status_code == 200
        assert "TABLE products" in response.json()["schema"]


def test_session_roundtrip(meta_paths):
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"title": "Route session"}
        )
        assert created.status_code == 200
        session_id = created.json()["id"]

        listed = client.get("/api/sessions")
        assert session_id in {s["id"] for s in listed.json()["sessions"]}

        detail = client.get(f"/api/sessions/{session_id}")
        assert detail.status_code == 200
        assert detail.json()["session"]["id"] == session_id

        deleted = client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_ask_missing_session_404(meta_paths):
    with TestClient(app) as client:
        response = client.post(
            "/api/ask",
            json={
                "prompt": "show products",
                "session_id": "does-not-exist",
            },
        )
        assert response.status_code == 404


def test_approval_roundtrip(meta_paths):
    with TestClient(app) as client:
        created = client.post(
            "/api/approvals",
            json={"kind": "mutation", "sql": "DELETE FROM items"},
        )
        assert created.status_code == 200
        approval_id = created.json()["id"]
        assert created.json()["status"] == "pending"

        approved = client.post(
            f"/api/approvals/{approval_id}/approve"
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        pending = client.get("/api/approvals?status=pending")
        assert approval_id not in {a["id"] for a in pending.json()["approvals"]}


def test_upload_database_endpoint(meta_paths):
    with TestClient(app) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("items.db", make_db_bytes(), "application/octet-stream")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "database"
        assert body["tables"] == ["items"]

        listing = client.get("/api/imports")
        assert body["id"] in {i["id"] for i in listing.json()["imports"]}

        detail = client.get(f"/api/imports/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["table_count"] == 1


def test_report_endpoint(meta_paths):
    with TestClient(app) as client:
        response = client.post(
            "/api/report",
            json={
                "title": "Test Report",
                "question": "how many",
                "sql": "SELECT 1",
                "data": [{"id": 1, "value": 10}],
                "count": 1,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.content[:5] == b"%PDF-"