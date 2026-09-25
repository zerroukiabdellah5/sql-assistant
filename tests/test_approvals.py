from app import approvals


def test_create_and_get_approval(meta_paths):
    record = approvals.create_approval(
        "script", "INSERT INTO t (id) VALUES (1)",
        context="import script",
    )
    assert record["id"]
    assert record["kind"] == "script"
    assert record["status"] == approvals.PENDING
    assert approvals.get_approval(record["id"])["status"] == approvals.PENDING


def test_pending_listing(meta_paths):
    first = approvals.create_approval(
        "mutation", "DELETE FROM t"
    )
    second = approvals.create_approval(
        "script", "DROP TABLE x"
    )
    pending = approvals.pending_approvals()
    assert len(pending) == 2
    assert pending[0]["id"] == second["id"]
    assert first["id"] in {p["id"] for p in pending}


def test_approve_workflow(meta_paths):
    record = approvals.create_approval("mutation", "DELETE FROM t")
    assert approvals.approve_approval(record["id"]) is True
    updated = approvals.get_approval(record["id"])
    assert updated["status"] == approvals.APPROVED
    assert updated["decided_at"]


def test_reject_workflow(meta_paths):
    record = approvals.create_approval("mutation", "DELETE FROM t")
    assert approvals.reject_approval(record["id"]) is True
    assert approvals.get_approval(record["id"])["status"] == approvals.REJECTED


def test_approve_missing_returns_false(meta_paths):
    assert approvals.approve_approval("missing") is False


def test_list_filter_by_status(meta_paths):
    one = approvals.create_approval("a", "DELETE FROM t1")
    two = approvals.create_approval("b", "DELETE FROM t2")
    approvals.approve_approval(one["id"])
    approved = approvals.list_approvals(status=approvals.APPROVED)
    assert [a["id"] for a in approved] == [one["id"]]
    pending = approvals.pending_approvals()
    assert [a["id"] for a in pending] == [two["id"]]