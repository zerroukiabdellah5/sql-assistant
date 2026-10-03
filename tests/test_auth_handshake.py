"""Tests for the Step 3 frontend authentication handshake.

The credential used here is the throwaway literal from conftest.py that
lives only in the test suite. No real secret is ever read from the
environment, so these tests are hermetic and safe to commit.

The handshake is deliberately credential-free in the browser: a native
HTML form posts straight to /api/auth/login-form and the server answers
with the existing HttpOnly session cookie. These tests pin that shape.
"""

import re

import pytest

from fastapi.testclient import TestClient

import app.config as config
from app.main import app

COOKIE = config.AUTH_COOKIE_NAME
LOGIN_FORM = "/api/auth/login-form"


@pytest.fixture(scope="module")
def index_source():
    """The shipped frontend, read as text for static assertions."""

    with open(config.INDEX_PATH, encoding="utf-8") as handle:
        return handle.read()


def post_form(client, password, **kwargs):
    """Submit the native form exactly as a browser would."""

    return client.post(
        LOGIN_FORM,
        data={"password": password},
        follow_redirects=False,
        **kwargs,
    )


# ------------------------------------------------------------
# 1. CORRECT PASSWORD -> 303 + THE EXISTING SESSION COOKIE
# ------------------------------------------------------------

