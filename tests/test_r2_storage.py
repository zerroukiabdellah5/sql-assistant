"""Cloudflare R2 (S3-compatible) backend: selection, isolation, errors.

Background
----------
app/storage.py implements a durable backend for Cloudflare R2 over its
S3-compatible API. It is reached through boto3, which is imported
lazily, so this suite runs without boto3 installed.

Everything here drives that backend through a **stubbed S3 client**: a
plain in-memory object dictionary that mimics the handful of S3 calls
the backend makes (put/get/head/delete/list). That is deliberate. The
tests exercise the real selection path (get_stores -> build_r2_stores),
the real key/scope layout and the real upload code, while asserting the
guarantees that matter, without a network, a bucket or a credential.

What is NOT tested here
-----------------------
The stubbed client proves the CODE, not the PROVIDER. A real bucket may
still reject a request for reasons no stub models (signing, DNS, ACLs,
eventual-consistency timing). tests/test_r2_integration.py is the opt-in
test that talks to an actual bucket, and it is skipped unless dedicated
R2 test credentials are configured. Nothing in this file claims a real
integration has passed.
"""

import json
import time

import pytest

import app.config as config
import app.storage as storage
import app.uploads as uploads


# ============================================================
# A STUBBED S3-COMPATIBLE CLIENT
# ============================================================

class StubClientError(Exception):
    """Mimics botocore's ClientError shape closely enough to classify.

    The backend reads ``.response["Error"]["Code"]`` and
    ``.response["ResponseMetadata"]["HTTPStatusCode"]`` to tell a
    missing object from a real fault, exactly as it does against a
    genuine botocore exception. No botocore import is needed.
    """

    def __init__(self, code, status=404):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def close(self):
        pass


class StubS3Client:
    """A tiny in-memory S3: enough for the backend's five calls."""

    def __init__(self):
        self.objects = {}
        self.page_size = 1000
        self.fail_put = False
        self.fail_list = False

    def put_object(self, Bucket, Key, Body, **kwargs):
        if self.fail_put:
            raise StubClientError("InternalError", 500)
        self.objects[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, Bucket, Key, **kwargs):
        try:
            data = self.objects[(Bucket, Key)]
        except KeyError:
            raise StubClientError("NoSuchKey", 404)
        return {"Body": _Body(data)}

    def head_object(self, Bucket, Key, **kwargs):
        if (Bucket, Key) not in self.objects:
            raise StubClientError("404", 404)
        return {}

    def delete_object(self, Bucket, Key, **kwargs):
        self.objects.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(
        self, Bucket, Prefix="", ContinuationToken=None, **kwargs
    ):
        if self.fail_list:
            raise StubClientError("AccessDenied", 403)

        keys = sorted(
            key
            for (bucket, key) in self.objects
            if bucket == Bucket and key.startswith(Prefix)
        )

        start = 0
        if ContinuationToken:
            try:
                start = keys.index(ContinuationToken) + 1
            except ValueError:
                start = 0

        chunk = keys[start:start + self.page_size]

        response = {
            "Contents": [{"Key": key} for key in chunk],
            "IsTruncated": False,
        }

        if len(keys) > start + self.page_size and chunk:
            response["IsTruncated"] = True
            response["NextContinuationToken"] = chunk[-1]

        return response


def _visitor_id(token="a"):
    return (token * 32)[:32]


def _configure_r2(monkeypatch, stub, prefix=""):
    """Put the app on the R2 backend, wired to the stub client."""

    monkeypatch.setattr(config, "STORAGE_BACKEND", "r2")
    monkeypatch.setenv("R2_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "test-key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "test-secret-value")
    monkeypatch.setenv("R2_BUCKET", "test-bucket")
    monkeypatch.setenv("R2_PREFIX", prefix)
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    monkeypatch.setattr(
        storage, "_r2_client", lambda settings: stub
    )


# ============================================================
# CONFIGURATION VALIDATION
# ============================================================

def test_r2_settings_is_none_while_incomplete(monkeypatch):
    for name in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)

    assert config.r2_settings() is None

    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")

    # Bucket still missing -> not configured.
    assert config.r2_settings() is None


