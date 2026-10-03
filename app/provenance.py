# ============================================================
# RESULT PROVENANCE
# ============================================================
# Provenance answers "where did this result come from and how was it
# produced". It deliberately does NOT answer "is this result correct".
#
# WHAT PROVENANCE IS
#   * which database file the rows were read from (basename plus a
#     content fingerprint),
#   * which provider and model produced the SQL,
#   * when the SQL was executed and how long the database took,
#   * how many rows came back, how many the query produced, and whether
#     the application cut the result short,
#   * the request id that ties all of it to one HTTP request.
#
# WHAT PROVENANCE IS NOT
#   It is not a correctness signal. Nothing here verifies that the SQL
#   answers the user's question, that the columns are the right ones, or
#   that the numbers mean what the user thinks. generated_by_ai is
#   descriptive: it records that a language model wrote the statement.
#   The words "verified", "correct", "accurate" and "trusted" are
#   deliberately absent from this module, and the fingerprint identifies
#   the database file that was read, not the answer.
#
#   The absence of a correctness signal is published rather than left
#   to be inferred: app/correctness.build_verification() says outright
#   that nothing checked the answer, as a separate key beside this one.
#
# WHAT IS NEVER PUT IN PROVENANCE
#   No filesystem path (only a basename), no directory, no SQL, no
#   prompt, no schema, no rows, no session id, no client IP, no API key,
#   no cookie, no header. source_sha256 is a digest of the database file
#   only; it is never a digest of a query, a result set or a secret.
#
# FAILURE IS ALWAYS OPTIONAL METADATA
#   Every helper here returns null rather than raising. A missing file,
#   an unreadable file, a filesystem error or an unexpected type must
#   never turn a successful query into a failure, and must never put an
#   exception message in a response body.
#
# THE COUNTING RULE (see result_metadata)
#   rows_returned is what the client received. total_matched is the
#   number of rows the executed statement produced, and it is reported
#   only when the application consumed the entire result set. There is
#   no COUNT(*) wrapper and no second execution, because wrapping a
#   statement changes what LIMIT, OFFSET, DISTINCT, GROUP BY,
#   aggregates, CTEs, window functions and UNION mean. When the result
#   set was truncated, the total is unknowable from what was read, so
#   total_matched is null.
#
# LOGGING
#   No provenance log line is emitted. The access line, the token usage
#   line and this response body already describe the request, and a
#   third line per request would duplicate them. The client receives the
#   basename and the digest, which is the correlation data a support
#   question actually needs.
# ============================================================

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.token_usage import safe_field, safe_request_id

# Bounds. The basename is the only filesystem information that leaves
# the process, and it is short by definition; the digest is fixed length.
MAX_NAME_CHARS = 64

# A fingerprint reads the whole file. store.db is small, but an
# arbitrarily large file would turn a metadata field into a stall, so
# anything above this is reported as unavailable instead.
MAX_FINGERPRINT_BYTES = 256 * 1024 * 1024

_FINGERPRINT_CHUNK_BYTES = 1024 * 1024

# The largest elapsed time that is reported. A query cannot legitimately
# run for a day, and the bound stops a broken clock producing a silly
# number in a response body.
MAX_ELAPSED_MS = 86_400_000.0


# ------------------------------------------------------------
# A) SOURCE DATABASE NAME
# ------------------------------------------------------------

def source_database(path: Any) -> Optional[str]:
    """The basename of the database file, and nothing else.

    Returns "store.db" for the configured database. Parent directories,
    drive letters, the absolute path and DATABASE_PATH itself never
    leave this function: os.path.basename is applied first and the
    result is sanitised and bounded afterwards, so an unusual filename
    cannot smuggle a separator through.
    """

    try:

        # Only something that can actually be a path is accepted. A
        # stray integer, None or arbitrary object is not a database,
        # and stringifying it would invent a name.
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")

        if not isinstance(path, (str, os.PathLike)):
            return None

        target = os.fspath(path)

        if not isinstance(target, str):
            return None

        # A directory is not a database file.
        if os.path.isdir(target):
            return None

        name = os.path.basename(target)

        # basename() returns "." or "" for a directory-only path, and
        # on Windows a trailing separator can survive it.
        name = name.strip().strip("\\/")

        if name in ("", ".", ".."):
            return None

        # basename() already removed every separator, and safe_label
        # applies the same sanitising and bound the other short labels
        # get, so an unusual filename cannot smuggle anything through.
        return safe_label(name, MAX_NAME_CHARS)

    except Exception:
        return None


