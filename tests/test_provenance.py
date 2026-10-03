# ============================================================
# RESULT PROVENANCE
# ============================================================
# Provenance answers "where did this result come from and how was it
# produced". These tests hold two lines at once: it must tell a client
# enough to reason about a result, and it must never leak a path, a
# query, a row, a credential, or imply that the result is right.
#
# Every test below names which of the two it protects.
# ============================================================

import ast
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile

from datetime import datetime, timezone

import pytest

from app import config, db, llm, main, observability, provenance

from tests.test_llm import _FakeClient
from tests.test_observability import access_log

# Markers that exist only so a test can look for them where they must
# never appear.
PROMPT_MARKER = "PROMPT-MARKER-must-not-escape"
SQL_MARKER = "SELECT 1 AS sql_marker_must_not_escape"
PATH_MARKER = "/var/task/deployment/store.db"
SECRET_MARKER = "nvapi-marker-must-not-escape"

# The exact provenance contract. A new field has to be a deliberate
# decision, so the set is pinned rather than merely checked for
# presence.
REQUIRED_KEYS = {
    "request_id",
    "source_database",
    "source_sha256",
    "provider",
    "model",
    "executed_at",
    "elapsed_ms",
    "rows_returned",
    "total_matched",
    "truncated",
    "generated_by_ai",
}

# Words that would turn a description into a guarantee. Provenance
# cannot know any of them, so it must never use them.
CORRECTNESS_WORDS = (
    "verified",
    "verified?",
    "correct",
    "accurate",
    "trusted",
    "guaranteed",
    "proof",
)

# SQL that is valid against the real store.db, which is what the route
# reads. The stub provider returns this, so provenance is exercised
# through the genuine path instead of being injected.
STORE_SQL = "SELECT id, name FROM products"


# ============================================================
# FIXTURES
# ============================================================

def build_database(path, rows=3, label="row", user_version=0):
    """A small database at path, with a marker in one column."""

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE items (id INTEGER, label TEXT)")
    connection.executemany(
        "INSERT INTO items VALUES (?, ?)",
        [(index, f"{label}-{index}") for index in range(1, rows + 1)],
    )
    # user_version lives in the file header. Two databases can hold the
    # same schema and the same rows and still differ byte for byte, and
    # distinguishing those two files is the whole job of a fingerprint.
    connection.execute(f"PRAGMA user_version = {int(user_version)}")
    connection.commit()
    connection.close()

    return path


@pytest.fixture
def source_db(tmp_path):
    return build_database(str(tmp_path / "store.db"))


@pytest.fixture(scope="module")
def counting_db():
    """A database plus a helper that records every statement sent to it."""

    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    build_database(path, rows=4, label="sample")

    return path


@pytest.fixture
def traced_execution(monkeypatch):
    """Record every statement SQLite receives during one execution."""

    statements = []
    original = db.get_connection

    def watched_connection(path=None, read_only=False):
        connection = original(path, read_only=read_only)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(db, "get_connection", watched_connection)

    def statements_excluding_pragmas():
        return [
            " ".join(statement.split())
            for statement in statements
            if not statement.strip().upper().startswith("PRAGMA")
        ]

    return statements_excluding_pragmas


def stub_ask(sql=STORE_SQL, provider="nvidia", model="stub-model-1"):
    """A provider stand-in that fills the GenerationRef like a real one."""

    def fake_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
    ):
        if generation is not None:
            generation.record(provider, model)

        return sql, "reads the products table"

    return fake_ask


@pytest.fixture
def provider_stub(monkeypatch):
    fake_ask = stub_ask()
    monkeypatch.setattr(main, "ask_active", fake_ask)
    return fake_ask


@pytest.fixture
def asked(authed_client, provider_stub):
    """A successful /api/ask against the real store.db."""

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200, response.text

    return response


# ============================================================
# 1. SOURCE DATABASE NAME
# ============================================================

def test_source_database_is_the_basename(source_db):
    assert provenance.source_database(source_db) == "store.db"


def test_source_database_is_store_db_for_the_configured_database():
    assert (
        provenance.source_database(config.DATABASE_PATH) == "store.db"
    )


