"""Tests for Step 4B-3A: request ids, safe logging, error sanitisation.

Three properties are pinned here, and each one is a security property,
not a cosmetic one:

  1. every response carries a unique X-Request-ID, so a user can always
     quote the exact request that failed;
  2. the access log is built from five safe fields and never from a
     header, a cookie or a body;
  3. an unexpected exception never reaches the client - only a reference
     id does - while a hand-written validation failure keeps its own
     wording, because the application would be unusable without it.

The credential used here is the throwaway literal from conftest.py. No
real secret is read from the environment.
"""

import asyncio
import json
import logging
import logging.handlers
import re
import sqlite3

import pytest

from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.datastructures import MutableHeaders

import app.config as config
import app.main as main
from app import observability, ratelimit, uploads
from app.main import app
from app.validate import ValidationError

HEADER = observability.REQUEST_ID_HEADER

# Distinctive strings. If one of these ever shows up in a log line or in
# a response body, the corresponding leak test below fails.
SECRET_MARKER = "nvapi-marker-should-never-leak"
PATH_MARKER = "/var/task/deployment/store.db"
PROMPT_MARKER = "PROMPT-MARKER-should-never-be-logged"
SQL_MARKER = "SELECT 1 AS leaked_sql_marker"


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def access_log(caplog):
    """Capture the access lines (they are INFO, below the caplog default)."""

    caplog.set_level(
        logging.INFO, logger=observability._ACCESS_LOGGER_NAME
    )
    return caplog


def request_id(response):
    return response.headers.get(HEADER)


def assert_valid_id(value):
    assert value, "no request id on the response"
    assert re.fullmatch(r"[0-9a-f]{32}", value), value


# ============================================================
# A MINIMAL ASGI HARNESS
# ============================================================
# Used to prove the middleware adds the header regardless of WHO produced
# the response. Some of those statuses (403 in particular) are never
# produced by the current route table, and adding a route that produces
# them would change the protected surface the auth tests count.

def _scope(method="GET", path="/api/thing"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-api-key", SECRET_MARKER.encode())],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    }


async def _receive():
    return {
        "type": "http.request",
        "body": b"",
        "more_body": False,
    }


def drive(inner, method="GET", path="/api/thing"):
    """Run one request through the middleware and return (scope, messages)."""

    scope = _scope(method, path)
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(
        observability.RequestContextMiddleware(inner)(
            scope, _receive, send
        )
    )

    return scope, sent


def status_response(status_code):
    async def inner(scope, receive, send):
        await Response(status_code=status_code)(scope, receive, send)

    return inner


# ============================================================
# 1. EVERY RESPONSE CARRIES THE ID
# ============================================================

NORMAL_RESPONSES = [
    ("get", "/", None),
    ("get", "/health", None),
    ("get", "/api/version", None),
    ("get", "/api/schema", None),
    ("get", "/api/sessions", None),
    ("get", "/api/imports", None),
    ("post", "/api/sessions", {"title": "Observable"}),
    ("post", "/api/auth/login", {"password": "app_secret_placeholder"}),
]


@pytest.mark.parametrize(
    "method,path,payload",
    NORMAL_RESPONSES,
    ids=[f"{m} {p}" for m, p, _ in NORMAL_RESPONSES],
)
def test_every_normal_response_carries_the_request_id(
    authed_client, method, path, payload
):
    kwargs = {"json": payload} if payload is not None else {}

    if path == "/api/auth/login":
        # A rejected login is still a normal response to trace.
        kwargs = {"json": {"password": "wrong"}}

    response = getattr(authed_client, method)(path, **kwargs)

    assert response.status_code < 500, response.text
    assert_valid_id(request_id(response))


def test_redirect_response_carries_the_request_id(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login-form",
            data={"password": app_secret},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert_valid_id(request_id(response))


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/", {}),
        (
            "post",
            "/api/report",
            {
                "json": {
                    "title": "Streamed",
                    "sql": "SELECT 1",
                    "data": [{"a": 1}],
                    "count": 1,
                }
            },
        ),
    ],
    ids=["index-html", "report-pdf"],
)
def test_streamed_file_responses_carry_the_request_id(
    authed_client, method, path, kwargs
):
    """A FileResponse sends its headers before its body, so this is a
    different path from a buffered JSON answer."""

    response = getattr(authed_client, method)(path, **kwargs)

    assert response.status_code == 200
    assert_valid_id(request_id(response))


