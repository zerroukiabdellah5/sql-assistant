"""Enforcement tests: every /api route outside /api/auth is guarded.

These tests discover the protected endpoints from the live route table
rather than a hand-written list, so a route added later is covered
automatically. No handler is expected to perform its own check: the
guard is attached once, to the router.
"""

import re

import pytest

from fastapi.testclient import TestClient

import app.config as config
from app.main import api, app, auth_router

AUTH_PREFIX = "/api/auth"


# ------------------------------------------------------------
# ROUTE DISCOVERY
# ------------------------------------------------------------

def leaf_routes(router):
    """Yield leaf routes, unwrapping lazy included-router wrappers.

    FastAPI 0.141 keeps include_router() results as _IncludedRouter
    objects instead of flattening them into app.routes, so iterating
    app.routes directly would miss every mounted router.
    """

    for route in router.routes:
        original = getattr(route, "original_router", None)

        if original is not None:
            yield from leaf_routes(original)
            continue

        if getattr(route, "path", None) is not None:
            yield route


def concrete(path):
    """Replace path parameters so a request can actually be issued."""

    return re.sub(r"\{[^}]+\}", "unknown-id", path)


def discover_protected():
    """Every /api endpoint that is not a public auth endpoint."""

    found = set()

    for route in leaf_routes(app):
        path = route.path

        if not path.startswith("/api/"):
            continue

        if path.startswith(AUTH_PREFIX):
            continue

        for method in route.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            found.add((method, path, concrete(path)))

    return sorted(found)


PROTECTED_ENDPOINTS = discover_protected()

IDS = [f"{method} {path}" for method, path, _ in PROTECTED_ENDPOINTS]


# ------------------------------------------------------------
# THE GUARD IS ROUTER-LEVEL, NOT PER-HANDLER
# ------------------------------------------------------------

def test_discovery_found_the_protected_surface():
    """Fails loudly if discovery silently returns nothing."""

    assert len(PROTECTED_ENDPOINTS) == 16
    assert ("POST", "/api/ask", "/api/ask") in PROTECTED_ENDPOINTS
    assert ("POST", "/api/upload", "/api/upload") in PROTECTED_ENDPOINTS
    assert ("POST", "/api/report", "/api/report") in PROTECTED_ENDPOINTS
    assert ("GET", "/api/version", "/api/version") in PROTECTED_ENDPOINTS


def test_guard_is_attached_once_to_the_router():
    assert len(api.dependencies) == 1
    assert auth_router.dependencies == []


def test_every_protected_route_carries_exactly_one_guard():
    """One inherited guard per route, not a check copied into handlers."""

    for route in api.routes:
        names = [
            dependency.call.__name__
            for dependency in route.dependant.dependencies
        ]
        assert names.count("require_auth") == 1, route.path


# ------------------------------------------------------------
# UNAUTHENTICATED REQUESTS ARE BLOCKED
# ------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,target", PROTECTED_ENDPOINTS, ids=IDS
)
def test_protected_route_rejects_missing_credentials(
    anon_client, method, path, target
):
    response = anon_client.request(method, target)

    assert response.status_code == 401, (
        f"{method} {path} served an unauthenticated request"
    )


@pytest.mark.parametrize(
    "method,path,target", PROTECTED_ENDPOINTS, ids=IDS
)
def test_protected_route_rejects_wrong_credentials(
    anon_client, method, path, target
):
    forged = [
        {"X-API-Key": "not-the-secret"},
        {"Authorization": "Bearer not-the-secret"},
        {"Cookie": f"{config.AUTH_COOKIE_NAME}=forged.token.value"},
    ]

    for headers in forged:
        response = anon_client.request(
            method, target, headers=headers
        )
        assert response.status_code == 401, (
            f"{method} {path} accepted {headers}"
        )


@pytest.mark.parametrize(
    "method,path,target", PROTECTED_ENDPOINTS, ids=IDS
)
def test_protected_route_accepts_valid_credentials(
    authed_client, method, path, target
):
    """Auth passes and control reaches the handler layer.

    The handler may still answer 400/404/422 for its own reasons; what
    must never happen is 401 or 503.
    """

    response = authed_client.request(method, target)

    assert response.status_code not in (401, 503), (
        f"{method} {path} was rejected by the auth layer"
    )


# ------------------------------------------------------------
# POST /api/ask IN DETAIL
# ------------------------------------------------------------

def test_ask_without_credentials_is_unauthorized(anon_client):
    response = anon_client.post("/api/ask", json={"prompt": "anything"})

    assert response.status_code == 401


def test_ask_with_wrong_credentials_is_unauthorized(anon_client):
    for headers in (
        {"X-API-Key": "not-the-secret"},
        {"Authorization": "Bearer not-the-secret"},
    ):
        response = anon_client.post(
            "/api/ask",
            json={"prompt": "anything"},
            headers=headers,
        )
        assert response.status_code == 401


def test_ask_with_valid_credentials_reaches_the_handler(authed_client):
    # 400 is the handler's own "Prompt cannot be empty" rule, which
    # proves authentication passed and the business logic ran.
    response = authed_client.post("/api/ask", json={"prompt": "   "})

    assert response.status_code == 400
    assert response.json()["detail"] == "Prompt cannot be empty."


