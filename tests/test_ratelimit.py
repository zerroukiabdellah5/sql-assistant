"""Tests for the best-effort per-instance application rate limiter.

The limiter is deliberately NOT a global quota: on Vercel each warm
instance counts independently and a cold start clears the state.
These tests pin the behaviour that IS real - per-key counting, a
valid Retry-After, authentication ordering, and the absence of any
secret, token or IP in stored state.

No NVIDIA request is made: /api/ask has its provider call replaced
with a recorder. The credential used here is the throwaway literal
from conftest.py.
"""

import logging
import threading

import pytest

from fastapi.testclient import TestClient

import app.config as config
from app import ratelimit
from app.auth import create_session_token
from app.main import app

COOKIE = config.AUTH_COOKIE_NAME
LOGIN_FORM = "/api/auth/login-form"


class FakeClock:
    """Determinable clock. Never sleeps, never flakes."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    """An isolated limiter driven by the fake clock."""

    return ratelimit.FixedWindowLimiter(time_source=clock)


def signed_in_client(app_secret):
    """A TestClient holding a genuine session cookie."""

    client = TestClient(app)
    response = client.post(
        "/api/auth/login", json={"password": app_secret}
    )
    assert response.status_code == 200, response.text
    return client


def session_token(app_secret, ttl_seconds):
    return create_session_token(
        secret=app_secret, ttl_seconds=ttl_seconds
    )


def db_bytes():
    """A minimal valid SQLite database as bytes.

    Written to a real temp file: sqlite3 needs a path it can reopen,
    and a BytesIO has no usable name.
    """

    import os
    import sqlite3
    import tempfile

    descriptor, path = tempfile.mkstemp(suffix=".db")
    os.close(descriptor)

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (a INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()

    with open(path, "rb") as handle:
        content = handle.read()

    os.remove(path)

    return content


def upload_file(client, index=0):
    """POST one small valid database. Returns the response."""

    return client.post(
        "/api/upload",
        files={
            "file": (
                f"items{index}.db",
                db_bytes(),
                "application/octet-stream",
            )
        },
    )


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace the NVIDIA call so no test touches the network."""

    def fake_ask(prompt, turns, request_id=None, generation=None):
        return "SELECT 1", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)


# ============================================================
# 1-5. FIXED WINDOW ALGORITHM
# ============================================================

def test_first_request_is_allowed(limiter):
    allowed, retry_after = limiter.allow("k", 3, 60)

    assert allowed is True
    assert retry_after == 0


def test_request_over_the_limit_is_denied(limiter):
    limit = 3

    for _ in range(limit):
        assert limiter.allow("k", limit, 60)[0] is True

    allowed, retry_after = limiter.allow("k", limit, 60)

    assert allowed is False
    assert retry_after >= 1


def test_retry_after_is_a_positive_integer_within_the_window(
    limiter, clock
):
    limiter.allow("k", 1, 60)
    clock.advance(10)

    _allowed, retry_after = limiter.allow("k", 1, 60)

    assert isinstance(retry_after, int)
    assert 1 <= retry_after <= 60
    # The window is still 50s away, so that is what we advertise.
    assert retry_after == 50


def test_window_resets_after_it_elapses(limiter, clock):
    for _ in range(2):
        assert limiter.allow("k", 2, 60)[0] is True

    assert limiter.allow("k", 2, 60)[0] is False

    clock.advance(61)

    assert limiter.allow("k", 2, 60)[0] is True


def test_still_denied_mid_window(limiter, clock):
    for _ in range(2):
        limiter.allow("k", 2, 60)

    clock.advance(59)

    assert limiter.allow("k", 2, 60)[0] is False


def test_keys_are_independent(limiter):
    assert limiter.allow("a", 1, 60)[0] is True
    assert limiter.allow("a", 1, 60)[0] is False
    # A different key still has its own full allowance.
    assert limiter.allow("b", 1, 60)[0] is True


def test_expired_entries_are_pruned(limiter, clock):
    for index in range(50):
        limiter.allow(f"key-{index}", 5, 60)

    assert len(limiter._buckets) == 50

    clock.advance(120)
    limiter.allow("fresh", 5, 60)

    # Everything stale is gone, so memory does not grow forever.
    assert len(limiter._buckets) == 1


# ============================================================
# 2-3. 429 SHAPE OVER HTTP (LOGIN IS THE SIMPLEST SURFACE)
# ============================================================

