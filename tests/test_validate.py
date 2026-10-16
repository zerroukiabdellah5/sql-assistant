"""The read-only SQL contract, tested directly on the validator.

app/validate.py is what every generated statement passes through before
it is even compiled with EXPLAIN QUERY PLAN, so its rules are part of
the security contract rather than an implementation detail. The
integration tests prove the same statements are refused end to end
(app/db.py, /api/ask); these pin the rules themselves, including the
cases no other test names:

  * a mutating statement is refused;
  * a stacked statement (``;``) is refused, which is the multi-statement
    bypass this project deliberately closes at the validator rather than
    relying only on sqlite3's single-statement guard;
  * ATTACH/DETACH are refused;
  * a PRAGMA, including a dangerous one such as ``writable_schema``, is
    refused - the rule is "must start with SELECT or WITH", so no PRAGMA
    is reachable through this path at all;
  * the trailing-semicolon convenience is preserved, so ordinary
    generated SQL still runs.
"""

import pytest

from app.validate import ValidationError, validate_sql


# ============================================================
# WHAT MUST RUN
# ============================================================

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM products",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "  SELECT name FROM products WHERE id = 1  ",
        # One trailing semicolon is a common model artefact: allowed.
        "SELECT 1;",
        "SELECT 1;  ",
    ],
    ids=[
        "select",
        "select-star",
        "cte",
        "padded",
        "trailing-semicolon",
        "trailing-semicolon-space",
    ],
)
def test_read_only_statements_are_allowed(sql):
    validate_sql(sql)


# ============================================================
# MUTATIONS
# ============================================================

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO products VALUES (1, 'x')",
        "UPDATE products SET name = 'x'",
        "DELETE FROM products",
        "DROP TABLE products",
        "ALTER TABLE products ADD COLUMN c TEXT",
        "CREATE TABLE t (id INTEGER)",
        "REPLACE INTO products VALUES (1, 'x')",
        "TRUNCATE TABLE products",
    ],
    ids=[
        "insert", "update", "delete", "drop",
        "alter", "create", "replace", "truncate",
    ],
)
def test_mutations_are_refused(sql):
    with pytest.raises(ValidationError):
        validate_sql(sql)


def test_a_multi_statement_stack_is_refused():
    """The exact shape of the classic injection: a SELECT then a write."""

    with pytest.raises(
        ValidationError, match="Multiple SQL statements"
    ):
        validate_sql("SELECT 1; DROP TABLE products")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE products",
        "SELECT 1;DROP TABLE products",
        "SELECT * FROM products; DELETE FROM products",
        "SELECT 1; SELECT 2",
    ],
    ids=["space", "no-space", "select-then-delete", "two-selects"],
)
def test_any_stacked_statement_is_refused(sql):
    with pytest.raises(
        ValidationError, match="Multiple SQL statements"
    ):
        validate_sql(sql)


def test_a_semicolon_in_a_string_literal_is_refused_conservatively():
    """The splitter is deliberately not a full SQL parser.

    It refuses rather than risk admitting a real stack, so a semicolon
    inside a literal is rejected too. That is the safe direction and is
    pinned here so a future relaxation is a conscious decision.
    """

    with pytest.raises(ValidationError):
        validate_sql("SELECT ';' AS s")


# ============================================================
# ATTACH / DETACH / PRAGMA
# ============================================================

@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE 'other.db' AS other",
        "ATTACH 'other.db' AS other",
        "DETACH DATABASE other",
    ],
    ids=["attach-database", "attach-short", "detach"],
)
def test_attach_and_detach_are_refused(sql):
    with pytest.raises(ValidationError):
        validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA writable_schema = 1",
        "pragma table_info(products)",
        "PRAGMA journal_mode = WAL",
        "PRAGMA database_list",
    ],
    ids=["writable-schema", "table-info", "journal-mode", "database-list"],
)
def test_no_pragma_is_reachable(sql):
    """The allow-list is "SELECT or WITH", so no PRAGMA passes.

    A dangerous PRAGMA such as writable_schema is refused for the same
    reason as a harmless one: neither starts with SELECT or WITH.
    """

    with pytest.raises(
        ValidationError, match="Only SELECT queries"
    ):
        validate_sql(sql)


def test_a_stacked_pragma_is_refused_as_a_stack_first():
    with pytest.raises(
        ValidationError, match="Multiple SQL statements"
    ):
        validate_sql("SELECT 1; PRAGMA writable_schema = 1")


def test_vacuum_and_other_utility_statements_are_refused():
    for sql in ("VACUUM", "ANALYZE", "REINDEX"):
        with pytest.raises(ValidationError):
            validate_sql(sql)
