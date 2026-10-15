# ============================================================
# CHART PLANNING AND ANALYTICS
# ============================================================
# Pure helpers that turn the ACTUAL rows a query returned into a
# chart plan. Nothing here invents data: only columns present in
# the rows are charted, a NULL stays a gap (never a zero), and a
# column that is not numeric is never plotted as if it were a
# number. A negative value is drawn where it belongs, not flipped
# into a positive bar.
#
# This module also owns the row numeric helpers (numeric_series /
# numeric_columns). app.reports imports them so there is exactly
# one definition of "which column is a number" in the codebase.
#
# The result is a plain dict (data, not rendering). The client
# turns it into pixels, which keeps every charting decision
# testable here without a browser.
# ============================================================

import math
import re

# A chart with more points than this is unreadable and would
# duplicate the whole result set in the response. Those results
# are NOT silently charted or silently dropped: the plan says so
# explicitly and the full result table stays on screen.
MAX_CHART_POINTS = 200

# A part-to-whole chart is only readable while the slices fit.
MAX_PIE_SLICES = 8

# ISO-style dates: 2024, 2024-01, 2024-01-31, 2024/01/31, plus an
# optional time. Lexicographic order is chronological for these.
_DATE_RE = re.compile(
    r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?([ T]\d{1,2}:\d{2}(:\d{2})?)?$"
)


def _is_number(value):
    """A finite real number. Booleans are not numbers."""

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def numeric_series(data, column):
    """The column's values as floats/None, or None when not numeric.

    A column qualifies only when every non-null value is a finite
    real number and at least one value is present. A NULL stays a
    gap and is skipped by the chart, never turned into a zero; a
    text, blank, boolean, NaN or infinite value means the column as
    a whole is not numeric, so the caller must not chart it at all
    rather than chart a partial column as if the missing value were
    zero.
    """

    if not data:
        return None

    values = []
    seen_number = False

    for row in data:

        value = row.get(column)

        if value is None:
            values.append(None)
            continue

        if not _is_number(value):
            return None

        values.append(float(value))
        seen_number = True

    return values if seen_number else None


def numeric_columns(data):
    """Names of the columns that are numeric in every row."""

    if not data:
        return []

    return [
        key
        for key in data[0].keys()
        if numeric_series(data, key) is not None
    ]


def _columns(data):
    keys = []
    for row in data:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    return keys


def _present(values):
    return [value for value in values if value is not None]


def _all(values, predicate):
    present = _present(values)
    return bool(present) and all(predicate(value) for value in present)


def _looks_like_date(value):
    return bool(_DATE_RE.match(str(value).strip()))


def _looks_like_year(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1000 <= value <= 2999
    )


def _label_column(data, keys, numeric):
    """The column that names each row, or None when rows are unnamed.

    A text column wins: it is what a GROUP BY label looks like. An
    integer column that holds only years is treated as a time axis
    rather than a measure.
    """

    for key in keys:

        if key in numeric:
            continue

        values = [row.get(key) for row in data]

        if _all(values, lambda value: isinstance(value, str)):
            return key

    for key in keys:

        if key in numeric:
            continue

        if _present([row.get(key) for row in data]):
            return key

    for key in keys:

        if key not in numeric:
            continue

        if _all([row.get(key) for row in data], _looks_like_year):
            return key

    return None


def _time_order(data, label_key):
    """Row indices sorted chronologically by an ISO-ish label."""

    def sort_key(index):
        return str(data[index].get(label_key)).strip()

    return sorted(range(len(data)), key=sort_key)


def _title(kind, value_keys, label_key):
    values = ", ".join(str(key) for key in value_keys)

    if label_key is None:
        return values

    return (
        f"{values} over {label_key}"
        if kind == "line"
        else f"{values} by {label_key}"
    )


def _notes(datasets, labels):
    """Honest caveats about what the chart does and does not show.

    A gap is reported rather than drawn as zero, and repeated
    labels are reported rather than silently collapsed.
    """

    parts = []

    missing = sum(
        1 for dataset in datasets for value in dataset["data"]
        if value is None
    )

    if missing == 1:
        parts.append(
            "1 value was missing in the results and is not plotted."
        )
    elif missing > 1:
        parts.append(
            f"{missing} values were missing in the results "
            "and are not plotted."
        )

    duplicates = len(labels) - len(set(labels))

    if duplicates == 1:
        parts.append(
            "1 row shares a label with another row; each row is plotted."
        )
    elif duplicates > 1:
        parts.append(
            f"{duplicates} rows share a label with another row; "
            "each row is plotted."
        )

    return " ".join(parts)