def test_login_form_with_correct_password_redirects_to_root(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        response = post_form(client, app_secret)

        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_login_form_issues_the_existing_httponly_lax_cookie(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        header = post_form(
            client, app_secret
        ).headers["set-cookie"]
        lowered = header.lower()

        # The cookie is the one auth.py already builds, unchanged.
        assert header.startswith(f"{COOKIE}=")
        assert "httponly" in lowered
        assert "samesite=lax" in lowered
        assert "path=/" in lowered

        # Same token format as the JSON login, so require_auth accepts
        # it with no new code path.
        value = re.search(rf"{COOKIE}=([^;]+)", header).group(1)
        assert re.fullmatch(r"v1\.\d+\.[0-9a-f]{64}", value)


def test_login_form_cookie_secure_flag_follows_configuration(
    meta_paths, app_secret, monkeypatch
):
    # Local development: no Secure attribute over plain HTTP.
    with TestClient(app) as client:
        assert "secure" not in post_form(
            client, app_secret
        ).headers["set-cookie"].lower()

    # Deployed: Secure is present.
    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", True)

    with TestClient(app) as client:
        assert "secure" in post_form(
            client, app_secret
        ).headers["set-cookie"].lower()


# ------------------------------------------------------------
# 2. WRONG PASSWORD -> 401, NO SESSION
# ------------------------------------------------------------

def test_login_form_with_wrong_password_is_rejected(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        response = post_form(client, "not-the-secret")

        assert response.status_code == 401
        assert COOKIE not in response.headers.get("set-cookie", "")
        assert not client.cookies.get(COOKIE)


def test_login_form_rejection_is_generic(meta_paths, app_secret):
    """A wrong password reveals nothing about the server's setup."""

    with TestClient(app) as client:
        first = post_form(client, "not-the-secret")
        second = post_form(client, "also-wrong")

        # Every wrong credential fails identically, so the response
        # cannot be used to probe the configuration.
        assert first.status_code == second.status_code == 401
        assert first.json() == second.json()
        assert first.json()["detail"] == (
            "Invalid or missing credentials."
        )
        assert "not configured" not in first.text.lower()
        assert "not configured" not in second.text.lower()


# ------------------------------------------------------------
# 3. MISSING PASSWORD -> VALIDATION ERROR, NEVER AUTHENTICATES
# ------------------------------------------------------------

def test_login_form_with_missing_password_does_not_authenticate(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        # No field at all, and an empty field, are both rejected as
        # invalid input before any credential is compared.
        for data in ({}, {"password": ""}):
            response = client.post(
                LOGIN_FORM, data=data, follow_redirects=False
            )

            assert response.status_code == 422
            assert not client.cookies.get(COOKIE)

        # Still refused on the protected surface.
        assert client.get("/api/schema").status_code == 401


# ------------------------------------------------------------
# 4. NO CONFIGURED SECRET -> 503 (FAIL CLOSED)
# ------------------------------------------------------------

def test_login_form_without_configured_secret_fails_closed(
    meta_paths, no_app_secret
):
    with TestClient(app) as client:
        response = post_form(client, "anything")

        assert response.status_code == 503
        assert not client.cookies.get(COOKIE)


# ------------------------------------------------------------
# 5. THE ISSUED SESSION WORKS ON A PROTECTED ROUTE
# ------------------------------------------------------------

def test_cookie_from_login_form_authenticates_a_protected_route(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        header = post_form(
            client, app_secret
        ).headers["set-cookie"]
        token = re.search(rf"{COOKIE}=([^;]+)", header).group(1)

        # Replay the cookie on its own, exactly as the browser would
        # attach it to a same-origin request.
        response = client.get(
            "/api/schema",
            headers={"Cookie": f"{COOKIE}={token}"},
        )

        assert response.status_code == 200


def test_ask_stays_protected_without_a_session(anon_client):
    """The dependency rejects before any model call is attempted."""

    assert anon_client.post(
        "/api/ask", json={"prompt": "count rows"}
    ).status_code == 401


def test_tampered_session_cookie_is_refused(meta_paths, app_secret):
    """The cookie is signed, so editing it does not grant a session."""

    with TestClient(app) as client:
        header = post_form(
            client, app_secret
        ).headers["set-cookie"]
        token = re.search(rf"{COOKIE}=([^;]+)", header).group(1)
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

        assert client.get(
            "/api/schema",
            headers={"Cookie": f"{COOKIE}={tampered}"},
        ).status_code == 401


# ------------------------------------------------------------
# 6. NO SESSION -> STILL 401
# ------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/schema"),
        ("get", "/api/sessions"),
        ("get", "/api/approvals?status=pending"),
    ],
)
def test_protected_routes_still_require_a_session(
    anon_client, method, path
):
    assert getattr(anon_client, method)(path).status_code == 401


# ------------------------------------------------------------
# 7. THE SECRET IS NEVER ECHOED ANYWHERE
# ------------------------------------------------------------

def test_login_form_never_returns_the_secret(meta_paths, app_secret):
    with TestClient(app) as client:
        accepted = post_form(client, app_secret)
        refused = post_form(client, "not-the-secret")

        for response in (accepted, refused):
            assert app_secret not in response.text
            assert app_secret not in response.headers.get(
                "location", ""
            )
            assert app_secret not in response.headers.get(
                "set-cookie", ""
            )

        # The redirect carries no query string, so the submitted
        # credential cannot ride along in the URL.
        assert accepted.headers["location"] == "/"


# ------------------------------------------------------------
# 8 + 9. THE EXISTING JSON LOGIN AND LOGOUT ARE UNCHANGED
# ------------------------------------------------------------

def test_json_login_still_works(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"password": app_secret}
        )

        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert client.cookies.get(COOKIE)


def test_logout_still_clears_the_cookie(meta_paths, app_secret):
    with TestClient(app) as client:
        post_form(client, app_secret)
        assert client.cookies.get(COOKIE)

        response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json()["authenticated"] is False
        assert "max-age=0" in response.headers["set-cookie"].lower()


# ------------------------------------------------------------
# THE FRONTEND NEVER RECEIVES THE SECRET
# ------------------------------------------------------------

def test_index_never_mentions_the_secret(index_source):
    assert "APP_API_KEY" not in index_source


def test_login_form_is_native_and_never_read_by_script(
    index_source,
):
    source = index_source

    # A real native form posting straight to the endpoint.
    assert re.search(
        r'<form[^>]*method="post"[^>]*action="/api/auth/login-form"',
        source,
    )
    assert re.search(
        r'<input[^>]*type="password"[^>]*name="password"', source
    )
    assert 'autocomplete="current-password"' in source

    # No script captures the submit or the field, so the credential
    # can only ever be posted by the browser itself.
    assert not re.search(r"onsubmit", source)
    assert not re.search(
        r"addEventListener\(\s*['\"]submit", source
    )
    assert not re.search(
        r"(querySelector|getElementById)[^\n]*password", source
    )

    # Login is not performed with fetch().
    assert not re.search(
        r"fetch\([^)]*login-form", source, re.DOTALL
    )