def test_ask_accepts_a_session_cookie_from_login(meta_paths, app_secret):
    """The HttpOnly cookie mechanism still grants access end to end."""

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login", json={"password": app_secret}
        )
        assert login.status_code == 200
        assert client.cookies.get(config.AUTH_COOKIE_NAME)

        # No header is sent here: the cookie alone must authenticate.
        response = client.post("/api/ask", json={"prompt": "   "})
        assert response.status_code == 400


def test_ask_session_cookie_stops_working_after_logout(
    meta_paths, app_secret
):
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login", json={"password": app_secret}
        ).status_code == 200

        assert client.post(
            "/api/ask", json={"prompt": "   "}
        ).status_code == 400

        assert client.post("/api/auth/logout").status_code == 200
        assert not client.cookies.get(config.AUTH_COOKIE_NAME)

        assert client.post(
            "/api/ask", json={"prompt": "   "}
        ).status_code == 401


# ------------------------------------------------------------
# STATE-CHANGING ENDPOINTS
# ------------------------------------------------------------

STATE_CHANGING = [
    ("POST", "/api/ask", "/api/ask"),
    ("POST", "/api/upload", "/api/upload"),
    ("POST", "/api/sessions", "/api/sessions"),
    (
        "DELETE",
        "/api/sessions/{session_id}",
        "/api/sessions/unknown-id",
    ),
    ("POST", "/api/approvals", "/api/approvals"),
    (
        "POST",
        "/api/approvals/{approval_id}/approve",
        "/api/approvals/unknown-id/approve",
    ),
    (
        "POST",
        "/api/approvals/{approval_id}/reject",
        "/api/approvals/unknown-id/reject",
    ),
    (
        "DELETE",
        "/api/imports/{import_id}",
        "/api/imports/unknown-id",
    ),
    ("POST", "/api/report", "/api/report"),
]

STATE_IDS = [f"{m} {p}" for m, p, _ in STATE_CHANGING]


@pytest.mark.parametrize(
    "method,path,target", STATE_CHANGING, ids=STATE_IDS
)
def test_state_changing_endpoints_are_protected(
    anon_client, method, path, target
):
    assert anon_client.request(method, target).status_code == 401


def test_blocked_delete_does_not_destroy_a_session(
    authed_client, anon_client
):
    """A rejected mutating request must leave the data untouched."""

    created = authed_client.post(
        "/api/sessions", json={"title": "Must survive"}
    )
    assert created.status_code == 200
    session_id = created.json()["id"]

    blocked = anon_client.delete(f"/api/sessions/{session_id}")
    assert blocked.status_code == 401

    assert authed_client.get(
        f"/api/sessions/{session_id}"
    ).status_code == 200


# ------------------------------------------------------------
# PUBLIC SURFACE STAYS PUBLIC
# ------------------------------------------------------------

def test_home_is_public(anon_client):
    assert anon_client.get("/").status_code == 200


def test_health_is_public(anon_client):
    response = anon_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_is_public(meta_paths, app_secret):
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"password": app_secret}
        )
        assert response.status_code == 200


def test_logout_is_public(anon_client):
    assert anon_client.post("/api/auth/logout").status_code == 200


def test_login_is_not_behind_the_guard(meta_paths, no_app_secret):
    """Login answers 503 when unconfigured, never 401.

    A 401 here would mean the guard had been attached to the auth
    router and the key could never be presented at all.
    """

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"password": "anything"}
        )
        assert response.status_code == 503


def test_documentation_endpoints_remain_disabled(anon_client):
    for path in (
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
    ):
        assert anon_client.get(path).status_code == 404


# ------------------------------------------------------------
# FAIL CLOSED
# ------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,target", PROTECTED_ENDPOINTS, ids=IDS
)
def test_protected_route_fails_closed_without_configuration(
    meta_paths, no_app_secret, method, path, target
):
    with TestClient(app) as client:
        response = client.request(method, target)

        assert response.status_code == 503, (
            f"{method} {path} did not fail closed"
        )


def test_credentials_do_not_help_when_unconfigured(
    meta_paths, no_app_secret
):
    """A guess is still refused; misconfiguration is not a bypass."""

    with TestClient(
        app, headers={"X-API-Key": "guess"}
    ) as client:
        assert client.post(
            "/api/ask", json={"prompt": "x"}
        ).status_code == 503


# ------------------------------------------------------------
# THE SECRET IS NEVER REFLECTED
# ------------------------------------------------------------

def test_auth_failures_never_echo_the_secret(anon_client, app_secret):
    near_miss = app_secret[:-1] + "x"

    for headers in (
        {},
        {"X-API-Key": "not-the-secret"},
        {"X-API-Key": near_miss},
        {"Authorization": f"Bearer {near_miss}"},
    ):
        response = anon_client.post(
            "/api/ask", json={"prompt": "x"}, headers=headers
        )
        assert response.status_code == 401
        assert app_secret not in response.text