# ------------------------------------------------------------
# B) SOURCE FINGERPRINT
# ------------------------------------------------------------

def source_sha256(path: Any) -> Optional[str]:
    """SHA-256 of the SQLite file that was actually read.

    64 lowercase hexadecimal characters, or null. The path is never
    part of the answer, an unreadable file yields null rather than an
    exception message, and the query result is never hashed: this
    identifies the database file at execution time and nothing about
    the rows or the statement.
    """

    try:

        if not isinstance(path, (str, bytes, os.PathLike)):
            return None

        target = os.fspath(path)

        if isinstance(target, bytes):
            target = target.decode("utf-8", "replace")

        if not isinstance(target, str):
            return None

        # A directory, a missing file or anything oversized is simply
        # not fingerprinted. os.stat is the cheap check; the read below
        # is the authoritative one.
        if not os.path.isfile(target):
            return None

        if os.path.getsize(target) > MAX_FINGERPRINT_BYTES:
            return None

        digest = hashlib.sha256()

        with open(target, "rb") as handle:

            while True:

                chunk = handle.read(
                    _FINGERPRINT_CHUNK_BYTES
                )

                if not chunk:
                    break

                digest.update(chunk)

        return digest.hexdigest()

    except Exception:
        # Unreadable, locked, a symlink to nowhere, a race with a
        # writer. The digest is optional metadata: null, not an error.
        return None


# ------------------------------------------------------------
# B2) SHAPES FOR LABELS THAT ARRIVE FROM OUTSIDE
# ------------------------------------------------------------
# A report is generated later, in a different request, from values a
# client sends back. Those values have the same legal shapes as the
# ones this module produces, so they are put through the same rules
# rather than trusted: a digest has exactly one shape, and a label is
# a bounded single line. This keeps a client from writing arbitrary
# text into an exported document by calling a field "source_sha256".

_DIGEST_CHARS = set("0123456789abcdef")


def safe_label(value: Any, limit: int = MAX_NAME_CHARS) -> Optional[str]:
    """A short, single-line, printable label, or null.

    Only a string is accepted: an arbitrary object must not be
    stringified into something that reads like a database name or a
    model name. safe_field strips control characters and collapses the
    rest, and "-" for a value with nothing usable in it is reported as
    absent rather than passed on as if it were a name.
    """

    try:

        if not isinstance(value, str):
            return None

        cleaned = safe_field(value, limit)

        if cleaned in ("", "-"):
            return None

        return cleaned

    except Exception:
        return None


def hex_digest(value: Any) -> Optional[str]:
    """A SHA-256 digest in its only legal form, or null.

    Exactly 64 lowercase hexadecimal characters. Anything else is
    null: a digest is compared and displayed, and a value that merely
    resembles one - uppercase, truncated, prefixed with a path, a
    model name, a sentence - is not a fingerprint of any file and is
    not presented as one.
    """

    try:

        if not isinstance(value, str):
            return None

        if len(value) != 64:
            return None

        if not set(value) <= _DIGEST_CHARS:
            return None

        return value

    except Exception:
        return None


# ------------------------------------------------------------
# C) EXECUTION TIMESTAMP
# ------------------------------------------------------------