def test_source_database_hides_directories_and_drive_letters(tmp_path):
    nested = str(tmp_path / "deep" / "deeper" / "sales.sqlite3")
    name = provenance.source_database(nested)

    assert name == "sales.sqlite3"
    assert os.sep not in name
    assert "/" not in name
    assert "\\" not in name
    assert ":" not in name
    assert str(tmp_path) not in name


def test_source_database_is_never_the_configured_path():
    name = provenance.source_database(config.DATABASE_PATH)

    assert name != config.DATABASE_PATH
    assert os.path.dirname(config.DATABASE_PATH) not in name
    assert config.BASE_DIR not in name


def test_source_database_of_nothing_is_null():
    assert provenance.source_database(None) is None
    assert provenance.source_database("") is None


def test_source_database_of_a_directory_is_null(tmp_path):
    assert provenance.source_database(str(tmp_path)) is None


def test_source_database_of_an_unusable_name_is_null():
    """A name with nothing safe in it reports none, not a placeholder."""

    assert provenance.source_database("???") is None
    assert provenance.source_database("..") is None


# ============================================================
# 2. SOURCE FINGERPRINT
# ============================================================

def test_source_sha256_is_64_lowercase_hex_characters(source_db):
    digest = provenance.source_sha256(source_db)

    assert digest is not None
    assert re.fullmatch(r"[0-9a-f]{64}", digest), digest


