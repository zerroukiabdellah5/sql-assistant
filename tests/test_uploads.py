import io
import os
import sqlite3

import pytest

import app.uploads as uploads


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def make_db_bytes(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE items (id INTEGER, label TEXT)")
    connection.execute(
        "INSERT INTO items VALUES (1, 'alpha'), (2, 'beta')"
    )
    connection.commit()
    connection.close()
    with open(path, "rb") as handle:
        return handle.read()


def make_xlsx_bytes():
    from openpyxl import Workbook

    stream = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales Data"
    sheet.append(["id", "amount", "note"])
    sheet.append([1, 12.5, "one"])
    sheet.append([2, 30, "two"])
    workbook.save(stream)
    return stream.getvalue()


def test_import_database(meta_paths, tmp_path):
    source = tmp_path / "up.db"
    imported = uploads.import_database(
        "up.db", make_db_bytes(str(source))
    )
    assert imported["kind"] == "database"
    assert imported["table_count"] == 1
    assert imported["tables"] == ["items"]
    assert "TABLE items" in imported["schema_text"]
    assert os.path.exists(os.path.join(uploads._upload_root(), imported["path"]))


def test_import_sql_script_creates_sandbox(meta_paths):
    script = (
        "CREATE TABLE t (id INTEGER, name TEXT);\n"
        "INSERT INTO t VALUES (1, 'x');\n"
        "-- a comment\n"
        "INSERT INTO t VALUES (2, 'y');\n"
    )
    imported = uploads.import_sql_script("schema.sql", script.encode())
    assert imported["kind"] == "sql_script"
    assert imported["table_count"] == 1
    assert imported["statement_count"] == 3
    assert imported["tables"] == ["t"]


def test_import_sql_script_rejects_unsafe(meta_paths):
    with pytest.raises(ValueError):
        uploads.import_sql_script(
            "evil.sql",
            b"ATTACH 'store.db' AS s;",
        )


def test_import_sql_script_rejects_disallowed_statement(meta_paths):
    with pytest.raises(ValueError):
        uploads.import_sql_script(
            "x.sql",
            b"DROP TABLE t;",
        )


def test_import_excel(meta_paths):
    imported = uploads.import_excel("sales.xlsx", make_xlsx_bytes())
    assert imported["kind"] == "excel"
    assert imported["table_count"] == 1
    assert imported["tables"] == ["Sales_Data"]
    assert imported["sheet_count"] == 1
    assert imported["row_count"] == 2


def test_import_excel_type_inference(meta_paths):
    imported = uploads.import_excel("sales.xlsx", make_xlsx_bytes())
    schema_text = imported["schema_text"]
    assert "amount REAL" in schema_text
    assert "id INTEGER" in schema_text


def test_import_file_dispatch(meta_paths, tmp_path):
    source = tmp_path / "up.db"
    result = uploads.import_file(
        "up.db", make_db_bytes(str(source))
    )
    assert result["kind"] == "database"


def test_import_file_unsupported(meta_paths):
    with pytest.raises(ValueError):
        uploads.import_file("notes.txt", b"hello")


def test_upload_too_large(meta_paths, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    with pytest.raises(ValueError):
        uploads.import_database("big.db", b"x" * 1024)


def test_delete_import(meta_paths, tmp_path):
    source = tmp_path / "up.db"
    imported = uploads.import_database(
        "up.db", make_db_bytes(str(source))
    )
    assert uploads.delete_import(imported["id"]) is True
    assert uploads.delete_import(imported["id"]) is False