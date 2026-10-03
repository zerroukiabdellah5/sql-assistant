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


# ============================================================
# AUTHENTICATION FIXTURES
# ============================================================
# The secret below is a throwaway literal that exists only in the test
# suite. No real credential is ever read from the environment, so the
# tests are hermetic and safe to commit and to run anywhere.
TEST_API_KEY = "test-only-app-api-key"


@pytest.fixture
def app_secret(monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "APP_API_KEY", TEST_API_KEY)
    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", False)
    return TEST_API_KEY


@pytest.fixture
def no_app_secret(monkeypatch):
    """Remove the configured secret so the app must fail closed."""

    import app.config as config

    monkeypatch.setattr(config, "APP_API_KEY", None)
    return None


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Isolate the process-wide rate limiter between tests.

    The limiter keeps module-level state on purpose (a single
    instance, so one reset covers every scope). That state would
    otherwise accumulate across the whole suite and make unrelated
    tests order-dependent. Production defaults are untouched; this
    only clears the counter.
    """

    from app.ratelimit import limiter

    limiter.reset()

    yield

    limiter.reset()


@pytest.fixture(autouse=True)
def reset_llm_concurrency_cap():
    """Isolate the process-wide LLM concurrency gate between tests.

    The gate keeps module-level state on purpose, for the same reason
    app.ratelimit keeps one limiter: a single reset() has to be able
    to isolate the whole suite. A permit leaked by one test would make
    an unrelated later test fail with a 429, which is exactly the
    order-dependence the fixture above exists to prevent. Production
    defaults are untouched; this only clears the counter.
    """

    from app.llm_concurrency import gate

    gate.reset()

    yield

    gate.reset()


@pytest.fixture
def anon_client(meta_paths, app_secret):
    """A client that presents NO credential.

    The server still has its secret configured, so a blocked request
    is a 401 (rejected) rather than a 503 (misconfigured).
    """

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def authed_client(meta_paths, app_secret):
    """A client that authenticates with the test-only secret."""

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(
        app, headers={"X-API-Key": app_secret}
    ) as client:
        yield client
