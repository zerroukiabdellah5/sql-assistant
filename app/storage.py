# ============================================================
# PERSISTENT UPLOAD STORAGE
# ============================================================
# Uploaded imports are kept in three pieces:
#
#   1. the materialised SQLite file (the data itself);
#   2. a metadata record naming its owner, kind, tables and object key;
#   3. a namespace that isolates one visitor's keys from another's.
#
# This module owns all three behind two small interfaces so the rest of
# the application never assumes the filesystem is where bytes live:
#
#   * ObjectStore   - put/get/exists/delete/materialise opaque blobs;
#   * MetadataStore - save/get/list/delete records keyed by (scope, id).
#
# WHY AN INTERFACE
#   The runtime this application targets (Vercel Functions) has a
#   read-only filesystem and a per-instance /tmp. A per-instance path is
#   fine for a request that starts and ends inside one invocation, and
#   wrong for uploads, which have to survive from the request that
#   stored them to the request that queries them - possibly on a
#   different instance. Making the store an explicit dependency is what
#   lets a durable backend be dropped in without touching callers.
#
# WHAT IS IMPLEMENTED HERE
#   Two backends:
#
#   * LOCAL - a filesystem implementation with no external dependency at
#     all, which is what development and tests use. It is the same
#     layout the application used before this module existed, so nothing
#     about local behaviour changes.
#
#   * R2 - an S3-compatible implementation for Cloudflare R2, selected
#     with STORAGE_BACKEND=r2, which is the durable backend production
#     uses. It is reached through boto3 (imported lazily, so local
#     development and the test suite never need it installed).
#
# WHAT IS NOT, AND WHY
#   Selecting a backend that is neither "local" nor "r2" FAILS CLOSED
#   (StorageNotConfigured) rather than silently falling back to /tmp.
#   The "r2" backend fails closed the same way while its credentials are
#   incomplete. An operator who asks for durable storage either gets it
#   or gets an error, never a quiet degradation. See DEPLOYMENT.md.
# ============================================================

import hashlib
import json
import logging
import os
import re
import time
import uuid

from typing import NamedTuple

import app.config as config

logger = logging.getLogger("app.storage")


# ============================================================
# ERRORS
# ============================================================
# StorageError is deliberately NOT a ValidationError: it describes a
# server-side condition (misconfiguration, an object that went missing),
# not something the caller did wrong. app.observability answers it with
# a 503 and a generic message, never the underlying detail.

class StorageError(Exception):
    """A storage operation could not be completed."""


class StorageNotConfigured(StorageError):
    """No usable backend is configured for the requested operation."""


# ============================================================
# KEY AND SCOPE SAFETY
# ============================================================
# Every key and scope is validated before it is joined to a path, so a
# value that could escape the storage root - a separator, a parent
# reference, a drive colon, a NUL - is refused before any filesystem
# call. This mirrors the import-id and visitor-id rules in
# app/uploads.py one level down.

_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")

# A scope is either the shared (owner) namespace or one visitor's. A
# visitor id is new_visitor_id() from app/visitor.py: 32 hex characters.
_SCOPE_RE = re.compile(r"^(shared|visitors/[A-Za-z0-9]{16,64})$")

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def safe_key(key):
    """Validate an object key, returning it unchanged when it is safe.

    Keys are forward-slash relative paths built only from known
    components (a scope, a subdirectory, a generated filename). The
    rules are stated explicitly anyway, because a key is joined to a
    path and must never be able to leave the root.
    """

    if not isinstance(key, str) or not key:
        raise StorageError("Invalid storage key.")

    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise StorageError("Invalid storage key.")

    if _DRIVE_RE.match(key):
        raise StorageError("Invalid storage key.")

    parts = key.split("/")

    if any(part in ("", ".", "..") for part in parts):
        raise StorageError("Invalid storage key.")

    return key


def _safe_scope(scope):

    if not isinstance(scope, str) or not _SCOPE_RE.match(scope):
        raise StorageError("Invalid storage scope.")

    return scope


# ============================================================
# INTERFACES
# ============================================================