def test_source_sha256_matches_a_hand_computed_hash(source_db):
    with open(source_db, "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()

    assert provenance.source_sha256(source_db) == expected


def test_source_sha256_matches_the_real_configured_database():
    with open(config.DATABASE_PATH, "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()

    assert provenance.source_sha256(config.DATABASE_PATH) == expected


def test_source_sha256_differs_for_two_databases_that_differ(tmp_path):
    """Same schema, same rows, different bytes: different digests.

    If these matched, the fingerprint would identify content rather
    than a file, and two different databases would be
    indistinguishable.
    """

    first = build_database(str(tmp_path / "a.db"), user_version=0)
    second = build_database(str(tmp_path / "b.db"), user_version=7)

    assert provenance.source_sha256(first) != (
        provenance.source_sha256(second)
    )


def test_source_sha256_of_a_missing_file_is_null(tmp_path):
    assert provenance.source_sha256(str(tmp_path / "absent.db")) is None


def test_source_sha256_of_an_unreadable_file_is_null(
    source_db, monkeypatch
):
    import builtins

    def refuse(*args, **kwargs):
        raise PermissionError("access is denied")

    monkeypatch.setattr(builtins, "open", refuse)

    assert provenance.source_sha256(source_db) is None


def test_source_sha256_of_an_oversized_file_is_null(
    source_db, monkeypatch
):
    """A huge file is reported unavailable rather than stalling a request."""

    monkeypatch.setattr(
        provenance, "MAX_FINGERPRINT_BYTES", 8
    )

    assert provenance.source_sha256(source_db) is None


def test_source_sha256_never_contains_the_path(source_db):
    digest = provenance.source_sha256(source_db)

    assert digest is not None
    assert source_db not in digest
    assert os.sep not in digest
    assert config.DATABASE_PATH not in digest


# ============================================================
# 3. TIMESTAMP AND DURATION
# ============================================================

def test_executed_at_is_timezone_aware_utc(asked):
    stamp = asked.json()["provenance"]["executed_at"]

    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert stamp.endswith("Z")


def test_executed_at_is_a_real_moment(asked):
    stamp = asked.json()["provenance"]["executed_at"]
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    assert parsed > datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_utc_timestamp_is_always_aware_even_for_a_naive_value():
    naive = datetime(2024, 5, 1, 12, 0, 0)

    stamp = provenance.utc_timestamp(naive)

    assert stamp.endswith("Z")
    assert (
        datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo
        is not None
    )


def test_elapsed_ms_is_a_non_negative_number(asked):
    elapsed = asked.json()["provenance"]["elapsed_ms"]

    assert isinstance(elapsed, (int, float))
    assert not isinstance(elapsed, bool)
    assert elapsed >= 0


def test_elapsed_ms_never_goes_negative():
    started = provenance.execution_clock()

    assert provenance.elapsed_ms(started) >= 0

    # A monotonic clock cannot travel backwards, so the value is
    # forced: the point is that a negative duration can never reach a
    # client even if the clock misbehaves.
    assert provenance.elapsed_ms(started + 5000) == 0.0


def test_elapsed_ms_excludes_llm_generation_time(
    authed_client, monkeypatch
):
    """A slow model must not inflate the database duration."""

    import time

    def slow_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
    ):
        time.sleep(0.25)
        generation.record("nvidia", "slow-model")
        return STORE_SQL, "ok"

    monkeypatch.setattr(main, "ask_active", slow_ask)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200
    assert response.json()["provenance"]["elapsed_ms"] < 200


# ============================================================
# 4. THE ENVELOPE SHAPE
# ============================================================

def test_provenance_contains_exactly_the_required_fields(asked):
    assert set(asked.json()["provenance"]) == REQUIRED_KEYS


def test_every_provenance_field_is_json_serialisable(asked):
    block = asked.json()["provenance"]

    assert json.loads(json.dumps(block)) == block

    for key, value in block.items():
        assert value is None or isinstance(
            value, (str, int, float, bool)
        ), (key, type(value))


def test_successful_ask_still_returns_every_existing_field(asked):
    """The envelope is additive: nothing existing was renamed or removed."""

    body = asked.json()

    for field in (
        "success",
        "prompt",
        "sql",
        "explanation",
        "data",
        "count",
        "truncated",
    ):
        assert field in body, field

    assert body["success"] is True
    assert body["count"] == len(body["data"])


# ============================================================
# 5. ROW COUNTING SEMANTICS
# ============================================================

def test_rows_returned_counts_the_rows_the_client_received(asked):
    block = asked.json()["provenance"]

    assert block["rows_returned"] == asked.json()["count"]
    assert block["rows_returned"] == len(asked.json()["data"])


def test_untruncated_total_matched_is_the_row_count(counting_db):
    rows, execution = db.execute_sql_with_metadata(
        "SELECT * FROM items", path=counting_db
    )

    assert execution.truncated is False
    assert execution.rows_returned == len(rows)
    assert execution.total_matched == len(rows)


def test_exact_row_cap_is_not_truncated(tmp_path):
    """MAX_ROWS rows exactly is not a truncated result.

    Without a look-ahead this reported truncated=True although not one
    row had been lost, leaving a client unable to tell "cut short" from
    "exactly this many".
    """

    target = build_database(
        str(tmp_path / "exact.db"), rows=config.MAX_ROWS
    )

    rows, execution = db.execute_sql_with_metadata(
        "SELECT * FROM items", path=target
    )

    assert len(rows) == config.MAX_ROWS
    assert execution.truncated is False
    assert execution.total_matched == config.MAX_ROWS


def test_past_the_cap_truncates_and_omits_the_total(tmp_path):
    target = build_database(
        str(tmp_path / "big.db"), rows=config.MAX_ROWS + 25
    )

    rows, execution = db.execute_sql_with_metadata(
        "SELECT * FROM items", path=target
    )

    assert len(rows) == config.MAX_ROWS
    assert execution.truncated is True

    # Unknowable from what was read: absent, rather than equal to the
    # cap and rather than the true count.
    assert execution.total_matched is None
    assert execution.rows_returned == config.MAX_ROWS


def test_rows_returned_is_distinct_from_total_matched(tmp_path):
    """The two fields are not the same fact.

    One is what came back; the other is what the statement produced.
    When the result set was cut short there is no honest total, so the
    field is null instead of echoing the cap.
    """

    target = build_database(
        str(tmp_path / "big.db"), rows=config.MAX_ROWS + 25
    )

    _, execution = db.execute_sql_with_metadata(
        "SELECT * FROM items", path=target
    )

    assert execution.rows_returned == config.MAX_ROWS
    assert execution.total_matched is None
    assert execution.total_matched != execution.rows_returned


def test_result_metadata_never_raises_on_garbage():
    block = provenance.result_metadata("x", "maybe", object())

    assert block["rows_returned"] is None
    assert block["total_matched"] is None
    assert isinstance(block["truncated"], bool)


def test_execute_sql_keeps_its_original_two_value_return(counting_db):
    """The legacy helper still returns (rows, truncated)."""

    rows, truncated = db.execute_sql(
        "SELECT * FROM items", path=counting_db
    )

    assert isinstance(rows, list)
    assert truncated is False


# Wrapping a statement in COUNT(*) changes what LIMIT, DISTINCT,
# GROUP BY, CTEs, window functions and UNION mean, and running the
# statement twice doubles the load. So the total is reported only when
# the rows that came back already proved it.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM items LIMIT 1",
        "SELECT COUNT(*) AS n FROM items",
        "SELECT DISTINCT label FROM items",
        "WITH recent AS (SELECT * FROM items) SELECT * FROM recent",
        "SELECT id FROM items UNION SELECT id FROM items",
        "SELECT label, COUNT(*) AS n FROM items GROUP BY label",
        "SELECT id, RANK() OVER (ORDER BY id) AS r FROM items",
        "SELECT * FROM items ORDER BY id DESC",
    ],
)
def test_no_count_wrapper_and_no_second_execution(
    sql, counting_db, traced_execution
):
    rows, execution = db.execute_sql_with_metadata(
        sql, path=counting_db
    )

    # Every statement that reached SQLite. The plan check may or may
    # not be reported by the trace hook depending on the interpreter
    # build, so it is filtered out rather than counted.
    executed = [
        statement
        for statement in traced_execution()
        if not statement.upper().startswith("EXPLAIN")
    ]

    # Exactly one execution, and it is the user's own statement: no
    # wrapper around it, no second run of it.
    assert executed == [sql]

    # No wrapper was slipped around it either.
    assert not any(
        "COUNT(*) FROM (" in statement.upper()
        for statement in executed
    )

    # And the total is only claimed when nothing was cut.
    if execution.truncated:
        assert execution.total_matched is None
    else:
        assert execution.total_matched == len(rows)


