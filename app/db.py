# ============================================================
# DATABASE CONNECTION + SAFE EXECUTION
# ============================================================

import os
import sqlite3

from urllib.parse import quote

from app.config import (
    DATABASE_PATH,
    DB_BUSY_TIMEOUT_MS,
    MAX_ROWS,
)
from app.validate import validate_sql


def _file_uri(path):

    # Windows paths contain backslashes and drive colons; build a
    # valid file: URI that sqlite3 can open with uri=True.
    return "file:" + quote(os.path.abspath(path).replace("\\", "/"))


def get_connection(path=None, read_only=False):

    database = path or DATABASE_PATH

    if not os.path.exists(database):
        raise FileNotFoundError(
            f"Database not found: {database}"
        )

    if read_only:
        connection = sqlite3.connect(
            _file_uri(database) + "?mode=ro",
            uri=True,
            timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
        )
    else:
        connection = sqlite3.connect(
            database,
            timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
        )

    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {int(DB_BUSY_TIMEOUT_MS)}")
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


# ============================================================
# QUERY PLAN VERIFICATION
# ============================================================

def verify_query_plan(sql, path=None, read_only=True):
    """Compile the statement with EXPLAIN QUERY PLAN.

    Raises the same errors sqlite3 would raise for a syntax error or a
    reference to a missing table/column, without ever executing the
    statement body. Used to gate auto-executed queries.
    """

    validate_sql(sql)

    connection = get_connection(path, read_only=read_only)

    try:
        return connection.execute(
            f"EXPLAIN QUERY PLAN {sql}"
        ).fetchall()

    finally:
        connection.close()


# ============================================================
# EXECUTE SQL (AUTO READ-ONLY PATH)
# ============================================================

def execute_sql(sql, path=None):
    """Validate, plan-check, then execute a read-only SELECT/WITH."""

    validate_sql(sql)
    verify_query_plan(sql, path=path, read_only=True)

    connection = get_connection(path, read_only=True)

    try:

        cursor = connection.cursor()
        cursor.execute(sql)

        rows = []
        truncated = False

        while True:

            batch = cursor.fetchmany(500)

            if not batch:
                break

            rows.extend(batch)

            if len(rows) >= MAX_ROWS:

                truncated = True
                rows = rows[:MAX_ROWS]
                break

        return (
            [
                dict(row)
                for row in rows
            ],
            truncated,
        )

    finally:

        connection.close()