class ObjectStore:
    """Opaque blob storage, keyed by a safe relative string."""

    def put(self, key, data):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def exists(self, key):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError

    def materialize(self, key):
        """Return a local filesystem path holding the object's bytes.

        SQLite can only open a real file, so the read path needs one.
        A filesystem backend returns the stored path directly; a remote
        backend would download to a local cache file and return that.
        """

        raise NotImplementedError


class MetadataStore:
    """Import records keyed by (scope, import_id)."""

    def save(self, scope, record):
        raise NotImplementedError

    def get(self, scope, import_id):
        raise NotImplementedError

    def list(self, scope):
        raise NotImplementedError

    def delete(self, scope, import_id):
        raise NotImplementedError


# ============================================================
# LOCAL (FILESYSTEM) BACKEND
# ============================================================

class LocalObjectStore(ObjectStore):
    """Store objects as files under ``root``.

    The key is the relative path, so the on-disk layout is unchanged
    from the pre-abstraction application: ``databases/<id>_<name>`` for
    the shared namespace and ``visitors/<id>/sandboxes/<id>_<name>`` for
    a visitor's. Writes go to a temporary sibling and are committed with
    os.replace, so a reader never sees a half-written object.
    """

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _path(self, key):

        safe_key(key)

        base = os.path.realpath(self.root)
        candidate = os.path.realpath(
            os.path.join(base, *key.split("/"))
        )

        try:
            inside = os.path.commonpath([candidate, base]) == base
        except ValueError:
            inside = False

        if not inside:
            raise StorageError("Invalid storage key.")

        return candidate

    def put(self, key, data):

        path = self._path(key)
        directory = os.path.dirname(path)

        os.makedirs(directory, exist_ok=True)

        temporary = f"{path}.tmp-{uuid.uuid4().hex}"

        try:

            with open(temporary, "wb") as handle:
                handle.write(data)

            os.replace(temporary, path)

        except Exception:

            try:
                os.remove(temporary)
            except OSError:
                pass

            raise

    def get(self, key):

        path = self._path(key)

        if not os.path.isfile(path):
            raise StorageError("Object not found.")

        with open(path, "rb") as handle:
            return handle.read()

    def exists(self, key):

        return os.path.isfile(self._path(key))

    def delete(self, key):

        path = self._path(key)

        if os.path.isfile(path):
            os.remove(path)
            return True

        return False

    def materialize(self, key):

        path = self._path(key)

        if not os.path.isfile(path):
            raise StorageError("Object not found.")

        return path


class LocalMetadataStore(MetadataStore):
    """Store import records as one JSON document per (scope, id).

    Records live under ``<root>/_meta/<scope>/<id>.json``, beside the
    objects but outside the ``databases``/``sandboxes`` trees that the
    object layout uses, so the two never collide. A truncated or
    unreadable record is treated as absent and left on disk rather than
    crashing a listing.
    """

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _directory(self, scope):

        _safe_scope(scope)

        return os.path.join(
            self.root, "_meta", *scope.split("/")
        )

    def _path(self, scope, import_id):

        if not _IMPORT_ID_RE.match(import_id or ""):
            raise StorageError("Invalid import id.")

        return os.path.join(
            self._directory(scope), import_id + ".json"
        )

    def save(self, scope, record):

        path = self._path(scope, record.get("id"))

        os.makedirs(os.path.dirname(path), exist_ok=True)

        temporary = f"{path}.tmp-{uuid.uuid4().hex}"

        try:

            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)

            os.replace(temporary, path)

        except Exception:

            try:
                os.remove(temporary)
            except OSError:
                pass

            raise

    def get(self, scope, import_id):

        try:
            path = self._path(scope, import_id)
        except StorageError:
            return None

        if not os.path.isfile(path):
            return None

        try:

            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)

        except (OSError, ValueError):

            logger.warning("UNREADABLE IMPORT RECORD")
            return None

        return record if isinstance(record, dict) else None

    def list(self, scope):

        try:
            directory = self._directory(scope)
        except StorageError:
            return []

        if not os.path.isdir(directory):
            return []

        records = []

        for name in sorted(os.listdir(directory)):

            if not name.endswith(".json"):
                continue

            record = self.get(scope, name[:-5])

            if record is not None:
                records.append(record)

        return records

    def delete(self, scope, import_id):

        try:
            path = self._path(scope, import_id)
        except StorageError:
            return False

        if os.path.isfile(path):
            os.remove(path)
            return True

        return False