def test_login_returns_429_with_retry_after_after_the_limit(
    meta_paths, app_secret
):
    limit = config.LOGIN_RATE_LIMIT_REQUESTS

    with TestClient(app) as client:

        for _ in range(limit):
            assert client.post(
                "/api/auth/login",
                json={"password": "wrong"},
            ).status_code == 401

        response = client.post(
            "/api/auth/login", json={"password": "wrong"}
        )

        assert response.status_code == 429

        retry_after = int(response.headers["Retry-After"])

        assert 1 <= retry_after <= (
            config.LOGIN_RATE_LIMIT_WINDOW_SECONDS
        )


def test_limited_response_reveals_nothing(meta_paths, app_secret):
    with TestClient(app) as client:

        for _ in range(config.LOGIN_RATE_LIMIT_REQUESTS):
            client.post(
                "/api/auth/login", json={"password": "wrong"}
            )

        response = client.post(
            "/api/auth/login", json={"password": "wrong"}
        )

        body = response.text.lower()

        # No count, no window, no identity, and no hint about
        # whether the server is configured.
        assert str(config.LOGIN_RATE_LIMIT_REQUESTS) not in body
        assert "app_api_key" not in body
        assert "configured" not in body
        assert app_secret not in response.text


# ============================================================
# 6. SESSION IDENTITIES ARE SEPARATE AND NEVER STORED RAW
# ============================================================

def test_two_sessions_have_independent_allowances(
    meta_paths, app_secret, stub_provider
):
    limit = config.ASK_RATE_LIMIT_REQUESTS
    first = session_token(app_secret, 40000)
    second = session_token(app_secret, 50000)

    assert first != second

    with TestClient(app) as client:

        # One client, swapping the cookie, so a 429 can only come from
        # the bucket and not from some second app lifespan.
        client.cookies.set(COOKIE, first)

        # Exhaust only the first session's bucket.
        for _ in range(limit):
            client.post("/api/ask", json={"prompt": "x"})

        blocked = client.post("/api/ask", json={"prompt": "x"})
        assert blocked.status_code == 429

        # A different validated session has its own allowance.
        client.cookies.set(COOKIE, second)

        assert client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 200

        # And the first session is still blocked.
        client.cookies.set(COOKIE, first)
        assert client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 429


def test_limiter_state_never_contains_a_raw_token(
    meta_paths, app_secret, stub_provider
):
    token = session_token(app_secret, 43200)

    with TestClient(app) as client:
        client.cookies.set(COOKIE, token)
        client.post("/api/ask", json={"prompt": "x"})

    state = " ".join(ratelimit.limiter._buckets)

    assert token not in state
    # Nor any recognisable fragment of it.
    assert token[:16] not in state
    assert app_secret not in state


def test_state_keys_are_scoped_and_digested(
    meta_paths, app_secret, stub_provider
):
    with signed_in_client(app_secret) as client:
        # Signing in created a login bucket. Clear it so this test
        # observes the ask key on its own.
        ratelimit.limiter.reset()

        client.post("/api/ask", json={"prompt": "x"})

    keys = list(ratelimit.limiter._buckets)

    assert len(keys) == 1
    assert keys[0].startswith("ask:")

    # scope prefix + a hex digest, nothing else.
    digest = keys[0].split(":", 1)[1]

    assert len(digest) == 32
    assert all(c in "0123456789abcdef" for c in digest)


# ============================================================
# 7-8. BOTH LOGIN ROUTES ARE LIMITED
# ============================================================

@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/api/auth/login", {"json": {"password": "wrong"}}),
        (LOGIN_FORM, {"data": {"password": "wrong"}}),
    ],
)
def test_both_login_routes_are_rate_limited(
    meta_paths, app_secret, path, kwargs
):
    with TestClient(app) as client:

        for _ in range(config.LOGIN_RATE_LIMIT_REQUESTS):
            assert client.post(
                path, **kwargs
            ).status_code == 401

        assert client.post(
            path, **kwargs
        ).status_code == 429


def test_login_form_is_limited_before_validation(
    meta_paths, app_secret
):
    """A 422 (missing field) must still consume the budget, or the
    route could be probed for free."""

    with TestClient(app) as client:

        for _ in range(config.LOGIN_RATE_LIMIT_REQUESTS):
            client.post(LOGIN_FORM, data={})

        response = client.post(LOGIN_FORM, data={})

        assert response.status_code == 429


