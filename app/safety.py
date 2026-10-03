# ============================================================
# OPERATION CLASSIFICATION + SAFETY
# ============================================================
# AUTO      = safe read-only operations (SELECT / WITH).
# APPROVAL  = mutations, imports, scripts or anything that must
#             not run silently. Never auto-execute APPROVAL.
# ============================================================

import re

from app.validate import ValidationError, validate_sql

AUTO = "AUTO"
APPROVAL = "APPROVAL"

_READ_ONLY_RE = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)


def classify_operation(sql):
    """Return AUTO for pure reads, APPROVAL for everything else."""

    cleaned = (sql or "").strip()

    if not cleaned:
        raise ValidationError("SQL is empty.")

    if _READ_ONLY_RE.match(cleaned):
        return AUTO

    return APPROVAL


def describe_operation(sql, limit=120):
    """Short human-readable label for approval contexts."""

    cleaned = (sql or "").strip()

    keyword = cleaned.split(None, 1)[0].upper() if cleaned else ""

    snippet = re.sub(r"\s+", " ", cleaned)

    if len(snippet) > limit:
        snippet = snippet[:limit] + "…"

    return f"{keyword} — {snippet}"


def ensure_auto(sql):
    """Gate: raise for anything that is not auto-safe."""

    validate_sql(sql)

    if classify_operation(sql) != AUTO:
        raise ValidationError(
            "This operation is not AUTO-safe and requires approval."
        )

    return AUTO