# ============================================================
# S3-COMPATIBLE (CLOUDFLARE R2) BACKEND
# ============================================================
# R2 speaks the S3 API, so one implementation covers both stores with
# the same client. The two only differ in their key layout:
#
#   * objects are stored at the same keys the local backend uses, so an
#     object key means the same thing everywhere:
#     "databases/<id>_<name>", "sandboxes/<id>_<name>", or
#     "visitors/<id>/databases/<id>_<name>";
#
#   * metadata records are objects too, under
#     "<prefix>/_meta/<scope>/<id>.json", listed with ListObjectsV2.
#
# ISOLATION AND CONCURRENCY
#   * Isolation is structural: a record's key begins with its scope, so
#     two visitors can never address each other's records, and a lookup
#     re-checks record["scope"] anyway (defence in depth). A visitor id
#     and an import id are validated exactly as the local backend
#     validates them, before any key is built.
#   * There is NO read-modify-write and no shared mutable row: save()
#     PUTs one whole record to one key. Two concurrent uploads use
#     different ids, so they touch different keys and cannot lose each
#     other's write; S3 PUT is atomic, so a reader sees the old or the
#     new document, never a half-written one. An id collision (uuid4
#     hex[:12]) would overwrite, which is why ids are random and not
#     sequential.
#   * Ownership cannot be forged: the scope is taken from a signed
#     cookie (the visitor) or the owner route, never from the request
#     body, and every get/list/delete re-filters by that scope.
#
# ERRORS NEVER LEAK
#   Every client exception is converted to a StorageError with a fixed
#   message. No bucket, endpoint, key, credential or botocore detail
#   reaches a caller, and app.observability turns a StorageError into a
#   503 with only a reference id.

_META_PREFIX = "_meta"

# Missing-object signals from an S3 head/get. boto3 raises ClientError
# with a .response payload; the same shape is reproduced by the stub
# client the tests inject, so no botocore import is needed here.
_NOT_FOUND_CODES = ("NoSuchKey", "NotFound", "404")


def _is_not_found(exception):
    """True when an S3 error means "no such object" (not a real fault)."""

    response = getattr(exception, "response", None)

    if not isinstance(response, dict):
        return False

    code = str(
        (response.get("Error") or {}).get("Code") or ""
    )

    if code in _NOT_FOUND_CODES:
        return True

    status = (
        response.get("ResponseMetadata") or {}
    ).get("HTTPStatusCode")

    return status == 404


class S3ObjectStore(ObjectStore):
    """Store objects in an S3/R2 bucket.

    ``materialize`` downloads the object to a local cache file, because
    SQLite can only open a real path. The cache is keyed by a hash of
    the object key (so a hostile key can never shape a path) and pruned
    of files older than the upload TTL, which keeps the per-instance
    /tmp bounded without a background job.
    """

    def __init__(self, client, bucket, cache_dir, prefix=""):
        self.client = client
        self.bucket = bucket
        self.cache_dir = os.path.abspath(cache_dir)
        self.prefix = (prefix or "").strip("/")

    def _full(self, key):

        return f"{self.prefix}/{key}" if self.prefix else key

    def _call(self, method, **kwargs):

        try:
            return method(**kwargs)
        except Exception as exception:
            # Deliberately a fixed message: the botocore detail carries
            # the endpoint and bucket and must not reach a caller.
            raise StorageError(
                "Storage operation failed."
            ) from exception

    def put(self, key, data):

        safe_key(key)

        self._call(
            self.client.put_object,
            Bucket=self.bucket,
            Key=self._full(key),
            Body=bytes(data),
        )

    def _download(self, key):

        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self._full(key)
            )
        except Exception as exception:

            if _is_not_found(exception):
                raise StorageError("Object not found.") from exception

            raise StorageError(
                "Storage operation failed."
            ) from exception

        body = response["Body"]

        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def get(self, key):

        safe_key(key)
        return self._download(key)

    def exists(self, key):

        safe_key(key)

        try:
            self.client.head_object(
                Bucket=self.bucket, Key=self._full(key)
            )
            return True
        except Exception as exception:

            if _is_not_found(exception):
                return False

            raise StorageError(
                "Storage operation failed."
            ) from exception

    def delete(self, key):

        safe_key(key)

        self._call(
            self.client.delete_object,
            Bucket=self.bucket,
            Key=self._full(key),
        )

        return True

    def _cache_path(self, key):

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()

        return os.path.join(self.cache_dir, digest)

    def _prune_cache(self):

        ttl = int(
            getattr(config, "UPLOAD_TTL_SECONDS", 0) or 0
        )

        if ttl <= 0 or not os.path.isdir(self.cache_dir):
            return

        cutoff = time.time() - ttl

        for name in os.listdir(self.cache_dir):

            path = os.path.join(self.cache_dir, name)

            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                # A cache entry that cannot be removed is not a failure:
                # it is re-created on the next download either way.
                continue

    def materialize(self, key):

        safe_key(key)

        target = self._cache_path(key)

        if os.path.isfile(target):
            return target

        self._prune_cache()

        data = self._download(key)

        os.makedirs(self.cache_dir, exist_ok=True)

        temporary = f"{target}.tmp-{uuid.uuid4().hex}"

        try:

            with open(temporary, "wb") as handle:
                handle.write(data)

            os.replace(temporary, target)

        except Exception:

            try:
                os.remove(temporary)
            except OSError:
                pass

            raise

        return target


