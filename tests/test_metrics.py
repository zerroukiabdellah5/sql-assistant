# ============================================================
# BUSINESS METRICS — SUPPORTED OR EXPLICITLY NOT
# ============================================================
# Phase 2. A metric may be computed only when a definition exists AND
# every column it requires exists in the schema. When either is
# missing the answer is a named limitation, never a guessed cost, a
# defaulted price or a substituted zero.
#
# The tests read the real sample schema and a few synthetic ones. The
# synthetic schemas exist to prove the negative case: no definition is
# silently filled in when the columns are not there.
# ============================================================

import app.metrics as metrics
import app.prompts as prompts
import app.schema as schema_mod


def _schema(sample_db):
    return schema_mod.inspect_database(sample_db)


def _table(name, columns):
    return {
        "name": name,
        "columns": [
            {"name": column, "type": declared, "notnull": False, "pk": 0}
            for column, declared in columns
        ],
        "foreign_keys": [],
    }


def _available_keys(schema):
    available, _unavailable = metrics.resolve_metrics(schema)
    return {metric["key"] for metric in available}


def _unavailable_keys(schema):
    _available, unavailable = metrics.resolve_metrics(schema)
    return {metric["key"] for metric in unavailable}


# ============================================================
# 1. WHAT THE SCHEMA SUPPORTS
# ============================================================

def test_revenue_uses_the_price_and_quantity_columns_that_exist(sample_db):
    schema = _schema(sample_db)

    available, _unavailable = metrics.resolve_metrics(schema)

    revenue = next(item for item in available if item["key"] == "revenue")

    assert revenue["columns"]["price"] == "products.price"
    assert revenue["columns"]["quantity"] == "orders.quantity"
    assert revenue["expression"] == (
        "SUM(products.price * orders.quantity)"
    )


def test_stock_uses_the_stock_column_that_exists(sample_db):
    schema = _schema(sample_db)

    assert "stock" in _available_keys(schema)

    available, _unavailable = metrics.resolve_metrics(schema)

    stock = next(item for item in available if item["key"] == "stock")

    assert stock["expression"] == "SUM(products.stock)"


def test_cost_and_profit_are_unavailable_without_a_cost_column(sample_db):
    schema = _schema(sample_db)

    assert "cost" not in _available_keys(schema)
    assert "profit" not in _available_keys(schema)

    reasons = {
        item["key"]: item["reason"]
        for item in metrics.resolve_metrics(schema)[1]
    }

    assert "cost" in reasons["cost"]
    assert "cost" in reasons["profit"]


# ============================================================
# 2. NOTHING IS INVENTED WHEN A COLUMN IS ABSENT
# ============================================================

def test_a_schema_with_no_metric_columns_supports_no_metrics():
    schema = {
        "tables": [_table("notes", [("id", "INTEGER"), ("body", "TEXT")])],
        "table_count": 1,
        "column_count": 2,
    }

    available, unavailable = metrics.resolve_metrics(schema)

    assert available == []
    assert {item["key"] for item in unavailable} == {
        "revenue",
        "cost",
        "profit",
        "stock",
    }


def test_a_text_quantity_column_is_not_a_quantity():
    schema = {
        "tables": [
            _table("products", [("id", "INTEGER"), ("price", "REAL")]),
            _table("orders", [("quantity", "TEXT")]),
        ],
        "table_count": 2,
        "column_count": 4,
    }

    # price exists, but a quantity stored as text is not accepted as a
    # quantity: summing it would mean treating free text as a number.
    assert "revenue" not in _available_keys(schema)
    assert "revenue" in _unavailable_keys(schema)


def test_a_metric_definition_is_required_even_with_columns_present(sample_db):
    schema = _schema(sample_db)

    # "margin" is not one of the defined metrics; the columns are
    # irrelevant because there is no business definition for it.
    text = metrics.limitation("margin", schema)

    assert text
    assert "not a defined business metric" in text


# ============================================================
# 3. THE LIMITATION IS CLEAR AND NAMES THE REASON
# ============================================================

def test_limitation_is_none_for_a_supported_metric(sample_db):
    schema = _schema(sample_db)

    assert metrics.limitation("revenue", schema) is None
    assert metrics.limitation("stock", schema) is None


def test_limitation_names_the_missing_column(sample_db):
    schema = _schema(sample_db)

    text = metrics.limitation("profit", schema)

    assert text
    assert "profit" in text.lower()
    assert "cost" in text.lower()


def test_limitation_is_case_insensitive(sample_db):
    schema = _schema(sample_db)

    assert metrics.limitation("  REVENUE ", schema) is None


# ============================================================
# 4. DATE GROUPING IS ONLY OFFERED WHEN A DATE COLUMN EXISTS
# ============================================================

def test_date_columns_are_reported(sample_db):
    schema = _schema(sample_db)

    assert metrics.date_columns(schema) == ["orders.order_date"]


def test_no_date_column_is_invented():
    schema = {
        "tables": [_table("t", [("id", "INTEGER"), ("value", "REAL")])],
        "table_count": 1,
        "column_count": 2,
    }

    assert metrics.date_columns(schema) == []


# ============================================================
# 5. THE PROMPT CARRIES THE DEFINITIONS AND THE GUARDRAILS
# ============================================================

def test_prompt_states_which_metrics_are_available(sample_db):
    instruction = prompts.build_system_instruction(_schema(sample_db))

    assert "BUSINESS METRICS" in instruction
    assert "Revenue = SUM(products.price * orders.quantity)" in instruction
    assert "Stock on hand = SUM(products.stock)" in instruction


def test_prompt_states_which_metrics_are_unavailable(sample_db):
    instruction = prompts.build_system_instruction(_schema(sample_db))

    assert "Unavailable" in instruction
    assert "Profit:" in instruction


def test_prompt_forbids_inventing_missing_values(sample_db):
    instruction = prompts.build_system_instruction(
        _schema(sample_db)
    ).lower()

    assert "never invent a price" in instruction
    assert "a cost" in instruction
    assert "a date" in instruction
    assert "a quantity" in instruction


def test_prompt_treats_null_as_missing_not_zero(sample_db):
    instruction = prompts.build_system_instruction(
        _schema(sample_db)
    ).lower()

    assert "null" in instruction
    assert "not a zero" in instruction


def test_prompt_guards_division_by_zero(sample_db):
    instruction = prompts.build_system_instruction(
        _schema(sample_db)
    ).lower()

    assert "division" in instruction
    assert "nullif" in instruction


def test_prompt_reports_date_availability(sample_db):
    instruction = prompts.build_system_instruction(_schema(sample_db))

    assert "orders.order_date" in instruction


def test_prompt_says_no_date_when_there_is_none():
    schema = {
        "tables": [_table("t", [("id", "INTEGER"), ("amount", "REAL")])],
        "table_count": 1,
        "column_count": 2,
    }

    instruction = prompts.build_system_instruction(schema)

    assert "No date column exists" in instruction