def _plan_for_series(kind, data, value_keys, label_key, label_values, order):
    labels = [str(label_values[index]) for index in order]

    datasets = []

    for key in value_keys:

        series = numeric_series(data, key)

        datasets.append(
            {
                "label": str(key),
                "data": [series[index] for index in order],
            }
        )

    summary = {}

    if len(datasets) == 1:

        values = datasets[0]["data"]
        present = _present(values)

        summary = {
            "points": len(values),
            "missing": len(values) - len(present),
            "total": sum(present) if present else None,
        }

    return {
        "type": kind,
        "title": _title(kind, value_keys, label_key),
        "x_label": str(label_key) if label_key is not None else "",
        "y_label": (
            ", ".join(str(key) for key in value_keys)
            if len(value_keys) <= 2
            else ""
        ),
        "labels": labels,
        "datasets": datasets,
        "summary": summary,
        "note": _notes(datasets, labels),
    }


def _plan_for_kpi(data, value_keys, label_key):
    column = value_keys[0]
    value = data[0].get(column)

    if value is None:
        return None

    return {
        "type": "kpi",
        "title": str(column),
        "x_label": "",
        "y_label": "",
        "labels": [],
        "datasets": [],
        "kpi": {
            "label": str(label_key) if label_key is not None else str(column),
            "column": str(column),
            "value": float(value),
        },
        "summary": {"points": 1, "missing": 0, "total": float(value)},
        "note": "",
    }


def _plan_for_unreadable(data):
    """A plan that explains why there is no chart, instead of silence.

    The rows are valid and numeric but there are too many to chart
    readably. Nothing is sampled or dropped: the full table is
    already on screen, and this says why no chart accompanies it.
    """

    return {
        "type": "table",
        "title": "Automatic Visualization",
        "x_label": "",
        "y_label": "",
        "labels": [],
        "datasets": [],
        "summary": {},
        "note": (
            f"This result has {len(data):,} rows, more than can be "
            "charted readably, so the full result table is shown "
            "instead."
        ),
    }


def plan_chart(data, sql=None):
    """A chart plan for the rows, or None when no chart fits.

    The type is chosen from the shape of the actual results:
    dates present -> line; a small, non-negative, part-to-whole
    result -> doughnut; a single value -> a KPI card; anything
    else with a numeric column -> bar. No numeric column or a
    single all-NULL value means no chart at all, and the table on
    screen stands alone; too many rows to plot readably returns an
    explicit "table" plan that names the limitation.

    ``sql`` is accepted for call-site symmetry and does not
    affect the plan: only the returned rows decide the chart.
    """

    if not data:
        return None

    if not all(isinstance(row, dict) for row in data):
        return None

    keys = _columns(data)

    numeric = {}

    for key in keys:
        series = numeric_series(data, key)
        if series is not None:
            numeric[key] = series

    if not numeric:
        return None

    if len(data) > MAX_CHART_POINTS:
        return _plan_for_unreadable(data)

    label_key = _label_column(data, keys, numeric)
    value_keys = [key for key in keys if key in numeric and key != label_key]

    if not value_keys:
        return None

    label_values = (
        [row.get(label_key) for row in data]
        if label_key is not None
        else list(range(1, len(data) + 1))
    )

    if len(data) == 1 and len(value_keys) == 1:
        return _plan_for_kpi(data, value_keys, label_key)

    is_time_series = label_key is not None and (
        _all(
            label_values,
            lambda value: isinstance(value, str) and _looks_like_date(value),
        )
        or _all(label_values, _looks_like_year)
    )

    if is_time_series:
        order = _time_order(data, label_key)
        return _plan_for_series(
            "line", data, value_keys, label_key, label_values, order
        )

    first = numeric[value_keys[0]]
    present = _present(first)

    part_to_whole = (
        len(value_keys) == 1
        and 2 <= len(data) <= MAX_PIE_SLICES
        and len(present) == len(data)
        and all(value >= 0 for value in present)
        and sum(present) > 0
    )

    kind = "doughnut" if part_to_whole else "bar"
    order = list(range(len(data)))

    return _plan_for_series(
        kind, data, value_keys, label_key, label_values, order
    )
