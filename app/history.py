# ============================================================
# PERSISTENT SESSIONS + MESSAGES
# ============================================================
# Sessions and messages live in a separate metadata database
# (app_meta.db). Application data (store.db) is never mixed in.
# Connections are short-lived and opened per call.
# ============================================================

import os
import sqlite3
import uuid

from datetime import datetime, timezone

import app.config as config
from app.config import MAX_HISTORY_TURNS

_META_TABLES = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    prompt TEXT,
    sql TEXT,
    explanation TEXT,
    count INTEGER DEFAULT 0,
    truncated INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    sql TEXT NOT NULL,
    context TEXT DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages (session_id, id);

CREATE INDEX IF NOT EXISTS idx_approvals_status
    ON approvals (status);

CREATE TABLE IF NOT EXISTS visitor_sessions (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitor_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    prompt TEXT,
    sql TEXT,
    explanation TEXT,
    count INTEGER DEFAULT 0,
    truncated INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_visitor_messages_session
    ON visitor_messages (session_id, id);

CREATE INDEX IF NOT EXISTS idx_visitor_sessions_owner
    ON visitor_sessions (visitor_id, updated_at);

CREATE TABLE IF NOT EXISTS visitor_approvals (
    visitor_id TEXT NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sql TEXT NOT NULL,
    context TEXT DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    PRIMARY KEY (visitor_id, id)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_meta_db():
    connection = sqlite3.connect(config.META_DB_PATH)
    try:
        connection.executescript(_META_TABLES)
        connection.commit()
    finally:
        connection.close()


def _connect():
    if config.META_DB_PATH:
        directory = os.path.dirname(config.META_DB_PATH)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(config.META_DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(_META_TABLES)
    return connection


# ============================================================
# SESSIONS
# ============================================================

def create_session(title="New session"):
    connection = _connect()
    try:
        session_id = uuid.uuid4().hex
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO sessions (id, title, summary, created_at, updated_at)
            VALUES (?, ?, '', ?, ?)
            """,
            (session_id, title, timestamp, timestamp),
        )
        connection.commit()
        return {
            "id": session_id,
            "title": title,
            "summary": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    finally:
        connection.close()


def session_exists(session_id):
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def get_session(session_id):
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_sessions(limit=50):
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def delete_session(session_id):
    connection = _connect()
    try:
        connection.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (session_id,),
        )
        connection.execute(
            "DELETE FROM sessions WHERE id = ?",
            (session_id,),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def _touch_session(connection, session_id):
    connection.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (_now(), session_id),
    )


def _current_summary(connection, session_id):
    """Rolling summary of the most recent session activity."""

    rows = connection.execute(
        """
        SELECT prompt, sql FROM messages
        WHERE session_id = ? AND role = 'user'
        ORDER BY id DESC LIMIT 3
        """,
        (session_id,),
    ).fetchall()

    if not rows:
        return ""

    lines = []
    for row in reversed(rows):
        prompt = (row["prompt"] or "").strip()
        if prompt:
            lines.append(f"- {prompt[:140]}")
    return "Recent questions:\n" + "\n".join(lines)


# ============================================================
# MESSAGES
# ============================================================

def add_message(
    session_id,
    role,
    prompt="",
    sql="",
    explanation="",
    count=0,
    truncated=False,
):
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO messages
                (session_id, role, prompt, sql, explanation,
                 count, truncated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                prompt,
                sql,
                explanation,
                count,
                int(bool(truncated)),
                _now(),
            ),
        )
        _touch_session(connection, session_id)

        if role == "user":
            user_total = connection.execute(
                """
                SELECT COUNT(*) AS n FROM messages
                WHERE session_id = ? AND role = 'user'
                """,
                (session_id,),
            ).fetchone()["n"]
            if user_total % 10 == 0:
                summary = _current_summary(connection, session_id)
                connection.execute(
                    "UPDATE sessions SET summary = ? WHERE id = ?",
                    (summary, session_id),
                )

        connection.commit()
        return True
    finally:
        connection.close()


def get_messages(session_id, limit=50):
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def build_history_turns(session_id, max_turns=MAX_HISTORY_TURNS):
    """Return recent [user, assistant] turns as {prompt, sql} pairs."""

    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, max_turns * 2),
        ).fetchall()
    finally:
        connection.close()

    messages = list(reversed([dict(row) for row in rows]))

    turns = []
    current = None

    for message in messages:

        if message["role"] == "user":

            if current:
                turns.append(current)

            current = {
                "prompt": message["prompt"] or "",
                "sql": message["sql"] or "",
            }

        elif message["role"] == "assistant" and current:

            current["sql"] = message["sql"] or current["sql"]

    if current:
        turns.append(current)

    return turns[-max_turns:]


# ============================================================
# VISITOR SESSIONS (PER-BROWSER, NO CREDENTIAL)
# ============================================================
# A browser with no owner credential still gets the Sessions feature,
# but in a namespace of its own: every row carries the caller's signed
# visitor id and every read filters on it. The owner tables above are
# never touched by these functions, so an anonymous visitor can neither
# list, load nor delete an owner session, and two visitors never see
# each other's. Same storage engine, same connection helper; only the
# table and the mandatory visitor_id filter differ.

def create_visitor_session(visitor_id, title="New session"):
    connection = _connect()
    try:
        session_id = uuid.uuid4().hex
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO visitor_sessions
                (id, visitor_id, title, summary, created_at, updated_at)
            VALUES (?, ?, ?, '', ?, ?)
            """,
            (session_id, visitor_id, title, timestamp, timestamp),
        )
        connection.commit()
        return {
            "id": session_id,
            "title": title,
            "summary": "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    finally:
        connection.close()


def visitor_session_exists(visitor_id, session_id):
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT id FROM visitor_sessions
            WHERE id = ? AND visitor_id = ?
            """,
            (session_id, visitor_id),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def get_visitor_session(visitor_id, session_id):
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT * FROM visitor_sessions
            WHERE id = ? AND visitor_id = ?
            """,
            (session_id, visitor_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_visitor_sessions(visitor_id, limit=50):
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM visitor_sessions
            WHERE visitor_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (visitor_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def delete_visitor_session(visitor_id, session_id):
    connection = _connect()
    try:
        cursor = connection.execute(
            """
            DELETE FROM visitor_sessions
            WHERE id = ? AND visitor_id = ?
            """,
            (session_id, visitor_id),
        )
        connection.execute(
            "DELETE FROM visitor_messages WHERE session_id = ?",
            (session_id,),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def _touch_visitor_session(connection, session_id):
    connection.execute(
        "UPDATE visitor_sessions SET updated_at = ? WHERE id = ?",
        (_now(), session_id),
    )


def _current_visitor_summary(connection, session_id):
    rows = connection.execute(
        """
        SELECT prompt FROM visitor_messages
        WHERE session_id = ? AND role = 'user'
        ORDER BY id DESC LIMIT 3
        """,
        (session_id,),
    ).fetchall()

    if not rows:
        return ""

    lines = []
    for row in reversed(rows):
        prompt = (row["prompt"] or "").strip()
        if prompt:
            lines.append(f"- {prompt[:140]}")
    return "Recent questions:\n" + "\n".join(lines)


def add_visitor_message(
    visitor_id,
    session_id,
    role,
    prompt="",
    sql="",
    explanation="",
    count=0,
    truncated=False,
):
    """Append a message, but only inside this visitor's own session."""

    if not visitor_session_exists(visitor_id, session_id):
        return False

    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO visitor_messages
                (session_id, role, prompt, sql, explanation,
                 count, truncated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                prompt,
                sql,
                explanation,
                count,
                int(bool(truncated)),
                _now(),
            ),
        )
        _touch_visitor_session(connection, session_id)

        if role == "user":
            user_total = connection.execute(
                """
                SELECT COUNT(*) AS n FROM visitor_messages
                WHERE session_id = ? AND role = 'user'
                """,
                (session_id,),
            ).fetchone()["n"]
            if user_total % 10 == 0:
                summary = _current_visitor_summary(connection, session_id)
                connection.execute(
                    "UPDATE visitor_sessions SET summary = ? WHERE id = ?",
                    (summary, session_id),
                )

        connection.commit()
        return True
    finally:
        connection.close()


def get_visitor_messages(visitor_id, session_id, limit=50):
    if not visitor_session_exists(visitor_id, session_id):
        return []

    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM visitor_messages
            WHERE session_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def build_visitor_history_turns(
    visitor_id, session_id, max_turns=MAX_HISTORY_TURNS
):
    """Recent [user, assistant] turns for a visitor's own session."""

    if not visitor_session_exists(visitor_id, session_id):
        return []

    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT * FROM visitor_messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, max_turns * 2),
        ).fetchall()
    finally:
        connection.close()

    messages = list(reversed([dict(row) for row in rows]))

    turns = []
    current = None

    for message in messages:

        if message["role"] == "user":

            if current:
                turns.append(current)

            current = {
                "prompt": message["prompt"] or "",
                "sql": message["sql"] or "",
            }

        elif message["role"] == "assistant" and current:

            current["sql"] = message["sql"] or current["sql"]

    if current:
        turns.append(current)

    return turns[-max_turns:]