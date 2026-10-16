"""Opt-in integration test against a REAL Cloudflare R2 bucket.

This is the only test that talks to a real S3-compatible endpoint. It
runs ONLY when dedicated test credentials are present, and it is
skipped otherwise, so an ordinary `pytest` run neither needs a bucket
nor touches one.

It deliberately does NOT read the production variables
(R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET).
Those name the live bucket the application uses. This test uses a
separate, clearly-labelled set instead, so a misconfigured local .env
can never point an automated test at production data:

    R2_TEST_ACCOUNT_ID
    R2_TEST_ACCESS_KEY_ID
    R2_TEST_SECRET_ACCESS_KEY
    R2_TEST_BUCKET
    R2_TEST_ENDPOINT     (optional; derived from the account when unset)
    R2_TEST_PREFIX       (optional; a unique prefix is chosen per run)

Run it with, for example:

    R2_TEST_ACCOUNT_ID=... R2_TEST_ACCESS_KEY_ID=... \
    R2_TEST_SECRET_ACCESS_KEY=... R2_TEST_BUCKET=... \
    pytest tests/test_r2_integration.py -v

The bucket should be a scratch bucket: the test writes an object under
a unique prefix and removes everything it created. Until it has been
run green against a real bucket, NO claim is made that R2 works in
production - the stubbed suite proves the code, not the provider.
"""

import os
import uuid

import pytest

import app.storage as storage


_REQUIRED = (
    "R2_TEST_ACCOUNT_ID",
    "R2_TEST_ACCESS_KEY_ID",
    "R2_TEST_SECRET_ACCESS_KEY",
    "R2_TEST_BUCKET",
)


def _missing():
    return [name for name in _REQUIRED if not os.getenv(name)]


def _boto3_available():
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    bool(_missing()) or not _boto3_available(),
    reason=(
        "R2 test credentials (R2_TEST_*) are not configured, or "
        "boto3 is not installed."
    ),
)


@pytest.fixture
def r2_client():
    import boto3
    from botocore.config import Config

    account = os.environ["R2_TEST_ACCOUNT_ID"]
    endpoint = os.getenv("R2_TEST_ENDPOINT") or (
        f"https://{account}.r2.cloudflarestorage.com"
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_TEST_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ[
            "R2_TEST_SECRET_ACCESS_KEY"
        ],
        region_name=os.getenv("R2_TEST_REGION", "auto"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )

    bucket = os.environ["R2_TEST_BUCKET"]
    prefix = os.getenv("R2_TEST_PREFIX") or (
        "tiix-it-" + uuid.uuid4().hex
    )

    yield client, bucket, prefix.strip("/")

    # Clean up everything this run created, under its unique prefix.
    try:
        response = client.list_objects_v2(
            Bucket=bucket, Prefix=prefix
        )
        contents = response.get("Contents") or []
        if contents:
            client.delete_objects(
                Bucket=bucket,
                Delete={
                    "Objects": [
                        {"Key": item["Key"]} for item in contents
                    ]
                },
            )
    except Exception:
        # Cleanup is best-effort; a leftover scratch object is not a
        # test failure, and deleting is never allowed to mask one.
        pass


def test_real_r2_object_round_trip(r2_client):
    client, bucket, prefix = r2_client

    store = storage.S3ObjectStore(
        client, bucket, cache_dir=os.getenv("TMPDIR") or "/tmp",
        prefix=prefix,
    )

    key = "databases/integration.db"

    store.put(key, b"hello-r2")

    try:
        assert store.exists(key) is True
        assert store.get(key) == b"hello-r2"

        path = store.materialize(key)
        with open(path, "rb") as handle:
            assert handle.read() == b"hello-r2"

        assert store.delete(key) is True
        assert store.exists(key) is False
    finally:
        try:
            store.delete(key)
        except storage.StorageError:
            pass


def test_real_r2_metadata_round_trip(r2_client):
    client, bucket, prefix = r2_client

    store = storage.S3MetadataStore(
        client, bucket, prefix=prefix
    )

    scope = "visitors/" + ("a" * 32)
    record = {"id": "it0000000001", "scope": scope}

    store.save(scope, record)

    try:
        assert store.get(scope, "it0000000001") == record
        assert [
            r["id"] for r in store.list(scope)
        ] == ["it0000000001"]

        # Isolation holds against the real backend too.
        other = "visitors/" + ("b" * 32)
        assert store.get(other, "it0000000001") is None

        assert store.delete(scope, "it0000000001") is True
        assert store.get(scope, "it0000000001") is None
    finally:
        try:
            store.delete(scope, "it0000000001")
        except storage.StorageError:
            pass
