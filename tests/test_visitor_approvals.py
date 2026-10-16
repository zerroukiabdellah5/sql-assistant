"""Anonymous visitor Approvals: open access, isolation and the untouched gates.

The requirement these tests exist to prove:

  * the Approvals panel works for a browser that holds no credential and
    never opens a sign-in;
  * each visitor sees their own copy of a small, fixed set of EXAMPLE
    operations and their own decisions - and can never read an owner
    approval row;
  * the owner approval ledger is unchanged and still behind the guard.

They run against the real application and assert over HTTP.
"""

import pytest

from fastapi.testclient import TestClient

import app.approvals as approvals
import app.config as config

from app.main import app


# ============================================================
# HELPERS
# ============================================================

def _list(client, status=None):
    params = {"status": status} if status else None
    return client.get("/api/visitor/approvals", params=params)


def _demo_ids():
    return [item["id"] for item in approvals.DEMO_APPROVALS]


# ============================================================
# AN ANONYMOUS VISITOR CAN LIST AND DECIDE
# ============================================================

def test_anonymous_approvals_need_no_credential(anon_client):
    response = _list(anon_client)

    assert response.status_code == 200

    listed = response.json()["approvals"]

    assert {item["id"] for item in listed} == set(_demo_ids())
    assert all(item["status"] == approvals.PENDING for item in listed)

    assert anon_client.cookies.get(config.VISITOR_COOKIE_NAME)


def test_seeding_is_idempotent(anon_client):
    assert len(_list(anon_client).json()["approvals"]) == len(
        approvals.DEMO_APPROVALS
    )
    assert len(_list(anon_client).json()["approvals"]) == len(
        approvals.DEMO_APPROVALS
    )


def test_an_anonymous_visitor_can_approve_an_example(anon_client):
    approval_id = _demo_ids()[0]

    response = anon_client.post(
        f"/api/visitor/approvals/{approval_id}/approve"
    )

    assert response.status_code == 200
    assert response.json()["status"] == approvals.APPROVED
    assert response.json()["decided_at"]


def test_an_anonymous_visitor_can_reject_an_example(anon_client):
    approval_id = _demo_ids()[1]

    response = anon_client.post(
        f"/api/visitor/approvals/{approval_id}/reject"
    )

    assert response.status_code == 200
    assert response.json()["status"] == approvals.REJECTED


def test_filtering_by_status(anon_client):
    approved = _demo_ids()[0]
    rejected = _demo_ids()[1]

    anon_client.post(f"/api/visitor/approvals/{approved}/approve")
    anon_client.post(f"/api/visitor/approvals/{rejected}/reject")

    approved_list = _list(
        anon_client, status=approvals.APPROVED
    ).json()["approvals"]

    assert [item["id"] for item in approved_list] == [approved]

    pending = _list(
        anon_client, status=approvals.PENDING
    ).json()["approvals"]

    assert [item["id"] for item in pending] == [_demo_ids()[2]]


def test_an_invalid_status_is_refused(anon_client):
    assert _list(anon_client, status="nonsense").status_code == 400


def test_an_unknown_approval_is_a_404(anon_client):
    assert anon_client.post(
        "/api/visitor/approvals/deadbeef0000/approve"
    ).status_code == 404


# ============================================================
# ONE VISITOR NEVER SEES ANOTHER'S DECISIONS
# ============================================================

def test_a_second_visitor_gets_a_fresh_ledger(meta_paths, app_secret):
    with TestClient(app) as first, TestClient(app) as second:

        approval_id = _demo_ids()[0]

        assert first.post(
            f"/api/visitor/approvals/{approval_id}/approve"
        ).status_code == 200

        # The second browser's copy is still pending.
        listed = second.get("/api/visitor/approvals").json()["approvals"]

        assert all(
            item["status"] == approvals.PENDING for item in listed
        )

        # And the first browser's decision survives the second's view.
        assert first.get(
            "/api/visitor/approvals",
            params={"status": approvals.APPROVED},
        ).json()["approvals"][0]["id"] == approval_id


def test_an_owner_approval_is_never_exposed_to_a_visitor(
    authed_client, anon_client
):
    created = authed_client.post(
        "/api/approvals",
        json={
            "kind": "script",
            "sql": "DROP TABLE secret",
            "context": "owner only",
        },
    )

    assert created.status_code == 200

    listed = _list(anon_client).json()["approvals"]

    assert created.json()["id"] not in {item["id"] for item in listed}
    assert "DROP TABLE secret" not in anon_client.get(
        "/api/visitor/approvals"
    ).text


# ============================================================
# THE OWNER LEDGER IS UNCHANGED
# ============================================================

def test_the_owner_approvals_route_is_still_protected(anon_client):
    assert anon_client.get("/api/approvals").status_code == 401

    assert anon_client.post(
        "/api/approvals",
        json={"kind": "dml", "sql": "DELETE FROM t"},
    ).status_code == 401


# ============================================================
# FAIL CLOSED
# ============================================================

def test_the_visitor_approval_routes_fail_closed_without_a_secret(
    meta_paths, no_app_secret
):
    with TestClient(app) as client:

        assert client.get("/api/visitor/approvals").status_code == 503
