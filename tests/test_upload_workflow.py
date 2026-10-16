"""The Upload workflow: the picker opens and the owner route stays owner-only.

Reported defect: a visitor who clicked Upload was shown the
"This workspace is protected - sign in" dialog. That dialog is the
frontend's reaction to a 401 from ``require_auth``, and it appeared
because BOTH opening the import panel and posting an import fed their
401 into ``handleAuthFailure()``. The browser holds no owner credential
that could succeed there - the application access key never enters
JavaScript - so there was nothing to ask the visitor for.

The fix moved the browser's upload workflow onto the VISITOR upload
routes (POST/GET/DELETE /api/visitor/imports and GET /api/visitor/schema),
which need no credential and file everything under the caller's own
signed visitor cookie. The visitor-side behaviour itself - that an
anonymous browser can upload, list, select, query and delete its own
data, and cannot reach anyone else's - is pinned in
tests/test_visitor_uploads.py.

This file pins the two things that did NOT move:

  * the OWNER routes /api/upload and /api/imports are still on the
    guarded router: 401 with no credential, 401 with a wrong one, and
    nothing stored on a refusal;
  * the frontend uses the visitor routes and never opens the sign-in
    gate or the access-code dialog from the upload workflow.

The rate limit on the owner route is pinned separately, in
tests/test_ratelimit.py (``test_upload_is_limited_before_the_handler_runs``),
and SQL mutation blocking in tests/test_import_queries.py.
"""

import os
import re
import sqlite3

import pytest

from fastapi.testclient import TestClient

import app.config as config
import app.uploads as uploads

from app.main import api, app, visitor_router

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# HELPERS
# ============================================================

def _sqlite_bytes(tmp_path):
    """A minimal, valid SQLite database to post as an upload."""

    path = os.path.join(str(tmp_path), "sample.db")

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE items (id INTEGER, label TEXT)"
    )
    connection.execute(
        "INSERT INTO items VALUES (1, 'alpha')"
    )
    connection.commit()
    connection.close()

    with open(path, "rb") as handle:
        return handle.read()


def _upload(client, content, filename="up.db"):
    """Post to the OWNER upload route, deliberately, by its own path."""

    return client.post(
        "/api/upload",
        files={
            "file": (filename, content, "application/octet-stream")
        },
    )


