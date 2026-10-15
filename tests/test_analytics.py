# ============================================================
# CHART SELECTION AND TRANSFORMATION
# ============================================================
# Phase 3. A chart is chosen from the SHAPE of the actual result
# rows, and it may only plot numbers the rows contained. These
# tests pin the selection rules and the transformations that
# protect them: a NULL stays a gap, a negative stays negative, a
# non-numeric column is never plotted, and the chart type matches
# what the data can honestly show.
# ============================================================

import pytest

from app import analytics
from app import main as main_mod


def _plan(rows):
    return analytics.plan_chart(rows)


# ============================================================
# 1. TYPE SELECTION FROM RESULT SHAPE
# ============================================================

def test_dates_over_a_measure_become_a_line_chart():
    plan = _plan(
        [
            {"month": "2024-01", "revenue": 10.0},
            {"month": "2024-02", "revenue": 20.0},
        ]
    )

    assert plan["type"] == "line"
    assert plan["x_label"] == "month"
    assert plan["y_label"] == "revenue"


def test_a_line_chart_is_ordered_chronologically_not_as_returned():
    plan = _plan(
        [
            {"month": "2024-03", "revenue": 30.0},
            {"month": "2024-01", "revenue": 10.0},
            {"month": "2024-02", "revenue": 20.0},
        ]
    )

    assert plan["type"] == "line"
    assert plan["labels"] == ["2024-01", "2024-02", "2024-03"]
    assert plan["datasets"][0]["data"] == [10.0, 20.0, 30.0]


def test_integer_years_are_a_time_axis_not_a_measure():
    plan = _plan(
        [
            {"year": 2022, "orders": 4.0},
            {"year": 2023, "orders": 6.0},
        ]
    )

    assert plan["type"] == "line"
    assert plan["x_label"] == "year"
    assert plan["labels"] == ["2022", "2023"]


def test_a_small_non_negative_category_result_is_part_to_whole():
    plan = _plan(
        [
            {"category": "A", "total": 5.0},
            {"category": "B", "total": 3.0},
            {"category": "C", "total": 2.0},
        ]
    )

    assert plan["type"] == "doughnut"


def test_a_negative_value_is_not_part_of_a_whole():
    plan = _plan(
        [
            {"category": "A", "total": 5.0},
            {"category": "B", "total": -3.0},
            {"category": "C", "total": 2.0},
        ]
    )

    assert plan["type"] == "bar"


def test_a_zero_total_cannot_be_drawn_as_slices():
    plan = _plan(
        [
            {"category": "A", "total": 0.0},
            {"category": "B", "total": 0.0},
        ]
    )

    assert plan["type"] == "bar"


def test_too_many_categories_for_a_readable_pie_fall_back_to_bars():
    rows = [
        {"category": f"C{index}", "total": float(index + 1)}
        for index in range(analytics.MAX_PIE_SLICES + 1)
    ]

    assert _plan(rows)["type"] == "bar"


def test_a_single_value_becomes_a_kpi_card_not_a_one_bar_chart():
    plan = _plan([{"total_orders": 42}])

    assert plan["type"] == "kpi"
    assert plan["kpi"]["column"] == "total_orders"
    assert plan["kpi"]["value"] == 42.0
    assert plan["datasets"] == []


def test_a_single_row_with_several_measures_is_a_comparison():
    plan = _plan([{"revenue": 10.0, "cost": 4.0}])

    assert plan["type"] == "bar"
    assert {dataset["label"] for dataset in plan["datasets"]} == {
        "revenue",
        "cost",
    }


def test_categories_with_a_single_measure_become_bars():
    plan = _plan(
        [
            {"region": "North", "sales": 10.0},
            {"region": "South", "sales": 12.0},
            {"region": "East", "sales": 8.0},
            {"region": "West", "sales": 9.0},
            {"region": "Centre", "sales": 7.0},
            {"region": "North East", "sales": 6.0},
            {"region": "South West", "sales": 5.0},
            {"region": "North West", "sales": 4.0},
            {"region": "South East", "sales": 3.0},
        ]
    )

    assert plan["type"] == "bar"
    assert plan["labels"][0] == "North"


# ============================================================
# 2. NO CHART WHEN THE DATA CANNOT SUPPORT ONE
# ============================================================

def test_no_numeric_column_means_no_chart():
    assert _plan([{"name": "widget"}, {"name": "gadget"}]) is None


def test_an_all_null_numeric_column_is_not_a_measure():
    assert _plan([{"total": None}, {"total": None}]) is None


def test_empty_results_have_no_chart():
    assert _plan([]) is None
    assert _plan(None) is None


def test_more_points_than_can_be_read_fall_back_to_the_table():
    rows = [
        {"n": float(index)}
        for index in range(analytics.MAX_CHART_POINTS + 1)
    ]

    plan = _plan(rows)

    assert plan["type"] == "table"
    assert plan["datasets"] == []


