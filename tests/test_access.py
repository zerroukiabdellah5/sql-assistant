"""Tests for the visitor access model.

Five free attempts for anyone, an App Access Code for access beyond
that, and the owner's own credentials working exactly as they did
before. These tests pin the promises the frontend makes of this part of
the API:

  * the free allowance is spent only by work actually sent upstream;
  * an attempt is charged even when that work fails, because it was
    still paid for;
  * nothing about the owner side regressed - a session cookie or
    APP_API_KEY is never charged and is never locked out;
  * the flat error bodies the modal depends on stay flat, and carry no
    secret, no code and no server detail.
"""

import pytest

from types import SimpleNamespace

import app.config as config
import app.llm as llm

from app.access import TRIAL_EXHAUSTED_CODE


ANSWER = "SELECT 1 AS a"


class _StubCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        message = SimpleNamespace(content=self._content)

        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)]
        )


class _StubClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(
            completions=_StubCompletions(content)
        )


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace the NVIDIA call so no test touches the network."""

    def fake_ask(prompt, turns, request_id=None, generation=None):
        return "SELECT 1", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)


@pytest.fixture
def provider_stub(monkeypatch):
    """The real /api/ask path, wired at the provider client.

    ask_active is deliberately NOT stubbed here: the concurrency cap
    lives below it, inside the provider call, so replacing ask_active
    would remove the code under test.
    """

    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only-key")
    monkeypatch.setattr(llm, "NVIDIA_MODEL", "test-model")
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    monkeypatch.setattr(
        llm.NVIDIAProvider,
        "_get_client",
        lambda self: _StubClient(ANSWER),
    )


def failing_provider(monkeypatch):
    """A provider that always raises, as a real outage would."""

    def broken_ask(prompt, turns, request_id=None, generation=None):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.main.ask_active", broken_ask)


def spend_allowance(client):
    """Use every free attempt. Returns the last response."""

    response = None

    for _ in range(config.TRIAL_MAX_ATTEMPTS):
        response = client.post(
            "/api/ask", json={"prompt": "show products"}
        )

        assert response.status_code == 200

    return response


# ============================================================
# 1-3. THE FREE ALLOWANCE
# ============================================================

def test_the_whole_allowance_is_available_to_a_new_visitor(
    anon_client, stub_provider
):
    for attempt in range(1, config.TRIAL_MAX_ATTEMPTS + 1):
        response = anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

        assert response.status_code == 200
        assert access_used(response) == attempt


def test_the_sixth_attempt_is_refused(anon_client, stub_provider):
    spend_allowance(anon_client)

    response = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == TRIAL_EXHAUSTED_CODE


def test_the_refusal_is_a_flat_body_the_modal_can_read(
    anon_client, stub_provider
):
    """No nesting. The frontend reads detail at the top level."""

    spend_allowance(anon_client)

    body = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).json()

    assert isinstance(body["detail"], str)
    assert isinstance(body["message"], str)
    assert body["trial_max_attempts"] == config.TRIAL_MAX_ATTEMPTS
    assert body["reference_id"]
    assert body["reference_id"] == body["reference_id"].strip()


def test_the_refusal_leaks_nothing(
    anon_client, stub_provider, app_secret
):
    spend_allowance(anon_client)

    response = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    payload = response.text

    assert app_secret not in payload
    assert config.APP_ACCESS_CODE not in payload
    assert config.TRIAL_COOKIE_NAME not in payload
    assert "Traceback" not in payload


def test_the_refusal_names_the_owner_only_when_one_is_configured(
    anon_client, stub_provider, monkeypatch
):
    spend_allowance(anon_client)

    body = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).json()

    assert "owner_contact" not in body

    monkeypatch.setattr(
        config, "OWNER_CONTACT_EMAIL", "owner@example.com"
    )

    body = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).json()

    assert body["owner_contact"] == "owner@example.com"


# ============================================================
# 4-6. WHAT AN ATTEMPT IS SPENT ON
# ============================================================

def test_a_request_the_server_refuses_before_working_costs_nothing(
    anon_client, stub_provider
):
    """An empty prompt is not an attempt.

    Nothing was sent upstream, so there is nothing to charge for. This is
    why the count is taken at the provider call and not at the door.
    """

    for _ in range(config.TRIAL_MAX_ATTEMPTS * 2):
        response = anon_client.post("/api/ask", json={"prompt": "  "})

        assert response.status_code == 400

    assert anon_client.cookies.get(config.TRIAL_COOKIE_NAME) is None

    # The whole allowance is still available.
    assert anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 200


def test_a_prompt_that_is_too_long_costs_nothing(
    anon_client, stub_provider
):
    response = anon_client.post(
        "/api/ask",
        json={"prompt": "x" * (config.MAX_PROMPT_CHARS + 1)},
    )

    assert response.status_code == 400
    assert anon_client.cookies.get(config.TRIAL_COOKIE_NAME) is None


def test_an_attempt_is_charged_even_when_the_provider_fails(
    anon_client, monkeypatch
):
    """A call that was sent and then failed was still paid for.

    This is the case the injected Response cannot carry. FastAPI builds
    the 500 from scratch, so a cookie written on that response is
    dropped, and a visitor whose provider kept failing would keep every
    attempt while spending the budget behind all of them.
    """

    failing_provider(monkeypatch)

    for attempt in range(1, config.TRIAL_MAX_ATTEMPTS + 1):
        response = anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

        assert response.status_code == 500
        assert access_used(response) == attempt

    response = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == TRIAL_EXHAUSTED_CODE


def test_an_attempt_is_charged_even_when_the_cap_refuses(
    anon_client, provider_stub, monkeypatch
):
    """A refusal by the concurrency cap is a refusal after acceptance.

    The visitor cleared the access gate and the rate limiter and was
    accepted; the provider was simply busy. Nothing was sent upstream
    and no upstream quota was spent, but the request was ours to absorb,
    so the attempt counts. What must NOT happen is a runaway visitor:
    the gate refuses them first, which is covered in
    tests/test_llm_concurrency.py.
    """

    from app.llm_concurrency import gate

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    for attempt in range(1, config.TRIAL_MAX_ATTEMPTS + 1):
        response = anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

        assert response.status_code == 429
        assert access_used(response) == attempt

    assert anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 403


# ============================================================
# 7-9. THE OWNER IS NEVER CHARGED
# ============================================================

def test_the_app_api_key_is_never_charged(
    authed_client, stub_provider
):
    for _ in range(config.TRIAL_MAX_ATTEMPTS * 2):
        assert authed_client.post(
            "/api/ask", json={"prompt": "show products"}
        ).status_code == 200

    assert authed_client.cookies.get(
        config.TRIAL_COOKIE_NAME
    ) is None


def test_an_exhausted_trial_counter_does_not_lock_the_owner_out(
    authed_client, anon_client, stub_provider
):
    """The counter belongs to the visitor, not to the browser.

    An owner who happens to share a machine with an exhausted visitor
    must still be able to work.
    """

    spend_allowance(anon_client)

    assert anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 403

    assert authed_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 200


def test_an_owner_session_is_still_required_for_the_guarded_surface(
    anon_client
):
    assert anon_client.get("/api/schema").status_code == 401
    assert anon_client.get("/api/sessions").status_code == 401


# ============================================================
# 10-13. THE ACCESS CODE
# ============================================================

def test_the_wrong_code_is_refused_without_saying_which_part_failed(
    anon_client
):
    response = anon_client.post(
        "/api/access/unlock", json={"code": "not-the-code"}
    )

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert config.APP_ACCESS_CODE not in response.text
    assert anon_client.cookies.get(config.ACCESS_COOKIE_NAME) is None


def test_the_right_code_unlocks(anon_client, stub_provider):
    response = anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    )

    assert response.status_code == 200
    assert response.json()["unlocked"] is True
    assert anon_client.cookies.get(config.ACCESS_COOKIE_NAME)


def test_unlocking_clears_the_free_counter(
    anon_client, stub_provider
):
    spend_allowance(anon_client)

    assert anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    ).status_code == 200

    assert anon_client.cookies.get(
        config.TRIAL_COOKIE_NAME
    ) is None

    # And the unlocked visitor is not counted down any more.
    for _ in range(config.TRIAL_MAX_ATTEMPTS * 2):
        assert anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        ).status_code == 200


def test_unlocking_does_not_spend_anything(anon_client, stub_provider):
    anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    )

    assert anon_client.cookies.get(
        config.TRIAL_COOKIE_NAME
    ) is None


def test_an_unlocked_visitor_cannot_be_locked_out_by_the_allowance(
    anon_client, stub_provider
):
    anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    )

    spend_allowance(anon_client)

    assert anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    ).status_code == 200


# ============================================================
# 14-16. THE COOKIES THEMSELVES
# ============================================================

def test_the_counter_is_a_signed_cookie_the_client_cannot_read(
    anon_client, stub_provider
):
    response = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    header = cookie_header(response, config.TRIAL_COOKIE_NAME)

    assert header
    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()
    assert "path=/" in header.lower()
    assert "secure" not in header.lower()


def test_the_counter_is_secure_when_the_deployment_is(
    anon_client, stub_provider, monkeypatch
):
    """A public https deployment must not hand the counter over in clear.

    The flag is shared with the auth cookie, so the two cannot
    disagree about the transport they expect.
    """

    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", True)

    response = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert "secure" in cookie_header(
        response, config.TRIAL_COOKIE_NAME
    ).lower()


def test_a_forged_counter_is_not_believed(anon_client, stub_provider):
    """Editing the cookie cannot buy more attempts.

    The counter is signed, so a hand-made token is treated as no token
    at all rather than as a chosen number.
    """

    anon_client.cookies.delete(config.TRIAL_COOKIE_NAME)

    anon_client.cookies.set(
        config.TRIAL_COOKIE_NAME, "tv1.4.99999999999.forged"
    )

    response = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert response.status_code == 200

    # It is not trusted as "4 used", so the count starts from what the
    # server can prove rather than from the claim.
    assert access_used(response) == 1


# ============================================================
# 17-19. FAILING CLOSED
# ============================================================

def test_ask_fails_closed_when_no_secret_is_configured(
    no_app_secret, stub_provider
):
    """No APP_API_KEY means no signing key, so no trial for anyone."""

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/ask", json={"prompt": "show products"}
        )

    assert response.status_code == 503


def test_unlock_fails_closed_when_no_secret_is_configured(no_app_secret):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/access/unlock",
            json={"code": config.APP_ACCESS_CODE},
        )

    assert response.status_code == 503
    assert client.cookies.get(config.ACCESS_COOKIE_NAME) is None


def test_the_public_schema_is_readable_without_any_credential(
    no_app_secret, meta_paths
):
    """Nothing about the visitor entry point depends on the secret.

    The page has to render before anyone has asked for anything.
    """

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/public/schema")

    assert response.status_code == 200
    assert "schema" in response.json()


# ============================================================
# 20-21. ASKING FOR A CODE
# ============================================================

def test_a_request_for_a_code_is_accepted_and_honest(
    anon_client
):
    """No mail transport exists, so the response must say so.

    Pretending a message was sent would be worse than useless: the
    visitor would wait for an email that is never coming.
    """

    response = anon_client.post(
        "/api/access/request", json={"email": "user@example.com"}
    )

    assert response.status_code == 200

    body = response.json()

    assert body["accepted"] is True
    assert body["notification_sent"] is False


def test_a_request_for_a_code_does_not_store_the_address(
    anon_client, caplog
):
    import logging

    with caplog.at_level(logging.DEBUG):
        anon_client.post(
            "/api/access/request",
            json={"email": "user@example.com"},
        )

    assert "user@example.com" not in caplog.text


def test_a_malformed_request_for_a_code_is_refused(anon_client):
    for email in ("not-an-address", "user@", "@example.com"):
        response = anon_client.post(
            "/api/access/request", json={"email": email}
        )

        assert response.status_code == 400
        assert email not in response.text

    # An empty address is a missing one, and must not read as valid.
    assert anon_client.post(
        "/api/access/request", json={"email": ""}
    ).status_code == 400


# ============================================================
# HELPERS
# ============================================================

def cookie_header(response, name):
    """The Set-Cookie value this response wrote for one cookie."""

    for value in response.headers.get_list("set-cookie"):
        if value.startswith(f"{name}="):
            return value

    return ""


def access_used(response):
    """How many attempts the server will honour after this response.

    Read from the cookie the response actually wrote, verified with the
    same function the server uses. The client's own jar is not consulted
    because it cannot express what is being asserted here: that the
    counter which came back is one the server signed.
    """

    from app.access import verify_trial_token

    header = cookie_header(response, config.TRIAL_COOKIE_NAME)

    if not header:
        return None

    token = header.split(";")[0].split("=", 1)[1]

    return verify_trial_token(token)