def test_401_response_carries_the_request_id(anon_client):
    response = anon_client.get("/api/schema")

    assert response.status_code == 401
    assert_valid_id(request_id(response))


def test_404_response_carries_the_request_id(anon_client):
    response = anon_client.get("/no-such-path")

    assert response.status_code == 404
    assert_valid_id(request_id(response))


def test_405_response_carries_the_request_id(authed_client):
    response = authed_client.post("/health")

    assert response.status_code == 405
    assert_valid_id(request_id(response))


def test_422_response_carries_the_request_id(authed_client):
    response = authed_client.post(
        "/api/ask", json={"prompt": ["not", "a", "string"]}
    )

    assert response.status_code == 422
    assert_valid_id(request_id(response))


def test_429_response_carries_the_request_id(
    meta_paths, app_secret
):
    with TestClient(app) as client:

        for _ in range(config.LOGIN_RATE_LIMIT_REQUESTS):
            client.post(
                "/api/auth/login", json={"password": "wrong"}
            )

        response = client.post(
            "/api/auth/login", json={"password": "wrong"}
        )

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert_valid_id(request_id(response))


def test_500_response_carries_the_request_id(
    authed_client, monkeypatch
):
    """An unhandled exception becomes a 500 that still carries the id."""

    def explode():
        raise sqlite3.OperationalError(
            f"unable to open database file {PATH_MARKER}"
        )

    # list_sessions has no handler-level try/except, so this reaches the
    # middleware's own 500 path rather than a sanitised HTTPException.
    monkeypatch.setattr(
        main.history, "list_sessions", explode
    )

    response = authed_client.get("/api/sessions")

    assert response.status_code == 500
    assert_valid_id(request_id(response))


@pytest.mark.parametrize(
    "status_code",
    [200, 201, 400, 401, 403, 404, 405, 422, 429, 500],
)
def test_the_header_is_added_whatever_produced_the_response(
    status_code,
):
    """The id is attached in one place, so no status can lose it."""

    scope, sent = drive(status_response(status_code))

    start = sent[0]

    assert start["type"] == "http.response.start"
    assert start["status"] == status_code
    assert_valid_id(MutableHeaders(scope=start)[HEADER])
    assert scope["state"]["request_id"] == (
        MutableHeaders(scope=start)[HEADER]
    )


def test_ids_are_unique_across_requests(authed_client):
    seen = set()

    for _ in range(25):
        seen.add(
            request_id(authed_client.get("/api/version"))
        )

    assert len(seen) == 25


def test_generated_ids_are_unique_and_hex():
    ids = {observability.new_request_id() for _ in range(2000)}

    assert len(ids) == 2000
    for value in ids:
        assert re.fullmatch(r"[0-9a-f]{32}", value)


def test_state_request_id_is_available_to_dependencies(
    meta_paths, app_secret, monkeypatch
):
    """request.state.request_id is readable inside the request, and it is
    the same value the client is given.

    _login_identity is the first thing the login limiter runs, and it
    receives the live Request, so it is a faithful probe of what a
    dependency sees.
    """

    seen = {}
    original = ratelimit._login_identity

    def spy(request):
        seen["request_id"] = getattr(
            request.state, "request_id", None
        )
        return original(request)

    monkeypatch.setattr(ratelimit, "_login_identity", spy)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"password": "wrong"}
        )

    assert response.status_code == 401
    assert_valid_id(seen["request_id"])
    assert seen["request_id"] == request_id(response)


