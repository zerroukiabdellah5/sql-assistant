import os
import sqlite3

import pytest

import app.db as db


def test_get_connection_read_write(sample_db):
    connection = db.get_connection(sample_db)
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()


def test_get_connection_read_only_blocks_writes(sample_db):
    connection = db.get_connection(sample_db, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute(
            "INSERT INTO brands (name) VALUES ('Nope')"
        )
    connection.close()


def test_get_connection_missing_database():
    with pytest.raises(FileNotFoundError):
        db.get_connection("does/not/exist.db")


def test_verify_query_plan_ok(sample_db):
    plan = db.verify_query_plan(
        "SELECT * FROM products",
        path=sample_db,
    )
    assert len(plan) >= 1


def test_verify_query_plan_rejects_bad_table(sample_db):
    with pytest.raises(sqlite3.OperationalError):
        db.verify_query_plan(
            "SELECT * FROM nonexistent_table",
            path=sample_db,
        )


def test_verify_query_plan_rejects_mutation(sample_db):
    with pytest.raises(ValueError):
        db.verify_query_plan(
            "DELETE FROM products",
            path=sample_db,
        )


def test_execute_sql_returns_rows_and_count(sample_db):
    rows, truncated = db.execute_sql(
        "SELECT name, price FROM products",
        path=sample_db,
    )
    assert truncated is False
    assert len(rows) == 2
    assert {"name": "Widget", "price": 9.99} in rows


def test_execute_sql_rejects_unsafe(sample_db):
    with pytest.raises(ValueError):
        db.execute_sql("DROP TABLE products", path=sample_db)


def test_execute_sql_truncates(sample_db, tmp_path):
    big_db = str(tmp_path / "big.db")
    connection = sqlite3.connect(big_db)
    connection.execute("CREATE TABLE t (id INTEGER)")
    connection.executemany(
        "INSERT INTO t (id) VALUES (?)",
        [(i,) for i in range(2500)],
    )
    connection.commit()
    connection.close()

    rows, truncated = db.execute_sql(
        "SELECT id FROM t",
        path=big_db,
    )
    assert truncated is True
    assert len(rows) == db.MAX_ROWS