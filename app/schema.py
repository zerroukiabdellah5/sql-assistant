# ============================================================
# SCHEMA DISCOVERY (DATA-DRIVEN)
# ============================================================
# Inspects any SQLite database generically from its metadata:
# sqlite_master + PRAGMA table_info / foreign_key_list.
# No store-specific assumptions live here.
# ============================================================

import re

from app.config import (
    DATABASE_PATH,
    MAX_SCHEMA_COLUMNS,
    MAX_SCHEMA_TABLES,
)
from app.db import get_connection

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def inspect_database(path=DATABASE_PATH):
    """Return a structured view of tables, columns and FKs."""

    connection = get_connection(path, read_only=True)

    try:

        cursor = connection.cursor()

        cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)

        tables = []

        for row in cursor.fetchall():

            table_name = row["name"]

            if not _TABLE_NAME_RE.match(table_name):
                continue

            cursor.execute(
                f"PRAGMA table_info({table_name})"
            )

            columns = [
                {
                    "name": column["name"],
                    "type": (column["type"] or "TEXT").upper(),
                    "notnull": bool(column["notnull"]),
                    "pk": column["pk"],
                }
                for column in cursor.fetchall()
            ]

            cursor.execute(
                f"PRAGMA foreign_key_list({table_name})"
            )

            foreign_keys = [
                {
                    "from": fk["from"],
                    "table": fk["table"],
                    "to": fk["to"],
                }
                for fk in cursor.fetchall()
            ]

            tables.append(
                {
                    "name": table_name,
                    "columns": columns,
                    "foreign_keys": foreign_keys,
                }
            )

        column_count = sum(
            len(table["columns"])
            for table in tables
        )

        return {
            "tables": tables,
            "table_count": len(tables),
            "column_count": column_count,
        }

    finally:

        connection.close()


def schema_to_text(schema):
    """Render an inspected schema to the same text contract used by
    /api/schema (TABLE ... / FOREIGN KEYS lines)."""

    lines = []

    for table in schema["tables"][:MAX_SCHEMA_TABLES]:

        columns = table["columns"][:MAX_SCHEMA_COLUMNS]
        column_text = ", ".join(
            f"{column['name']} {column['type']}"
            for column in columns
        )

        if len(table["columns"]) > MAX_SCHEMA_COLUMNS:
            column_text += (
                f" (+{len(table['columns']) - MAX_SCHEMA_COLUMNS} more)"
            )

        table_line = f"TABLE {table['name']} ({column_text})"

        if table["foreign_keys"]:

            fk_text = ", ".join(
                f"{fk['from']} -> {fk['table']}.{fk['to']}"
                for fk in table["foreign_keys"]
            )

            table_line += f" FOREIGN KEYS: {fk_text}"

        lines.append(table_line)

    extra = schema["table_count"] - MAX_SCHEMA_TABLES

    if extra > 0:
        lines.append(
            f"(+{extra} more tables not shown)"
        )

    return "\n".join(lines)


def get_schema(path=DATABASE_PATH):
    """Text schema for prompts and the /api/schema endpoint."""

    return schema_to_text(inspect_database(path))


# ============================================================
# PROMPT FRIENDLY EXTRACTS
# ============================================================

def list_relationships(schema):
    """Derive relationship lines from actual FK pragmas."""

    relationships = []

    for table in schema["tables"]:

        for fk in table["foreign_keys"]:

            relationships.append(
                f"{table['name']}.{fk['from']} = "
                f"{fk['table']}.{fk['to']}"
            )

    return relationships


def table_names(schema, limit=MAX_SCHEMA_TABLES):
    """Comma-separated, ANSI-quoted table names (for prompts)."""

    names = [
        f"'{table['name']}'"
        for table in schema["tables"][:limit]
    ]

    if schema["table_count"] > limit:
        names.append(
            f"... and {schema['table_count'] - limit} more"
        )

    return ", ".join(names)