def test_the_route_executes_sql_exactly_once_per_request(
    authed_client, monkeypatch, provider_stub, traced_execution
):
    """A count for provenance must not cost a second query."""

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200

    executed = [
        statement
        for statement in traced_execution()
        if not statement.upper().startswith("EXPLAIN")
    ]

    assert executed == [STORE_SQL]


def test_a_query_with_limit_reports_the_limited_count(counting_db):
    """The executed statement includes its own LIMIT, so it is honoured.

    total_matched counts what the statement produced, which for a
    limited statement is the limited number of rows.
    """

    rows, execution = db.execute_sql_with_metadata(
        "SELECT * FROM items LIMIT 2", path=counting_db
    )

    assert len(rows) == 2
    assert execution.total_matched == 2
    assert execution.truncated is False


# ============================================================
# 6. PROVIDER AND MODEL
# ============================================================

def test_provider_and_model_come_from_the_answering_provider(asked):
    block = asked.json()["provenance"]

    assert block["provider"] == "nvidia"
    assert block["model"] == "stub-model-1"


def test_gemini_is_reported_when_gemini_answers(
    authed_client, monkeypatch
):
    monkeypatch.setattr(
        main,
        "ask_active",
        stub_ask(provider="gemini", model="gemini-2.5-flash"),
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    block = response.json()["provenance"]

    assert block["provider"] == "gemini"
    assert block["model"] == "gemini-2.5-flash"


def test_provider_is_not_inferred_when_the_layer_is_silent(
    authed_client, monkeypatch
):
    """No answer from a provider means no claim about one.

    Reading LLM_PROVIDER here would report "nvidia" for an answer that
    never came from NVIDIA, which is exactly the guess provenance is
    meant to remove.
    """

    def silent_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
    ):
        return STORE_SQL, "ok"

    monkeypatch.setattr(main, "ask_active", silent_ask)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    block = response.json()["provenance"]

    assert block["provider"] is None
    assert block["model"] is None

    # The result itself is unaffected.
    assert response.status_code == 200
    assert block["generated_by_ai"] is True


def test_a_real_provider_fills_the_generation_ref(monkeypatch):
    """The ref is written by the provider instance, not by the route."""

    provider = llm.NVIDIAProvider(model="real-model-x")

    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only-key")
    monkeypatch.setattr(
        provider,
        "_get_client",
        lambda: _FakeClient(
            '{"sql": "SELECT * FROM products", '
            '"explanation": "All products."}'
        ),
    )
    monkeypatch.setattr(llm, "inspect_database", lambda: {})
    monkeypatch.setattr(
        llm, "build_system_instruction", lambda schema: "be brief"
    )
    monkeypatch.setattr(
        llm, "build_user_message", lambda prompt, history: prompt
    )

    ref = llm.GenerationRef()
    provider.generate("Show me products", [], generation=ref)

    assert ref.provider == "nvidia"
    assert ref.model == "real-model-x"