def test_state_request_id_is_set_before_the_handler_runs(
    authed_client, monkeypatch
):
    """The id exists from the first line of the lifecycle, so even a
    handler that crashes can report it."""

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "get_schema", explode)

    response = authed_client.get("/api/schema")

    assert response.status_code == 500
    assert request_id(response) in response.text


def test_no_id_is_invented_when_the_middleware_did_not_run():
    class Fake:
        class state:
            pass

    value = observability.request_id_of(Fake())

    # A fixed placeholder, never a random value: an invented id would
    # point a support ticket at a request that does not exist.
    assert value == observability._UNKNOWN_REQUEST_ID


# ============================================================
# 2. THE ACCESS LOG CARRIES SAFE METADATA ONLY
# ============================================================

def test_access_line_reports_method_path_status_duration_and_id(
    authed_client, access_log
):
    response = authed_client.post(
        "/api/ask", json={"prompt": "   "}
    )

    assert response.status_code == 400

    lines = [
        record.getMessage()
        for record in access_log.records
        if record.name == observability._ACCESS_LOGGER_NAME
    ]

    assert lines, "no access line was written"

    line = lines[-1]

    assert line.startswith("POST /api/ask status=400 duration_ms=")
    assert line.endswith(f"request_id={request_id(response)}")
    assert re.search(r"duration_ms=\d+", line)


def test_access_line_is_written_for_every_status(
    meta_paths, app_secret, access_log
):
    with TestClient(app) as client:

        client.get("/health")
        client.get("/api/schema")                      # 401
        client.get("/no-such-path")                    # 404

    statuses = [
        record.getMessage().split(" status=")[1].split(" ")[0]
        for record in access_log.records
        if record.name == observability._ACCESS_LOGGER_NAME
    ]

    assert statuses == ["200", "401", "404"]


def test_credentials_cookies_and_headers_are_never_logged(
    meta_paths, app_secret, access_log
):
    """Every credential transport in one request, plus the cookie."""

    with TestClient(app) as client:

        # The cookie transport.
        client.post(
            "/api/auth/login", json={"password": app_secret}
        )
        session_token = client.cookies.get(config.AUTH_COOKIE_NAME)
        assert session_token

        response = client.get(
            "/api/ask-does-not-exist",
            headers={
                "Authorization": f"Bearer {SECRET_MARKER}",
                "X-API-Key": app_secret,
            },
        )

    logged = "\n".join(
        record.getMessage() for record in access_log.records
    )

    assert response.status_code == 404
    assert logged

    # Neither the secret, nor a fragment of it, nor the signed session
    # token, nor the header names themselves.
    assert app_secret not in logged
    assert app_secret[:8] not in logged
    assert session_token not in logged
    assert session_token[:16] not in logged
    assert SECRET_MARKER not in logged

    assert "authorization" not in logged.lower()
    assert "x-api-key" not in logged.lower()
    assert "cookie" not in logged.lower()
    assert "bearer" not in logged.lower()


