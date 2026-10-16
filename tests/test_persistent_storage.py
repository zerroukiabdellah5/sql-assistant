"""Persistent upload storage: the backend abstraction and its guarantees.

Background
----------
An imported upload used to live only as a file under UPLOAD_DIR, and
UPLOAD_DIR is /tmp on Vercel: a per-instance, read-only-filesystem
scratch space. An import stored by one instance was therefore invisible
to another and gone after a recycle, while every test passed because a
single process shares a single /tmp.

The fix moves WHERE bytes and records live behind two small interfaces
(app/storage.py): an ObjectStore for the file and a MetadataStore for
the record that names its owner. The local filesystem backend keeps the
old layout, so nothing about development changes; a durable backend can
be supplied for production without touching the import code.

What this file pins
-------------------
Invariants that do NOT depend on which backend is configured:

  * the local backend round-trips objects and records, and its key/scope
    validation refuses anything that could escape the root;
  * get_stores() fails CLOSED - an unimplemented backend, or "local"
    while persistence is required, raises rather than silently writing
    to ephemeral storage;
  * an import stored through one store instance is visible through a
    freshly constructed one, which is exactly what "survives an
    instance" means;
  * visitor namespaces stay isolated even when they share a backend;
  * metadata is authoritative: a stray file with no record is neither
    listed nor resolvable;
  * expired imports are swept and their objects deleted;
  * a storage failure reaches the client as 503, never a leaky 500;
  * an import materialised from any backend is still read-only SQL.
"""

import json
import os
import sqlite3
import time

import pytest

import app.config as config
import app.storage as storage
import app.uploads as uploads

from fastapi.testclient import TestClient

from app.db import execute_sql_with_metadata
from app.main import app
from app.validate import ValidationError


# ============================================================
# HELPERS
# ============================================================

def _sqlite_bytes(path, value="alpha"):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE items (id INTEGER, label TEXT)")
    connection.execute(
        "INSERT INTO items VALUES (1, ?)", (value,)
    )
    connection.commit()
    connection.close()
    with open(path, "rb") as handle:
        return handle.read()


def _visitor_id(token="a"):
    return (token * 32)[:32]


# ============================================================
# A DURABLE-LIKE BACKEND
# ============================================================
# Two plain dicts stand in for a shared, out-of-process store: objects
# live in one, records in the other, and they outlive every Store object
# built on top of them. Building a fresh Stores on each get_stores()
# call is what lets a test model two function instances reading and
# writing the same durable backend.

class FakeObjectStore(storage.ObjectStore):
    def __init__(self, blobs, workspace):
        self.blobs = blobs
        self.workspace = workspace
        self.materialized = 0

    def put(self, key, data):
        storage.safe_key(key)
        self.blobs[key] = bytes(data)

    def get(self, key):
        return self.blobs[key]

    def exists(self, key):
        return key in self.blobs

    def delete(self, key):
        return self.blobs.pop(key, None) is not None

    def materialize(self, key):
        if key not in self.blobs:
            raise storage.StorageError("Object not found.")
        self.materialized += 1
        path = os.path.join(
            str(self.workspace), f"materialized-{self.materialized}.db"
        )
        with open(path, "wb") as handle:
            handle.write(self.blobs[key])
        return path


class FakeMetadataStore(storage.MetadataStore):
    def __init__(self, records):
        self.records = records

    def save(self, scope, record):
        self.records[(scope, record["id"])] = dict(record)

    def get(self, scope, import_id):
        record = self.records.get((scope, import_id))
        return dict(record) if record is not None else None

    def list(self, scope):
        return [
            dict(record)
            for (record_scope, _), record in self.records.items()
            if record_scope == scope
        ]

    def delete(self, scope, import_id):
        return self.records.pop((scope, import_id), None) is not None


