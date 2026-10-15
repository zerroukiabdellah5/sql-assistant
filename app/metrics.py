# ============================================================
# BUSINESS METRICS — DEFINED, NOT GUESSED
# ============================================================
# A metric is computable only when BOTH of these hold:
#   1. the definition exists here (the "business definition"), and
#   2. every column the definition requires exists in the schema.
# If either is missing, the metric is reported unavailable together
# with the reason, so a caller states a limitation instead of
# inventing a cost, a price, a date or a quantity.
#
# WHY THIS IS A REGISTRY AND NOT A CALCULATION
#   This module never runs SQL and never reads a row. It answers a
#   factual question about a schema — "is this metric well defined
#   against these columns?" — and nothing else. The arithmetic is
#   left to the generated SELECT, which the caller writes from the
#   definitions returned here.
#
# WHY DEFINITIONS ARE DATA-DRIVEN
#   Column matching is by name pattern over whatever the inspected
#   schema contains. No table or column name from any particular
#   database is hard-coded, so the module works for the shipped demo
#   dataset and for an uploaded one without changes.
#
# WHAT IS NEVER DONE HERE
#   No substitution of a missing value, no default of zero, and no
#   fallback column. A role that does not resolve stays missing, and
#   a metric missing any required role stays unavailable.
# ============================================================

import re

# Column types that can carry a number. An empty type (SQLite allows
# a column with no declared type) is treated as unknown rather than
# rejected, because the type affinity is not evidence either way.
_NUMERIC_TYPES = {
    "",
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "REAL",
    "DOUBLE",
    "DOUBLE PRECISION",
    "FLOAT",
    "NUMERIC",
    "DECIMAL",
    "NUMBER",
    "MONEY",
    "CURRENCY",
}

# Types that are explicitly not a number. A column whose name matches
# a role but whose declared type is text is not that role: treating a
# text column as a quantity would be inventing a number.
_NON_NUMERIC_MARKERS = (
    "CHAR",
    "TEXT",
    "CLOB",
    "BLOB",
    "BOOL",
    "DATE",
    "TIME",
    "JSON",
    "UUID",
)

# role -> the column names that satisfy it. Matching is by name
# token, so "unit_price" and "list_price" both satisfy "price" and
# neither is mistaken for a quantity. When several columns match, the
# first in schema order wins; the caller can see which one was chosen.
_ROLE_PATTERNS = {
    "price": re.compile(
        r"(^|_)(price|unit_price|list_price|sale_price|sales_price|rrp)($|_)"
    ),
    "cost": re.compile(
        r"(^|_)(cost|unit_cost|cogs|cost_price|purchase_price)($|_)"
    ),
    "quantity": re.compile(
        r"(^|_)(quantity|qty|units|units_sold|sold|volume)($|_)"
    ),
    "stock": re.compile(
        r"(^|_)(stock|inventory|on_hand|in_stock|stock_qty|available_qty)($|_)"
    ),
}

# Date-like column names, used only to tell a caller whether grouping
# by a date is possible. A missing date column is never synthesized.
_DATE_PATTERN = re.compile(
    r"(^|_)(date|day|month|year|timestamp|created|ordered|_at|_on)($|_)"
)

_ROLE_LABEL = {
    "price": "price",
    "cost": "cost",
    "quantity": "quantity",
    "stock": "stock",
}

# Each definition states what it needs and how it is expressed. The
# placeholder {times_quantity} expands to " * <quantity column>" only
# when a quantity column was actually resolved; it is never filled in
# with a guess.
METRIC_DEFINITIONS = (
    {
        "key": "revenue",
        "label": "Revenue",
        "requires": ("price", "quantity"),
        "optional": (),
        "expression": "SUM({price} * {quantity})",
    },
    {
        "key": "cost",
        "label": "Cost",
        "requires": ("cost",),
        "optional": ("quantity",),
        "expression": "SUM({cost}{times_quantity})",
    },
    {
        "key": "profit",
        "label": "Profit",
        "requires": ("price", "cost"),
        "optional": ("quantity",),
        "expression": "SUM(({price} - {cost}){times_quantity})",
    },
    {
        "key": "stock",
        "label": "Stock on hand",
        "requires": ("stock",),
        "optional": (),
        "expression": "SUM({stock})",
    },
)


