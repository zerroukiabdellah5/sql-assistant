# ============================================================
# UPLOAD + SANDBOX IMPORT
# ============================================================
# Uploaded databases, SQL scripts and Excel files are stored and
# processed in a sandbox directory. Imported SQL never runs
# against store.db and uploaded SQL scripts only execute against
# a brand-new throwaway database.
# ============================================================

import io
import os
import re
import shutil
import sqlite3
import uuid

import app.config as config
from app.schema import (
    inspect_database,
    schema_to_text,
)

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")
_STATEMENT_START_RE = re.compile(
    r"^(CREATE\s+(TABLE|UNIQUE\s+INDEX|INDEX|VIEW)|INSERT\s+INTO)\b",
    re.IGNORECASE,
)


def _upload_root():
    return os.path.abspath(config.UPLOAD_DIR)


def _subdir(name):
    directory = os.path.join(_upload_root(), name)
    os.makedirs(directory, exist_ok=True)
    return directory


def _new_id():
    return uuid.uuid4().hex[:12]


def _sanitize_filename(filename):
    safe = _SAFE_ID_RE.sub("_", os.path.basename(filename or "upload"))
    return safe or "upload"


def _ensure_upload_size(content):
    maximum = config.MAX_UPLOAD_MB * 1024 * 1024
    if len(content) > maximum:
        raise ValueError(
            f"Upload is too large. Maximum is {config.MAX_UPLOAD_MB} MB."
        )
    return content


def inspect_upload(path):
    """Inspect an importable database file (db or converted sandbox)."""

    schema = inspect_database(path)
    return {
        "schema": schema,
        "schema_text": schema_to_text(schema),
        "tables": [t["name"] for t in schema["tables"]],
        "table_count": schema["table_count"],
        "column_count": schema["column_count"],
    }


# ============================================================
# SQLITE DATABASE FILES
# ============================================================

def import_database(filename, content):
    """Copy an uploaded .db into the sandbox and inspect it."""

    _ensure_upload_size(content)

    file_id = _new_id()
    stored_name = f"{file_id}_{_sanitize_filename(filename)}"

    target = os.path.join(_subdir("databases"), stored_name)

    with open(target, "wb") as handle:
        handle.write(content)

    inspected = inspect_upload(target)

    return {
        "id": file_id,
        "kind": "database",
        "filename": filename,
        "path": os.path.relpath(target, _upload_root()),
        **inspected,
    }


# ============================================================
# SQL SCRIPTS (SANDBOX ONLY, NEVER store.db)
# ============================================================

def _split_statements(text):
    """Split SQL text on top-level semicolons, ignoring strings/-- lines."""

    statements = []
    current = []
    in_single = False
    in_double = False

    for line in text.splitlines():

        stripped = line.strip()

        if not in_single and not in_double and stripped.startswith("--"):
            continue

        for char in line:

            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double

            current.append(char)

            if char == ";" and not in_single and not in_double:
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []

    remainder = "".join(current).strip()
    if remainder:
        statements.append(remainder)

    return statements


def import_sql_script(filename, content):
    """Execute an uploaded SQL script into a fresh sandbox database."""

    _ensure_upload_size(content)

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")

    statements = _split_statements(text)

    if not statements:
        raise ValueError("The SQL script contains no statements.")

    if len(statements) > config.MAX_SQL_SCRIPT_STATEMENTS:
        raise ValueError(
            f"SQL script has too many statements "
            f"(max {config.MAX_SQL_SCRIPT_STATEMENTS})."
        )

    file_id = _new_id()
    target = os.path.join(
        _subdir("sandboxes"),
        f"{file_id}_sandbox.db",
    )

    connection = sqlite3.connect(target)
    connection.execute("PRAGMA foreign_keys = ON")

    try:

        for statement in statements:

            if not _STATEMENT_START_RE.match(statement):
                raise ValueError(
                    "Only CREATE TABLE/INDEX/VIEW and INSERT INTO "
                    "statements are allowed in imported SQL scripts. "
                    f"Rejected: {statement[:80]}"
                )

            lower = statement.lower()
            if any(
                word in lower
                for word in ["attach", "detach", "vacuum", "load_extension"]
            ):
                raise ValueError(
                    "Import scripts cannot ATTACH/VACUUM or load extensions."
                )

            connection.execute(statement)

        connection.commit()

    except Exception:

        connection.rollback()
        connection.close()

        if os.path.exists(target):
            os.remove(target)

        raise

    else:

        connection.close()

    inspected = inspect_upload(target)

    return {
        "id": file_id,
        "kind": "sql_script",
        "filename": filename,
        "path": os.path.relpath(target, _upload_root()),
        "statement_count": len(statements),
        **inspected,
    }


# ============================================================
# EXCEL FILES
# ============================================================

def _sanitize_table_name(sheet_name, index):

    value = re.sub(r"[^A-Za-z0-9_]", "_", sheet_name).strip("_")

    if not value:
        value = f"sheet_{index}"

    if not re.match(r"^[A-Za-z]", value):
        value = f"s_{value}"

    return value


def _unique_header(column_index, name):

    if not _TABLE_NAME_RE.match(name):
        name = f"column_{column_index}"

    return name