def _install_shared_backend(monkeypatch, workspace):
    """Point app.storage at a durable backend shared across instances."""

    blobs = {}
    records = {}

    def factory():
        return storage.Stores(
            object_store=FakeObjectStore(blobs, workspace),
            metadata_store=FakeMetadataStore(records),
            backend="fake-durable",
            persistent=True,
        )

    monkeypatch.setattr(storage, "get_stores", factory)

    return blobs, records


# ============================================================
# LOCAL BACKEND: OBJECTS
# ============================================================

def test_local_object_store_round_trips(meta_paths):
    store = storage.LocalObjectStore(str(meta_paths / "objects"))

    store.put("databases/abc_items.db", b"payload")

    assert store.exists("databases/abc_items.db") is True
    assert store.get("databases/abc_items.db") == b"payload"

    path = store.materialize("databases/abc_items.db")

    assert os.path.isfile(path)
    with open(path, "rb") as handle:
        assert handle.read() == b"payload"

    assert store.delete("databases/abc_items.db") is True
    assert store.exists("databases/abc_items.db") is False
    assert store.delete("databases/abc_items.db") is False


def test_local_object_store_missing_object(meta_paths):
    store = storage.LocalObjectStore(str(meta_paths / "objects"))

    with pytest.raises(storage.StorageError):
        store.get("databases/missing.db")

    with pytest.raises(storage.StorageError):
        store.materialize("databases/missing.db")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/abs/path.db",
        "../escape.db",
        "databases/../../escape.db",
        "databases\\escape.db",
        "databases//gap.db",
        "databases/./dot.db",
        "C:databases/x.db",
        "databases/nul\x00.db",
    ],
)
def test_safe_key_refuses_anything_that_could_escape(key):
    with pytest.raises(storage.StorageError):
        storage.safe_key(key)


def test_safe_key_accepts_a_built_key():
    assert (
        storage.safe_key("visitors/" + "a" * 32 + "/databases/x.db")
        == "visitors/" + "a" * 32 + "/databases/x.db"
    )


# ============================================================
# LOCAL BACKEND: RECORDS
# ============================================================

def test_local_metadata_store_round_trips(meta_paths):
    store = storage.LocalMetadataStore(str(meta_paths / "objects"))

    record = {"id": "abc123", "scope": "shared", "kind": "database"}

    store.save("shared", record)

    assert store.get("shared", "abc123") == record
    assert store.list("shared") == [record]
    assert store.delete("shared", "abc123") is True
    assert store.get("shared", "abc123") is None


def test_local_metadata_store_isolates_scopes(meta_paths):
    store = storage.LocalMetadataStore(str(meta_paths / "objects"))

    store.save("shared", {"id": "one", "scope": "shared"})
    store.save(
        "visitors/" + _visitor_id(),
        {"id": "two", "scope": "visitors/" + _visitor_id()},
    )

    assert [r["id"] for r in store.list("shared")] == ["one"]
    assert [
        r["id"]
        for r in store.list("visitors/" + _visitor_id())
    ] == ["two"]


def test_local_metadata_store_ignores_a_corrupt_record(meta_paths):
    root = str(meta_paths / "objects")
    store = storage.LocalMetadataStore(root)
    store.save("shared", {"id": "good", "scope": "shared"})

    # A truncated record must not crash a listing: it is skipped.
    os.makedirs(os.path.join(root, "_meta", "shared"), exist_ok=True)
    with open(
        os.path.join(root, "_meta", "shared", "broken.json"),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write("{not json")

    assert [r["id"] for r in store.list("shared")] == ["good"]


def test_local_metadata_store_refuses_bad_scope(meta_paths):
    store = storage.LocalMetadataStore(str(meta_paths / "objects"))

    with pytest.raises(storage.StorageError):
        store.save("../escape", {"id": "x"})

    with pytest.raises(storage.StorageError):
        store.save("shared", {"id": "../escape"})


# ============================================================
# PROVIDER SELECTION FAILS CLOSED
# ============================================================

def test_default_backend_is_local_and_not_persistent(meta_paths):
    stores = storage.get_stores()

    assert stores.backend == "local"
    assert stores.persistent is False


def test_unimplemented_backend_fails_closed(meta_paths, monkeypatch):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "s3")

    with pytest.raises(storage.StorageNotConfigured):
        storage.get_stores()


