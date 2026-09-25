import os
import sqlite3
import sys

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)


def create_sample_database(path):
    """Build a small store-like DB (tables with real FKs) at path."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE brands (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            price REAL,
            stock INTEGER,
            brand_id INTEGER REFERENCES brands(id),
            category_id INTEGER REFERENCES categories(id)
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            quantity INTEGER,
            order_date TEXT
        );
        CREATE TABLE regions (
            id INTEGER PRIMARY KEY,
            code TEXT,
            label TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO categories (name) VALUES ('Clothing'), ('Accessories')"
    )
    connection.execute(
        "INSERT INTO brands (name) VALUES ('Acme'), ('Globex')"
    )
    connection.execute(
        """
        INSERT INTO products
            (name, description, price, stock, brand_id, category_id)
        VALUES
            ('Widget', 'A small widget', 9.99, 40, 1, 1),
            ('Gadget', 'A shiny gadget', 19.99, 5, 2, 2)
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def sample_db(tmp_path):
    return create_sample_database(
        str(tmp_path / "sample.db")
    )


@pytest.fixture
def meta_paths(tmp_path, monkeypatch):
    import app.config as config

    monkeypatch.setattr(
        config, "META_DB_PATH", str(tmp_path / "meta.db")
    )
    monkeypatch.setattr(
        config, "UPLOAD_DIR", str(tmp_path / "uploads")
    )
    return tmp_path