def test_unauthenticated_requests_do_not_consume_login_budget(
    meta_paths, app_secret
):
    """Only real login attempts count; ordinary traffic is unaffected."""

    with TestClient(app) as client:

        for _ in range(config.LOGIN_RATE_LIMIT_REQUESTS * 2):
            assert client.get("/api/schema").status_code in (
                200, 401
            )

        assert client.post(
            LOGIN_FORM, data={"password": "wrong"}
        ).status_code == 401


# ============================================================
# 9. THE LIMITER REJECTS BEFORE ask_active() IS CALLED
# ============================================================

def test_ask_is_blocked_before_the_provider_is_called(
    meta_paths, app_secret, monkeypatch
):
    calls = []

    def fake_ask(prompt, turns, request_id=None, generation=None):
        calls.append(prompt)
        return "SELECT 1", "ok"

    monkeypatch.setattr(
        "app.main.ask_active", fake_ask
    )

    limit = config.ASK_RATE_LIMIT_REQUESTS

    with signed_in_client(app_secret) as client:

        for _ in range(limit):
            assert client.post(
                "/api/ask", json={"prompt": "x"}
            ).status_code == 200

        blocked = client.post("/api/ask", json={"prompt": "x"})

        assert blocked.status_code == 429

        # The decisive assertion: no further provider call happened,
        # so the rejected request spent no NVIDIA quota.
        assert len(calls) == limit


def test_ask_still_returns_401_before_the_limiter(
    meta_paths, app_secret, monkeypatch
):
    """An unauthenticated caller is rejected by auth, so it never
    consumes an authenticated bucket."""

    monkeypatch.setattr(
        "app.main.ask_active",
        lambda prompt, turns, request_id=None, generation=None: ("SELECT 1", "ok"),
    )

    with TestClient(app) as anon:
        for _ in range(config.ASK_RATE_LIMIT_REQUESTS * 2):
            assert anon.post(
                "/api/ask", json={"prompt": "x"}
            ).status_code == 401

    assert ratelimit.limiter._buckets == {}


# ============================================================
# 10-13. THE OTHER LIMITED ENDPOINTS
# ============================================================

def test_upload_is_limited_before_the_handler_runs(
    meta_paths, app_secret
):
    limit = config.UPLOAD_RATE_LIMIT_REQUESTS

    with signed_in_client(app_secret) as client:

        created = [
            upload_file(client, index).json()["id"]
            for index in range(limit)
        ]

        # Every upload the limiter admitted really reached the handler
        # and produced a distinct import.
        assert len(set(created)) == limit

        # The next one is refused, so the file is never buffered,
        # parsed, or written to disk.
        blocked = upload_file(client, 99)

        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers

        listing = client.get("/api/imports").json()["imports"]
        assert len(listing) == limit


def test_report_is_limited(meta_paths, app_secret):
    limit = ratelimit.REPORT_RATE_LIMIT_REQUESTS
    payload = {
        "title": "T",
        "question": "q",
        "sql": "SELECT 1",
        "explanation": "e",
        "data": [{"a": 1}],
        "count": 1,
    }

    with signed_in_client(app_secret) as client:

        for _ in range(limit):
            assert client.post(
                "/api/report", json=payload
            ).status_code == 200

        assert client.post(
            "/api/report", json=payload
        ).status_code == 429


def test_approval_actions_are_limited(meta_paths, app_secret):
    limit = ratelimit.APPROVAL_RATE_LIMIT_REQUESTS

    with signed_in_client(app_secret) as client:

        ids = []
        for _ in range(limit + 1):
            created = client.post(
                "/api/approvals",
                json={"kind": "sql", "sql": "SELECT 1"},
            )
            ids.append(created.json()["id"])

        for approval_id in ids[:-1]:
            assert client.post(
                f"/api/approvals/{approval_id}/approve"
            ).status_code == 200

        assert client.post(
            f"/api/approvals/{ids[-1]}/approve"
        ).status_code == 429


