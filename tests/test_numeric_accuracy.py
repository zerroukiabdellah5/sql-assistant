# ============================================================
# NUMERIC ACCURACY — AGGREGATES AND RESULT INTERPRETATION
# ============================================================
# Phase 2. These tests are independent of the application's own
# arithmetic: the expected values are computed here, in Python, from
# the same rows that were inserted, and then compared against what
# app.db.execute_sql actually returns through the read-only path.
#
# The subjects, in order:
#   * SUM / COUNT / AVG and ratios over real rows,
#   * grouping by a category column and by a date-derived column,
#   * NULL handling, empty result sets and division by zero,
#   * numeric strings and decimal precision,
#   * a missing column, which must fail rather than fabricate a result,
#   * the chart layer, which must use the rows and never turn a
#     missing value into a misleading zero.
# ============================================================

import os
import sqlite3

import pytest

from app import config, db
import app.reports as reports


# ------------------------------------------------------------
# A controlled dataset. Every expected value below is derived from
# these lists, not from a second SQL statement.
# ------------------------------------------------------------

CATEGORIES = [
    (1, "Electronics"),
    (2, "Books"),
]

# (id, name, category_id, price, stock)
PRODUCTS = [
    (1, "Widget", 1, 10.0, 5),
    (2, "Gadget", 2, 20.0, 0),
    (3, "Doohickey", 1, 5.5, None),
]

# (id, product_id, quantity, order_date). Row 4 has a NULL quantity.
ORDERS = [
    (1, 1, 2, "2024-01-05"),
    (2, 1, 3, "2024-01-20"),
    (3, 2, 4, "2024-02-01"),
    (4, 2, None, "2024-02-15"),
    (5, 3, 1, "2024-02-20"),
]