@pytest.fixture(scope="module")
def index_source():
    """The shipped frontend, read as text for static assertions."""

    with open(config.INDEX_PATH, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace the provider call so no test touches the network."""

    def fake_ask(prompt, turns, request_id=None, generation=None):
        return "SELECT 1", "ok"

    monkeypatch.setattr("app.main.ask_active", fake_ask)


def _function_body(source, name):
    """The body of a top-level JavaScript function, by brace matching.

    index.html is one large document, so an assertion has to be scoped
    to the function under test instead of matching the same word
    somewhere else in the file.
    """

    match = re.search(
        r"\bfunction\s+" + re.escape(name) + r"\s*\(", source
    )

    assert match, name + " is not defined in index.html"

    start = source.index("{", match.end())
    depth = 0
    index = start

    while index < len(source):

        character = source[index]

        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]

        index += 1

    raise AssertionError(name + " has unbalanced braces")


def _markup_between(source, start_marker, end_marker):
    """The slice of index.html between two literal markers."""

    start = source.index(start_marker)
    end = source.index(end_marker, start)

    return source[start:end]


# ============================================================
# THE OWNER ROUTES ARE STILL OWNER-ONLY
# ============================================================

def test_upload_without_a_credential_is_refused(
    anon_client, tmp_path
):
    """No credential is refused, and the refusal stores nothing."""

    before = uploads.list_imports()

    response = _upload(anon_client, _sqlite_bytes(tmp_path))

    assert response.status_code == 401
    assert "credential" in response.json()["detail"].lower()
    assert uploads.list_imports() == before


def test_upload_with_a_wrong_credential_is_refused(
    anon_client, tmp_path
):
    """A forged key, bearer token or cookie buys nothing."""

    content = _sqlite_bytes(tmp_path)

    forged = [
        {"X-API-Key": "not-the-secret"},
        {"Authorization": "Bearer not-the-secret"},
        {
            "Cookie": (
                config.AUTH_COOKIE_NAME + "=forged.token.value"
            )
        },
    ]

    for headers in forged:

        response = _upload(anon_client, content, filename="forged.db")

        assert response.status_code == 401, headers
        assert config.APP_API_KEY not in response.text


def test_the_application_key_imports_and_the_import_is_listed(
    authed_client, tmp_path
):
    response = _upload(authed_client, _sqlite_bytes(tmp_path))

    assert response.status_code == 200

    body = response.json()

    assert body["kind"] == "database"
    assert body["table_count"] == 1
    assert body["tables"] == ["items"]

    listed = authed_client.get("/api/imports").json()["imports"]

    assert any(item["id"] == body["id"] for item in listed)


def test_a_session_cookie_alone_imports(
    meta_paths, app_secret, tmp_path
):
    """The browser path: the HttpOnly cookie, no header at all."""

    with TestClient(app) as client:

        assert client.post(
            "/api/auth/login", json={"password": app_secret}
        ).status_code == 200

        response = _upload(client, _sqlite_bytes(tmp_path))

        assert response.status_code == 200


def test_unlocking_the_trial_does_not_unlock_the_owner_route(
    anon_client, tmp_path
):
    """The visitor gate and the owner surface are separate credentials.

    The App Access Code admits a visitor to /api/ask. It must not
    authenticate the guarded workspace API, and this is the assertion
    that keeps the two gates from being merged.
    """

    unlocked = anon_client.post(
        "/api/access/unlock",
        json={"code": config.APP_ACCESS_CODE},
    )

    assert unlocked.status_code == 200
    assert anon_client.cookies.get(config.ACCESS_COOKIE_NAME)

    assert anon_client.get("/api/imports").status_code == 401

    assert _upload(
        anon_client, _sqlite_bytes(tmp_path)
    ).status_code == 401


def test_the_upload_route_is_still_guarded_by_the_router():
    """The guard is inherited, not copied into the handler."""

    names = None

    for route in api.routes:

        if getattr(route, "path", None) == "/api/upload":

            names = [
                dependency.call.__name__
                for dependency in route.dependant.dependencies
            ]

    assert names is not None, "POST /api/upload is missing"
    assert names.count("require_auth") == 1


def test_the_owner_routes_did_not_become_public():
    """Only the /api/visitor/* copies are public, not the originals."""

    public = {
        getattr(route, "path", "")
        for route in visitor_router.routes
    }

    assert "/api/upload" not in public
    assert "/api/imports" not in public
    assert "/api/ask" in public
    assert "/api/visitor/imports" in public


# ============================================================
# THE FRONTEND CONTRACT
# ============================================================

def test_clicking_upload_opens_the_file_picker_directly(
    index_source
):
    body = _function_body(index_source, "openWorkspace")

    assert 'which === "upload"' in body
    assert "openUploadPicker()" in body

    picker = _function_body(index_source, "openUploadPicker")

    assert 'getElementById("uploadInput")' in picker
    assert ".click()" in picker


def test_opening_the_upload_panel_never_opens_a_dialog(index_source):
    body = _function_body(index_source, "openWorkspace")

    assert "showSignIn" not in body
    assert "showAccessModal" not in body
    assert "handleAuthFailure" not in body


def test_importing_uses_the_visitor_route_and_never_a_dialog(
    index_source
):
    """handleUpload must not touch the owner route or a dialog."""

    body = _function_body(index_source, "handleUpload")

    assert '"/api/visitor/imports"' in body
    assert '"/api/upload"' not in body
    assert "showSignIn" not in body
    assert "showAccessModal" not in body
    assert "handleAuthFailure" not in body
    assert "renderUploadLocked" not in body


def test_the_import_list_and_schema_use_the_visitor_routes(
    index_source
):
    listing = _function_body(index_source, "loadImports")

    assert '"/api/visitor/imports"' in listing
    assert '"/api/imports"' not in listing

    schema = _function_body(index_source, "sourceSchemaUrl")

    assert "/api/visitor/schema?import_id=" in schema
    assert '"/api/schema?import_id="' not in schema

    removing = _function_body(index_source, "deleteImport")

    assert "/api/visitor/imports/" in removing
    assert '"/api/imports/"' not in removing


def test_the_locked_state_helpers_are_gone(index_source):
    """The owner-feature notices no longer exist, so they cannot return."""

    assert "renderImportsLocked" not in index_source
    assert "renderUploadLocked" not in index_source


def test_the_upload_surface_offers_no_access_code_field(index_source):
    """The panel a visitor sees contains no credential input at all."""

    panel = _markup_between(
        index_source,
        'id="uploadBackdrop"',
        "<!-- ================= APPROVALS",
    )

    assert "accessCodeInput" not in panel
    assert 'type="password"' not in panel
    assert "APP_ACCESS_CODE" not in panel


def test_the_frontend_holds_no_server_credential(index_source):
    """No secret, no header: the browser has nothing to sign with."""

    for name in (
        "openWorkspace",
        "openUploadPicker",
        "loadImports",
        "handleUpload",
        "deleteImport",
        "sourceSchemaUrl",
        "showSignIn",
        "handleAuthFailure",
    ):

        body = _function_body(index_source, name)

        assert "APP_API_KEY" not in body, name
        assert "X-API-Key" not in body, name
        assert "x-api-key" not in body, name
        assert "Bearer" not in body, name

    assert "APP_API_KEY" not in index_source
    assert "APP_ACCESS_CODE" not in index_source


def test_the_trial_dialog_is_untouched_by_this_fix(index_source):
    """/api/ask keeps its own gate: six questions, then the dialog."""

    assert "FREE_TRIAL_EXHAUSTED" in index_source
    assert "showAccessModal(" in index_source


def test_the_trial_still_runs_six_then_refuses(
    anon_client, stub_provider
):
    """A compact guard that the upload fix changed no trial arithmetic."""

    for _ in range(config.TRIAL_MAX_ATTEMPTS):

        assert anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        ).status_code == 200

    refused = anon_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert refused.status_code == 403
    assert refused.json()["detail"] == "FREE_TRIAL_EXHAUSTED"