def test_approval_reject_shares_the_approval_bucket(
    meta_paths, app_secret
):
    limit = ratelimit.APPROVAL_RATE_LIMIT_REQUESTS

    with signed_in_client(app_secret) as client:

        ids = [
            client.post(
                "/api/approvals",
                json={"kind": "sql", "sql": "SELECT 1"},
            ).json()["id"]
            for _ in range(limit + 1)
        ]

        for approval_id in ids[:-1]:
            assert client.post(
                f"/api/approvals/{approval_id}/reject"
            ).status_code == 200

        assert client.post(
            f"/api/approvals/{ids[-1]}/reject"
        ).status_code == 429


def test_session_delete_is_limited(meta_paths, app_secret):
    limit = ratelimit.DELETE_RATE_LIMIT_REQUESTS

    with signed_in_client(app_secret) as client:

        ids = [
            client.post(
                "/api/sessions", json={"title": "s"}
            ).json()["id"]
            for _ in range(limit + 1)
        ]

        for session_id in ids[:-1]:
            assert client.delete(
                f"/api/sessions/{session_id}"
            ).status_code == 200

        assert client.delete(
            f"/api/sessions/{ids[-1]}"
        ).status_code == 429


def test_import_delete_is_limited(meta_paths, app_secret):
    limit = ratelimit.DELETE_RATE_LIMIT_REQUESTS

    # The upload budget is the same size as the delete budget, so only
    # `limit` imports can be created here. That is enough: the delete
    # limiter counts the request before the handler runs, so the
    # over-limit request is refused regardless of what the handler
    # would have done with an already-deleted id.
    with signed_in_client(app_secret) as client:

        ids = [
            upload_file(client, index).json()["id"]
            for index in range(limit)
        ]

        assert client.delete(
            f"/api/imports/{ids[0]}"
        ).status_code == 200

        # Spend the rest of the allowance. These 404 because the ids
        # are already gone, but the limiter counted them all.
        for import_id in ids[1:]:
            client.delete(f"/api/imports/{import_id}")

        blocked = client.delete(
            f"/api/imports/{ids[0]}"
        )

        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


# ============================================================
# 14. READ ROUTES ARE DELIBERATELY NOT LIMITED
# ============================================================

@pytest.mark.parametrize(
    "path",
    [
        "/api/version",
        "/api/schema",
        "/api/sessions",
        "/api/approvals",
        "/api/imports",
    ],
)
def test_read_routes_are_never_limited(
    meta_paths, app_secret, path
):
    hammer = config.ASK_RATE_LIMIT_REQUESTS * 2

    with signed_in_client(app_secret) as client:

        for _ in range(hammer):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.status_code != 429


def test_session_creation_is_not_double_counted(
    meta_paths, app_secret
):
    with signed_in_client(app_secret) as client:

        for _ in range(config.ASK_RATE_LIMIT_REQUESTS * 2):
            assert client.post(
                "/api/sessions", json={"title": "s"}
            ).status_code == 200


def test_logout_and_health_are_not_limited(meta_paths, app_secret):
    with signed_in_client(app_secret) as client:

        for _ in range(config.LOGIN_RATE_LIMIT_REQUESTS * 2):
            assert client.post(
                "/api/auth/logout"
            ).status_code == 200
            client.post(
                "/api/auth/login",
                json={"password": app_secret},
            )

        assert client.get("/health").status_code == 200


# ============================================================
# 15. THE KILL SWITCH
# ============================================================

def test_disabling_the_limiter_bypasses_it(
    meta_paths, app_secret, monkeypatch, stub_provider
):
    monkeypatch.setattr(
        config, "APP_RATE_LIMIT_ENABLED", False
    )

    hammer = config.ASK_RATE_LIMIT_REQUESTS * 2

    with signed_in_client(app_secret) as client:

        for _ in range(hammer):
            assert client.post(
                "/api/ask", json={"prompt": "x"}
            ).status_code == 200

    assert ratelimit.limiter._buckets == {}


def test_kill_switch_does_not_weaken_authentication(
    meta_paths, app_secret, monkeypatch, stub_provider
):
    """Disabling the limiter must never open the API."""

    monkeypatch.setattr(
        config, "APP_RATE_LIMIT_ENABLED", False
    )

    with TestClient(app) as anon:
        assert anon.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 401
        assert anon.get("/api/schema").status_code == 401


# ============================================================
# 16. STATE ISOLATION
# ============================================================

def test_reset_clears_state(limiter):
    limiter.allow("a", 1, 60)
    limiter.allow("b", 1, 60)

    assert limiter._buckets

    limiter.reset()

    assert limiter._buckets == {}
    assert limiter.allow("a", 1, 60)[0] is True