def _infer_column_type(values):

    non_null = [v for v in values if v is not None]

    if non_null and all(
        isinstance(v, int) and not isinstance(v, bool) for v in non_null
    ):
        return "INTEGER"

    if non_null and all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in non_null
    ):
        return "REAL"

    return "TEXT"


def _normalize_cell(value):

    if value is None:
        return None

    if isinstance(value, bool):
        return 1 if value else 0

    if isinstance(value, (int, float)):
        return value

    return str(value)


def import_excel(filename, content):
    """Convert an .xlsx workbook into a sandbox SQLite database."""

    _ensure_upload_size(content)

    from openpyxl import load_workbook

    workbook = load_workbook(
        io.BytesIO(content),
        read_only=True,
        data_only=True,
    )

    file_id = _new_id()
    target = os.path.join(
        _subdir("sandboxes"),
        f"{file_id}_excel.db",
    )

    connection = sqlite3.connect(target)

    try:

        sheet_count = 0
        row_count = 0

        for index, sheet in enumerate(workbook.worksheets, start=1):

            rows = sheet.iter_rows(values_only=True)

            try:
                header_row = next(rows)
            except StopIteration:
                continue

            header = []
            seen = set()

            for column_index, raw in enumerate(header_row, start=1):
                name = _unique_header(
                    column_index,
                    re.sub(r"\s+", "_", str(raw or "")).strip("_"),
                )
                while name in seen:
                    name = f"{name}_2"
                seen.add(name)
                header.append(name)

            if not header:
                continue

            data_rows = []

            for values in rows:

                if len(data_rows) >= config.MAX_IMPORT_ROWS:
                    break

                data_rows.append(
                    [
                        _normalize_cell(v)
                        for v in values[: len(header)]
                    ]
                    + [None] * max(0, len(header) - len(values))
                )

            if not data_rows:
                continue

            columns = []

            for column_index, name in enumerate(header):
                column_values = [
                    row[column_index] for row in data_rows
                ]
                columns.append(
                    f"{name} {_infer_column_type(column_values)}"
                )

            table_name = _sanitize_table_name(sheet.title, index)

            connection.execute(
                f"CREATE TABLE {table_name} ({', '.join(columns)})"
            )

            placeholders = ", ".join(["?"] * len(header))
            connection.executemany(
                f"INSERT INTO {table_name} "
                f"({', '.join(header)}) VALUES ({placeholders})",
                [tuple(row) for row in data_rows],
            )

            sheet_count += 1
            row_count += len(data_rows)

        connection.commit()

    except Exception:

        connection.rollback()
        connection.close()

        if os.path.exists(target):
            os.remove(target)

        workbook.close()

        raise

    else:

        connection.close()
        workbook.close()

    if sheet_count == 0:
        if os.path.exists(target):
            os.remove(target)
        raise ValueError("The Excel file contains no importable sheets.")

    inspected = inspect_upload(target)

    return {
        "id": file_id,
        "kind": "excel",
        "filename": filename,
        "path": os.path.relpath(target, _upload_root()),
        "sheet_count": sheet_count,
        "row_count": row_count,
        **inspected,
    }


# ============================================================
# ROUTER
# ============================================================

def import_file(filename, content):
    """Dispatch an upload by extension. Never touches store.db."""

    extension = os.path.splitext(filename)[1].lower()

    if extension in config.ALLOWED_DATABASE_EXTENSIONS:
        return import_database(filename, content)

    if extension in config.ALLOWED_SQL_EXTENSIONS:
        return import_sql_script(filename, content)

    if extension in config.ALLOWED_EXCEL_EXTENSIONS:
        return import_excel(filename, content)

    raise ValueError(
        "Unsupported file type. Use .db/.sqlite/.sqlite3, .sql, or .xlsx."
    )


def delete_import(import_id):
    """Remove an imported file from any sandbox subdirectory."""

    for sub in ["databases", "sandboxes"]:

        directory = os.path.join(_upload_root(), sub)

        if not os.path.isdir(directory):
            continue

        for filename in os.listdir(directory):

            if filename.startswith(import_id):

                os.remove(os.path.join(directory, filename))
                return True

    return False


def get_import(import_id):
    """Look up an import by id, inspecting it on demand."""

    for sub in ["databases", "sandboxes"]:

        directory = os.path.join(_upload_root(), sub)

        if not os.path.isdir(directory):
            continue

        for filename in os.listdir(directory):

            if filename.startswith(import_id):

                path = os.path.join(directory, filename)
                kind = "database" if sub == "databases" else "sql_script"

                return {
                    "id": import_id,
                    "kind": kind,
                    "filename": filename,
                    "path": os.path.relpath(path, _upload_root()),
                    **inspect_upload(path),
                }

    return None


def list_imports():
    """List stored imports without re-inspecting each file."""

    items = []

    for sub, kind in [
        ("databases", "database"),
        ("sandboxes", "sql_script"),
    ]:

        directory = os.path.join(_upload_root(), sub)

        if not os.path.isdir(directory):
            continue

        for filename in sorted(os.listdir(directory)):

            if not filename.endswith(".db"):
                continue

            items.append(
                {
                    "id": filename.split("_", 1)[0],
                    "kind": kind,
                    "filename": filename,
                    "path": os.path.join(sub, filename),
                }
            )

    return items