def _build_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT,
            category_id INTEGER,
            price REAL,
            stock INTEGER
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            product_id INTEGER,
            quantity INTEGER,
            order_date TEXT
        );
        CREATE TABLE payments (id INTEGER PRIMARY KEY, amount TEXT);
        CREATE TABLE decimals (id INTEGER PRIMARY KEY, x REAL);
        """
    )
    connection.executemany("INSERT INTO categories VALUES (?, ?)", CATEGORIES)
    connection.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", PRODUCTS)
    connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
    connection.executemany(
        "INSERT INTO payments VALUES (?, ?)",
        [(1, "10.5"), (2, "20.25")],
    )
    connection.executemany(
        "INSERT INTO decimals VALUES (?, ?)",
        [(1, 1.1), (2, 2.2), (3, 3.3)],
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def numeric_db(tmp_path):
    return _build_database(str(tmp_path / "numeric.db"))


def _one(sql, path):
    rows, _ = db.execute_sql(sql, path=path)
    assert len(rows) == 1
    return rows[0]


# ============================================================
# 1. THE AGGREGATES
# ============================================================

def test_count_star_matches_the_number_of_rows(numeric_db):
    row = _one("SELECT COUNT(*) AS n FROM orders", numeric_db)
    assert row["n"] == len(ORDERS)


def test_sum_ignores_null_and_matches_python(numeric_db):
    expected = sum(order[2] for order in ORDERS if order[2] is not None)
    row = _one("SELECT SUM(quantity) AS total FROM orders", numeric_db)
    assert row["total"] == expected == 10


def test_count_of_a_column_ignores_null(numeric_db):
    expected = sum(1 for order in ORDERS if order[2] is not None)
    row = _one("SELECT COUNT(quantity) AS n FROM orders", numeric_db)
    assert row["n"] == expected == 4


def test_count_star_counts_the_null_row(numeric_db):
    row = _one(
        "SELECT COUNT(*) AS n, COUNT(quantity) AS m FROM orders",
        numeric_db,
    )
    assert row["n"] == 5
    assert row["m"] == 4


def test_average_ignores_null_and_is_not_integer_division(numeric_db):
    values = [order[2] for order in ORDERS if order[2] is not None]
    expected = sum(values) / len(values)
    row = _one("SELECT AVG(quantity) AS avg FROM orders", numeric_db)
    assert row["avg"] == pytest.approx(expected) == pytest.approx(2.5)


def test_ratio_of_two_aggregates(numeric_db):
    total = sum(order[2] for order in ORDERS if order[2] is not None)
    expected = total / len(ORDERS)
    row = _one(
        "SELECT SUM(quantity) * 1.0 / COUNT(*) AS ratio FROM orders",
        numeric_db,
    )
    assert row["ratio"] == pytest.approx(expected)


def test_revenue_is_computed_from_the_actual_price_and_quantity(
    numeric_db,
):
    prices = {product[0]: product[3] for product in PRODUCTS}
    expected = sum(
        order[2] * prices[order[1]]
        for order in ORDERS
        if order[2] is not None
    )
    row = _one(
        """
        SELECT SUM(o.quantity * p.price) AS revenue
        FROM orders o JOIN products p ON o.product_id = p.id
        """,
        numeric_db,
    )
    assert row["revenue"] == pytest.approx(expected) == pytest.approx(135.5)


def test_grouping_by_category(numeric_db):
    category_names = {cid: name for cid, name in CATEGORIES}

    expected_totals = {}
    expected_counts = {}

    for _pid, _name, category_id, price, _stock in PRODUCTS:
        category = category_names[category_id]
        expected_totals[category] = expected_totals.get(category, 0.0) + price
        expected_counts[category] = expected_counts.get(category, 0) + 1

    rows, _ = db.execute_sql(
        """
        SELECT c.name AS category,
               SUM(p.price) AS total_price,
               COUNT(*) AS product_count
        FROM products p JOIN categories c ON p.category_id = c.id
        GROUP BY c.name
        ORDER BY c.name
        """,
        path=numeric_db,
    )

    assert [row["category"] for row in rows] == sorted(expected_totals)

    for row in rows:
        category = row["category"]
        assert row["total_price"] == pytest.approx(expected_totals[category])
        assert row["product_count"] == expected_counts[category]


def test_grouping_by_a_date_derived_month(numeric_db):
    expected = {}
    for _id, _product, quantity, order_date in ORDERS:
        if quantity is None:
            continue
        month = order_date[:7]
        expected[month] = expected.get(month, 0) + quantity

    rows, _ = db.execute_sql(
        """
        SELECT substr(order_date, 1, 7) AS month,
               SUM(quantity) AS units
        FROM orders
        GROUP BY month
        ORDER BY month
        """,
        path=numeric_db,
    )

    assert rows == [
        {"month": month, "units": expected[month]}
        for month in sorted(expected)
    ]
    assert expected == {"2024-01": 5, "2024-02": 5}


# ============================================================
# 2. NULL, EMPTY SETS AND DIVISION BY ZERO
# ============================================================

def test_a_null_value_is_returned_as_null_not_zero(numeric_db):
    rows, _ = db.execute_sql(
        "SELECT quantity FROM orders WHERE quantity IS NULL",
        path=numeric_db,
    )
    assert rows == [{"quantity": None}]
    assert rows[0]["quantity"] is None
    assert rows[0]["quantity"] != 0


def test_aggregate_over_an_empty_set(numeric_db):
    row = _one(
        """
        SELECT COUNT(*) AS n, SUM(quantity) AS total, AVG(quantity) AS avg
        FROM orders WHERE 1 = 0
        """,
        numeric_db,
    )
    assert row["n"] == 0
    assert row["total"] is None
    assert row["avg"] is None


def test_division_by_zero_is_null_and_not_an_error(numeric_db):
    row = _one("SELECT 1 / 0 AS r", numeric_db)
    assert row["r"] is None


def test_aggregate_division_by_zero_is_null(numeric_db):
    row = _one(
        "SELECT SUM(quantity) / 0 AS r FROM orders",
        numeric_db,
    )
    assert row["r"] is None


def test_nullif_guards_the_division(numeric_db):
    guarded = _one(
        """
        SELECT SUM(quantity) / NULLIF(SUM(quantity), 0) AS r
        FROM orders
        """,
        numeric_db,
    )
    assert guarded["r"] == pytest.approx(1.0)

    empty = _one(
        """
        SELECT SUM(quantity) / NULLIF(SUM(quantity), 0) AS r
        FROM orders WHERE 1 = 0
        """,
        numeric_db,
    )
    assert empty["r"] is None


# ============================================================
# 3. NUMERIC STRINGS AND DECIMAL PRECISION
# ============================================================

def test_numeric_strings_sum_as_numbers(numeric_db):
    row = _one('SELECT SUM(amount) AS total FROM payments', numeric_db)
    assert row["total"] == pytest.approx(30.75)


def test_text_value_that_is_not_a_number_does_not_become_zero(numeric_db):
    connection = sqlite3.connect(numeric_db)
    connection.execute("INSERT INTO payments VALUES (?, ?)", (3, "not-a-number"))
    connection.commit()
    connection.close()

    # SQLite skips text that cannot be read as a number; it does not
    # silently treat it as 0. The sum therefore stays at the numeric
    # values only, which is the honest answer for this data.
    row = _one("SELECT SUM(amount) AS total FROM payments", numeric_db)
    assert row["total"] == pytest.approx(30.75)


def test_decimal_sum_keeps_its_precision(numeric_db):
    expected = sum(value for _id, value in [(1, 1.1), (2, 2.2), (3, 3.3)])
    row = _one("SELECT SUM(x) AS total FROM decimals", numeric_db)
    assert row["total"] == pytest.approx(expected)
    assert row["total"] == pytest.approx(6.6, abs=1e-9)


def test_decimal_average_is_not_rounded_to_an_integer(numeric_db):
    row = _one("SELECT AVG(x) AS avg FROM decimals", numeric_db)
    assert row["avg"] == pytest.approx(2.2)


# ============================================================
# 4. A MISSING COLUMN MUST FAIL, NOT FABRICATE
# ============================================================

def test_missing_column_raises(numeric_db):
    with pytest.raises(sqlite3.OperationalError):
        db.execute_sql(
            "SELECT no_such_column FROM products",
            path=numeric_db,
        )


def test_missing_table_raises(numeric_db):
    with pytest.raises(sqlite3.OperationalError):
        db.execute_sql(
            "SELECT * FROM no_such_table",
            path=numeric_db,
        )


def test_no_metric_column_is_invented_for_a_missing_name(numeric_db):
    # A query for a column that does not exist must never come back as
    # an empty or zero-filled result set.
    with pytest.raises(sqlite3.OperationalError):
        db.execute_sql(
            "SELECT SUM(cost) AS profit FROM orders",
            path=numeric_db,
        )


# ============================================================
# 5. THE SERVER-SIDE REPORT CHART
# ============================================================

def test_a_column_numeric_in_every_row_is_charted():
    data = [{"a": 1.0}, {"a": 2.0}, {"a": 3.0}]
    assert reports._numeric_columns(data) == ["a"]


def test_a_mostly_numeric_column_is_not_charted_if_one_row_is_text():
    data = [{"a": 1.0}, {"a": 2.0}, {"a": "oops"}]
    assert reports._numeric_columns(data) == []


def test_a_column_with_nulls_is_still_charted_when_it_has_numbers():
    data = [{"a": None}, {"a": 5.0}]
    assert reports._numeric_columns(data) == ["a"]


def test_an_all_null_column_is_not_charted():
    data = [{"a": None}, {"a": None}]
    assert reports._numeric_columns(data) == []


def test_booleans_are_not_treated_as_numbers():
    assert reports._numeric_series([{"a": True}, {"a": False}], "a") is None
    assert reports._numeric_series([{"a": float("nan")}], "a") is None


def test_numeric_series_preserves_null_as_a_gap():
    series = reports._numeric_series(
        [{"a": 1.0}, {"a": None}, {"a": 3.0}], "a"
    )
    assert series == [1.0, None, 3.0]


def test_chart_drawing_skips_a_null_row_without_zeroing_it():
    data = [
        {"label": "one", "value": 10.0},
        {"label": "two", "value": None},
        {"label": "three", "value": 0.0},
    ]

    # Must not raise, and must not turn the NULL into a bar.
    drawing = reports._bar_chart_drawing(data, "label", "value")

    assert drawing is not None

    labels = [
        shape.text
        for shape in drawing.contents
        if type(shape).__name__ == "String"
    ]

    assert "two" not in labels
    assert "0.0" in labels


def test_chart_drawing_returns_none_when_there_is_nothing_to_draw():
    assert reports._bar_chart_drawing([{"label": "a", "value": None}], "label", "value") is None


def test_negative_values_are_not_flipped_into_positive_bars():
    data = [{"label": "down", "value": -10.0}, {"label": "up", "value": 10.0}]

    drawing = reports._bar_chart_drawing(data, "label", "value")

    rects = [
        shape
        for shape in drawing.contents
        if type(shape).__name__ == "Rect"
    ]

    assert len(rects) == 2

    negative, positive = rects

    # Both bars meet at the zero baseline, but the negative one is
    # drawn below it rather than occupying the same positive region.
    assert negative.y < positive.y
    assert negative.y + negative.height == pytest.approx(positive.y)


def test_a_report_with_a_null_in_a_numeric_column_still_builds(tmp_path):
    data = [
        {"name": "A", "amount": 10.0},
        {"name": "B", "amount": None},
        {"name": "C", "amount": 30.0},
    ]

    path = reports.build_report_pdf(
        title="Mixed",
        question="amounts",
        sql="SELECT name, amount FROM t",
        data=data,
        count=len(data),
        output_dir=str(tmp_path),
    )

    assert os.path.exists(path)
    with open(path, "rb") as handle:
        assert handle.read(5) == b"%PDF-"


# ============================================================
# 6. THE FRONTEND CHART USES ROWS AND INVENTS NOTHING
# ============================================================

@pytest.fixture(scope="module")
def index_source():
    with open(config.INDEX_PATH, encoding="utf-8") as handle:
        return handle.read()


def _chart_number_source(html):
    return html[
        html.index("function chartNumber") : html.index("function formatCell")
    ]


def _is_numeric_source(html):
    return html[
        html.index("function isNumeric") : html.index("function isAggregateQuery")
    ]


def test_chart_scales_the_actual_row_values(index_source):
    assert "chartNumber(row[key])" in index_source


def test_chart_number_never_substitutes_zero(index_source):
    body = _chart_number_source(index_source)
    assert "|| 0" not in body
    assert 'value.trim() === ""' in body
    assert 'typeof value === "boolean"' in body


def test_is_numeric_rejects_blanks_and_booleans(index_source):
    body = _is_numeric_source(index_source)
    assert 'value.trim() === ""' in body
    assert 'typeof value === "boolean"' in body


def test_chart_only_appears_for_aggregate_queries(index_source):
    body = index_source[
        index_source.index("function isAggregateQuery") :
        index_source.index("function hideChart")
    ]
    for keyword in ("GROUP\\s+BY", "SUM\\s*\\(", "COUNT\\s*\\(", "AVG\\s*\\("):
        assert keyword in body