def test_suite_limiter_starts_empty_each_test(
    meta_paths, app_secret
):
    """The autouse conftest fixture must isolate the global limiter."""

    assert ratelimit.limiter._buckets == {}


# ============================================================
# 17. CONCURRENCY
# ============================================================

def test_concurrent_requests_never_over_admit(limiter):
    """Sync handlers share the AnyIO threadpool, so the lock matters.

    Deterministic: more workers than the limit all race for the same
    key, and exactly `limit` of them may be granted. This fails if the
    check-and-increment is not atomic.
    """

    limit = 10
    workers = 40
    results = []
    errors = []
    barrier = threading.Barrier(workers)

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(limiter.allow("shared", limit, 60))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker)
        for _ in range(workers)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, errors
    assert len(results) == workers

    granted = sum(1 for allowed, _ in results if allowed)

    assert granted == limit


# ============================================================
# 18. NO SECRET, TOKEN OR IP IS EXPOSED
# ============================================================

def test_no_secret_is_reachable_through_the_limiter(
    meta_paths, app_secret, stub_provider
):
    token = session_token(app_secret, 43200)

    with TestClient(app) as client:
        client.cookies.set(COOKIE, token)

        for _ in range(config.ASK_RATE_LIMIT_REQUESTS):
            client.post("/api/ask", json={"prompt": "x"})

        response = client.post(
            "/api/ask", json={"prompt": "x"}
        )

    assert response.status_code == 429

    surface = (
        response.text
        + str(dict(response.headers))
        + " ".join(ratelimit.limiter._buckets)
        + " ".join(map(str, ratelimit.limiter._buckets.values()))
    )

    assert app_secret not in surface
    assert token not in surface


def test_client_host_is_hashed_not_stored(meta_paths, app_secret):
    with TestClient(app) as client:
        client.post(
            LOGIN_FORM, data={"password": "wrong"}
        )

    # TestClient presents host "testclient".
    assert "testclient" not in " ".join(ratelimit.limiter._buckets)


def test_forwarded_headers_are_ignored(meta_paths, app_secret):
    """x-forwarded-for must never influence the bucket key, or an
    attacker could mint a fresh allowance per request."""

    from app.ratelimit import _digest, _login_identity

    class _Client:
        host = "10.0.0.1"

    class _Request:
        client = _Client()
        headers = {
            "x-forwarded-for": "1.2.3.4",
            "x-real-ip": "5.6.7.8",
        }

    identity = _login_identity(_Request())

    assert identity == _digest("10.0.0.1")
    assert identity != _digest("1.2.3.4")
    assert identity != _digest("5.6.7.8")


def test_missing_client_host_falls_back_to_one_shared_bucket(
    monkeypatch,
):
    from app.ratelimit import _login_identity, _UNKNOWN_HOST_IDENTITY

    class _Request:
        client = None
        headers = {}

    assert _login_identity(_Request()) == _UNKNOWN_HOST_IDENTITY


# ============================================================
# 19. FAIL CLOSED
# ============================================================

class _CorruptBuckets(dict):
    """Returns a malformed entry for every key."""

    def get(self, key, default=None):
        return ("corrupt",)


def test_malformed_state_denies_instead_of_crashing(
    limiter, monkeypatch, caplog
):
    monkeypatch.setattr(
        limiter, "_buckets", _CorruptBuckets()
    )

    with caplog.at_level(
        logging.WARNING, logger="app.ratelimit"
    ):
        allowed, retry_after = limiter.allow(
            "k", 5, 60
        )

    assert allowed is False
    assert retry_after == 60

    # The line reaches the project logger, not the console, and it
    # carries no key material.
    records = [
        record
        for record in caplog.records
        if record.name == "app.ratelimit"
    ]

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        "RATE LIMITER STATE REJECTED"
    )
    assert "k" not in records[0].getMessage()


