import json
import os
import re
import sqlite3
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from google import genai
from google.genai import types


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE_PATH = os.path.join(BASE_DIR, "store.db")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not configured."
    )

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="Natural Language to SQL AI Assistant",
    version="4.0-GEMINI"
)

MAX_HISTORY_TURNS = 10


# ============================================================
# REQUEST
# ============================================================

class HistoryTurn(BaseModel):
    prompt: str
    sql: str = ""


class QueryRequest(BaseModel):
    prompt: str
    history: List[HistoryTurn] = Field(default_factory=list)


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():

    if not os.path.exists(DATABASE_PATH):
        raise FileNotFoundError(
            f"Database not found: {DATABASE_PATH}"
        )

    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


# ============================================================
# GET REAL DATABASE SCHEMA
# ============================================================

def get_schema():

    connection = get_connection()

    try:

        cursor = connection.cursor()

        cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name NOT LIKE 'sqlite_%'
        """)

        tables = cursor.fetchall()

        schema = []

        for table in tables:

            table_name = table["name"]

            if not re.match(
                r"^[A-Za-z_][A-Za-z0-9_]*$",
                table_name
            ):
                continue

            cursor.execute(
                f"PRAGMA table_info({table_name})"
            )

            columns = cursor.fetchall()

            column_list = []

            for column in columns:

                column_list.append(
                    f"{column['name']} {column['type']}"
                )

            cursor.execute(
                f"PRAGMA foreign_key_list({table_name})"
            )

            foreign_keys = cursor.fetchall()

            fk_list = []

            for fk in foreign_keys:

                fk_list.append(
                    f"{fk['from']} -> {fk['table']}.{fk['to']}"
                )

            table_line = (
                f"TABLE {table_name} "
                f"({', '.join(column_list)})"
            )

            if fk_list:
                table_line += (
                    f" FOREIGN KEYS: {', '.join(fk_list)}"
                )

            schema.append(table_line)

        return "\n".join(schema)

    finally:

        connection.close()


# ============================================================
# GEMINI TEXT → SQL + EXPLANATION
# ============================================================

def build_history_block(history):

    recent = history[-MAX_HISTORY_TURNS:]

    if not recent:
        return "None. This is the first question in the session."

    lines = []

    for index, turn in enumerate(recent, start=1):

        lines.append(f"Turn {index}")
        lines.append(f"User: {turn.prompt.strip()}")

        if turn.sql.strip():
            lines.append(f"SQL: {turn.sql.strip()}")

        lines.append("")

    return "\n".join(lines).strip()


def parse_gemini_payload(raw_text):

    cleaned = raw_text.strip()

    cleaned = re.sub(
        r"^```(?:json|sql)?",
        "",
        cleaned,
        flags=re.IGNORECASE
    ).strip()

    cleaned = cleaned.replace("```", "").strip()

    payload = None

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            payload = json.loads(match.group(0))

    if isinstance(payload, dict):
        sql = str(payload.get("sql") or "").strip()
        explanation = str(payload.get("explanation") or "").strip()
    else:
        sql = cleaned
        explanation = ""

    sql = re.sub(
        r"^```sql",
        "",
        sql,
        flags=re.IGNORECASE
    ).replace("```", "").strip()

    if not sql:
        raise ValueError(
            "Gemini did not return a SQL query."
        )

    if not explanation:
        explanation = "This query reads the requested rows from the store database."

    return sql, explanation


def ask_gemini(user_prompt, history):

    schema = get_schema()
    history_block = build_history_block(history)

    system_instruction = f"""
You are a Text-to-SQL engine with conversational memory.

Convert the user's natural language request into
ONE valid SQLite SELECT query, plus a short plain-English
explanation of that query.

DATABASE SCHEMA:

{schema}

RELATIONSHIPS:

- products.category_id = categories.id
- products.brand_id = brands.id
- orders.product_id = products.id

OUTPUT FORMAT:

Return a JSON object with exactly these keys:
- "sql": one SQLite SELECT (or WITH ... SELECT) statement
- "explanation": 1-2 sentences describing what the query does, in plain English

STRICT RULES:

- Return ONLY valid JSON. No Markdown. No extra keys.
- Only SELECT statements are allowed.
- Use only tables and columns from the schema.
- Never invent columns.
- Never invent tables.
- Preserve the exact meaning of the user's request.
- Preserve ALL conditions requested by the user.
- Do not add conditions that were not requested.
- When the user mentions brand, category, customer, order, or country, JOIN the related tables.
- Do not filter products.category or products.brand: those columns no longer exist. Use categories.name and brands.name instead.
- Prefer INNER JOIN unless the request clearly needs unmatched rows.
- Use conversation history to resolve follow-ups such as "them", "those products", "how many were ordered", "same brand", or "break that down".
- If a follow-up refers to a previous result, keep the same filters and joins, then add the new aggregation, join, or projection.
- The explanation must describe the SQL, not repeat the user's wording verbatim.

