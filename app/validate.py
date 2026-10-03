# ============================================================
# SQL VALIDATION
# ============================================================

import re


class ValidationError(ValueError):
    """A hand-written validation failure, written for a user.

    Subclasses ValueError, so every existing caller that catches
    ValueError keeps working unchanged.

    The type is the marker app.observability trusts when deciding what a
    client may be told. Raise it only for a message this project wrote on
    purpose to be read by the person who made the request. Anything else
    - a database, provider, filesystem, OS or SDK error - must not be
    wrapped in it, and is answered with a generic message plus a
    reference id instead.
    """


def validate_sql(sql):

    cleaned = sql.strip()

    # Allow one trailing semicolon (common in generated SQL) but reject
    # stacked/multiple statements.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    if ";" in cleaned:
        raise ValidationError(
            "Multiple SQL statements are not allowed."
        )

    if not re.match(
        r"^(SELECT|WITH)\b",
        cleaned,
        re.IGNORECASE
    ):
        raise ValidationError(
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
            raise ValidationError(
                f"Forbidden SQL keyword: {keyword}"
            )