def test_http_layer_returns_429_not_500_on_corrupt_state(
    meta_paths, app_secret, monkeypatch, stub_provider
):
    # Sign in first: a corrupt bucket denies everything, so the login
    # would be refused too and there would be no session to test with.
    client = signed_in_client(app_secret)

    with client:
        monkeypatch.setattr(
            ratelimit.limiter, "_buckets", _CorruptBuckets()
        )

        response = client.post(
            "/api/ask", json={"prompt": "x"}
        )

    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_broken_clock_denies_rather_than_raises(
    limiter, caplog
):
    def explode():
        raise RuntimeError("clock is gone")

    limiter._time = explode

    with caplog.at_level(
        logging.WARNING, logger="app.ratelimit"
    ):
        allowed, retry_after = limiter.allow(
            "k", 5, 60
        )

    assert allowed is False
    assert retry_after == 60

    records = [
        record
        for record in caplog.records
        if record.name == "app.ratelimit"
    ]

    assert [record.getMessage() for record in records] == [
        "RATE LIMITER CLOCK FAILURE"
    ]
    assert records[0].levelno == logging.WARNING


# ============================================================
# 20. FAILURES ARE LOGGED, NEVER WRITTEN TO THE CONSOLE
# ============================================================

def _explode(*_args):
    raise RuntimeError("boom")


def test_unexpected_failure_is_logged_with_a_traceback(
    limiter, monkeypatch, caplog
):
    """The catch-all must still fail closed, and it must leave the real
    cause on the server: an ERROR record carrying exc_info.

    The exception text lives in exc_info, never in the message, so a log
    search finds the fixed phrase while the detail stays in the
    traceback on stderr."""

    def explode():
        raise RuntimeError(
            "internal detail that must not leak"
        )

    monkeypatch.setattr(limiter, "_prune", explode)

    with caplog.at_level(
        logging.ERROR, logger="app.ratelimit"
    ):
        allowed, retry_after = limiter.allow(
            "k", 5, 60
        )

    assert allowed is False
    assert retry_after == 60

    records = [
        record
        for record in caplog.records
        if record.name == "app.ratelimit"
    ]

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == "RATE LIMITER FAILURE"
    assert records[0].exc_info, "no traceback was recorded"
    assert "internal detail" not in records[0].getMessage()


def test_the_limiter_writes_nothing_to_the_console(
    limiter, monkeypatch, capsys
):
    """All three failure paths are silent on stdout and stderr.

    print() bypasses the logging configuration: it lands wherever the
    process happens to be writing and can be neither silenced nor
    filtered. Every failure must speak through the logger only, so once
    the logger is muted, all three paths must produce no output at all.
    A recorder is attached directly to the limiter's own logger, so the
    records are still observable while nothing is written."""

    recorded = []

    class Recorder(logging.Handler):
        def emit(self, record):
            recorded.append(record)

    application_logger = logging.getLogger("app")
    limiter_logger = logging.getLogger("app.ratelimit")

    # Mute the project logger without removing it: a handler is still
    # present, so logging.lastResort does not take over and write to
    # stderr either.
    monkeypatch.setattr(
        application_logger,
        "handlers",
        [logging.NullHandler()],
    )
    monkeypatch.setattr(
        application_logger, "propagate", False
    )
    monkeypatch.setattr(
        limiter_logger, "handlers", [Recorder()]
    )

    monkeypatch.setattr(limiter, "_prune", _explode)
    limiter.allow("k", 5, 60)

    limiter._time = _explode
    limiter.allow("k", 5, 60)

    # The clock is restored first: the corrupt-state path lives after it.
    limiter._time = FakeClock()
    monkeypatch.setattr(
        limiter, "_buckets", _CorruptBuckets()
    )
    limiter.allow("k", 5, 60)

    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""

    # All three really did run and really did reach the logger.
    assert {record.getMessage() for record in recorded} == {
        "RATE LIMITER FAILURE",
        "RATE LIMITER CLOCK FAILURE",
        "RATE LIMITER STATE REJECTED",
    }


def test_the_module_contains_no_print_calls():
    """A static check, so a reintroduced print() cannot hide in a branch
    this suite happens not to exercise."""

    import inspect

    source = inspect.getsource(ratelimit)

    assert "print(" not in source


# ============================================================
# DOCUMENTATION HONESTY
# ============================================================

def test_module_states_it_is_not_a_global_control():
    """The limiter must not be mistaken for a global quota.

    If this wording is ever removed, the file is over-claiming, and
    that is exactly the failure mode this test exists to catch.
    """

    import inspect

    header = inspect.getsource(ratelimit)
    header = header.split("def ", 1)[0].lower()

    assert "best-effort per-instance" in header
    assert "not a global quota" in header
    assert "not shared between vercel instances" in header
    assert "cold start" in header
