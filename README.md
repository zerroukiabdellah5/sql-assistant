<p align="center">
  <img src="public/tiix-logo-symbol.png" alt="TIIX SQL Studio logo" width="160">
</p>

<h1 align="center">TIIX SQL Studio</h1>

<p align="center">
  Ask questions in plain English. Get validated, read-only SQL — and the results.
</p>

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white">
  <img alt="Deployed on Vercel" src="https://img.shields.io/badge/Deployed-Vercel-000000?logo=vercel&logoColor=white">
</p>

---

## Overview

**TIIX SQL Studio** is a natural-language interface to a SQLite database. A
user types a question such as *"Which product category has the highest total
sales?"*, and the application translates it into a single, validated,
read-only SQL query, executes it, and returns the rows, the generated SQL and
a short explanation.

The project targets a common gap between non-technical users and relational
data: people understand the questions they want to ask but not the SQL needed
to answer them. TIIX SQL Studio bridges that gap while keeping the safety
properties of a read-only database tool — the generated query is never
allowed to mutate data.

## Problem Statement

- Analysing relational data normally requires knowing SQL, a schema and a
  client tool.
- Letting a language model run arbitrary SQL against a live database is
  dangerous: it can drop, alter or exfiltrate data.
- A useful assistant therefore needs to **translate language into SQL** and
  simultaneously **guarantee that only safe, read-only queries ever execute**.

TIIX SQL Studio addresses this by pairing a language model with a strict
validation and read-only execution layer, and by treating every generated
query as untrusted input.

## How It Works

1. **Question → prompt.** The user's natural-language question is combined
   with a compact description of the database schema (table, column and
   foreign-key metadata only) and, optionally, the recent turns of the
   current session.
2. **Prompt → SQL.** The configured large language model provider returns a
   candidate SQL query plus a plain-English explanation.
3. **Validation.** Every candidate query passes through a validation layer
   that:
   - accepts only statements beginning with `SELECT` or `WITH`,
   - rejects multiple/stacked statements,
   - rejects `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`,
     `TRUNCATE`, `REPLACE`, `ATTACH` and `DETACH`.
4. **Plan check.** Before execution, the query plan is inspected to confirm
   the statement is read-only.
5. **Read-only execution.** The database is opened in SQLite read-only mode
   (`mode=ro`), results are fetched in batches and capped at a maximum number
   of rows.
6. **Response.** The API returns the SQL, the explanation, the data, a
   truncated flag, a provenance block (provider, model, timing, row counts)
   and a correctness/verification note (the rows are produced reliably; the
   system does not claim the query answers the question).

## Features

Verified against the current implementation (`app/`):

- **Natural-language to SQL** through a provider abstraction that supports
  **NVIDIA** (default) and **Google Gemini**, selected with `LLM_PROVIDER`.
- **Strict read-only enforcement**: only `SELECT`/`WITH` statements, single
  statements only, forbidden-keyword rejection, SQLite `mode=ro`, and a
  read-only query-plan check.
- **Schema inspection** over tables, columns and foreign keys, served to the
  UI without exposing rows or credentials.
- **Multi-turn sessions and history**: create, list, view and delete
  sessions; recent turns are passed back to the model as context.
- **Approval workflow**: risky, non-read-only operations are classified and
  routed for explicit approve/reject rather than executed silently.
- **Sandboxed uploads**: import `.db` / `.sqlite` / `.sqlite3`, `.sql` and
  `.xlsx` files into an isolated sandbox; uploads never mix with the main
  dataset. Size and row limits apply.
- **Client-side CSV export** and **chart visualisation** (Chart.js) of result
  sets.
- **Server-side PDF reports** generated with ReportLab (tables and native
  bar charts).
- **Access control for visitors**: a free trial allowance per browser, after
  which an App Access Code unlocks a session. The admin API key continues to
  guard the session, upload, approval and report endpoints.
- **Provenance and observability**: request correlation IDs, token-usage
  tracking, timing metadata and error sanitisation that never returns raw
  provider, database or filesystem messages.
