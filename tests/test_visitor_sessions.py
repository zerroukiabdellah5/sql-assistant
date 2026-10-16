"""Anonymous visitor Sessions: open access, isolation and the untouched gates.

The requirement these tests exist to prove:

  * the Sessions panel works for a browser that holds no credential and
    never opens a sign-in;
  * each visitor reaches only their own sessions - another browser cannot
    list, load or delete them, and can never read an owner session;
  * the owner session surface is unchanged, the six-question trial still
    runs, and /api/ask is namespace-aware so a selected visitor session
    persists and reloads its own context without touching owner history.

They run against the real application (no hand-written route list) and
assert over HTTP, so a claim untested here does not belong in the report.
"""

import pytest

import app.config as config

from fastapi.testclient import TestClient

from app.main import app


# ============================================================
# HELPERS
# ============================================================

def _create(client, title="Demo session"):
    return client.post(
        "/api/visitor/sessions", json={"title": title}
    )


@pytest.fixture
def stub_provider(monkeypatch):
    """Answer /api/ask without a network call."""

    def fake_ask(
        prompt,
        turns,
        request_id=None,
        generation=None,
        database_path=None,
        **_,
    ):
        if database_path:
            return "SELECT * FROM items", "ok"

        return "SELECT * FROM products", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)


# ============================================================
# AN ANONYMOUS VISITOR CAN CREATE, LIST, LOAD AND DELETE
# ============================================================

def test_anonymous_session_create_needs_no_credential(anon_client):
    """No credential at all: the session is created and answered."""

    response = _create(anon_client, "My demo")

    assert response.status_code == 200

    body = response.json()

    assert body["id"]
    assert body["title"] == "My demo"

    # The identity travels in an HttpOnly cookie the browser cannot read.
    assert anon_client.cookies.get(config.VISITOR_COOKIE_NAME)


def test_a_blank_title_falls_back_to_the_default(anon_client):
    body = _create(anon_client, title="   ").json()

    assert body["title"] == "New session"


def test_an_anonymous_visitor_lists_only_their_own_sessions(
    anon_client,
):
    created = _create(anon_client, "Only mine").json()

    listed = anon_client.get("/api/visitor/sessions").json()["sessions"]

    assert [item["id"] for item in listed] == [created["id"]]


def test_a_visitor_can_load_their_own_session_detail(anon_client):
    created = _create(anon_client).json()

    response = anon_client.get(
        "/api/visitor/sessions/" + created["id"]
    )

    assert response.status_code == 200

    body = response.json()

    assert body["session"]["id"] == created["id"]
    assert body["messages"] == []


def test_a_visitor_can_delete_their_own_session(anon_client):
    created = _create(anon_client).json()

    deleted = anon_client.delete(
        "/api/visitor/sessions/" + created["id"]
    )

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == created["id"]

    assert anon_client.get(
        "/api/visitor/sessions/" + created["id"]
    ).status_code == 404


# ============================================================
# ONE VISITOR NEVER REACHES ANOTHER'S SESSIONS
# ============================================================

def test_a_second_visitor_cannot_see_or_touch_the_first(
    meta_paths, app_secret
):
    with TestClient(app) as first, TestClient(app) as second:

        created = _create(first).json()

        assert second.get(
            "/api/visitor/sessions"
        ).json()["sessions"] == []

        assert second.get(
            "/api/visitor/sessions/" + created["id"]
        ).status_code == 404

        assert second.delete(
            "/api/visitor/sessions/" + created["id"]
        ).status_code == 404

        # The first visitor's session is untouched by all of that.
        assert first.get(
            "/api/visitor/sessions/" + created["id"]
        ).status_code == 200


def test_a_guessed_session_id_cannot_escape_the_namespace(anon_client):
    _create(anon_client)

    assert anon_client.get(
        "/api/visitor/sessions/deadbeef0000"
    ).status_code == 404


# ============================================================
# THE TWO STORES NEVER MIX
# ============================================================

def test_a_visitor_cannot_read_an_owner_session(
    authed_client, anon_client
):
    owner = authed_client.post(
        "/api/sessions", json={"title": "Owner only"}
    ).json()

    assert anon_client.get(
        "/api/visitor/sessions/" + owner["id"]
    ).status_code == 404


def test_an_owner_cannot_read_a_visitor_session(authed_client, anon_client):
    visitor = _create(anon_client).json()

    assert authed_client.get(
        "/api/sessions/" + visitor["id"]
    ).status_code == 404


# ============================================================
# /api/ask IS NAMESPACE-AWARE
# ============================================================

def test_a_selected_visitor_session_persists_its_own_turns(
    anon_client, stub_provider
):
    created = _create(anon_client).json()

    response = anon_client.post(
        "/api/ask",
        json={"prompt": "list products", "session_id": created["id"]},
    )

    assert response.status_code == 200

    detail = anon_client.get(
        "/api/visitor/sessions/" + created["id"]
    ).json()

    roles = [message["role"] for message in detail["messages"]]

    assert roles == ["user", "assistant"]
    assert detail["messages"][0]["prompt"] == "list products"


def test_ask_refuses_a_session_from_another_namespace(
    authed_client, anon_client, stub_provider
):
    owner = authed_client.post(
        "/api/sessions", json={"title": "Owner"}
    ).json()

    # A visitor naming an owner session is a 404, not a peek.
    assert anon_client.post(
        "/api/ask",
        json={"prompt": "x", "session_id": owner["id"]},
    ).status_code == 404


def test_an_owner_cannot_ask_into_a_visitor_session(
    authed_client, anon_client, stub_provider
):
    created = _create(anon_client).json()

    assert authed_client.post(
        "/api/ask",
        json={"prompt": "x", "session_id": created["id"]},
    ).status_code == 404


# ============================================================
# THE TRIAL AND THE OWNER SURFACE ARE UNCHANGED
# ============================================================

def test_creating_sessions_does_not_spend_a_trial_attempt(
    anon_client, stub_provider
):
    _create(anon_client)

    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 200


def test_sessions_still_work_after_the_trial_is_exhausted(
    anon_client, stub_provider
):
    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 200

    refused = anon_client.post("/api/ask", json={"prompt": "x"})

    assert refused.status_code == 403
    assert refused.json()["detail"] == "FREE_TRIAL_EXHAUSTED"

    # The exhausted visitor can still use Sessions.
    assert _create(anon_client).status_code == 200


def test_the_owner_sessions_route_is_still_protected(anon_client):
    assert anon_client.get("/api/sessions").status_code == 401

    assert anon_client.post(
        "/api/sessions", json={"title": "x"}
    ).status_code == 401


# ============================================================
# FAIL CLOSED AND LEAK-FREE
# ============================================================

def test_the_visitor_session_routes_fail_closed_without_a_secret(
    meta_paths, no_app_secret
):
    with TestClient(app) as client:

        assert client.get("/api/visitor/sessions").status_code == 503
        assert _create(client).status_code == 503


def test_a_forged_visitor_cookie_is_ignored(meta_paths, app_secret):
    with TestClient(app) as client:

        client.cookies.set(
            config.VISITOR_COOKIE_NAME, "vv1.forged.token.value"
        )

        assert client.get(
            "/api/visitor/sessions"
        ).json()["sessions"] == []


def test_no_sign_in_is_ever_required(anon_client):
    """The whole point: no credential, and no 401 from the panel."""

    assert _create(anon_client).status_code == 200
    assert anon_client.get("/api/visitor/sessions").status_code == 200
