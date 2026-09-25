# ============================================================
# PROMPT CONSTRUCTION (SCHEMA-DRIVEN)
# ============================================================
# The system prompt is generated from the inspected database:
# tables, columns and FK-derived relationships. Nothing here is
# hard-coded to a specific store schema.
# ============================================================

from app.config import (
    MAX_HISTORY_FIELD_CHARS,
    MAX_HISTORY_TURNS,
)
from app.schema import (
    list_relationships,
    schema_to_text,
    table_names,
)


def truncate_field(value, limit):

    text = str(value or "")

    if len(text) > limit:
        return text[:limit] + "…"

    return text


def build_history_block(history):

    recent = history[-MAX_HISTORY_TURNS:]

    if not recent:
        return "None. This is the first question in the session."

    lines = []

    for index, turn in enumerate(recent, start=1):

        lines.append(f"Turn {index}")
        lines.append(
            f"User: {truncate_field(turn.prompt.strip(), MAX_HISTORY_FIELD_CHARS)}"
        )

        if turn.sql.strip():
            lines.append(
                f"SQL: {truncate_field(turn.sql.strip(), MAX_HISTORY_FIELD_CHARS)}"
            )

        lines.append("")

    return "\n".join(lines).strip()


# ============================================================
# DATA-DRIVEN EXAMPLES
# ============================================================

def _build_examples(schema):

    examples = []

    for table in schema["tables"]:

        if len(table["columns"]) >= 2:

            name = table["name"]
            examples.append(
                (
                    f"User:\n"
                    f"Show me all rows from {name}\n\n"
                    f"JSON:\n"
                    f'{{"sql": "SELECT * FROM \'{name}\'", '
                    f'"explanation": "Reads the {name} table in full."}}'
                )
            )
            break

    # A single join example built from a real foreign key.
    for table in schema["tables"]:

        if not table["foreign_keys"]:
            continue

        fk = table["foreign_keys"][0]
        left = table["name"]
        right = fk["table"]
        left_on = fk["from"]
        right_on = fk["to"]

        left_col = next(
            (c["name"] for c in table["columns"] if c["name"] != left_on),
            "*",
        )
        right_columns = [
            c["name"]
            for t in schema["tables"]
            if t["name"] == right
            for c in t["columns"]
        ]
        right_col = right_columns[0] if right_columns else "*"

        examples.append(
            (
                f"User:\n"
                f"List items from {left} joined with {right}\n\n"
                f"JSON:\n"
                f'{{"sql": "SELECT {left}.{left_col}, {right}.{right_col} '
                f'FROM {left} JOIN {right} ON {left}.{left_on} = '
                f'{right}.{right_on}", '
                f'"explanation": "Combines {left} and {right} on '
                f'{left}.{left_on} = {right}.{right_on}."}}'
            )
        )
        break

    return examples


# ============================================================
# SYSTEM INSTRUCTION
# ============================================================

def build_system_instruction(schema):
    """schema: result of app.schema.inspect_database(...)."""

    schema_text = schema_to_text(schema)
    relationships = list_relationships(schema)
    tables = table_names(schema)

    example_block = "\n\n".join(_build_examples(schema))

    relationships_block = (
        "\n".join(f"- {rel}" for rel in relationships)
        if relationships
        else "- None detected. Join only via columns that clearly match."
    )

    return f"""
You are a Text-to-SQL engine with conversational memory.

Convert the user's natural language request into
ONE valid SQLite SELECT query, plus a short plain-English
explanation of that query.

DATABASE SCHEMA:

{schema_text}

TABLES AVAILABLE:

{tables}

RELATIONSHIPS (foreign keys detected in this database):

{relationships_block}

OUTPUT FORMAT:

Return a JSON object with exactly these keys:
- "sql": one SQLite SELECT (or WITH ... SELECT) statement
- "explanation": 1-2 sentences describing what the query does, in plain English

STRICT RULES:

- Return ONLY valid JSON. No Markdown. No extra keys.
- Only SELECT statements are allowed.
- Use only tables and columns from the schema. Never invent tables or columns.
- Preserve the exact meaning of the user's request and ALL conditions.
- Do not add conditions that were not requested.
- Prefer INNER JOIN unless the request clearly needs unmatched rows.
- If a table has no foreign keys, do not guess joins between unrelated tables.
- Use conversation history to resolve follow-ups such as "them", "those products", "how many were ordered", or "break that down".
- If a follow-up refers to a previous result, keep the same filters and joins, then add the new aggregation, join, or projection.
- The explanation must describe the SQL, not repeat the user's wording verbatim.

Examples:

{example_block}
""".strip()


# ============================================================
# USER MESSAGE
# ============================================================

def build_user_message(user_prompt, history):

    history_block = build_history_block(history)

    return f"""
CONVERSATION HISTORY:
{history_block}

CURRENT REQUEST:
{user_prompt}
""".strip()