def test_request_bodies_are_never_logged(
    authed_client, app_secret, access_log, monkeypatch
):
    """The prompt, the generated SQL and the result rows stay out of it."""

    monkeypatch.setattr(
        main,
        "ask_active",
        lambda prompt, turns, request_id=None, generation=None: (SQL_MARKER, "explains it"),
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 200
    # The response body legitimately contains them; the log must not.
    assert PROMPT_MARKER in response.text
    assert SQL_MARKER in response.text

    logged = "\n".join(
        record.getMessage() for record in access_log.records
    )

    assert logged
    assert PROMPT_MARKER not in logged
    assert SQL_MARKER not in logged
    assert "prompt" not in logged.lower()
    assert "explain" not in logged.lower()


def test_query_strings_are_never_logged(authed_client, access_log):
    """A query string can carry a token or a prompt; the path is enough."""

    response = authed_client.get(
        f"/api/approvals?status=pending&note={PROMPT_MARKER}"
    )

    assert response.status_code == 200

    logged = "\n".join(
        record.getMessage() for record in access_log.records
    )

    assert "/api/approvals" in logged
    assert "status=pending" not in logged
    assert PROMPT_MARKER not in logged


def test_path_control_characters_cannot_forge_a_log_line():
    """A crafted path must not be able to inject a second log record."""

    forged = "/api/thing\nPOST /api/fake status=200 request_id=forged"

    cleaned = observability._safe_path(forged)

    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert cleaned.count("status=200") == 1


def test_a_crafted_method_cannot_forge_a_log_line(access_log):
    async def inner(scope, receive, send):
        await Response(status_code=200)(scope, receive, send)

    forged = "GET\nGET /api/fake status=999 request_id=forged"

    scope, sent = drive(inner, method=forged)

    lines = [
        record.getMessage()
        for record in access_log.records
        if record.name == observability._ACCESS_LOGGER_NAME
    ]

    assert sent and sent[0]["status"] == 200
    assert lines, "no access line was written"
    assert "\n" not in lines[-1]
    assert lines[-1].count("status=") == 1
    assert lines[-1].endswith(
        f"request_id={scope['state']['request_id']}"
    )


def test_a_very_long_path_is_bounded():
    cleaned = observability._safe_path("/api/" + "a" * 5000)

    assert len(cleaned) <= observability._MAX_PATH_CHARS


def test_unexpected_exception_is_logged_with_its_traceback_and_id(
    authed_client, caplog
):
    caplog.set_level(
        logging.INFO, logger=observability._ERROR_LOGGER_NAME
    )

    def explode():
        raise RuntimeError(
            f"provider failed using {SECRET_MARKER} at {PATH_MARKER}"
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(main, "get_schema", explode)
        response = authed_client.get("/api/schema")

    errors = [
        record
        for record in caplog.records
        if record.name == observability._ERROR_LOGGER_NAME
    ]

    assert errors, "the failure was not logged"

    record = errors[-1]

    assert record.levelno == logging.ERROR
    assert f"request_id={request_id(response)}" in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError

    # The traceback is attached to the record, so the real cause is on
    # the server even though the client never sees it.
    rendered = caplog.text

    assert SECRET_MARKER in rendered
    assert PATH_MARKER in rendered


def test_logging_never_writes_a_file(access_log):
    """No persistent local log file: the filesystem is read-only on Vercel
    and an instance is disposable, so a file would be a lie."""

    handlers = logging.getLogger(
        observability._ROOT_LOGGER_NAME
    ).handlers

    assert handlers
    for handler in handlers:
        assert not isinstance(
            handler, (logging.FileHandler, logging.handlers.WatchedFileHandler)
        )


def test_configure_logging_is_idempotent():
    first = observability.configure_logging()
    count = len(first.handlers)

    for _ in range(3):
        observability.configure_logging()

    assert len(first.handlers) == count == 1


def test_error_records_go_to_a_stream_capable_of_platform_collection(
    authed_client, caplog, capsys
):
    """Nothing exotic: a handler on the application logger writing text."""

    handlers = logging.getLogger(
        observability._ROOT_LOGGER_NAME
    ).handlers

    assert len(handlers) == 1
    handler = handlers[0]

    assert isinstance(handler, logging.Handler)
    assert handler.formatter is not None

    def explode():
        raise RuntimeError("boom")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(main, "get_schema", explode)
        authed_client.get("/api/schema")

    captured = capsys.readouterr()

    # The failure went to stderr, the request line to stdout.
    assert "unexpected_error" in captured.err
    assert "Traceback" in captured.err
    assert "/api/schema status=500" in captured.out


# ============================================================
# 3. UNEXPECTED EXCEPTIONS NEVER REACH THE CLIENT
# ============================================================

def test_schema_failure_is_generic(authed_client, monkeypatch):
    def explode():
        raise sqlite3.OperationalError(
            f"unable to open database file {PATH_MARKER}"
        )

    monkeypatch.setattr(main, "get_schema", explode)

    response = authed_client.get("/api/schema")

    assert response.status_code == 500

    body = response.json()

    assert body == {
        "detail": (
            f"Request failed. Reference ID: {request_id(response)}"
        )
    }

    assert PATH_MARKER not in response.text
    assert "OperationalError" not in response.text
    assert "unable to open" not in response.text
    assert "Traceback" not in response.text


def test_report_failure_is_generic(authed_client, monkeypatch):
    def explode(**kwargs):
        raise OSError(
            f"cannot write /tmp/uploads/reports/x.pdf ({PATH_MARKER})"
        )

    monkeypatch.setattr(main, "build_report_pdf", explode)

    response = authed_client.post(
        "/api/report", json={"title": "T", "sql": "SELECT 1"}
    )

    assert response.status_code == 500

    body = response.json()

    assert body["detail"] == (
        f"Request failed. Reference ID: {request_id(response)}"
    )
    assert PATH_MARKER not in response.text
    assert "Report generation failed" not in response.text


def test_upload_internal_failure_is_generic(
    authed_client, monkeypatch
):
    def explode(filename, content):
        raise sqlite3.OperationalError(
            f"no such table: users in {PATH_MARKER}"
        )

    monkeypatch.setattr(uploads, "import_file", explode)

    response = authed_client.post(
        "/api/upload",
        files={
            "file": ("x.db", b"not-a-database", "application/octet-stream")
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        f"Request failed. Reference ID: {request_id(response)}"
    )
    assert PATH_MARKER not in response.text
    assert "no such table" not in response.text


def test_ask_provider_failure_is_generic(
    authed_client, monkeypatch
):
    """The /api/ask leak: a provider or SDK message used to be returned
    verbatim, carrying the key and the upstream body."""

    def explode(prompt, turns, request_id=None, generation=None):
        raise ValueError(
            "NVIDIA API request failed (HTTP 400). Upstream said: "
            f"rejected key {SECRET_MARKER} ({PATH_MARKER})"
        )

    monkeypatch.setattr(main, "ask_active", explode)

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        f"Request failed. Reference ID: {request_id(response)}"
    )

    assert SECRET_MARKER not in response.text
    assert PATH_MARKER not in response.text
    assert "NVIDIA" not in response.text
    assert "Upstream said" not in response.text


def test_ask_database_failure_is_generic(authed_client, monkeypatch):
    def explode(sql, path=None):
        raise sqlite3.DatabaseError(
            f"file is not a database: {PATH_MARKER}"
        )

    monkeypatch.setattr(
        main, "execute_sql_with_metadata", explode
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 500
    assert PATH_MARKER not in response.text


def test_unhandled_exception_produces_the_same_generic_body(
    access_log, caplog
):
    """The middleware's own 500, with no route involved."""

    async def inner(scope, receive, send):
        raise RuntimeError(f"SDK exploded with {SECRET_MARKER}")

    scope, sent = drive(inner, path="/api/secret")

    start, body = sent[0], sent[1]

    assert start["status"] == 500

    identifier = MutableHeaders(scope=start)[HEADER]

    assert identifier == scope["state"]["request_id"]
    assert json.loads(body["body"]) == {
        "detail": f"Request failed. Reference ID: {identifier}"
    }
    assert SECRET_MARKER.encode() not in body["body"]


# ============================================================
# 4. SAFE HAND-WRITTEN VALIDATION ERRORS REMAIN VISIBLE
# ============================================================

def test_is_user_facing_only_for_our_own_validation_error():
    assert observability.is_user_facing_error(
        ValidationError("Only SELECT queries are allowed.")
    )


@pytest.mark.parametrize(
    "exception",
    [
        ValueError("Unterminated string starting at: line 1"),
        sqlite3.OperationalError(f"no such table at {PATH_MARKER}"),
        FileNotFoundError(PATH_MARKER),
        OSError("Permission denied"),
        RuntimeError("GEMINI_API_KEY is not configured"),
        json.JSONDecodeError("bad", "{", 0),
    ],
)
def test_third_party_and_internal_errors_are_not_user_facing(exception):
    assert not observability.is_user_facing_error(exception)


def test_validation_error_is_still_a_value_error():
    """Every existing caller that catches ValueError keeps working."""

    assert issubclass(ValidationError, ValueError)

    with pytest.raises(ValueError, match="Only SELECT queries"):
        from app.validate import validate_sql

        validate_sql("DELETE FROM products")


def test_ask_keeps_the_hand_written_sql_rule_visible(
    authed_client, monkeypatch
):
    """The LLM returned a mutating statement: the user must be told."""

    monkeypatch.setattr(
        main,
        "ask_active",
        lambda prompt, turns, request_id=None, generation=None: ("DROP TABLE products", "nope"),
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Only SELECT queries are allowed."
    )


def test_upload_keeps_the_hand_written_file_type_rule_visible(
    authed_client,
):
    response = authed_client.post(
        "/api/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Unsupported file type. Use .db/.sqlite/.sqlite3, .sql, or .xlsx."
    )


@pytest.mark.parametrize(
    "content,expected",
    [
        (b"", "The SQL script contains no statements."),
        (
            b"ATTACH DATABASE 'x' AS y;",
            "Only CREATE TABLE/INDEX/VIEW and INSERT INTO",
        ),
        (b"SELECT 1;", "Only CREATE TABLE/INDEX/VIEW"),
    ],
    ids=["empty", "attach", "select"],
)
def test_upload_keeps_the_hand_written_sql_script_rule_visible(
    authed_client, content, expected
):
    response = authed_client.post(
        "/api/upload",
        files={
            "file": ("script.sql", content, "application/sql")
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith(expected)


def test_user_facing_error_still_gets_a_request_id(authed_client):
    """Sanitisation must not cost the user the reference id."""

    response = authed_client.post(
        "/api/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert_valid_id(request_id(response))


# ============================================================
# 5. VALIDATION ERRORS (422) ARE SANITIZED
# ============================================================

def test_422_shape_is_safe_and_actionable(authed_client):
    response = authed_client.post(
        "/api/ask", json={"prompt": 12345}
    )

    assert response.status_code == 422

    body = response.json()

    assert set(body) == {"detail", "reference_id"}
    assert body["detail"] == "Invalid request data."
    assert body["reference_id"] == request_id(response)


def test_422_never_echoes_the_submitted_password(
    meta_paths, app_secret
):
    """A malformed credential must not be reflected back to the caller."""

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"password": [PROMPT_MARKER, SECRET_MARKER]},
        )

    assert response.status_code == 422

    body = response.json()

    assert body["detail"] == "Invalid request data."
    assert PROMPT_MARKER not in response.text
    assert SECRET_MARKER not in response.text

    # The field name is not echoed either: reporting which field failed
    # turns a malformed-request probe into a schema oracle.
    assert "password" not in json.dumps(body).lower()
    assert "loc" not in body and "msg" not in body
    assert "input" not in body
    assert not client.cookies.get(config.AUTH_COOKIE_NAME)


def test_422_never_echoes_prompts_sql_or_session_ids(authed_client):
    response = authed_client.post(
        "/api/ask",
        json={
            "prompt": ["nope", PROMPT_MARKER],
            "session_id": PROMPT_MARKER,
            "history": SQL_MARKER,
        },
    )

    assert response.status_code == 422

    assert PROMPT_MARKER not in response.text
    assert SQL_MARKER not in response.text
    assert "prompt" not in response.text.lower()
    assert "session_id" not in response.text.lower()


def test_422_still_refuses_to_authenticate(meta_paths, app_secret):
    """Sanitisation must not weaken the login route."""

    with TestClient(app) as client:

        for payload in ({}, {"password": ""}):
            response = client.post(
                "/api/auth/login-form", data=payload
            )
            assert response.status_code == 422

        # A repeated field is not a validation error the client can use:
        # it simply never authenticates.
        assert client.post(
            "/api/auth/login-form", data={"password": ["a", "b"]}
        ).status_code != 200

        assert not client.cookies.get(config.AUTH_COOKIE_NAME)
        assert client.get("/api/schema").status_code == 401


# ============================================================
# 6. /api/version DISCLOSES NO PATHS
# ============================================================

def test_version_does_not_expose_the_database_path(authed_client):
    response = authed_client.get("/api/version")

    assert response.status_code == 200

    body = response.json()

    assert "database" not in body
    assert "DATABASE_PATH" not in json.dumps(body)
    assert "store.db" not in json.dumps(body)
    assert config.DATABASE_PATH not in response.text
    assert config.BASE_DIR not in response.text


def test_version_contains_no_filesystem_path_at_all(authed_client):
    response = authed_client.get("/api/version")

    text = response.text

    assert "/" not in text
    assert "\\" not in text
    assert not re.search(r"[A-Za-z]:", text)


def test_version_still_reports_safe_metadata(authed_client):
    """The disclosure is removed, not the operational answer."""

    body = authed_client.get("/api/version").json()

    assert body["version"] == "4.0"
    assert body["backend"] == "PROD"
    assert body["provider"]
    assert body["database_exists"] is True
    assert "gemini_configured" in body
    assert "nvidia_configured" in body


def test_health_still_exposes_no_path(anon_client):
    body = anon_client.get("/health").json()

    assert set(body) == {"status", "database_exists"}
    assert "/" not in json.dumps(body)


# ============================================================
# 7. THE EXISTING SECURITY ORDER IS UNTOUCHED
# ============================================================

def test_authentication_still_precedes_the_limiter(
    meta_paths, app_secret, monkeypatch
):
    """The middleware must not become the thing that guards a route."""

    seen = []
    original = ratelimit.limiter.allow

    def spy(key, limit, window_seconds):
        seen.append(key)
        return original(key, limit, window_seconds)

    monkeypatch.setattr(ratelimit.limiter, "allow", spy)

    with TestClient(app) as anon:

        # A route that is both guarded and limited: /api/report. The
        # guard answers first, so the counter is never consulted.
        assert anon.post(
            "/api/report", json={"title": "t"}
        ).status_code == 401

    assert seen == []
    assert ratelimit.limiter._buckets == {}


def test_the_request_id_is_not_used_as_a_rate_limit_key(
    authed_client,
):
    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    identifier = request_id(response)
    keys = " ".join(ratelimit.limiter._buckets)

    assert keys
    assert identifier not in keys

    # Still exactly the documented shape: a scope prefix and either the
    # shared api-key constant or a one-way digest of the session token.
    for key in ratelimit.limiter._buckets:
        scope, _, identity = key.partition(":")

        assert scope

        if identity == ratelimit._API_KEY_IDENTITY:
            continue

        assert len(identity) == 32
        assert all(char in "0123456789abcdef" for char in identity)


def test_a_401_is_unchanged_by_the_new_layer(anon_client, app_secret):
    """The auth contract itself: same status, same wording, no id leak
    into the body beyond what the client already had."""

    response = anon_client.get("/api/schema")

    assert response.status_code == 401
    assert response.json() == {
        "detail": "Invalid or missing credentials."
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert app_secret not in response.text


def test_the_guard_is_still_attached_once_to_the_router():
    assert len(main.api.dependencies) == 1


# ============================================================
# DOCUMENTATION HONESTY
# ============================================================

def test_module_states_what_it_never_logs():
    import inspect

    header = inspect.getsource(observability)
    header = header.split("import logging", 1)[0].lower()

    assert "never logs" in header
    assert "authorization" in header
    assert "request bodies" in header
    assert "not permanent" in header
    assert "rate-limit key" in header