def test_persistence_required_with_only_local_fails_closed(
    meta_paths, monkeypatch
):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(
        config, "STORAGE_REQUIRE_PERSISTENT", True
    )

    with pytest.raises(storage.StorageNotConfigured):
        storage.get_stores()


def test_status_reports_without_disclosing_anything(meta_paths, monkeypatch):
    state = storage.status()

    assert set(state) == {
        "backend",
        "available",
        "persistent",
        "require_persistent",
    }
    assert state["available"] is True
    assert state["persistent"] is False

    monkeypatch.setattr(config, "STORAGE_BACKEND", "s3")
    unavailable = storage.status()

    assert unavailable["available"] is False
    assert unavailable["backend"] == "s3"


# ============================================================
# AN IMPORT SURVIVES A NEW STORE INSTANCE
# ============================================================

def test_import_is_visible_through_a_new_store_instance(
    meta_paths, monkeypatch, tmp_path
):
    _install_shared_backend(monkeypatch, tmp_path)

    source = tmp_path / "up.db"
    imported = uploads.import_database(
        "up.db", _sqlite_bytes(str(source))
    )

    # A fresh get_stores() call - the equivalent of a different function
    # instance - builds new store objects over the same durable backend.
    listed = uploads.list_imports()

    assert [item["id"] for item in listed] == [imported["id"]]

    resolved = uploads.resolve_import_path(imported["id"])

    assert os.path.isfile(resolved)


def test_sql_against_a_materialized_import_stays_read_only(
    meta_paths, monkeypatch, tmp_path
):
    _install_shared_backend(monkeypatch, tmp_path)

    source = tmp_path / "up.db"
    imported = uploads.import_database(
        "up.db", _sqlite_bytes(str(source))
    )

    path = uploads.resolve_import_path(imported["id"])

    rows, _ = execute_sql_with_metadata(
        "SELECT label FROM items", path=path
    )
    assert rows == [{"label": "alpha"}]

    with pytest.raises(ValidationError):
        execute_sql_with_metadata(
            "DELETE FROM items", path=path
        )


# ============================================================
# ISOLATION AND METADATA AUTHORITY
# ============================================================

def test_visitor_namespaces_stay_isolated_on_a_shared_backend(
    meta_paths, monkeypatch, tmp_path
):
    _install_shared_backend(monkeypatch, tmp_path)

    source = tmp_path / "up.db"
    content = _sqlite_bytes(str(source))

    a = uploads.import_file_for_visitor(
        _visitor_id("a"), "up.db", content
    )

    # The same store still keeps the two identities apart.
    assert [
        item["id"]
        for item in uploads.list_visitor_imports(_visitor_id("a"))
    ] == [a["id"]]
    assert uploads.list_visitor_imports(_visitor_id("b")) == []

    with pytest.raises(uploads.ImportNotFound):
        uploads.resolve_visitor_import_path(
            _visitor_id("b"), a["id"]
        )


def test_a_stray_object_without_a_record_is_not_an_import(
    meta_paths, monkeypatch, tmp_path
):
    blobs, _ = _install_shared_backend(monkeypatch, tmp_path)

    # A file-shaped object with no metadata record. The old
    # directory-scan lookup would have surfaced it; the record-first
    # lookup must not.
    blobs["databases/aaaaaaaaaaaa_orphan.db"] = b"not a database"

    assert uploads.list_imports() == []
    assert uploads.get_import("aaaaaaaaaaaa") is None

    with pytest.raises(uploads.ImportNotFound):
        uploads.resolve_import_path("aaaaaaaaaaaa")


# ============================================================
# EXPIRY
# ============================================================