class S3MetadataStore(MetadataStore):
    """Store import records as objects under ``_meta/<scope>/``."""

    def __init__(self, client, bucket, prefix=""):
        self.client = client
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")

    def _parts(self, *components):

        return "/".join(
            part for part in (self.prefix, *components) if part
        )

    def _record_key(self, scope, import_id):

        _safe_scope(scope)

        if not _IMPORT_ID_RE.match(import_id or ""):
            raise StorageError("Invalid import id.")

        return self._parts(
            _META_PREFIX, scope, import_id + ".json"
        )

    def _scope_prefix(self, scope):

        _safe_scope(scope)

        return self._parts(_META_PREFIX, scope) + "/"

    def _call(self, method, **kwargs):

        try:
            return method(**kwargs)
        except Exception as exception:
            raise StorageError(
                "Storage operation failed."
            ) from exception

    def save(self, scope, record):

        key = self._record_key(scope, record.get("id"))

        body = json.dumps(record, ensure_ascii=False).encode("utf-8")

        self._call(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )

    def get(self, scope, import_id):

        try:
            key = self._record_key(scope, import_id)
        except StorageError:
            return None

        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=key
            )
        except Exception as exception:

            if _is_not_found(exception):
                return None

            raise StorageError(
                "Storage operation failed."
            ) from exception

        body = response["Body"]

        try:
            data = body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

        try:
            record = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("UNREADABLE IMPORT RECORD")
            return None

        return record if isinstance(record, dict) else None

    def _list_keys(self, prefix):

        keys = []
        token = None

        while True:

            kwargs = {"Bucket": self.bucket, "Prefix": prefix}

            if token:
                kwargs["ContinuationToken"] = token

            response = self._call(
                self.client.list_objects_v2, **kwargs
            )

            for item in response.get("Contents") or []:
                keys.append(item.get("Key"))

            if response.get("IsTruncated"):
                token = response.get("NextContinuationToken")
                if not token:
                    break
            else:
                break

        return keys

    def list(self, scope):

        try:
            prefix = self._scope_prefix(scope)
        except StorageError:
            return []

        records = []

        for key in self._list_keys(prefix):

            if not key or not key.endswith(".json"):
                continue

            import_id = key[len(prefix):-5]

            # A record is one path segment; anything deeper, or an id
            # that fails the shared rule, is not one of ours and is
            # ignored rather than resolved.
            if not import_id or "/" in import_id:
                continue

            if not _IMPORT_ID_RE.match(import_id):
                continue

            record = self.get(scope, import_id)

            if record is not None:
                records.append(record)

        return records

    def delete(self, scope, import_id):

        try:
            key = self._record_key(scope, import_id)
        except StorageError:
            return False

        self._call(
            self.client.delete_object,
            Bucket=self.bucket,
            Key=key,
        )

        return True


# ============================================================
# PROVIDER SELECTION
# ============================================================

class Stores(NamedTuple):
    """The stores a request uses, plus how durable they are."""

    object_store: ObjectStore
    metadata_store: MetadataStore
    backend: str
    persistent: bool


