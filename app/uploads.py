# ============================================================
# UPLOAD + SANDBOX IMPORT
# ============================================================
# Uploaded databases, SQL scripts and Excel files are stored and
# processed in a sandbox directory. Imported SQL never runs
# against store.db and uploaded SQL scripts only execute against
# a brand-new throwaway database.
# ============================================================

import csv
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
from app.validate import ValidationError


class ImportNotFound(ValidationError):
    """A supplied import_id is well formed but matches no stored import.

    Subclasses ValidationError because it is still a deliberate,
    user-facing message. Call sites that want a 404 rather than a 400
    can catch this type specifically; everything that only knows about
    ValidationError keeps working unchanged.
    """


_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")

# A stored import id is uuid4().hex[:12]: lowercase hex. The pattern is
# deliberately narrow so a value that could ever be a path fragment
# (a slash, a dot, a backslash, a drive colon) is rejected before any
# filesystem call is made.
_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")

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
        raise ValidationError(
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
        raise ValidationError("The SQL script contains no statements.")

    if len(statements) > config.MAX_SQL_SCRIPT_STATEMENTS:
        raise ValidationError(
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
                raise ValidationError(
                    "Only CREATE TABLE/INDEX/VIEW and INSERT INTO "
                    "statements are allowed in imported SQL scripts. "
                    f"Rejected: {statement[:80]}"
                )

            lower = statement.lower()
            if any(
                word in lower
                for word in ["attach", "detach", "vacuum", "load_extension"]
            ):
                raise ValidationError(
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
        truncated = False

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
                    # The cap is reached and there is at least one more
                    # row to read, so data is being dropped. Recorded
                    # rather than left silent.
                    truncated = True
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
        raise ValidationError("The Excel file contains no importable sheets.")

    inspected = inspect_upload(target)

    return {
        "id": file_id,
        "kind": "excel",
        "filename": filename,
        "path": os.path.relpath(target, _upload_root()),
        "sheet_count": sheet_count,
        "row_count": row_count,
        "truncated": truncated,
        **inspected,
    }


# ============================================================
# CSV FILES
# ============================================================
# A CSV is decoded, sniffed for a delimiter, and materialised as one
# sandbox SQLite table. The result reuses the same column-name
# sanitising and type inference as the Excel path, so the two import
# kinds produce the same shape of database and the same schema text.

# Only a plainly numeric token is coerced. A leading zero (an id or a
# phone number) and an underscore-separated token are left as text, so
# formatting is never silently changed by the import.
_CSV_INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
_CSV_FLOAT_RE = re.compile(r"^-?(0|[1-9][0-9]*)\.[0-9]+$")

# Enough of the file for csv.Sniffer to detect a delimiter without
# reading the whole upload twice.
_CSV_SAMPLE_CHARS = 4096


def _decode_text(content):
    """Decode an uploaded text file, preferring UTF-8.

    utf-8-sig strips a BOM when Excel wrote one. cp1252 and latin-1 are
    tried so a Windows-exported file is still readable; latin-1 cannot
    fail, so a final fallback is not reachable.
    """

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue

    return content.decode("utf-8", errors="replace")


def _read_csv_rows(text):
    """Parse CSV text into a list of non-empty rows."""

    try:
        dialect = csv.Sniffer().sniff(
            text[:_CSV_SAMPLE_CHARS],
            delimiters=",;\t|",
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)

    return [row for row in reader if row]


def _clean_headers(raw_header):
    """Sanitise and uniquify a CSV header row.

    Each name is trimmed, whitespace collapsed to underscores, forced
    through the shared identifier rule, and made unique against the
    names already used in this table.
    """

    header = []
    seen = set()

    for index, raw in enumerate(raw_header, start=1):

        name = _unique_header(
            index,
            re.sub(r"\s+", "_", str(raw or "")).strip("_"),
        )

        base = name
        suffix = 2

        while name in seen:
            name = f"{base}_{suffix}"
            suffix += 1

        seen.add(name)
        header.append(name)

    return header


def _coerce_csv_value(value):

    if value is None:
        return None

    text = str(value).strip()

    if text == "":
        return None

    if _CSV_INT_RE.match(text):
        return int(text)

    if _CSV_FLOAT_RE.match(text):
        return float(text)

    return text


def import_csv(filename, content):
    """Convert an uploaded .csv into a sandbox SQLite database."""

    _ensure_upload_size(content)

    rows = _read_csv_rows(_decode_text(content))

    if not rows:
        raise ValidationError("The CSV file is empty.")

    header = _clean_headers(rows[0])

    if not header:
        raise ValidationError("The CSV file has no columns.")

    data_rows = rows[1:]

    if not data_rows:
        raise ValidationError("The CSV file has no data rows.")

    truncated = len(data_rows) > config.MAX_IMPORT_ROWS

    if truncated:
        data_rows = data_rows[:config.MAX_IMPORT_ROWS]

    normalized = []

    for values in data_rows:

        # A short row is padded and a long row is trimmed to the
        # header width, so an inconsistent file still loads and no
        # column silently shifts into another.
        row = [
            _coerce_csv_value(value)
            for value in values[:len(header)]
        ]
        row += [None] * (len(header) - len(row))
        normalized.append(row)

    columns = [
        f"{name} "
        f"{_infer_column_type([row[index] for row in normalized])}"
        for index, name in enumerate(header)
    ]

    table_name = _sanitize_table_name(
        os.path.splitext(os.path.basename(filename))[0],
        1,
    )

    file_id = _new_id()
    target = os.path.join(
        _subdir("sandboxes"),
        f"{file_id}_csv.db",
    )

    connection = sqlite3.connect(target)

    try:

        connection.execute(
            f"CREATE TABLE {table_name} ({', '.join(columns)})"
        )

        placeholders = ", ".join(["?"] * len(header))

        connection.executemany(
            f"INSERT INTO {table_name} "
            f"({', '.join(header)}) VALUES ({placeholders})",
            [tuple(row) for row in normalized],
        )

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
        "kind": "csv",
        "filename": filename,
        "path": os.path.relpath(target, _upload_root()),
        "row_count": len(normalized),
        "truncated": truncated,
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

    if extension in config.ALLOWED_CSV_EXTENSIONS:
        return import_csv(filename, content)

    raise ValidationError(
        "Unsupported file type. "
        "Use .db/.sqlite/.sqlite3, .sql, .xlsx, or .csv."
    )


def resolve_import_path(import_id):
    """Absolute path of a stored import, confined to the upload root.

    Raises ValidationError for a malformed id or any resolved path that
    would escape UPLOAD_DIR, and ImportNotFound when a well-formed id
    matches nothing. It never falls back to the caller's default: an
    explicit import_id either resolves to a stored file or fails.

    The id is only ever used as a filename prefix under the two known
    sandbox subdirectories, and the resolved path is checked against
    the real upload root, so a value containing a separator or a parent
    reference cannot reach outside it.
    """

    if not import_id or not _IMPORT_ID_RE.match(import_id):
        raise ValidationError("Invalid import id.")

    root = os.path.realpath(_upload_root())
    prefix = import_id + "_"

    for sub in ["databases", "sandboxes"]:

        directory = os.path.join(root, sub)

        if not os.path.isdir(directory):
            continue

        for filename in os.listdir(directory):

            if not filename.startswith(prefix):
                continue

            candidate = os.path.realpath(
                os.path.join(directory, filename)
            )

            # Belt and braces: os.listdir cannot return a name with a
            # separator, but the confinement check is what actually
            # guarantees the returned path stays under the upload root
            # even if a stored filename held one.
            try:
                inside = (
                    os.path.commonpath([candidate, root]) == root
                )
            except ValueError:
                inside = False

            if not inside:
                raise ValidationError("Invalid import path.")

            if os.path.isfile(candidate):
                return candidate

    raise ImportNotFound("Import not found.")


def delete_import(import_id):
    """Remove an imported file from any sandbox subdirectory."""

    if not import_id or not _IMPORT_ID_RE.match(import_id):
        return False

    prefix = import_id + "_"

    for sub in ["databases", "sandboxes"]:

        directory = os.path.join(_upload_root(), sub)

        if not os.path.isdir(directory):
            continue

        for filename in os.listdir(directory):

            if filename.startswith(prefix):

                os.remove(os.path.join(directory, filename))
                return True

    return False


def get_import(import_id):
    """Look up an import by id, inspecting it on demand."""

    if not import_id or not _IMPORT_ID_RE.match(import_id):
        return None

    prefix = import_id + "_"

    for sub in ["databases", "sandboxes"]:

        directory = os.path.join(_upload_root(), sub)

        if not os.path.isdir(directory):
            continue

        for filename in os.listdir(directory):

            if filename == prefix.rstrip("_"):  # unlikely
                pass

            if filename.startswith(prefix):
                path = os.path.join(directory, filename)

                return {
                    "id": import_id,
                    "kind": _kind_for(sub, filename),
                    "filename": filename,
                    "path": os.path.relpath(path, _upload_root()),
                    **inspect_upload(path),
                }

    return None


def _kind_for(sub, filename):
    """Best-effort import kind from the sandbox a file lives in.

    Databases keep their own name. A sandbox file is named after the
    importer that produced it, so the suffix distinguishes a SQL
    script from a converted Excel or CSV workbook without opening it.
    """

    if sub == "databases":
        return "database"

    if filename.endswith("_excel.db"):
        return "excel"

    if filename.endswith("_csv.db"):
        return "csv"

    return "sql_script"


def list_imports():
    """List stored imports without re-inspecting each file."""

    items = []

    for sub in ["databases", "sandboxes"]:

        directory = os.path.join(_upload_root(), sub)

        if not os.path.isdir(directory):
            continue

        for filename in sorted(os.listdir(directory)):

            if not filename.endswith(".db"):
                continue

            items.append(
                {
                    "id": filename.split("_", 1)[0],
                    "kind": _kind_for(sub, filename),
                    "filename": filename,
                    "path": os.path.join(sub, filename),
                }
            )

    return items