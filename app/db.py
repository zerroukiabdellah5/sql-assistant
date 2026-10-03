# ============================================================
# DATABASE CONNECTION + SAFE EXECUTION
# ============================================================
# The read path is also where result provenance is measured.
# execute_sql_with_metadata() reports the file it opened, when the
# statement ran, how long it took and how many rows came back; see
# app/provenance.py for what may be derived from those facts, and for
# what provenance deliberately does not claim.
# ============================================================

import os
import sqlite3

from urllib.parse import quote

from app import provenance
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

# How many rows are pulled from SQLite per round trip. An internal
# detail: it never changes what the caller sees, only how many
# fetchmany calls it takes to get there.
_FETCH_BATCH = 500


class Execution:
    """What happened while one validated statement was executed.

    These are observations, not verdicts. Nothing here says the
    statement was correct, only which file it ran against, when, how
    long it took and how many rows came back.

    database_path
        The file actually opened, kept server-side so provenance can
        derive a basename and a digest from it. It is never returned
        to a client.
    executed_at
        UTC ISO-8601 time at which execution started, taken
        immediately before the statement reached SQLite. elapsed_ms
        covers the whole statement, so the pair brackets the real work.
        Neither includes validation, plan checking or connection setup.
    rows_returned
        Rows the caller received, after MAX_ROWS was applied.
    total_matched
        Rows the statement produced. Set only when the fetch loop ran
        to exhaustion and the cursor therefore had nothing left; null
        whenever the result set was cut short. No COUNT(*) wrapper and
        no second execution, because wrapping a statement changes what
        LIMIT, DISTINCT, GROUP BY, aggregates, CTEs and UNION mean.
    truncated
        True only when MAX_ROWS dropped at least one row that existed.
    """

    __slots__ = (
        "database_path",
        "executed_at",
        "elapsed_ms",
        "rows_returned",
        "total_matched",
        "truncated",
    )

    def __init__(
        self,
        database_path=None,
        executed_at=None,
        elapsed_ms=None,
        rows_returned=0,
        total_matched=None,
        truncated=False,
    ):
        self.database_path = database_path
        self.executed_at = executed_at
        self.elapsed_ms = elapsed_ms
        self.rows_returned = rows_returned
        self.total_matched = total_matched
        self.truncated = truncated


def execute_sql_with_metadata(sql, path=None):
    """Validate, plan-check, then execute a read-only SELECT/WITH.

    Returns (rows, Execution). The rows are unchanged from
    execute_sql; the Execution carries how they were produced.
    """

    validate_sql(sql)
    verify_query_plan(sql, path=path, read_only=True)

    database = path or DATABASE_PATH
    connection = get_connection(path, read_only=True)

    try:

        cursor = connection.cursor()

        # Started and stamped here, immediately around the statement,
        # so neither value is contaminated by validation or connection
        # setup and neither is affected by LLM generation time.
        started = provenance.execution_clock()
        executed_at = provenance.utc_timestamp()

        cursor.execute(sql)

        rows = []
        truncated = False
        exhausted = False

        while True:

            batch = cursor.fetchmany(_FETCH_BATCH)

            if not batch:
                exhausted = True
                break

            rows.extend(batch)

            if len(rows) >= MAX_ROWS:

                rows = rows[:MAX_ROWS]

                # Peek one row further. Without this a result set of
                # exactly MAX_ROWS rows is reported as truncated
                # although nothing was lost, and a client cannot tell
                # the two apart.
                truncated = bool(cursor.fetchmany(1))

                # The peek answered the only question that was open: if
                # nothing came back, the cursor had nothing left to
                # give, so the total is known rather than unknowable.
                exhausted = not truncated
                break

        duration = provenance.elapsed_ms(started)

        return (
            [
                dict(row)
                for row in rows
            ],
            Execution(
                database_path=database,
                executed_at=executed_at,
                elapsed_ms=duration,
                rows_returned=len(rows),
                total_matched=len(rows) if exhausted else None,
                truncated=truncated,
            ),
        )

    finally:

        connection.close()


def execute_sql(sql, path=None):
    """Validate, plan-check, then execute a read-only SELECT/WITH.

    The original two-value return, kept for callers that only need the
    rows. New code should use execute_sql_with_metadata.
    """

    rows, execution = execute_sql_with_metadata(sql, path=path)

    return rows, execution.truncated