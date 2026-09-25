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