def test_expired_imports_are_swept_and_objects_deleted(
    meta_paths, monkeypatch, tmp_path
):
    blobs, records = _install_shared_backend(monkeypatch, tmp_path)

    source = tmp_path / "up.db"
    imported = uploads.import_database(
        "up.db", _sqlite_bytes(str(source))
    )

    key = records[("shared", imported["id"])]["object_key"]
    assert key in blobs

    removed = uploads.purge_expired_imports(
        now=time.time() + config.UPLOAD_TTL_SECONDS + 1
    )

    assert removed == 1
    assert uploads.list_imports() == []
    assert key not in blobs


def test_expiry_is_disabled_when_the_ttl_is_zero(
    meta_paths, monkeypatch, tmp_path
):
    _install_shared_backend(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "UPLOAD_TTL_SECONDS", 0)

    source = tmp_path / "up.db"
    uploads.import_database("up.db", _sqlite_bytes(str(source)))

    removed = uploads.purge_expired_imports(
        now=time.time() + 10_000_000
    )

    assert removed == 0
    assert len(uploads.list_imports()) == 1


# ============================================================
# HTTP: A BROKEN BACKEND IS A 503, NOT A LEAK OR A 500
# ============================================================

def test_unconfigured_backend_answers_503_on_list(
    anon_client, monkeypatch
):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "s3")

    response = anon_client.get("/api/visitor/imports")

    assert response.status_code == 503

    body = response.json()

    assert "detail" in body
    # No provider name, module path or filesystem location leaks.
    assert "s3" not in json.dumps(body).lower()
    assert "/" not in json.dumps(body)


def test_unconfigured_backend_answers_503_on_upload(
    anon_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "s3")

    response = anon_client.post(
        "/api/visitor/imports",
        files={
            "file": (
                "up.db",
                _sqlite_bytes(str(tmp_path / "up.db")),
                "application/octet-stream",
            )
        },
    )

    assert response.status_code == 503


def test_required_persistence_with_no_backend_answers_503(
    anon_client, monkeypatch
):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(
        config, "STORAGE_REQUIRE_PERSISTENT", True
    )

    assert (
        anon_client.get("/api/visitor/imports").status_code
        == 503
    )


# ============================================================
# THE VISITOR FLOW IS UNCHANGED BY THE STORAGE MOVE
# ============================================================

def test_a_visitor_can_still_upload_list_and_query(
    anon_client, tmp_path
):
    source = tmp_path / "up.db"

    uploaded = anon_client.post(
        "/api/visitor/imports",
        files={
            "file": (
                "up.db",
                _sqlite_bytes(str(source)),
                "application/octet-stream",
            )
        },
    )

    assert uploaded.status_code == 200

    body = uploaded.json()

    assert body["kind"] == "database"
    assert "path" not in body

    listed = anon_client.get(
        "/api/visitor/imports"
    ).json()["imports"]

    assert any(item["id"] == body["id"] for item in listed)

    schema = anon_client.get(
        f"/api/visitor/schema?import_id={body['id']}"
    )

    assert schema.status_code == 200
    assert "items" in schema.json()["schema"]


def test_a_visitor_cannot_delete_another_visitors_import(
    meta_paths, app_secret, tmp_path
):
    from app import visitor as visitor_module

    source = tmp_path / "up.db"
    content = _sqlite_bytes(str(source))

    id_a = _visitor_id("a")
    id_b = _visitor_id("b")

    imported = uploads.import_file_for_visitor(
        id_a, "up.db", content
    )

    with TestClient(app) as client:

        client.cookies.set(
            config.VISITOR_COOKIE_NAME,
            visitor_module.create_visitor_token(id_b),
        )

        refused = client.delete(
            f"/api/visitor/imports/{imported['id']}"
        )

        assert refused.status_code == 404

    # The owner still has it.
    assert [
        item["id"]
        for item in uploads.list_visitor_imports(id_a)
    ] == [imported["id"]]