def test_r2_settings_derives_endpoint_and_region(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    monkeypatch.delenv("R2_REGION", raising=False)
    monkeypatch.setenv("R2_PREFIX", "/app/")

    settings = config.r2_settings()

    assert settings["endpoint"] == (
        "https://acct.r2.cloudflarestorage.com"
    )
    assert settings["region"] == "auto"
    assert settings["prefix"] == "app"
    assert settings["bucket"] == "bucket"


def test_r2_settings_respects_endpoint_override(monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv(
        "R2_ENDPOINT", "https://example.internal"
    )

    assert (
        config.r2_settings()["endpoint"]
        == "https://example.internal"
    )


# ============================================================
# SELECTION AND FAIL-CLOSED
# ============================================================

def test_get_stores_selects_r2_when_configured(meta_paths, monkeypatch):
    stub = StubS3Client()
    _configure_r2(monkeypatch, stub)

    stores = storage.get_stores()

    assert stores.backend == "r2"
    assert stores.persistent is True


def test_r2_without_credentials_fails_closed(meta_paths, monkeypatch):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "r2")

    for name in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(storage.StorageNotConfigured):
        storage.get_stores()


def test_r2_with_missing_boto3_fails_closed(meta_paths, monkeypatch):
    # Fully configured, but the S3 client library is unavailable. The
    # real _r2_client factory runs (not stubbed) and its import fails.
    monkeypatch.setenv("R2_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "test-key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "test-secret-value")
    monkeypatch.setenv("R2_BUCKET", "test-bucket")

    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "boto3" or name.startswith("botocore"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    with pytest.raises(storage.StorageNotConfigured):
        storage.build_r2_stores()


def test_status_reports_r2_without_disclosing_anything(
    meta_paths, monkeypatch
):
    stub = StubS3Client()
    _configure_r2(monkeypatch, stub)

    state = storage.status()

    assert state == {
        "backend": "r2",
        "available": True,
        "persistent": True,
        "require_persistent": False,
    }

    serialized = json.dumps(state)

    assert "test-secret-value" not in serialized
    assert "test-bucket" not in serialized
    assert "cloudflarestorage" not in serialized


# ============================================================
# OBJECT STORE
# ============================================================

def test_r2_object_store_round_trips(meta_paths):
    stub = StubS3Client()
    store = storage.S3ObjectStore(
        stub, "b", str(meta_paths / "cache")
    )

    store.put("databases/abc_items.db", b"payload")

    assert store.exists("databases/abc_items.db") is True
    assert store.get("databases/abc_items.db") == b"payload"

    path = store.materialize("databases/abc_items.db")

    with open(path, "rb") as handle:
        assert handle.read() == b"payload"

    # Materialising twice reuses the cached file (no second download).
    before = dict(stub.objects)
    store.materialize("databases/abc_items.db")
    assert stub.objects == before

    assert store.delete("databases/abc_items.db") is True
    assert store.exists("databases/abc_items.db") is False


def test_r2_object_store_missing_object(meta_paths):
    store = storage.S3ObjectStore(
        StubS3Client(), "b", str(meta_paths / "cache")
    )

    with pytest.raises(storage.StorageError):
        store.get("databases/missing.db")

    with pytest.raises(storage.StorageError):
        store.materialize("databases/missing.db")


def test_r2_object_store_rejects_unsafe_key(meta_paths):
    store = storage.S3ObjectStore(
        StubS3Client(), "b", str(meta_paths / "cache")
    )

    for key in ("", "/abs.db", "../escape.db", "a\\b.db"):
        with pytest.raises(storage.StorageError):
            store.put(key, b"x")


def test_r2_object_store_applies_prefix(meta_paths):
    stub = StubS3Client()
    store = storage.S3ObjectStore(
        stub, "b", str(meta_paths / "cache"), prefix="app"
    )

    store.put("databases/x.db", b"payload")

    assert ("b", "app/databases/x.db") in stub.objects
    assert store.get("databases/x.db") == b"payload"


def test_r2_object_store_error_message_does_not_leak(meta_paths):
    stub = StubS3Client()
    stub.fail_put = True

    store = storage.S3ObjectStore(
        stub, "secret-bucket", str(meta_paths / "cache")
    )

    with pytest.raises(storage.StorageError) as caught:
        store.put("databases/x.db", b"payload")

    message = str(caught.value)

    assert "secret-bucket" not in message
    assert "cloudflarestorage" not in message


# ============================================================
# METADATA STORE
# ============================================================

def test_r2_metadata_round_trips(meta_paths):
    store = storage.S3MetadataStore(StubS3Client(), "b")

    record = {"id": "abc123", "scope": "shared", "kind": "database"}

    store.save("shared", record)

    assert store.get("shared", "abc123") == record
    assert store.list("shared") == [record]
    assert store.delete("shared", "abc123") is True
    assert store.get("shared", "abc123") is None


def test_r2_metadata_isolates_scopes(meta_paths):
    stub = StubS3Client()
    store = storage.S3MetadataStore(stub, "b")

    va = "visitors/" + _visitor_id("a")
    vb = "visitors/" + _visitor_id("b")

    store.save("shared", {"id": "one", "scope": "shared"})
    store.save(va, {"id": "two", "scope": va})

    assert [r["id"] for r in store.list("shared")] == ["one"]
    assert [r["id"] for r in store.list(va)] == ["two"]
    assert store.list(vb) == []

    # The same id in another scope is a different record.
    assert store.get(vb, "one") is None
    assert store.get("shared", "two") is None

    # Keys are physically separated by scope, not merely filtered.
    assert (
        "b",
        "_meta/shared/one.json",
    ) in stub.objects
    assert (
        "b",
        f"_meta/{va}/two.json",
    ) in stub.objects


def test_r2_metadata_ignores_a_corrupt_record(meta_paths):
    stub = StubS3Client()
    store = storage.S3MetadataStore(stub, "b")

    store.save("shared", {"id": "good", "scope": "shared"})

    stub.objects[("b", "_meta/shared/broken.json")] = b"{not json"

    assert [r["id"] for r in store.list("shared")] == ["good"]
    assert store.get("shared", "broken") is None


def test_r2_metadata_list_paginates(meta_paths):
    stub = StubS3Client()
    stub.page_size = 1
    store = storage.S3MetadataStore(stub, "b")

    for index in range(3):
        store.save(
            "shared",
            {"id": f"id{index}", "scope": "shared"},
        )

    listed = store.list("shared")

    assert sorted(r["id"] for r in listed) == [
        "id0",
        "id1",
        "id2",
    ]


def test_r2_metadata_refuses_bad_scope_and_id(meta_paths):
    store = storage.S3MetadataStore(StubS3Client(), "b")

    with pytest.raises(storage.StorageError):
        store.save("../escape", {"id": "x"})

    with pytest.raises(storage.StorageError):
        store.save("shared", {"id": "../escape"})


def test_r2_metadata_scope_prefix_is_exact(meta_paths):
    stub = StubS3Client()
    store = storage.S3MetadataStore(stub, "b")

    short = "visitors/" + _visitor_id("a")
    # A longer id that begins with the short one must not be listed.
    longer = "visitors/" + _visitor_id("a") + "b"

    store.save(short, {"id": "one", "scope": short})
    store.save(longer, {"id": "two", "scope": longer})

    assert [r["id"] for r in store.list(short)] == ["one"]


# ============================================================
# THE UPLOAD FLOW ON R2 (STUBBED PROVIDER)
# ============================================================

def test_import_is_visible_through_a_new_store_instance(
    meta_paths, monkeypatch, tmp_path
):
    stub = StubS3Client()
    _configure_r2(monkeypatch, stub)

    source = tmp_path / "up.db"
    content = _sqlite_bytes(str(source))

    imported = uploads.import_database("up.db", content)

    # Each call to get_stores() builds fresh store objects over the same
    # bucket - the equivalent of a different function instance.
    listed = uploads.list_imports()

    assert [item["id"] for item in listed] == [imported["id"]]

    resolved = uploads.resolve_import_path(imported["id"])

    with open(resolved, "rb") as handle:
        assert handle.read() == content


def test_r2_visitor_namespaces_stay_isolated(meta_paths, monkeypatch, tmp_path):
    stub = StubS3Client()
    _configure_r2(monkeypatch, stub)

    source = tmp_path / "up.db"
    content = _sqlite_bytes(str(source))

    a = uploads.import_file_for_visitor(
        _visitor_id("a"), "up.db", content
    )

    assert [
        item["id"]
        for item in uploads.list_visitor_imports(_visitor_id("a"))
    ] == [a["id"]]
    assert uploads.list_visitor_imports(_visitor_id("b")) == []

    with pytest.raises(uploads.ImportNotFound):
        uploads.resolve_visitor_import_path(
            _visitor_id("b"), a["id"]
        )


def test_r2_expired_imports_are_swept(meta_paths, monkeypatch, tmp_path):
    stub = StubS3Client()
    _configure_r2(monkeypatch, stub)

    source = tmp_path / "up.db"
    imported = uploads.import_database(
        "up.db", _sqlite_bytes(str(source))
    )

    assert any(
        key.endswith(".db") for (_, key) in stub.objects
    )

    removed = uploads.purge_expired_imports(
        now=time.time() + config.UPLOAD_TTL_SECONDS + 1
    )

    assert removed == 1
    assert uploads.list_imports() == []

    # Both the object and its metadata record are gone.
    assert not any(
        key.endswith(".json") for (_, key) in stub.objects
    )
    assert imported["id"] not in json.dumps(
        sorted(stub.objects)
    )


def test_r2_backend_failure_reaches_the_client_as_503(
    anon_client, monkeypatch
):
    stub = StubS3Client()
    stub.fail_list = True
    _configure_r2(monkeypatch, stub)

    response = anon_client.get("/api/visitor/imports")

    assert response.status_code == 503

    text = response.text.lower()

    # No bucket, endpoint, key or credential leaks in the body.
    assert "test-bucket" not in text
    assert "cloudflarestorage" not in text
    assert "test-secret" not in text


# ============================================================
# HELPERS
# ============================================================

def _sqlite_bytes(path, value="alpha"):
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE items (id INTEGER, label TEXT)")
    connection.execute(
        "INSERT INTO items VALUES (1, ?)", (value,)
    )
    connection.commit()
    connection.close()

    with open(path, "rb") as handle:
        return handle.read()