- **Rate limiting and LLM concurrency caps** as per-instance defence in
  depth.

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 |
| Web framework | FastAPI + Starlette |
| ASGI server (local) | Uvicorn |
| Data validation | Pydantic |
| Database | SQLite (standard library `sqlite3`) |
| LLM providers | NVIDIA (OpenAI-compatible API), Google Gemini |
| Documents | ReportLab (PDF), openpyxl (Excel import) |
| Configuration | python-dotenv |
| Frontend | Static HTML/CSS/JavaScript, Chart.js (CDN) |
| Testing | pytest |
| Deployment | Vercel (Python serverless functions) |
| Dependency management | uv (`uv.lock`), pinned `requirements.txt` |

## Architecture

```
Browser (index.html + JS)
        │  HTTP
        ▼
FastAPI application  (app/main.py, served by api/index.py on Vercel)
        │
        ├── Authentication / visitor access   app/auth.py, app/access.py
        ├── Rate limiting + concurrency cap   app/ratelimit.py, app/llm_concurrency.py
        ├── Prompt construction               app/prompts.py
        ├── LLM provider abstraction          app/llm.py
        ├── SQL validation + safety gate      app/validate.py, app/safety.py
        ├── Read-only execution               app/db.py  (SQLite, mode=ro)
        ├── Schema metadata                   app/schema.py
        ├── Sessions / history / approvals    app/history.py, app/approvals.py
        ├── Uploads (sandboxed)               app/uploads.py
        ├── Reports (PDF)                     app/reports.py
        └── Provenance / observability        app/provenance.py, app/observability.py,
                                              app/token_usage.py, app/correctness.py
```

The local development server and the Vercel deployment both serve the **same**
FastAPI object (`app.main:app`). On Vercel, `api/index.py` simply re-exports
it, so local and deployed behaviour cannot drift apart.

## Repository Structure

```
sql-assistant/
├── api/
│   └── index.py            # Vercel serverless entrypoint; re-exports app.main:app
├── app/                    # Application package
│   ├── main.py             # FastAPI app, routes, startup
│   ├── config.py           # Central environment configuration
│   ├── auth.py             # API-key authentication and signed sessions
│   ├── access.py           # Visitor trial + App Access Code
│   ├── db.py               # Read-only SQLite connection and execution
│   ├── validate.py         # SQL validation rules
│   ├── safety.py           # Operation classification (AUTO / APPROVAL)
│   ├── llm.py              # Provider abstraction (NVIDIA, Gemini)
│   ├── llm_concurrency.py  # Per-instance LLM concurrency cap
│   ├── prompts.py          # Prompt construction
│   ├── schema.py           # Schema metadata extraction
│   ├── history.py          # Sessions and message history
│   ├── approvals.py        # Approval storage and decisions
│   ├── uploads.py          # Sandboxed import of .db/.sql/.xlsx
│   ├── reports.py          # PDF report generation
│   ├── provenance.py       # Query provenance metadata
│   ├── correctness.py      # Verification notes
│   ├── token_usage.py      # Token accounting
│   ├── observability.py    # Logging, request IDs, error sanitisation
│   └── ratelimit.py        # Per-instance rate limiting
├── tests/                  # pytest suite (637 tests)
├── public/                 # Brand assets served at the site root
│   ├── tiix-logo-symbol.png
│   └── tiix-logo-full.png
├── assets/                 # Source logo assets
├── index.html              # Main workspace UI
├── about.html              # About page
├── contact.html            # Contact page
├── privacy.html            # Privacy page
├── main.py                 # Local entrypoint → app.main.run()
├── database.py             # Demo dataset schema setup
├── seed_demo_data.py       # Optional demo-data generator
├── store.db                # Tracked, read-only demo dataset
├── requirements.txt        # Pinned production dependencies (generated)
├── pyproject.toml          # Project metadata and dependency groups
├── uv.lock                 # Locked dependency graph
├── .python-version         # Interpreter pin (3.13)
├── vercel.json             # Vercel build and routing configuration
├── .vercelignore           # Files excluded from the deployment upload
└── DEPLOYMENT.md           # Deployment reference (see below)
```

## Prerequisites

