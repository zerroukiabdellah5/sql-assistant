# ============================================================
# GENERATE vs RUN — THE SPLIT
# ============================================================
# The instructor feedback: the user must see the generated SQL before
# it executes. The server enforces that as two endpoints:
#
#   POST /api/ask  -> generate ONLY. Calls the model, validates the
#                     statement, returns it. Never touches a database.
#   POST /api/run  -> execute ONLY. Re-validates the SQL it is handed
#                     (the user may have edited it) and runs it. Never
#                     calls a provider and never charges an attempt.
#
# These tests pin that split at the server: generation cannot execute
# even if it wanted to, execution refuses anything unsafe that arrives
# from the browser, and the access and trial controls still hold.
# ============================================================

import app.config as config
from app import access, main, ratelimit

from tests.test_provenance import STORE_SQL, ask_then_run, stub_ask


# ============================================================
# 1. GENERATION NEVER EXECUTES
# ============================================================

def test_generation_never_executes(authed_client, monkeypatch):
    """The whole point: /api/ask must not run the SQL it produces."""

    executed = []

    def explode(sql, path=None):
        executed.append(sql)
        raise AssertionError(
            "generation must never reach the database"
        )

    monkeypatch.setattr(main, "ask_active", stub_ask())
    monkeypatch.setattr(main, "execute_sql_with_metadata", explode)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert response.status_code == 200

    # Nothing was executed, and the SQL came back for the user to read.
    assert executed == []
    assert response.json()["sql"] == STORE_SQL


def test_generation_answer_has_no_execution_artifacts(
    authed_client, monkeypatch
):
    """No rows, count, chart or provenance: nothing has run."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    body = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    for absent in (
        "data",
        "count",
        "truncated",
        "chart",
        "provenance",
        "verification",
    ):
        assert absent not in body, absent


def test_generation_still_refuses_unsafe_sql_before_showing_it(
    authed_client, monkeypatch
):
    """A model that answers with a write is refused at generation.

    The statement is validated so an unsafe answer is never even
    presented as runnable; the 400 is the same rule the run path keeps.
    """

    monkeypatch.setattr(
        main,
        "ask_active",
        stub_ask(sql="DROP TABLE products"),
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": "delete everything"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Only SELECT queries are allowed."
    )


def test_generation_echoes_the_answering_provider(
    authed_client, monkeypatch
):
    """The provider and model travel with the SQL to the run step."""

    monkeypatch.setattr(
        main,
        "ask_active",
        stub_ask(provider="gemini", model="gemini-2.5-flash"),
    )

    body = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    assert body["provider"] == "gemini"
    assert body["model"] == "gemini-2.5-flash"


# ============================================================
# 2. EXECUTION RE-VALIDATES WHAT IT IS HANDED
# ============================================================

def test_run_executes_the_reviewed_sql(authed_client):
    """An owner may run SQL directly; the split works end to end."""

    response = authed_client.post(
        "/api/run", json={"sql": "SELECT * FROM products"}
    )

    assert response.status_code == 200

    body = response.json()

    assert body["success"] is True
    assert body["data"]
    assert body["count"] == len(body["data"])


def test_run_refuses_a_write_even_though_generation_would_have(
    authed_client
):
    """The SQL arrives from the browser: it is untrusted at the door."""

    response = authed_client.post(
        "/api/run",
        json={"sql": "UPDATE products SET price = 0"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Only SELECT queries are allowed."
    )


def test_run_refuses_stacked_statements(authed_client):
    response = authed_client.post(
        "/api/run",
        json={"sql": "SELECT 1; DROP TABLE products"},
    )

    assert response.status_code == 400
    assert "Multiple SQL statements" in response.json()["detail"]


def test_run_refuses_an_empty_statement(authed_client):
    response = authed_client.post("/api/run", json={"sql": "   "})

    assert response.status_code == 400
    assert response.json()["detail"] == "SQL cannot be empty."


def test_run_never_calls_a_provider(authed_client, monkeypatch):
    """Execution is cheap: no model, no generation."""

    def explode(*args, **kwargs):
        raise AssertionError("run must not call a provider")

    monkeypatch.setattr(main, "ask_active", explode)

    response = authed_client.post(
        "/api/run", json={"sql": "SELECT * FROM products"}
    )

    assert response.status_code == 200


# ============================================================
# 3. THE RUN ANSWER CARRIES THE FULL RESULT ENVELOPE
# ============================================================

def test_run_answer_echoes_context_and_builds_the_blocks(
    authed_client, monkeypatch
):
    """The run response is the complete envelope the client renders."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    body = ask_then_run(authed_client).json()

    assert body["prompt"] == "show the products"
    assert body["explanation"] == "reads the products table"
    assert body["sql"] == STORE_SQL
    assert "data" in body
    assert "chart" in body
    assert "provenance" in body
    assert "verification" in body

    # The provider recorded during generation is named in provenance.
    assert body["provenance"]["provider"] == "nvidia"
    assert body["provenance"]["model"] == "stub-model-1"
    assert body["provenance"]["generated_by_ai"] is True


