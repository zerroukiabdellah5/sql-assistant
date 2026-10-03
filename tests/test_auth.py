"""Tests for the application authentication primitives and routes.

The credential used here is a throwaway literal that lives only in the
test suite. No real secret is ever read from the environment, so these
tests are hermetic, safe to run anywhere, and safe to commit.
"""

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.config as config
from app.auth import create_session_token, require_auth, verify_session_token
from app.main import app

COOKIE = config.AUTH_COOKIE_NAME


def fake_request(headers=None):
    """A bare Starlette request for exercising the dependency directly."""

    raw = [
        (name.lower().encode("utf-8"), value.encode("utf-8"))
        for name, value in (headers or {}).items()
    ]

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/ask",
            "headers": raw,
            "query_string": b"",
        }
    )


# The app_secret / no_app_secret fixtures now live in conftest.py so
# every auth test shares one definition of the test-only secret.


# ------------------------------------------------------------
# LOGIN
# ------------------------------------------------------------

def test_login_with_correct_secret_succeeds(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"password": app_secret},
        )
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert response.json()["expires_in"] > 0


def test_login_accepts_header_credential(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            headers={"X-API-Key": app_secret},
        )
        assert response.status_code == 200

        bearer = client.post(
            "/api/auth/login",
            headers={"Authorization": f"Bearer {app_secret}"},
        )
        assert bearer.status_code == 200


def test_login_with_wrong_secret_is_rejected(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"password": "not-the-secret"},
        )
        assert response.status_code == 401
        assert not client.cookies.get(COOKIE)


def test_login_with_empty_body_is_rejected(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={})
        assert response.status_code == 401


def test_login_without_configured_secret_fails_closed(
    meta_paths, no_app_secret
):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"password": "anything"},
        )
        assert response.status_code == 503
        assert not client.cookies.get(COOKIE)


# ------------------------------------------------------------
# COOKIE FLAGS
# ------------------------------------------------------------

def test_login_sets_httponly_lax_cookie(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"password": app_secret},
        )
        header = response.headers["set-cookie"]
        lowered = header.lower()

        assert COOKIE in header
        assert "httponly" in lowered
        assert "samesite=lax" in lowered
        assert client.cookies.get(COOKIE)


def test_cookie_is_secure_in_production(
    meta_paths, app_secret, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", True)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"password": app_secret},
        )
        assert "secure" in response.headers["set-cookie"].lower()


def test_logout_clears_cookie(meta_paths, app_secret):
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"password": app_secret},
        ).status_code == 200
        assert client.cookies.get(COOKIE)

        response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json()["authenticated"] is False
        assert "max-age=0" in response.headers["set-cookie"].lower()


# ------------------------------------------------------------
# THE SECRET IS NEVER ECHOED
# ------------------------------------------------------------

def test_auth_responses_never_contain_the_secret(meta_paths, app_secret):
    with TestClient(app) as client:
        responses = [
            client.post(
                "/api/auth/login",
                json={"password": app_secret},
            ),
            client.post(
                "/api/auth/login",
                json={"password": "not-the-secret"},
            ),
            client.post("/api/auth/logout"),
        ]

        for response in responses:
            assert app_secret not in response.text
            assert app_secret not in response.headers.get(
                "set-cookie", ""
            )


# ------------------------------------------------------------
# SESSION TOKENS
# ------------------------------------------------------------

def test_session_token_round_trip(app_secret):
    token = create_session_token(secret=app_secret, ttl_seconds=60)

    assert verify_session_token(token, secret=app_secret) is True
    assert app_secret not in token


def test_session_token_rejects_wrong_or_tampered_token(app_secret):
    token = create_session_token(secret=app_secret, ttl_seconds=60)

    assert verify_session_token(
        token, secret="another-secret"
    ) is False
    assert verify_session_token(
        token + "0", secret=app_secret
    ) is False
    assert verify_session_token("garbage", secret=app_secret) is False
    assert verify_session_token("", secret=app_secret) is False
    assert verify_session_token(None, secret=app_secret) is False


def test_session_token_expires(app_secret):
    token = create_session_token(secret=app_secret, ttl_seconds=60)

    # Long after the 60 second TTL, so the token must be refused.
    assert verify_session_token(
        token, secret=app_secret, now=99999999999
    ) is False


# ------------------------------------------------------------
# REQUIRE_AUTH
# ------------------------------------------------------------

def test_require_auth_accepts_static_secret(app_secret):
    assert require_auth(
        fake_request({"X-API-Key": app_secret})
    ) == "api-key"

    assert require_auth(
        fake_request({"Authorization": f"Bearer {app_secret}"})
    ) == "api-key"


def test_require_auth_accepts_session_cookie(app_secret):
    token = create_session_token(secret=app_secret, ttl_seconds=60)

    assert require_auth(
        fake_request({"Cookie": f"{COOKIE}={token}"})
    ) == "session"


def test_require_auth_rejects_missing_or_wrong_secret(app_secret):
    for headers in ({}, {"X-API-Key": "wrong"}, {"Cookie": f"{COOKIE}=junk"}):
        with pytest.raises(HTTPException) as failure:
            require_auth(fake_request(headers))
        assert failure.value.status_code == 401


def test_require_auth_fails_closed_without_configuration(no_app_secret):
    with pytest.raises(HTTPException) as failure:
        require_auth(fake_request({"X-API-Key": "anything"}))

    assert failure.value.status_code == 503


# ------------------------------------------------------------
# DOCUMENTATION IS DISABLED
# ------------------------------------------------------------

def test_documentation_endpoints_are_disabled(meta_paths):
    with TestClient(app) as client:
        for path in (
            "/docs",
            "/redoc",
            "/openapi.json",
            "/docs/oauth2-redirect",
        ):
            assert client.get(path).status_code == 404, path


# ------------------------------------------------------------
# STEP 2 GUARD: THE API IS NOW ENFORCED
# ------------------------------------------------------------
# Step 1 shipped the primitives without attaching them. They are now
# wired to the router, so this test fails if enforcement is ever
# silently removed again. Exhaustive coverage lives in
# test_auth_enforced.py.

def test_existing_api_routes_are_guarded(meta_paths, app_secret):
    with TestClient(app) as client:
        # Public.
        assert client.get("/health").status_code == 200

        # Protected: blocked without a credential, served with one.
        assert client.get("/api/version").status_code == 401
        assert client.post(
            "/api/sessions", json={"title": "Guarded"}
        ).status_code == 401

        assert client.get(
            "/api/version", headers={"X-API-Key": app_secret}
        ).status_code == 200
        assert client.post(
            "/api/sessions",
            json={"title": "Guarded"},
            headers={"X-API-Key": app_secret},
        ).status_code == 200