def utc_timestamp(moment: Optional[datetime] = None) -> str:
    """A timezone-aware UTC ISO-8601 timestamp.

    Aware by construction: the value is converted to UTC and tagged, so
    it cannot be read as a naive local time. The single value reported
    as executed_at is the moment execution STARTED, taken immediately
    before the statement was handed to SQLite; elapsed_ms then measures
    the whole execution, so the pair brackets the real work.
    """

    try:

        if moment is None:
            moment = datetime.now(timezone.utc)

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        return (
            moment.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    except Exception:
        return ""


def execution_clock() -> float:
    """A monotonic reading for measuring elapsed_ms.

    time.perf_counter is monotonic and unaffected by a clock change, so
    a duration can never come out negative because NTP stepped back.
    """

    return time.perf_counter()


def elapsed_ms(started_at: Any) -> Optional[float]:
    """Milliseconds since a monotonic reading, never negative.

    Rounded to milliseconds: provenance is descriptive, and fifteen
    decimal places of float noise is not information.
    """

    try:

        value = (execution_clock() - float(started_at)) * 1000.0

        if value < 0:
            return 0.0

        return round(min(value, MAX_ELAPSED_MS), 3)

    except Exception:
        return None


# ------------------------------------------------------------
# D) RESULT METADATA
# ------------------------------------------------------------

def result_metadata(
    rows_returned: Any,
    truncated: Any,
    total_matched: Any = ...,
) -> dict:
    """rows_returned, total_matched and truncated, as three distinct facts.

    rows_returned
        How many rows the client actually received, after the
        application's own limit was applied.

    truncated
        True only when the application cut the result short AND at
        least one further row existed. A result set that happened to be
        exactly the limit size is not truncated.

    total_matched
        How many rows the executed statement produced. The caller
        passes it only when it knows that, which this application does
        in exactly one case: the fetch loop ran to exhaustion, so the
        cursor had no more rows to give. Anything else is null.

    Passing total_matched=... keeps the caller's intent explicit: if it
    is not supplied, the value is derived from truncated alone, which
    can only ever yield the row count or null. It never runs a query.
    """

    count = _safe_count(rows_returned)
    was_truncated = bool(truncated)

    if total_matched is ...:
        matched = count if not was_truncated else None
    else:
        matched = _safe_count(total_matched)

    return {
        "rows_returned": count,
        "total_matched": matched,
        "truncated": was_truncated,
    }


def _safe_count(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None

    try:

        count = int(value)

    except (TypeError, ValueError):
        return None

    return count if count >= 0 else None


# ------------------------------------------------------------
# E + F + G) THE CLIENT-FACING ENVELOPE
# ------------------------------------------------------------

def build_provenance(
    *,
    database_path: Any,
    request_id: Any,
    provider: Any,
    model: Any,
    executed_at: Any,
    elapsed: Any,
    rows_returned: Any,
    truncated: Any,
    total_matched: Any = ...,
    generated_by_ai: bool = True,
) -> dict:
    """The provenance object attached to a successful /api/ask response.

Every field is optional and every failure degrades to null, so this
    function cannot fail a query that already returned rows.

    Each optional field is resolved through _attempt rather than called
    directly. The helpers guard their own bodies, but the guarantee
    required here is stronger and structural: whatever goes wrong while
    a field is being established, including a failure inside the
    filesystem or a caller passing something impossible, must leave that
    one field null and nothing else touched.

    The request_id is passed in by the caller from
    app.observability.request_id_of(request): this module never
    generates an identifier, so there is exactly one id per request and
    it is the same one the X-Request-ID header carries.
    """

    results = _attempt(
        result_metadata, None, rows_returned, truncated, total_matched
    ) or {}

    return {
        "request_id": _attempt(safe_request_id, "unknown", request_id),
        "source_database": _attempt(
            source_database, None, database_path
        ),
        "source_sha256": _attempt(
            source_sha256, None, database_path
        ),
        "provider": _attempt(_safe_name, None, provider, 32),
        "model": _attempt(_safe_name, None, model),
        "executed_at": _attempt(_safe_timestamp, None, executed_at),
        "elapsed_ms": _attempt(_safe_duration, None, elapsed),
        "rows_returned": results.get("rows_returned"),
        "total_matched": results.get("total_matched"),
        "truncated": bool(results.get("truncated")),
        "generated_by_ai": bool(generated_by_ai),
    }


def _attempt(function, default, *arguments):
    """Call an optional-metadata helper, absorbing any failure.

    The fallback is not None by default: a request id has to stay a
    string, while everything else degrades to null.
    """

    try:
        return function(*arguments)

    except Exception:
        return default


def _safe_name(value: Any, limit: int = 64) -> Optional[str]:
    """Provider and model: bounded, sanitised, or null.

    These come from the LLM layer, which knows the provider and model
    that actually answered. No API key, endpoint, credential or other
    provider internal is ever passed in here, and safe_label reduces
    one to harmless characters regardless.
    """

    return safe_label(value, limit)


def _safe_duration(value: Any) -> Optional[float]:
    """A duration in milliseconds, or null.

    A float is kept as a float: a query that took 0.4 ms is a real
    measurement, and rounding it to zero would be a different claim.
    """

    try:

        if value is None or isinstance(value, bool):
            return None

        duration = float(value)

        if duration != duration:
            return None

        if duration in (float("inf"), float("-inf")):
            return None

        if duration < 0:
            return None

        return round(min(duration, MAX_ELAPSED_MS), 3)

    except Exception:
        return None


def _safe_timestamp(value: Any) -> Optional[str]:
    """A timestamp string that is short and looks like one."""

    try:

        if not isinstance(value, str):
            return None

        cleaned = value.strip()

        if not cleaned or len(cleaned) > 40:
            return None

        if not cleaned.endswith("Z"):
            return None

        return cleaned

    except Exception:
        return None