# "local" is the development/test filesystem backend; "r2" is the
# durable S3-compatible backend. Anything else is listed only so an
# operator who selects it gets a clear fail-closed error naming what
# was asked for, instead of an AttributeError deep in a request.
SUPPORTED_BACKENDS = ("local", "r2")

# Unused for the local backend, but the R2 client needs a fixed base
# for its materialize cache. It lives under the writable upload
# directory so it is /tmp on Vercel and the project dir locally.
_CACHE_DIRNAME = "_cache"


def _r2_client(settings):
    """Build a boto3 S3 client for R2, or fail closed.

    boto3 is imported here, not at module import, so the local backend
    and the whole test suite run without it installed. A missing library
    is a configuration error, not a crash: it fails closed like any
    other unconfigured backend.
    """

    try:
        import boto3
        from botocore.config import Config
    except ImportError as exception:
        raise StorageNotConfigured(
            "No shared storage provider is configured."
        ) from exception

    return boto3.client(
        "s3",
        endpoint_url=settings["endpoint"],
        aws_access_key_id=settings["access_key_id"],
        aws_secret_access_key=settings["secret_access_key"],
        region_name=settings["region"],
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def build_r2_stores(client=None):
    """Build the R2 stores, or raise StorageNotConfigured.

    ``client`` lets a test inject a stub S3 client; production passes
    nothing and a real boto3 client is constructed. Config validation
    happens first, so an incomplete backend never reaches boto3.
    """

    settings = config.r2_settings()

    if not settings:
        raise StorageNotConfigured(
            "No shared storage provider is configured."
        )

    if client is None:

        try:
            client = _r2_client(settings)
        except StorageError:
            raise
        except Exception as exception:

            # A client that cannot even be constructed (a malformed
            # endpoint, a broken boto3 install) is treated the same as
            # an unconfigured backend: fail closed, with a message that
            # names nothing, rather than crash a request or the boot.
            raise StorageNotConfigured(
                "No shared storage provider is configured."
            ) from exception

    cache_dir = os.path.join(
        os.path.abspath(config.UPLOAD_DIR), _CACHE_DIRNAME
    )

    return Stores(
        object_store=S3ObjectStore(
            client,
            settings["bucket"],
            cache_dir,
            prefix=settings["prefix"],
        ),
        metadata_store=S3MetadataStore(
            client, settings["bucket"], prefix=settings["prefix"]
        ),
        backend="r2",
        persistent=True,
    )


def get_stores():
    """Build the configured stores, or fail closed.

    Rebuilt per call and never cached: construction is path arithmetic
    for the local backend and a client construction for R2, and reading
    config on every call keeps a test (or a rotated secret) from being
    masked by a stale singleton.
    """

    backend = (config.STORAGE_BACKEND or "local").strip().lower()

    if backend == "r2":
        return build_r2_stores()

    if backend != "local":

        # No credential, endpoint or bucket is invented here. An
        # unimplemented backend is an error, never a silent fall back.
        raise StorageNotConfigured(
            "No shared storage provider is configured."
        )

    if config.STORAGE_REQUIRE_PERSISTENT:

        # The operator has demanded durability and only ephemeral
        # storage is available: refuse rather than pretend.
        raise StorageNotConfigured(
            "Persistent storage is required but not configured."
        )

    root = os.path.abspath(config.UPLOAD_DIR)

    return Stores(
        object_store=LocalObjectStore(root),
        metadata_store=LocalMetadataStore(root),
        backend="local",
        persistent=False,
    )


def status():
    """A safe, secret-free description of the storage configuration.

    Used by /health and /api/version: a name and two booleans, never a
    path, credential or endpoint.
    """

    backend = (config.STORAGE_BACKEND or "local").strip().lower()

    base = {
        "backend": backend,
        "require_persistent": bool(
            config.STORAGE_REQUIRE_PERSISTENT
        ),
    }

    try:

        stores = get_stores()

    except StorageError:

        # Any failure to build the stores - unconfigured, or a client
        # that could not be constructed - is reported as unavailable.
        # status() is a diagnostic and must never take a request down.
        return {**base, "available": False, "persistent": False}

    return {
        **base,
        "available": True,
        "persistent": stores.persistent,
    }