- **Python 3.13** (the repository pins `3.13` in `.python-version` and
  `requires-python = ">=3.13"`).
- **Git**.
- A language-model provider key for the `/api/ask` endpoint:
  - NVIDIA: an API key and a model ID, **or**
  - Google Gemini: an API key.
- Optional: [uv](https://docs.astral.sh/uv/) for reproducible installs.

## Local Setup (Windows PowerShell)

```powershell
# 1. Clone the repository
git clone https://github.com/zerroukiabdellah5/sql-assistant.git
Set-Location sql-assistant

# 2. Create and activate a virtual environment
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1

# If script execution is blocked for this session only:
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 3. Install runtime dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Alternative: install with uv

```powershell
uv sync
```

`uv sync` installs the runtime dependencies **and** the `dev` dependency
group (which contains pytest), so it is convenient for development.

### Create the environment file

Create a `.env` file in the repository root. Use placeholders and replace
them with your own values. **Never commit this file.**

```dotenv
# Required: shared admin secret guarding the /api surface.
APP_API_KEY=<your-shared-admin-secret>

# Provider selection: nvidia (default) or gemini
LLM_PROVIDER=nvidia

# NVIDIA provider (required when LLM_PROVIDER=nvidia)
NVIDIA_API_KEY=<your-nvidia-api-key>
NVIDIA_MODEL=<your-nvidia-model-id>

# Google Gemini provider (required when LLM_PROVIDER=gemini)
GEMINI_API_KEY=<your-google-gemini-api-key>

# App Access Code handed to visitors after the free trial
APP_ACCESS_CODE=<your-app-access-code>

# Optional: contact address shown in the unlock modal
OWNER_CONTACT_EMAIL=<your-contact-email>
```

## Environment Variables

### Required

| Name | Purpose |
|---|---|
| `APP_API_KEY` | Shared admin secret guarding the `/api` surface. If unset, the application **fails closed** (503) rather than serving unauthenticated. It is also the derived signing key for visitor cookies. |

### Provider (at least one required for `/api/ask`)

| Name | Purpose |
|---|---|
| `LLM_PROVIDER` | Active provider: `nvidia` (default) or `gemini`. |
| `NVIDIA_API_KEY` | NVIDIA API key. |
| `NVIDIA_MODEL` | NVIDIA model ID. **Not defaulted** — must be provided. |
| `GEMINI_API_KEY` | Google Gemini API key. |

The UI, `/`, `/health`, `/api/version` and the public schema route all work
without a provider configured; only `/api/ask` needs one.

### Visitor access

| Name | Default | Purpose |
|---|---|---|
| `APP_ACCESS_CODE` | present in source | Code that unlocks access after the free trial. **Change it before any public deployment.** |
| `ACCESS_SIGNING_SECRET` | derived from `APP_API_KEY` | Optional dedicated signing secret for visitor cookies. |
| `TRIAL_MAX_ATTEMPTS` | `5` | Free requests per browser. |
| `TRIAL_SESSION_TTL_SECONDS` | `2592000` | Lifetime of a part-used free allowance. |
| `ACCESS_SESSION_TTL_SECONDS` | `43200` | Lifetime of an unlocked visitor session. |
| `OWNER_CONTACT_EMAIL` | empty | Shown in the unlock modal when set. No mail is sent. |

### Optional (safe defaults)

`NVIDIA_BASE_URL`, `AUTH_COOKIE_NAME`, `AUTH_SESSION_TTL_SECONDS`,
`AUTH_COOKIE_SECURE`, `APP_RATE_LIMIT_ENABLED`, `ASK_RATE_LIMIT_REQUESTS`,
`ASK_RATE_LIMIT_WINDOW_SECONDS`, `LOGIN_RATE_LIMIT_REQUESTS`,
`LOGIN_RATE_LIMIT_WINDOW_SECONDS`, `UPLOAD_RATE_LIMIT_REQUESTS`,
`UPLOAD_RATE_LIMIT_WINDOW_SECONDS`, `ACCESS_UNLOCK_RATE_LIMIT_REQUESTS`,
`ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS`, `ACCESS_REQUEST_RATE_LIMIT_REQUESTS`,
`ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS`, `LLM_MAX_CONCURRENCY`,
`LLM_CONCURRENCY_RETRY_AFTER_SECONDS`, `LLM_TIMEOUT_SECONDS`,
`LLM_RETRY_BACKOFF_CAP_SECONDS`, `META_DB_PATH`, `UPLOAD_DIR`.

See `app/config.py` for the authoritative definitions and defaults.

## Running the Application Locally

With the virtual environment active and `.env` configured:

```powershell
python main.py
```

The server starts on:

```
http://127.0.0.1:8000
```

Open that URL in a browser. Useful endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /` | Main workspace UI |
| `GET /health` | Liveness check |
| `GET /about`, `/contact`, `/privacy` | Informational pages |
| `GET /api/version` | Version and provider-configured flags |
| `GET /api/schema` | Schema metadata (protected) |
| `POST /api/ask` | Natural-language query endpoint |

## Running the Tests

The test suite lives in `tests/` and uses pytest. pytest is a development
dependency and is **not** part of `requirements.txt`.

```powershell
# If you installed runtime dependencies only:
pip install pytest

# If you used uv sync, pytest is already available:
# uv run pytest

python -m pytest -q
```

Current status of the suite:

```
637 passed
```

## Deployment Overview

The application is deployed to **Vercel** as a Python serverless function.
`vercel.json` builds `api/index.py` and routes every request to it, and
`public/**` is bundled so brand assets are served from the site root.

Key points:

- The Vercel build installs the pinned `requirements.txt` only; pytest is
  excluded.
- Required environment variables (`APP_API_KEY`, the provider key/model and
  visitor-access settings) are configured in the Vercel project, never in
  Git.
- Runtime writes fall back to `/tmp` because the deployment filesystem is
  read-only; sessions and uploads are therefore not durable across instances.

The full deployment reference — required variables, storage behaviour,
dependency generation, function-duration considerations and unverified
items — is documented in **[`DEPLOYMENT.md`](DEPLOYMENT.md)**. Read it before
deploying.

## Security Notes

The following properties are enforced by the current implementation:

- **Fail-closed authentication.** If `APP_API_KEY` is not configured, guarded
  routes answer `503` instead of serving unauthenticated.
- **Read-only by construction.** Only `SELECT`/`WITH` queries are permitted;
  multiple statements and mutating keywords are rejected, the database is
  opened with `mode=ro`, and the query plan is checked before execution.
- **Signed HttpOnly cookies.** Authentication and visitor-access state are
  stateless HMAC-signed tokens in `HttpOnly` cookies; credentials are never
  exposed to frontend JavaScript.
- **Constant-time credential comparison** for the shared API key.
- **Error sanitisation.** Raw provider, database and filesystem error messages
  are replaced with a generic message plus a reference ID; the real traceback
  is logged server-side only.
- **Secrets are never logged or returned.** `/api/version` reports only
  booleans indicating whether a provider is configured.
- **No secrets in the repository.** `.env` and `.env*` are ignored by Git and
  excluded from the deployment upload; `.gitignore` and `.vercelignore`
  document the intent.
- **Layered limits.** Per-instance rate limiting and an LLM concurrency cap
  bound abuse; these are defence-in-depth, not global quotas.
- **Default access code must be changed.** The App Access Code ships with a
  default value in source; set `APP_ACCESS_CODE` before any public use.

> If a real secret has ever been committed, rotate it and purge it from
> history — the repository should contain placeholder values only.

## Screenshots

No screenshots are included in this repository. Add them under a `docs/`
folder (for example `docs/screenshot-workspace.png`) and embed them here.

## Contributors

**Abdellah Zerrouki** — project author and sole contributor (confirmed by
Git history).

Responsibilities: application design and implementation (FastAPI routes,
authentication and visitor access, SQL validation and read-only execution,
LLM provider abstraction, sessions/history, approvals, uploads, reports,
provenance and observability), the frontend workspace, the test suite, and
the Vercel deployment configuration.

## License

No license file is present in this repository. No license is currently
specified; all rights are reserved by the author unless a license is added.