def test_the_readability_limit_is_stated_not_silent():
    rows = [
        {"n": float(index)}
        for index in range(analytics.MAX_CHART_POINTS + 1)
    ]

    plan = _plan(rows)

    assert plan["note"]
    assert str(len(rows)) in plan["note"].replace(",", "")


def test_a_non_numeric_row_is_rejected_rather_than_half_charted():
    assert _plan([{"v": 1.0}, "not-a-row"]) is None


def test_a_boolean_is_missing_data_not_a_number():
    assert _plan([{"v": True}, {"v": False}]) is None


def test_a_non_finite_value_disqualifies_the_column():
    assert _plan([{"v": 1.0}, {"v": float("inf")}]) is None


def test_nan_is_missing_data_not_a_number():
    assert _plan([{"v": 1.0}, {"v": float("nan")}]) is None


def test_a_whitespace_string_is_not_a_zero():
    assert _plan([{"v": 1.0}, {"v": "   "}]) is None


def test_a_text_number_is_not_silently_parsed():
    assert _plan([{"v": "10"}, {"v": "20"}]) is None


# ============================================================
# 3. TRANSFORMATION — ACTUAL VALUES ONLY
# ============================================================

def test_a_null_is_a_gap_not_a_zero():
    plan = _plan(
        [
            {"month": "2024-01", "revenue": 10.0},
            {"month": "2024-02", "revenue": None},
            {"month": "2024-03", "revenue": 30.0},
        ]
    )

    assert plan["type"] == "line"
    assert plan["datasets"][0]["data"] == [10.0, None, 30.0]


def test_missing_values_are_stated_rather_than_hidden():
    plan = _plan(
        [
            {"month": "2024-01", "revenue": 10.0},
            {"month": "2024-02", "revenue": None},
        ]
    )

    assert "1 value" in plan["note"]
    assert plan["summary"]["missing"] == 1


def test_a_negative_value_survives_the_transformation():
    plan = _plan(
        [
            {"month": "2024-01", "profit": -5.0},
            {"month": "2024-02", "profit": 8.0},
        ]
    )

    assert plan["datasets"][0]["data"] == [-5.0, 8.0]


def test_the_summary_totals_only_the_values_that_were_present():
    plan = _plan(
        [
            {"month": "2024-01", "revenue": 10.0},
            {"month": "2024-02", "revenue": None},
            {"month": "2024-03", "revenue": 30.0},
        ]
    )

    assert plan["summary"]["total"] == 40.0
    assert plan["summary"]["points"] == 3


def test_labels_are_rendered_as_text():
    plan = _plan(
        [
            {"category": "A", "total": 5.0},
            {"category": "B", "total": 3.0},
        ]
    )

    assert plan["labels"] == ["A", "B"]


def test_a_text_column_names_the_rows_even_beside_numbers():
    plan = _plan(
        [
            {"id": 100, "name": "widget", "price": 9.99},
            {"id": 200, "name": "gadget", "price": 4.5},
        ]
    )

    assert plan["type"] == "bar"
    assert plan["x_label"] == "name"
    assert plan["labels"] == ["widget", "gadget"]


def test_several_measures_are_preserved_in_separate_datasets():
    plan = _plan(
        [
            {"region": "North", "sales": 10.0, "cost": 4.0},
            {"region": "South", "sales": 12.0, "cost": 6.0},
        ]
    )

    assert plan["type"] == "bar"
    assert [dataset["label"] for dataset in plan["datasets"]] == [
        "sales",
        "cost",
    ]
    assert plan["y_label"] == "sales, cost"


# ============================================================
# 4. THE PLAN IS PLAIN JSON DATA
# ============================================================

def test_the_plan_is_json_serializable():
    import json

    plan = _plan(
        [
            {"month": "2024-01", "revenue": 10.0},
            {"month": "2024-02", "revenue": None},
        ]
    )

    assert json.loads(json.dumps(plan))["type"] == "line"


# ============================================================
# 5. THE ROUTE SHIPS THE PLAN (ADDITIVE)
# ============================================================

def test_ask_returns_a_chart_built_from_the_rows(
    authed_client, monkeypatch
):
    sql = "SELECT name, price FROM products"

    def fake_ask(prompt, turns, request_id=None, generation=None,
                 database_path=None, **_):
        if generation is not None:
            generation.record("nvidia", "stub-model-1")
        return sql, "lists products and prices"

    monkeypatch.setattr(main_mod, "ask_active", fake_ask)

    response = authed_client.post(
        "/api/ask", json={"prompt": "products and prices"}
    )

    assert response.status_code == 200, response.text

    body = response.json()

    assert "chart" in body

    chart = body["chart"]

    assert chart is not None
    assert chart["datasets"][0]["label"] == "price"
    assert len(chart["labels"]) == len(body["data"])