def _is_numeric_type(declared_type):
    """True when a declared SQLite type can hold a number."""

    normalized = str(declared_type or "").strip().upper()

    if normalized in _NUMERIC_TYPES:
        return True

    for marker in _NON_NUMERIC_MARKERS:
        if marker in normalized:
            return False

    # Anything not explicitly textual and not in the known set is
    # treated as unknown rather than numeric, which fails closed:
    # an unrecognized type never silently becomes a metric source.
    return False


def _resolve_role(schema, role):
    """Return "table.column" for the first column that satisfies role.

    None means the role is absent from this schema. It is never
    approximated by a different column or by a literal.
    """

    pattern = _ROLE_PATTERNS.get(role)

    if pattern is None:
        return None

    for table in schema.get("tables", []):

        for column in table.get("columns", []):

            name = str(column.get("name", "")).lower()

            if pattern.search(name) and _is_numeric_type(column.get("type")):

                return f"{table['name']}.{column['name']}"

    return None


def _missing_reason(missing):
    """A plain sentence naming the columns that do not exist."""

    labels = [_ROLE_LABEL.get(role, role) for role in missing]

    if len(labels) == 1:
        joined = labels[0]
    else:
        joined = ", ".join(labels[:-1]) + " or " + labels[-1]

    return f"this database has no {joined} column"


def resolve_metrics(schema):
    """Split the registry into what this schema supports and what not.

    Returns (available, unavailable). Each available entry carries the
    concrete expression and the columns it resolved; each unavailable
    entry carries the reason, never a guessed substitute.
    """

    available = []
    unavailable = []

    for definition in METRIC_DEFINITIONS:

        resolved = {}
        missing = []

        for role in definition["requires"]:

            column = _resolve_role(schema, role)

            if column is None:
                missing.append(role)
            else:
                resolved[role] = column

        if missing:

            unavailable.append(
                {
                    "key": definition["key"],
                    "label": definition["label"],
                    "missing": missing,
                    "reason": _missing_reason(missing),
                }
            )

            continue

        for role in definition.get("optional", ()):

            column = _resolve_role(schema, role)

            if column is not None:
                resolved[role] = column

        times_quantity = (
            " * " + resolved["quantity"]
            if "quantity" in resolved
            else ""
        )

        expression = definition["expression"].format(
            times_quantity=times_quantity,
            **resolved,
        )

        available.append(
            {
                "key": definition["key"],
                "label": definition["label"],
                "columns": resolved,
                "expression": expression,
            }
        )

    return available, unavailable


def limitation(metric_key, schema):
    """A clear limitation for a named metric, or None if it is available.

    Written for the person who asked, and it names the reason rather
    than returning an empty value a reader could mistake for zero.
    """

    key = str(metric_key or "").strip().lower()

    definition = next(
        (item for item in METRIC_DEFINITIONS if item["key"] == key),
        None,
    )

    if definition is None:
        return (
            f"'{key}' is not a defined business metric, "
            "so it cannot be computed from this database."
        )

    _available, unavailable = resolve_metrics(schema)

    match = next(
        (item for item in unavailable if item["key"] == key),
        None,
    )

    if match:
        return (
            f"{definition['label']} cannot be computed: "
            f"{match['reason']}."
        )

    return None


def date_columns(schema):
    """Names of columns that can carry a date, as "table.column"."""

    found = []

    for table in schema.get("tables", []):

        for column in table.get("columns", []):

            name = str(column.get("name", ""))

            if _DATE_PATTERN.search(name.lower()):

                found.append(f"{table['name']}.{column['name']}")

    return found


def describe_for_prompt(schema):
    """The metric definitions to place in the system instruction.

    Lists what is computable and, just as importantly, what is not, so
    the model is told the limitation instead of having to guess one.
    """

    available, unavailable = resolve_metrics(schema)

    lines = ["Available (use exactly these definitions):"]

    if available:

        for metric in available:
            lines.append(
                f"- {metric['label']} = {metric['expression']}"
            )

    else:

        lines.append("- none detected in this schema")

    lines.append("")
    lines.append(
        "Unavailable (required columns are absent; do not compute, "
        "approximate or substitute zero):"
    )

    if unavailable:

        for metric in unavailable:
            lines.append(f"- {metric['label']}: {metric['reason']}")

    else:

        lines.append("- none")

    dates = date_columns(schema)

    lines.append("")

    if dates:

        lines.append(
            "Date columns available for time grouping: "
            + ", ".join(dates)
        )

    else:

        lines.append(
            "No date column exists; do not group by a date or time."
        )

    return "\n".join(lines)
