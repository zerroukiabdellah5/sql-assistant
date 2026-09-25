# ============================================================
# SQL VALIDATION
# ============================================================

import re


def validate_sql(sql):

    cleaned = sql.strip()

    # Allow one trailing semicolon (common in generated SQL) but reject
    # stacked/multiple statements.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    if ";" in cleaned:
        raise ValueError(
            "Multiple SQL statements are not allowed."
        )

    if not re.match(
        r"^(SELECT|WITH)\b",
        cleaned,
        re.IGNORECASE
    ):
        raise ValueError(
            "Only SELECT queries are allowed."
        )

    forbidden = [
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "REPLACE",
        "ATTACH",
        "DETACH"
    ]

    for keyword in forbidden:

        if re.search(
            rf"\b{keyword}\b",
            cleaned,
            re.IGNORECASE
        ):
            raise ValueError(
                f"Forbidden SQL keyword: {keyword}"
            )