def test_a_failed_generation_leaves_the_ref_empty(monkeypatch):
    provider = llm.NVIDIAProvider(model="real-model-y")
    ref = llm.GenerationRef()

    def refuse(*args, **kwargs):
        raise RuntimeError("NVIDIA returned an empty response.")

    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only-key")
    monkeypatch.setattr(provider, "_call_model", refuse)
    monkeypatch.setattr(llm, "inspect_database", lambda: "")

    with pytest.raises(Exception):
        provider.generate("Show me products", [], generation=ref)

    assert ref.provider is None
    assert ref.model is None


def test_a_gemini_provider_fills_the_generation_ref(monkeypatch):
    class Response:
        text = (
            '{"sql": "SELECT * FROM products", '
            '"explanation": "All products."}'
        )

    provider = llm.GeminiProvider(model="gemini-real")

    monkeypatch.setattr(
        provider, "_call_model", lambda *args: Response()
    )
    monkeypatch.setattr(llm, "inspect_database", lambda: {})
    monkeypatch.setattr(
        llm, "build_system_instruction", lambda schema: "be brief"
    )
    monkeypatch.setattr(
        llm, "build_user_message", lambda prompt, history: prompt
    )

    ref = llm.GenerationRef()
    provider.generate("Show me products", [], generation=ref)

    assert ref.provider == "gemini"
    assert ref.model == "gemini-real"


def test_provider_and_model_cannot_forge_response_structure():
    """A hostile value stays bounded, single-line and free of separators.

    The guarantee is structural, not editorial: whatever arrives, it
    cannot add a field, end a line or flood the body. Content-level
    redaction is deliberately not attempted, because provider and model
    are operator configuration rather than request data.
    """

    class Hostile:
        name = "x" * 5000 + "\ninjected: true"
        model = "model=forged other=value"

    ref = llm.GenerationRef()
    llm._record_generation(ref, Hostile())

    block = provenance.build_provenance(
        database_path=None,
        request_id=None,
        provider=ref.provider,
        model=ref.model,
        executed_at=None,
        elapsed=None,
        rows_returned=0,
        truncated=False,
    )

    # Bounded.
    assert len(block["provider"]) <= 64

    # Single line: no newline can start a new record anywhere.
    assert "\n" not in block["provider"]
    assert "\r" not in block["provider"]

    # Single token: no space and no equals sign can add a field.
    assert " " not in block["provider"]
    assert "=" not in block["model"]
    assert block["model"] == "model forged other value"


# ============================================================
# 7. REQUEST ID
# ============================================================

def test_provenance_request_id_equals_the_response_header(asked):
    block = asked.json()["provenance"]

    assert block["request_id"] == asked.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", block["request_id"])


def test_one_request_carries_exactly_one_id(asked):
    """The header and the body agree, so there is nothing to correlate."""

    body = asked.json()

    assert body["provenance"]["request_id"] == (
        asked.headers["X-Request-ID"]
    )

    # Not also duplicated at the top level.
    assert "request_id" not in {
        field for field in body if field != "provenance"
    }


def test_two_requests_get_two_different_ids(
    authed_client, provider_stub
):
    first = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )
    second = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert (
        first.json()["provenance"]["request_id"]
        != second.json()["provenance"]["request_id"]
    )


def test_a_missing_request_id_becomes_unknown():
    block = provenance.build_provenance(
        database_path=None,
        request_id=None,
        provider="nvidia",
        model="m",
        executed_at=None,
        elapsed=None,
        rows_returned=0,
        truncated=False,
    )

    assert block["request_id"] == "unknown"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-request-id",
        SECRET_MARKER,
        "../../etc/passwd",
        "X-API-KEY-SECRET",
        "z" * 32,
        "A" * 32,
        {"a": 1},
        ["abc"],
        12345,
    ],
)
def test_an_invalid_request_id_is_never_echoed(value):
    """A credential or a path cannot become a request id by accident."""

    block = provenance.build_provenance(
        database_path=None,
        request_id=value,
        provider="nvidia",
        model="m",
        executed_at=None,
        elapsed=None,
        rows_returned=0,
        truncated=False,
    )

    assert block["request_id"] == "unknown"


