# ============================================================
# APPROVAL LEDGER
# ============================================================
# APPROVAL-classified operations are recorded here and never run
# silently. A request can be approved or rejected explicitly.
# ============================================================

import uuid

from app.history import _connect, _now
from app.safety import APPROVAL, describe_operation


PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"


def create_approval(kind, sql, context=""):

    approval_id = uuid.uuid4().hex

    connection = _connect()

    try:

        connection.execute(
            """
            INSERT INTO approvals (id, kind, sql, context, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                kind,
                sql,
                context or describe_operation(sql),
                PENDING,
                _now(),
            ),
        )

        connection.commit()

        return get_approval(approval_id)

    finally:

        connection.close()


def get_approval(approval_id):

    connection = _connect()

    try:

        row = connection.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()

        return dict(row) if row else None

    finally:

        connection.close()


def list_approvals(status=None, limit=50):

    connection = _connect()

    try:

        if status:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                WHERE status = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM approvals
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [dict(row) for row in rows]

    finally:

        connection.close()


def set_approval_status(approval_id, status):

    connection = _connect()

    try:

        cursor = connection.execute(
            """
            UPDATE approvals
            SET status = ?, decided_at = ?
            WHERE id = ?
            """,
            (status, _now(), approval_id),
        )

        connection.commit()

        return cursor.rowcount > 0

    finally:

        connection.close()


def approve_approval(approval_id):
    return set_approval_status(approval_id, APPROVED)


def reject_approval(approval_id):
    return set_approval_status(approval_id, REJECTED)


def pending_approvals():
    return list_approvals(status=PENDING)


# ============================================================
# VISITOR DEMO APPROVALS (PER-BROWSER, NO CREDENTIAL)
# ============================================================
# The approval ledger is an OWNER surface: rows are written by the
# application when an operation is classified APPROVAL, and they stay
# behind APP_API_KEY. An anonymous visitor must not read that ledger,
# so the demo cannot reuse it.
#
# Instead each browser gets its own copy of a small, fixed set of
# EXAMPLE operations - clearly illustrative, never anyone's real data -
# seeded into its own namespace on first view. The visitor approves or
# rejects those examples, and the decision is stored per visitor. This
# demonstrates the workflow end to end without exposing a single owner
# row and without inventing a permanent shared table.
#
# Seeding is idempotent and race-safe: the ids are fixed strings and
# the primary key is (visitor_id, id), so INSERT OR IGNORE can run on
# every view without duplicating a row.

DEMO_APPROVALS = (
    {
        "id": "demo-ddl-001",
        "kind": "ddl",
        "sql": "ALTER TABLE products ADD COLUMN discount REAL DEFAULT 0;",
        "context": (
            "Example operation: a schema change is classified APPROVAL "
            "and never runs silently."
        ),
    },
    {
        "id": "demo-dml-002",
        "kind": "dml",
        "sql": "UPDATE products SET stock = 0 WHERE category_id = 2;",
        "context": (
            "Example operation: a bulk update waits for an explicit "
            "human decision before it can run."
        ),
    },
    {
        "id": "demo-dml-003",
        "kind": "dml",
        "sql": "DELETE FROM orders WHERE order_date < '2000-01-01';",
        "context": (
            "Example operation: a destructive statement is logged here "
            "for review before anything is executed."
        ),
    },
)


def _ensure_visitor_approvals(connection, visitor_id):
    """Seed this visitor's example approvals, once, without duplicates."""

    timestamp = _now()

    for item in DEMO_APPROVALS:

        connection.execute(
            """
            INSERT OR IGNORE INTO visitor_approvals
                (visitor_id, id, kind, sql, context,
                 status, created_at, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                visitor_id,
                item["id"],
                item["kind"],
                item["sql"],
                item["context"],
                PENDING,
                timestamp,
            ),
        )

    connection.commit()


def list_visitor_approvals(visitor_id, status=None, limit=50):

    connection = _connect()

    try:

        _ensure_visitor_approvals(connection, visitor_id)

        if status:

            rows = connection.execute(
                """
                SELECT id, kind, sql, context, status,
                       created_at, decided_at
                FROM visitor_approvals
                WHERE visitor_id = ? AND status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (visitor_id, status, limit),
            ).fetchall()

        else:

            rows = connection.execute(
                """
                SELECT id, kind, sql, context, status,
                       created_at, decided_at
                FROM visitor_approvals
                WHERE visitor_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (visitor_id, limit),
            ).fetchall()

        return [dict(row) for row in rows]

    finally:

        connection.close()


def get_visitor_approval(visitor_id, approval_id):

    connection = _connect()

    try:

        _ensure_visitor_approvals(connection, visitor_id)

        row = connection.execute(
            """
            SELECT id, kind, sql, context, status,
                   created_at, decided_at
            FROM visitor_approvals
            WHERE visitor_id = ? AND id = ?
            """,
            (visitor_id, approval_id),
        ).fetchone()

        return dict(row) if row else None

    finally:

        connection.close()


def decide_visitor_approval(visitor_id, approval_id, status):
    """Record this visitor's decision. Returns the row, or None if the
    id is not one of the visitor's own example approvals."""

    connection = _connect()

    try:

        _ensure_visitor_approvals(connection, visitor_id)

        cursor = connection.execute(
            """
            UPDATE visitor_approvals
            SET status = ?, decided_at = ?
            WHERE visitor_id = ? AND id = ?
            """,
            (status, _now(), visitor_id, approval_id),
        )

        connection.commit()

        if cursor.rowcount <= 0:
            return None

        return get_visitor_approval(visitor_id, approval_id)

    finally:

        connection.close()