def test_ask_still_works_without_a_chart(authed_client, monkeypatch):
    sql = "SELECT name FROM products"

    def fake_ask(prompt, turns, request_id=None, generation=None,
                 database_path=None, **_):
        if generation is not None:
            generation.record("nvidia", "stub-model-1")
        return sql, "lists names"

    monkeypatch.setattr(main_mod, "ask_active", fake_ask)

    response = authed_client.post(
        "/api/ask", json={"prompt": "product names"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["chart"] is None


# ============================================================
# 6. THE CLIENT RENDERS THE SERVER PLAN
# ============================================================

@pytest.fixture(scope="module")
def index_source():
    import app.config as config

    with open(config.INDEX_PATH, encoding="utf-8") as handle:
        return handle.read()


def test_client_consumes_the_server_chart_plan(index_source):
    assert "lastChart = result.chart" in index_source
    assert "renderChartPlan" in index_source


def test_client_falls_back_only_when_the_field_is_absent(index_source):
    assert "lastChartKnown" in index_source
    assert "hasOwnProperty.call(" in index_source


def test_client_formats_chart_numbers_for_the_locale(index_source):
    assert "toLocaleString" in index_source
    assert "chartTickText" in index_source


# ============================================================
# 7. DUPLICATE AND LONG LABELS
# ============================================================

def test_duplicate_labels_keep_every_row_and_are_reported():
    plan = _plan(
        [
            {"region": "North", "sales": 10.0},
            {"region": "North", "sales": 4.0},
            {"region": "South", "sales": 7.0},
        ]
    )

    assert plan["labels"] == ["North", "North", "South"]
    assert plan["datasets"][0]["data"] == [10.0, 4.0, 7.0]
    assert "a label" in plan["note"]


def test_long_labels_are_preserved_for_the_client_to_fit():
    long_name = "A category name that is definitely far too long to fit"

    plan = _plan(
        [
            {"category": long_name, "total": 5.0},
            {"category": long_name + " two", "total": 3.0},
        ]
    )

    assert long_name in plan["labels"]
    assert plan["datasets"][0]["data"] == [5.0, 3.0]


# ============================================================
# 8. MISSING DATES AND MISSING MEASURES
# ============================================================

def test_without_a_date_column_the_result_is_not_a_time_series():
    plan = _plan(
        [
            {"category": "A", "total": 5.0},
            {"category": "B", "total": 3.0},
        ]
    )

    assert plan["type"] != "line"


def test_only_columns_present_in_the_rows_are_charted():
    plan = _plan(
        [
            {"product": "widget", "price": 9.99},
            {"product": "gadget", "price": 4.5},
        ]
    )

    labels = {dataset["label"] for dataset in plan["datasets"]}

    assert labels == {"price"}
    assert "profit" not in labels
    assert "cost" not in labels


def test_a_missing_measure_is_never_filled_with_zero():
    plan = _plan(
        [
            {"month": "2024-01", "revenue": 10.0, "cost": 4.0},
            {"month": "2024-02", "revenue": 20.0, "cost": None},
        ]
    )

    cost = next(
        dataset for dataset in plan["datasets"]
        if dataset["label"] == "cost"
    )

    assert cost["data"] == [4.0, None]


# ============================================================
# 9. ALIGNMENT AND ACTUAL-ROW ORIGIN
# ============================================================

def test_every_value_in_the_plan_comes_from_a_returned_row():
    rows = [
        {"region": "North", "sales": 10.0},
        {"region": "South", "sales": 12.0},
        {"region": "East", "sales": 8.0},
        {"region": "West", "sales": 9.0},
    ]

    plan = _plan(rows)

    returned = {row["region"]: row["sales"] for row in rows}

    assert len(plan["labels"]) == len(rows)

    for label, value in zip(plan["labels"], plan["datasets"][0]["data"]):
        assert returned[label] == value


def test_labels_and_values_stay_aligned_after_chronological_sorting():
    rows = [
        {"month": "2024-03", "revenue": 30.0},
        {"month": "2024-01", "revenue": None},
        {"month": "2024-02", "revenue": 20.0},
    ]

    plan = _plan(rows)

    assert plan["labels"] == ["2024-01", "2024-02", "2024-03"]
    assert plan["datasets"][0]["data"] == [None, 20.0, 30.0]


# ============================================================
# 10. ONE NUMBER DEFINITION, NOT TWO
# ============================================================

def test_the_report_and_the_planner_share_one_numeric_helper():
    import app.reports as reports

    assert reports._numeric_series is analytics.numeric_series
    assert reports._numeric_columns is analytics.numeric_columns


def test_client_shows_the_readability_limit_instead_of_a_chart(index_source):
    assert 'plan.type === "table"' in index_source