def test_the_module_never_invents_a_request_id():
    """One id per request means provenance never generates one."""

    source = _source(provenance)

    for forbidden in ("uuid", "random", "token_urlsafe", "secrets"):
        assert forbidden not in source, forbidden


# ============================================================
# 8. GENERATED BY AI
# ============================================================

def test_generated_by_ai_is_true_after_a_successful_ask(asked):
    assert asked.json()["provenance"]["generated_by_ai"] is True


def test_generated_by_ai_is_a_boolean_not_a_string(asked):
    value = asked.json()["provenance"]["generated_by_ai"]

    assert isinstance(value, bool)
    assert not isinstance(value, str)


def test_no_provenance_when_generation_fails(authed_client, monkeypatch):
    def fail(prompt, turns, request_id=None, generation=None):
        raise RuntimeError("NVIDIA request failed")

    monkeypatch.setattr(main, "ask_active", fail)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 500
    assert "provenance" not in response.json()


def test_no_provenance_when_sql_execution_fails(
    authed_client, monkeypatch, provider_stub
):
    def explode(sql, path=None):
        raise sqlite3.OperationalError("no such table: absent")

    monkeypatch.setattr(main, "execute_sql_with_metadata", explode)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code >= 400
    assert "provenance" not in response.json()

    # The existing generic body is unchanged by provenance.
    assert "no such table" not in response.text


# ============================================================
# 9. OPTIONAL METADATA NEVER BREAKS A RESULT
# ============================================================

def test_a_missing_digest_still_returns_rows(
    authed_client, monkeypatch, provider_stub
):
    """An unreadable file must not turn a good answer into a failure."""

    monkeypatch.setattr(provenance, "source_sha256", lambda path: None)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200
    assert response.json()["data"]
    assert response.json()["provenance"]["source_sha256"] is None


def test_a_failing_fingerprint_helper_is_contained(
    authed_client, monkeypatch, provider_stub
):
    def explode(path):
        raise OSError("the disk is on fire")

    monkeypatch.setattr(provenance, "source_sha256", explode)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200
    assert response.json()["data"]


def test_build_provenance_never_raises():
    """Garbage in, nulls out. The query has already succeeded."""

    class Hostile:
        def __fspath__(self):
            raise RuntimeError("no")

        def __str__(self):
            raise RuntimeError("no")

    for value in (None, "", Hostile(), object(), True, [1, 2]):
        block = provenance.build_provenance(
            database_path=value,
            request_id=value,
            provider=value,
            model=value,
            executed_at=value,
            elapsed=value,
            rows_returned=value,
            truncated=value,
            total_matched=value,
        )

        assert set(block) == REQUIRED_KEYS

        # Descriptive fields carry nothing that was not understood.
        assert block["request_id"] == "unknown"
        assert block["source_database"] is None
        assert block["source_sha256"] is None
        assert block["provider"] is None
        assert block["model"] is None
        assert block["executed_at"] is None
        assert block["elapsed_ms"] is None
        assert block["rows_returned"] is None
        assert block["total_matched"] is None

        # A boolean is the only thing a client ever sees here, whatever
        # went in.
        assert isinstance(block["truncated"], bool)


def test_numbers_are_accepted_where_numbers_are_meaningful():
    """A duration of 7 ms is a real measurement, not garbage."""

    block = provenance.build_provenance(
        database_path=None,
        request_id=None,
        provider="nvidia",
        model="m",
        executed_at=provenance.utc_timestamp(),
        elapsed=7,
        rows_returned=12,
        truncated=False,
        total_matched=12,
    )

    assert block["elapsed_ms"] == 7.0
    assert block["rows_returned"] == 12
    assert block["total_matched"] == 12