def test_run_honours_a_client_edited_statement(authed_client, monkeypatch):
    """What runs is what the user submitted, not what was generated."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = authed_client.post(
        "/api/ask", json={"prompt": "show the products"}
    ).json()

    assert generated["sql"] == STORE_SQL

    edited = "SELECT id FROM products ORDER BY id"

    run = authed_client.post(
        "/api/run", json={"sql": edited}
    )

    assert run.status_code == 200
    assert run.json()["sql"] == edited


# ============================================================
# 4. ACCESS, TRIAL AND RATE-LIMIT CONTROLS STILL HOLD
# ============================================================

def test_run_without_generation_is_refused_for_a_new_visitor(anon_client):
    """Zero attempts spent means nothing to run, and no free execution."""

    response = anon_client.post(
        "/api/run", json={"sql": "SELECT * FROM products"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "RUN_REQUIRES_GENERATION"


def test_a_visitor_may_run_after_generating(anon_client, monkeypatch):
    """The attempt spent generating unlocks running what it produced."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    generated = anon_client.post(
        "/api/ask", json={"prompt": "show the products"}
    )

    assert generated.status_code == 200

    run = anon_client.post(
        "/api/run", json={"sql": generated.json()["sql"]}
    )

    assert run.status_code == 200
    assert run.json()["data"]


def test_run_does_not_charge_a_trial_attempt(anon_client, monkeypatch):
    """Running spends no quota: the counter is unchanged by a run."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    anon_client.post("/api/ask", json={"prompt": "show the products"})

    before = access.verify_trial_token(
        anon_client.cookies.get(config.TRIAL_COOKIE_NAME)
    )

    anon_client.post(
        "/api/run", json={"sql": STORE_SQL}
    )
    anon_client.post(
        "/api/run", json={"sql": STORE_SQL}
    )

    after = access.verify_trial_token(
        anon_client.cookies.get(config.TRIAL_COOKIE_NAME)
    )

    assert before is not None
    assert before == after


def test_the_run_route_has_its_own_rate_limit_scope(anon_client, monkeypatch):
    """A run bucket exists, separate from the ask bucket."""

    monkeypatch.setattr(main, "ask_active", stub_ask())

    anon_client.post("/api/ask", json={"prompt": "show the products"})
    anon_client.post("/api/run", json={"sql": STORE_SQL})

    scopes = {
        key.partition(":")[0]
        for key in ratelimit.limiter._buckets
    }

    assert "run" in scopes
    assert "ask" in scopes


def test_the_run_limit_defaults_to_the_ask_limit():
    """Execution and generation share a ceiling unless an operator splits it."""

    assert (
        config.RUN_RATE_LIMIT_REQUESTS
        == config.ASK_RATE_LIMIT_REQUESTS
    )
    assert (
        config.RUN_RATE_LIMIT_WINDOW_SECONDS
        == config.ASK_RATE_LIMIT_WINDOW_SECONDS
    )


def test_the_run_route_uses_its_own_configured_limit(
    authed_client, monkeypatch
):
    """The limiter is called with the RUN constants, not the ASK ones."""

    seen = []
    original = ratelimit.limiter.allow

    def spy(key, limit, window_seconds):
        seen.append((key.partition(":")[0], limit, window_seconds))
        return original(key, limit, window_seconds)

    monkeypatch.setattr(ratelimit.limiter, "allow", spy)

    authed_client.post("/api/run", json={"sql": STORE_SQL})

    run_calls = [call for call in seen if call[0] == "run"]

    assert run_calls
    assert run_calls[0][1] == config.RUN_RATE_LIMIT_REQUESTS
    assert run_calls[0][2] == config.RUN_RATE_LIMIT_WINDOW_SECONDS


def test_the_run_route_requires_no_provider_key(authed_client, monkeypatch):
    """No secret needed: run is local execution, not a provider call."""

    monkeypatch.setattr(
        main, "execute_sql_with_metadata",
        lambda sql, path=None: (
            [{"n": 1}],
            _FakeExecution(),
        ),
    )

    response = authed_client.post(
        "/api/run", json={"sql": "SELECT 1 AS n"}
    )

    assert response.status_code == 200
    assert response.json()["data"] == [{"n": 1}]


class _FakeExecution:
    """The subset of db.Execution the run envelope reads."""

    database_path = None
    executed_at = "2024-01-01T00:00:00+00:00"
    elapsed_ms = 1.0
    rows_returned = 1
    truncated = False
    total_matched = 1