Examples:

User:
Show me Nike products

JSON:
{{
  "sql": "SELECT products.name, products.price, products.stock, brands.name AS brand, categories.name AS category FROM products JOIN brands ON products.brand_id = brands.id JOIN categories ON products.category_id = categories.id WHERE brands.name LIKE '%Nike%'",
  "explanation": "Lists Nike products with price, stock, brand, and category by joining products to brands and categories."
}}

User:
Total quantity ordered per brand

JSON:
{{
  "sql": "SELECT brands.name AS brand, SUM(orders.quantity) AS total_quantity FROM orders JOIN products ON orders.product_id = products.id JOIN brands ON products.brand_id = brands.id GROUP BY brands.name ORDER BY total_quantity DESC",
  "explanation": "Adds up ordered quantity for each brand and sorts brands from most to least ordered."
}}

Follow-up after Nike products:
User:
How many of them were ordered?

JSON:
{{
  "sql": "SELECT products.name, SUM(orders.quantity) AS total_ordered FROM orders JOIN products ON orders.product_id = products.id JOIN brands ON products.brand_id = brands.id WHERE brands.name LIKE '%Nike%' GROUP BY products.name",
  "explanation": "Counts how many units of each Nike product appear in orders, using the same Nike brand filter as the previous question."
}}
"""

    user_message = f"""
CONVERSATION HISTORY:
{history_block}

CURRENT REQUEST:
{user_prompt}
""".strip()

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0,
            max_output_tokens=1200,
            response_mime_type="application/json"
        )
    )

    if not response.text:
        raise ValueError(
            "Gemini returned an empty response."
        )

    return parse_gemini_payload(response.text)


# ============================================================
# SQL VALIDATION
# ============================================================

def validate_sql(sql):

    cleaned = sql.strip()

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


# ============================================================
# EXECUTE SQL
# ============================================================

def execute_sql(sql):

    validate_sql(sql)

    connection = get_connection()

    try:

        cursor = connection.cursor()

        cursor.execute(sql)

        rows = cursor.fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:

        connection.close()


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    return FileResponse(
        INDEX_PATH
    )


# ============================================================
# VERSION TEST
# ============================================================

@app.get("/api/version")
def version():

    return {
        "version": "4.0-GEMINI",
        "backend": "NEW_MAIN_PY",
        "database": DATABASE_PATH,
        "database_exists": os.path.exists(
            DATABASE_PATH
        ),
        "gemini_configured": True
    }


# ============================================================
# SCHEMA TEST
# ============================================================

@app.get("/api/schema")
def schema():

    try:

        return {
            "schema": get_schema()
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# MAIN AI ENDPOINT
# ============================================================

@app.post("/api/ask")
def ask_ai(request: QueryRequest):

    prompt = request.prompt.strip()

    if not prompt:

        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    try:

        print("\n")
        print("=" * 70)
        print("NEW REQUEST RECEIVED")
        print("=" * 70)

        print("USER PROMPT:")
        print(prompt)

        print("-" * 70)

        print("DATABASE:")
        print(DATABASE_PATH)

        print("-" * 70)

        print("HISTORY TURNS:", len(request.history))

        print("-" * 70)

        print("CALLING GEMINI...")

        generated_sql, explanation = ask_gemini(
            prompt,
            request.history
        )

        print("GEMINI SQL:")
        print(generated_sql)

        print("EXPLANATION:")
        print(explanation)

        print("-" * 70)

        data = execute_sql(
            generated_sql
        )

        print(
            f"RESULTS: {len(data)} rows"
        )

        print("=" * 70)
        print("\n")

        return {
            "success": True,
            "prompt": prompt,
            "sql": generated_sql,
            "explanation": explanation,
            "data": data,
            "count": len(data)
        }

    except Exception as e:

        print("ERROR:")
        print(str(e))

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    import uvicorn

    print("=" * 70)
    print("STARTING NEW MAIN.PY")
    print("VERSION: 4.0-GEMINI")
    print("=" * 70)
    print("DATABASE:", DATABASE_PATH)
    print("DATABASE EXISTS:", os.path.exists(DATABASE_PATH))
    print("GEMINI API: CONFIGURED")
    print("=" * 70)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False
    )