def test_a_negative_or_impossible_duration_is_rejected():
    for value in (-1, float("nan"), float("inf"), "fast"):
        block = provenance.build_provenance(
            database_path=None,
            request_id=None,
            provider="nvidia",
            model="m",
            executed_at=provenance.utc_timestamp(),
            elapsed=value,
            rows_returned=1,
            truncated=False,
        )

        assert block["elapsed_ms"] is None, value


def test_a_bytes_path_is_still_a_path(source_db):
    """os.PathLike and bytes are legitimate paths, so they work."""

    with open(source_db, "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()

    assert provenance.source_database(
        os.fsencode(source_db)
    ) == "store.db"
    assert provenance.source_sha256(os.fsencode(source_db)) == expected


def test_a_malformed_timestamp_is_reported_as_null():
    block = provenance.build_provenance(
        database_path=None,
        request_id=None,
        provider="nvidia",
        model="m",
        executed_at="yesterday",
        elapsed=1,
        rows_returned=1,
        truncated=False,
    )

    assert block["executed_at"] is None
    assert block["elapsed_ms"] == 1


def test_provenance_module_has_only_standard_library_imports():
    """Verified from the import statements themselves."""

    allowed = set(sys.stdlib_module_names) | {"app"}

    for node in ast.walk(ast.parse(_source(provenance))):
        names = []

        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module.split(".")[0]]

        for name in names:
            assert name in allowed, name


def test_provenance_module_cannot_reach_the_database():
    """It has no connection, so it cannot run a query of its own."""

    source = _source(provenance)

    assert "sqlite3" not in source
    assert "execute_sql" not in source
    assert "from app.db" not in source
    assert "connect" not in source


# ============================================================
# 10. LEAKAGE
# ============================================================

def test_no_filesystem_path_in_the_provenance_block(asked):
    block = str(asked.json()["provenance"])

    assert config.DATABASE_PATH not in block
    assert config.BASE_DIR not in block
    assert os.sep not in block
    assert not re.search(r"[A-Za-z]:\\", block)


def test_no_absolute_path_in_the_whole_response(asked):
    body = asked.text

    assert config.DATABASE_PATH not in body
    assert config.BASE_DIR not in body
    assert not re.search(r"[A-Za-z]:[\\/]", body)
    assert not re.search(r"/(home|var|Users|tmp)/", body)


def test_no_sql_in_the_provenance_block(asked):
    block = str(asked.json()["provenance"]).upper()

    assert "SELECT" not in block
    assert "FROM" not in block
    assert "PRODUCTS" not in block


def test_no_prompt_text_in_the_provenance_block(
    authed_client, monkeypatch
):
    monkeypatch.setattr(main, "ask_active", stub_ask())

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 200
    assert PROMPT_MARKER in response.text

    block = str(response.json()["provenance"])

    assert PROMPT_MARKER not in block
    assert response.json()["prompt"] == PROMPT_MARKER


def test_no_row_values_in_the_provenance_block(asked):
    """A text value from the result set is the strongest canary.

    Numbers are skipped on purpose: a count of 1 will appear in
    rows_returned, and pretending otherwise would make this test
    meaningless.
    """

    body = asked.json()
    block = str(body["provenance"])

    assert body["data"], "the fixture must return at least one row"

    text_values = [
        str(value)
        for value in body["data"][0].values()
        if isinstance(value, str)
    ]

    assert text_values

    for value in text_values:
        assert value not in block


def test_no_schema_contents_in_the_provenance_block(asked):
    block = str(asked.json()["provenance"]).lower()

    for table in ("products", "categories", "brands", "orders"):
        assert table not in block


def test_no_credential_vocabulary_in_the_provenance_block(asked):
    block = str(asked.json()["provenance"]).lower()

    for word in ("token", "api_key", "apikey", "secret", "cookie"):
        assert word not in block


def test_a_request_id_cannot_carry_a_credential(asked):
    """The one field that copies a caller-influenced value."""

    block = provenance.build_provenance(
        database_path=None,
        request_id=SECRET_MARKER,
        provider="nvidia",
        model="m",
        executed_at=provenance.utc_timestamp(),
        elapsed=1,
        rows_returned=1,
        truncated=False,
    )

    assert block["request_id"] == "unknown"
    assert SECRET_MARKER not in str(block)


def test_no_prompt_or_sql_reaches_any_log(
    authed_client, access_log, monkeypatch
):
    """Provenance logs nothing, and the existing lines stay clean.

    A third line per request would duplicate the access line and the
    usage line, so provenance does not log at all. What it returns must
    not have leaked into the lines that already exist either.
    """

    monkeypatch.setattr(
        main,
        "ask_active",
        stub_ask(sql=SQL_MARKER, model="stub-model-1"),
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 200

    logged = "\n".join(
        record.getMessage() for record in access_log.records
    )

    assert logged
    assert PROMPT_MARKER not in logged
    assert "sql_marker" not in logged
    assert "must_not_escape" not in logged
    assert config.DATABASE_PATH not in logged
    assert config.BASE_DIR not in logged
    assert SECRET_MARKER not in logged
    assert PATH_MARKER not in logged

    # No line claims to be a provenance line at all.
    assert "provenance" not in logged.lower()
    assert "source_sha256" not in logged


def test_the_digest_does_not_reach_the_log(authed_client, access_log):
    """The client gets the digest; the server does not log it."""

    authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    logged = "\n".join(
        record.getMessage() for record in access_log.records
    )

    assert provenance.source_sha256(config.DATABASE_PATH) not in logged


# ============================================================
# 11. WHAT PROVENANCE MUST NOT CLAIM
# ============================================================

def test_the_response_makes_no_correctness_claim(asked):
    block = str(asked.json()["provenance"]).lower()

    for word in CORRECTNESS_WORDS:
        assert word not in block, word


def test_no_field_name_promises_something_provenance_cannot_know(asked):
    for key in asked.json()["provenance"]:
        lowered = key.lower()

        for word in CORRECTNESS_WORDS:
            assert word not in lowered, (key, word)


def test_the_module_states_explicitly_what_it_does_not_know():
    """The disambiguation lives in the module, in plain words."""

    header = _header(provenance).lower()

    assert "correctness" in header
    assert "no correctness signal" in header or (
        "not a correctness signal" in header
    )
    assert "never" in header


def test_the_fingerprint_identifies_a_file_not_an_answer(source_db):
    """A digest tracks bytes. It proves nothing about the rows."""

    before = provenance.source_sha256(source_db)

    connection = sqlite3.connect(source_db)
    connection.execute("INSERT INTO items VALUES (999, 'later-row')")
    connection.commit()
    connection.close()

    assert provenance.source_sha256(source_db) != before


def test_the_module_says_so_where_a_reader_will_see_it():
    """app/provenance.py, and the database module it reads from."""

    assert "provenance" in _header(db).lower()

    assert "not" in _header(db).lower()


# ============================================================
# 12. STABILITY ACROSS REQUESTS
# ============================================================

def test_two_identical_requests_describe_the_same_source(
    authed_client, provider_stub
):
    first = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()["provenance"]
    second = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()["provenance"]

    for field in (
        "source_sha256",
        "source_database",
        "provider",
        "model",
    ):
        assert first[field] == second[field], field


def test_each_request_gets_its_own_timestamp(
    authed_client, provider_stub
):
    import time

    first = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()["provenance"]

    time.sleep(0.005)

    second = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()["provenance"]

    assert first["executed_at"] != second["executed_at"]


def test_a_fingerprint_error_is_contained_per_field():
    """One unavailable field must not blank the whole block."""

    block = provenance.build_provenance(
        database_path="/definitely/not/here/store.db",
        request_id=None,
        provider="nvidia",
        model="m",
        executed_at=provenance.utc_timestamp(),
        elapsed=1.5,
        rows_returned=4,
        truncated=False,
    )

    # The name is still known even though the file cannot be read.
    assert block["source_database"] == "store.db"
    assert block["source_sha256"] is None

    # And every unrelated field survives.
    assert block["elapsed_ms"] == 1.5
    assert block["rows_returned"] == 4
    assert block["total_matched"] == 4
    assert block["provider"] == "nvidia"
    assert block["executed_at"]


# ============================================================
# HELPERS
# ============================================================

def _source(module):
    import inspect

    return inspect.getsource(module)


def _header(module):
    """The leading comment block of a module."""

    lines = []

    for line in _source(module).splitlines():

        if not line.startswith("#"):
            if lines:
                break
            continue

        lines.append(line)

    return "\n".join(lines)