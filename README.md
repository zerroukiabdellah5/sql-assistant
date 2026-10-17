<p align="center">
  <img src="public/tiix-logo-symbol.png" alt="TIIX SQL Studio logo" width="160">
</p>

<h1 align="center">TIIX SQL Studio</h1>

<p align="center">
  Ask questions in plain English. Get validated, read-only SQL — and the results.
</p>

<p align="center">
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.141.1-009688?logo=fastapi&logoColor=white">
  <img alt="Deployed on Vercel" src="https://img.shields.io/badge/Deployed-Vercel-000000?logo=vercel&logoColor=white">
</p>

---

## Overview

**TIIX SQL Studio** is a natural-language interface to a SQLite database. A
user types a question such as *"Which product category has the highest total
sales?"*, and the application translates it into a single, validated,
read-only SQL query. The user reviews (and may edit) the generated SQL in an
editable box, then runs it explicitly; the application returns the rows, the
generated SQL and a short explanation.

The project targets a common gap between non-technical users and relational
data: people understand the questions they want to ask but not the SQL needed
to answer them. TIIX SQL Studio bridges that gap while keeping the safety
properties of a read-only database tool — the generated query is never
allowed to mutate data.

## Project Evolution: Before and During October 2026

TIIX SQL Studio was not created in October. The application already existed
before October 2026, and October was a phase of expansion on top of that
foundation. Everything in this section is taken from this repository's own
Git history; each commit ID below was checked against `git log`.

### Before October 2026 — Original Foundation

- **September 19, 2026 — `445bf8c`, initial project.** A FastAPI application
  over a SQLite dataset (`store.db`, `database.py`), natural-language
  questions translated into SQL by a language model (Google Gemini at this
  stage), `SELECT`-only validation, schema awareness, conversational context,
  and a web interface that already displayed results with charts and CSV
  export. `vercel.json` and `requirements.txt` were present from the first
  commit.
- **September 19, 2026 — `fa01850`, Vercel/FastAPI deployment fix.**
- **September 26, 2026 — `a2d864d`, deployment preparation.** The code moved
  into the `app/` package, the pytest suite started, and the read-only
  safeguards still in force today were introduced there: SQLite `mode=ro`
  connections and `EXPLAIN QUERY PLAN` checks (`app/db.py`).
- **September 26, 2026 — `7d2166d`, dependency fix.** Vercel dependency
  resolution repaired and the production environment locked.

By the end of September the project already had its purpose (natural language
to validated, read-only SQL), its FastAPI/SQLite architecture, its interface
and its deployment setup.

### During October 2026 — Expansion of That Foundation

- **October 3, 2026 — `643854d`, security, provenance, correctness and LLM
  concurrency.** API-key authentication with fail-closed behaviour, signed
  sessions, execution provenance, verification notes, token accounting,
  observability, per-instance rate limiting and an LLM concurrency cap.
- **October 3, 2026 — `4cadeaa`, release build for Vercel.**
- **October 8, 2026 — `2243e4a`, enriched demo data** (`seed_demo_data.py`).
- **October 9, 2026 — `93bb6a2`, logo assets served on Vercel and contact
  email updated.**
- **October 15, 2026 — `bfae50b`, analytics and CSV import support**
  (`app/analytics.py`, `app/metrics.py`).
- **October 16, 2026 — `a66db32`, anonymous visitor demo and pluggable upload
  storage** (`app/visitor.py`, `app/storage.py`).
- **October 16, 2026 — `682404b`, upload dialog and database import flow
  restored.**
- **October 16, 2026 — `7d98f86`, review-before-run workflow and report
  hardening.** `POST /api/run` was added, splitting generation from execution
  into **Generate → Review/Edit → Run**, with signed generation tickets
  (`app/generation_ticket.py`) that record how the reviewed SQL came to exist.

**Key distinction:** October changed how safely and how transparently results
are produced, and how users review SQL before it runs; it did not change the
project's original natural-language-to-SQL purpose. Provenance records *how* a
result was produced — it does not guarantee that a query answers the question
it was asked, which is one reason the review step exists.

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

The workflow is deliberately two steps, so the user always sees the SQL
before anything runs against the database:

1. **Question → prompt.** The user's natural-language question is combined
   with a compact description of the database schema (table, column and
   foreign-key metadata only) and, optionally, the recent turns of the
   current session.
2. **Prompt → SQL (generation only).** The configured large language model
   provider returns a candidate SQL query plus a plain-English explanation.
   `POST /api/ask` returns the SQL, the explanation, the answering
   provider/model and a signed generation ticket. **Nothing is executed.**
3. **Validation.** Every candidate query passes through a validation layer
   that:
   - accepts only statements beginning with `SELECT` or `WITH`,
   - rejects multiple/stacked statements,
   - rejects `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`,
     `TRUNCATE`, `REPLACE`, `ATTACH` and `DETACH`.
4. **Review.** The SQL is shown in an editable box so the user can read and
   change it before running.
5. **Run (execution only).** `POST /api/run` re-validates the statement it is
   handed (the user may have edited it), inspects the query plan to confirm it
   is read-only, opens the database in SQLite read-only mode (`mode=ro`),
   fetches results in batches capped at a maximum number of rows, and returns
   them. This step never calls a provider and never charges a trial attempt.
6. **Response.** The run response returns the SQL, the explanation, the data,
   a truncated flag, a provenance block (provider, model, `sql_origin`,
   timing, row counts) and a correctness/verification note (the rows are
   produced reliably; the system does not claim the query answers the
   question).

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
- **Approval ledger**: operations are classified `AUTO`/`APPROVAL`
  (`app/safety.py`), and approve/reject decisions are recorded in a ledger —
  for the owner through the protected `POST /api/approvals` route, and for any
  browser through a clearly-example visitor demo (`/api/visitor/approvals`).
  The ledger is review bookkeeping: nothing is queued into it automatically,
  approved SQL is never executed by the application, and in practice risky
  SQL cannot run at all — validation rejects every non-`SELECT`/`WITH`
  statement before any execution path.
- **Sandboxed uploads**: import `.db` / `.sqlite` / `.sqlite3`, `.sql`, `.xlsx`
  and `.csv` files into an isolated sandbox; uploads never mix with the main
  dataset, and the import a request works against is chosen per request with
  `import_id`. Size and row limits apply.
- **Anonymous per-visitor uploads**: a visitor with no credential can upload
  and then list, describe, query and delete *their own* imports. Each browser
  is given a signed `HttpOnly` visitor cookie, and every visitor route
  resolves strictly inside that visitor's own namespace, so one visitor can
  never read or touch another's data. The owner `POST /api/upload` /
  `GET /api/imports` routes stay behind `require_auth`.
- **Anonymous-safe demo of Sessions, Approvals and PDF reports**: the
  Sessions, Approvals and PDF-report panels work for any browser with no
  sign-in. Sessions are stored per browser (`/api/visitor/sessions`), the
  Approvals panel shows a per-browser copy of clearly *example* operations
  (`/api/visitor/approvals`), and the server-side report is built from the
  caller's own request and their own import (`/api/visitor/report`). Owner
  data is never read: the owner ledgers (`/api/sessions`, `/api/approvals`)
  and `/api/report` stay behind `require_auth` and are used by scripts.
- **Client-side CSV export** and **chart visualisation** (Chart.js) of result
  sets.
- **Server-side PDF reports** generated with ReportLab (tables and native
  bar charts), plus a **client-side PDF export** of the current result set
  (a separate, browser-side feature).
- **Access control for visitors**: six free question attempts per browser,
  then the access-code dialog on the seventh, and an App Access Code that
  unlocks a session. Uploading, Sessions, Approvals and PDF reports are *not*
  gated by the trial or the code. The admin API key continues to guard the
  owner session, owner-upload, owner-approval and owner-report endpoints.
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
| LLM providers | NVIDIA (OpenAI-compatible API, via the `openai` client), Google Gemini (via `google-genai`) |
| Object storage (optional) | boto3 (Cloudflare R2 backend, `STORAGE_BACKEND=r2`) |
| Documents | ReportLab (PDF), openpyxl (Excel import) |
| File uploads | python-multipart (multipart form parsing) |
| Configuration | python-dotenv |
| Frontend | Static HTML/CSS/JavaScript, Chart.js (CDN) |
| Testing | pytest |
| Deployment | Vercel (Python serverless functions) |
| Dependency management | uv (`uv.lock`), pinned `requirements.txt` |

**Database support:** only SQLite — the bundled dataset plus uploaded sandbox
files — is supported. PostgreSQL, MySQL/MariaDB, Oracle and other remote
database connections are not supported yet.

## Architecture

```
Browser (index.html + JavaScript)
    |
    | HTTP
    v
FastAPI application  (app/main.py, served by api/index.py on Vercel)
    |
    +-- Configuration                      app/config.py
    +-- Authentication / visitor access    app/auth.py, app/access.py, app/visitor.py
    +-- Rate limiting + concurrency        app/ratelimit.py, app/llm_concurrency.py
    +-- Prompt construction                app/prompts.py
    +-- LLM provider abstraction           app/llm.py
    +-- SQL validation + safety gate       app/validate.py, app/safety.py
    +-- Read-only execution                app/db.py  (SQLite, mode=ro)
    +-- Schema metadata                    app/schema.py
    +-- Sessions / history / approvals     app/history.py, app/approvals.py
    +-- Sandboxed uploads + storage        app/uploads.py, app/storage.py
    +-- Analytics                          app/analytics.py, app/metrics.py
    +-- Reports (PDF)                      app/reports.py
    +-- Provenance / correctness           app/provenance.py, app/correctness.py,
                                           app/token_usage.py, app/observability.py,
                                           app/generation_ticket.py
```

The local development server and the Vercel deployment both serve the **same**
FastAPI object (`app.main:app`). On Vercel, `api/index.py` simply re-exports
it, so local and deployed behaviour cannot drift apart.

## Repository Structure

```
sql-assistant/
|-- api/
|   \-- index.py            # Vercel serverless entrypoint; re-exports app.main:app
|-- app/                    # Application package
|   |-- __init__.py
|   |-- main.py             # FastAPI app, routes, startup
|   |-- config.py           # Central environment configuration
|   |-- auth.py             # API-key authentication and signed sessions
|   |-- access.py           # Visitor trial + App Access Code
|   |-- visitor.py          # Anonymous per-visitor upload identity
|   |-- db.py               # Read-only SQLite connection and execution
|   |-- validate.py         # SQL validation rules
|   |-- safety.py           # Operation classification (AUTO / APPROVAL)
|   |-- llm.py              # Provider abstraction (NVIDIA, Gemini)
|   |-- llm_concurrency.py  # Per-instance LLM concurrency cap
|   |-- prompts.py          # Prompt construction
|   |-- schema.py           # Schema metadata extraction
|   |-- history.py          # Sessions and message history
|   |-- approvals.py        # Approval storage and decisions
|   |-- uploads.py          # Sandboxed import of .db/.sql/.xlsx/.csv (owner + visitor namespace)
|   |-- storage.py          # Pluggable upload storage backends (local, r2)
|   |-- analytics.py        # Chart planning and numeric analysis of results
|   |-- metrics.py          # Result metrics used by analytics and reports
|   |-- reports.py          # PDF report generation
|   |-- provenance.py       # Query provenance metadata
|   |-- correctness.py      # Verification notes
|   |-- generation_ticket.py # Signed generation tickets (generated/edited/manual)
|   |-- token_usage.py      # Token accounting
|   |-- observability.py    # Logging, request IDs, error sanitisation
|   \-- ratelimit.py        # Per-instance rate limiting
|-- tests/                  # pytest suite (983 tests: 981 passing, 2 skipped)
|-- public/                 # Brand assets served at the site root
|   |-- tiix-logo-symbol.png
|   \-- tiix-logo-full.png
|-- assets/                 # Source logo assets
|-- index.html              # Main workspace UI
|-- about.html              # About page
|-- contact.html            # Contact page
|-- privacy.html            # Privacy page
|-- main.py                 # Local entrypoint -> app.main.run()
|-- database.py             # Demo dataset schema setup
|-- seed_demo_data.py       # Optional demo-data generator
|-- store.db                # Tracked, read-only demo dataset
|-- requirements.txt        # Pinned production dependencies (generated)
|-- pyproject.toml          # Project metadata and dependency groups
|-- uv.lock                 # Locked dependency graph
|-- .python-version         # Interpreter pin (3.13)
|-- .env.example            # Tracked placeholder environment template
|-- vercel.json             # Vercel build and routing configuration
|-- .vercelignore           # Files excluded from the deployment upload
\-- DEPLOYMENT.md           # Deployment reference (see below)
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

The repository ships a placeholder template at [`.env.example`](.env.example).
Copy it and fill in your own values:

```powershell
Copy-Item .env.example .env
```

`.env` is ignored by Git and excluded from the deployment upload. **Never
commit it, and never put a real credential in `.env.example`** — that file
is tracked on purpose and must contain placeholders only.

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

# App Access Code handed to visitors after the six free attempts.
# No default: left empty, nobody can unlock until you set one.
APP_ACCESS_CODE=

# Free questions per browser before the unlock dialog appears.
TRIAL_MAX_ATTEMPTS=6

# Contact address shown as a mailto: link in the unlock dialog.
OWNER_CONTACT_EMAIL=zerroukiabdellah5@gmail.com
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
| `APP_ACCESS_CODE` | **unset (fails closed)** | Code that unlocks access after the six free attempts. Deployment configuration: with no code configured, `/api/access/unlock` refuses everything (`401`), so there is no published default to guess. |
| `ACCESS_SIGNING_SECRET` | derived from `APP_API_KEY` | Optional dedicated signing secret for visitor cookies. |
| `TRIAL_MAX_ATTEMPTS` | `6` | Free questions per browser. |
| `TRIAL_SESSION_TTL_SECONDS` | `2592000` | Lifetime of a part-used free allowance. |
| `ACCESS_SESSION_TTL_SECONDS` | `43200` | Lifetime of an unlocked visitor session. |
| `VISITOR_COOKIE_NAME` | `tiix_visitor` | Name of the signed `HttpOnly` cookie that identifies an anonymous uploader. |
| `VISITOR_SESSION_TTL_SECONDS` | `2592000` | Lifetime of a visitor upload identity. |
| `OWNER_CONTACT_EMAIL` | empty | Shown in the unlock dialog as a `mailto:` link. No mail is ever sent by the application. |

### The free trial and the access-code dialog

Every visitor gets **six free question attempts per browser**
(`TRIAL_MAX_ATTEMPTS=6`), counted in a signed, `HttpOnly` cookie — no
account, no sign-up, nothing stored server-side.

| Situation | What happens |
|---|---|
| Attempts 1–6 | The question is answered normally: schema-aware prompt, generated SQL, validation. The signed generation ticket that comes back then lets the visitor **run** that SQL (reviewed or edited) against the read-only database. |
| Running the SQL | `POST /api/run` executes the reviewed statement and is **free**: it costs no attempt, because no provider is called. A visitor qualifies to run once they have generated at least one statement; a visitor who has never generated gets `403 {"detail": "RUN_REQUIRES_GENERATION"}`. |
| Attempt 7 | `/api/ask` answers `403` with the flat body `{"detail": "FREE_TRIAL_EXHAUSTED", ...}`. The frontend keys on that machine-readable value and opens the access-code dialog — it never triggers on page load or on opening a panel. |
| In the dialog | The message states that the six-use trial has ended and offers the two ways forward: enter an App Access Code, or contact the owner. When `OWNER_CONTACT_EMAIL` is configured, the address is rendered as a clickable `mailto:` link that opens the visitor's own mail client. |
| Valid code | `/api/access/unlock` checks it in constant time, sets an `HttpOnly` session cookie and drops the trial counter. The visitor keeps asking. |
| Wrong code | A flat `401` with fixed wording — identical whatever was wrong, so the response cannot be used to probe the code. |
| Owner with `APP_API_KEY` / a session | Never charged and never locked out; the guarded owner routes (sessions, uploads, approvals, reports) stay behind the application access key. |

**Nothing is sent automatically.** `/api/access/request` accepts and
validates an email address, stores nothing, sends nothing, and answers
`notification_sent: false`; requests for a code are honoured by hand,
through the contact address above.

**The counter is client-held.** Clearing cookies or opening a private
window starts a fresh allowance — a deliberate trade for running with no
server-side store. The token is signed, so it can be read but never
edited to buy attempts back.

### The Upload workflow needs no credential

Clicking **Upload** opens the file picker directly. No sign-in and no App
Access Code are asked for, and the trial counter is not involved: importing
is free, and the App Access Code only unlocks free *questions*.

A visitor with no credential uploads through `POST /api/visitor/imports`.
The first request mints a signed, `HttpOnly` visitor cookie (the browser
cannot read or edit it), and the file is stored under that visitor's own
directory. `GET /api/visitor/imports`, `GET /api/visitor/schema` and
`DELETE /api/visitor/imports/{import_id}` then resolve **only** inside the
same namespace, so a second browser sees an empty list and gets a `404` for
anything else — it cannot list, read, query or delete another visitor's
data. The upload response never includes a server path.

The owner routes `POST /api/upload` and `GET /api/imports` are unchanged and
remain behind `require_auth`; signing in still uses the native form that
exchanges the application access key for an `HttpOnly` session cookie. No
secret is embedded in the frontend and no header is trusted from it.

### Sessions, Approvals and PDF reports need no credential

The three panels that used to sit behind the owner sign-in are served to a
credential-free browser through the same visitor identity the upload uses, so
the whole workspace is usable without signing in.

- **Sessions** — `GET|POST /api/visitor/sessions`,
  `GET|DELETE /api/visitor/sessions/{session_id}`. A session is created,
  listed, reloaded (with its own message history) and deleted strictly inside
  the caller's signed visitor id, in a dedicated set of tables that never
  mixes with the owner's.
- **Approvals** — `GET /api/visitor/approvals` and
  `POST /api/visitor/approvals/{approval_id}/approve|reject`. Each browser is
  seeded, once and idempotently, with a small fixed set of clearly *example*
  operations (a DDL and two DML items) so the approve/reject flow can be
  demonstrated end to end. No real owner operation is exposed and no two
  visitors share a ledger.
- **PDF report** — `POST /api/visitor/report`. It is the same ReportLab
  builder as `/api/report`, driven by the caller's own request and resolved
  against the caller's own import, so it documents the visitor's own query —
  never the owner's database.

Because these routes resolve in the visitor namespace, one browser can never
see another's sessions, approvals or report, and none of them can read owner
state. The owner equivalents (`/api/sessions`, `/api/approvals`,
`/api/report`) remain behind `require_auth` for scripts.

### Persistent upload storage

The deployment target's only writable space is a per-instance `/tmp`, which
does not survive a recycle and is not shared between function instances.
Imports are therefore written through a small storage layer
(`app/storage.py`) rather than straight to the filesystem. The layer has two
parts: an **object store** for the file and a **metadata store** for the
record that names its owner (a visitor namespace or the shared owner
namespace), kind, tables and object key. A lookup goes through the record,
so an import resolves only if it was recorded in the same namespace — a
stray file with a matching name is never enough.

The backend is selected with `STORAGE_BACKEND`:

- **`local`** (default) — a filesystem store, keeping the previous layout.
  It is what development and the test suite use. On Vercel it is a
  per-instance `/tmp`, so it is **not durable** and must not be relied on
  in production.
- **`r2`** — a durable Cloudflare R2 backend over its S3-compatible API,
  reached through `boto3`. Configure `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY` and `R2_BUCKET` (optionally `R2_ENDPOINT`,
  `R2_REGION`, `R2_PREFIX`). An incomplete configuration is treated as
  **not configured**.

Any other value, `r2` with incomplete credentials, or `local` while
`STORAGE_REQUIRE_PERSISTENT` is set, **fails closed** with `503`; the
application never silently falls back to ephemeral storage. Set
`STORAGE_REQUIRE_PERSISTENT=1` in production so a lost backend is a loud
failure rather than silent data loss.

The R2 backend is reached through `boto3`, imported lazily so the local
backend and the test suite run without it installed. Both interfaces are
S3: objects keep the same keys the local backend uses, and metadata
records are JSON objects under `_meta/<scope>/<id>.json`, listed with
`ListObjectsV2`. Isolation is structural (a record's key begins with its
scope, and the record's scope is re-checked on every lookup) and there is
no read-modify-write: a save PUTs one whole document to one key, and S3
PUT is atomic, so concurrent uploads cannot lose each other's write.
`materialize` downloads an object to a per-instance cache under
`UPLOAD_DIR/_cache`, pruned by `UPLOAD_TTL_SECONDS`.

Imports expire after `UPLOAD_TTL_SECONDS` (default `2592000`, 30 days,
matching the visitor cookie; `0` disables). There is no scheduler: the next
import in a namespace sweeps that namespace's stale records and their
objects. `GET /api/version` (protected) reports the storage state as
`{"backend", "available", "persistent", "require_persistent"}`; the public
`/health` is unchanged.

**Why uploads are not streamed straight to storage from the browser.** The
upload reaches the application through its own function, and a Vercel
function rejects a request body larger than **4.5 MB** (`413
FUNCTION_PAYLOAD_TOO_LARGE`) before the application sees it, so the
`MAX_UPLOAD_MB=25` limit defined in `app/config.py` is unreachable in
production and the effective ceiling is the platform's. Lifting it needs a
different flow — the browser uploading **directly** to object storage under a
short-lived, namespace-scoped upload authorization issued by the server, after
which the server records the object key. That flow is deliberately **not
implemented in this phase**; the upload-size behaviour is unchanged. Permanent
storage credentials must never reach the browser, and no presigned URL is
generated by the current code.

### Optional (safe defaults)

`NVIDIA_BASE_URL`, `AUTH_COOKIE_NAME`, `AUTH_SESSION_TTL_SECONDS`,
`AUTH_COOKIE_SECURE`, `ACCESS_COOKIE_NAME`, `TRIAL_COOKIE_NAME`,
`APP_RATE_LIMIT_ENABLED`, `ASK_RATE_LIMIT_REQUESTS`,
`ASK_RATE_LIMIT_WINDOW_SECONDS`, `RUN_RATE_LIMIT_REQUESTS`,
`RUN_RATE_LIMIT_WINDOW_SECONDS`, `LOGIN_RATE_LIMIT_REQUESTS`,
`LOGIN_RATE_LIMIT_WINDOW_SECONDS`, `UPLOAD_RATE_LIMIT_REQUESTS`,
`UPLOAD_RATE_LIMIT_WINDOW_SECONDS`, `ACCESS_UNLOCK_RATE_LIMIT_REQUESTS`,
`ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS`, `ACCESS_REQUEST_RATE_LIMIT_REQUESTS`,
`ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS`, `LLM_MAX_CONCURRENCY`,
`LLM_CONCURRENCY_RETRY_AFTER_SECONDS`, `LLM_TIMEOUT_SECONDS`,
`LLM_RETRY_BACKOFF_CAP_SECONDS`, `META_DB_PATH`, `UPLOAD_DIR`,
`VISITOR_COOKIE_NAME`, `VISITOR_SESSION_TTL_SECONDS`, `STORAGE_BACKEND`,
`STORAGE_REQUIRE_PERSISTENT`, `UPLOAD_TTL_SECONDS`, `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT`,
`R2_REGION`, `R2_PREFIX`.

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
| `GET /api/version` | Version, provider-configured flags and storage state (protected) |
| `GET /api/schema` | Schema metadata (protected) |
| `POST /api/ask` | Generation endpoint: natural-language question → SQL + signed ticket. Never executes. |
| `POST /api/run` | Execution endpoint: reviewed SQL → rows, provenance and chart. Never calls a provider and never charges a trial attempt. |

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
983 collected: 981 passed, 2 skipped
```

The two skipped tests are the opt-in Cloudflare R2 integration tests
(`tests/test_r2_integration.py`); they run only when dedicated
`R2_TEST_*` credentials are configured.

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
  read-only. The metadata database is therefore not durable across
  instances. Imports go through `app/storage.py`; set `STORAGE_BACKEND=r2`
  with the `R2_*` credentials to make them durable (see
  [`DEPLOYMENT.md`](DEPLOYMENT.md)), or the backend fails closed (`503`)
  rather than silently using ephemeral storage.

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
  application metadata — the provider name, booleans for whether each
  provider is configured, and the storage state — never a path, environment
  variable or credential.
- **No secrets in the repository.** `.env` and every real `.env*` file are
  ignored by Git and excluded from the deployment upload; `.gitignore` and
  `.vercelignore` document the intent. The one exception is
  `.env.example`, a tracked **placeholder** template with no credential in
  it.
- **Layered limits.** Per-instance rate limiting and an LLM concurrency cap
  bound abuse; these are defence-in-depth, not global quotas.
- **No default App Access Code.** `APP_ACCESS_CODE` is read from the
  environment with no fallback: a deployment that forgets it refuses every
  unlock attempt instead of accepting a value published in this
  repository. The six free attempts and the trial counter are unaffected,
  because they are signed with the API key rather than the code.
- **The demo UI never asks a visitor to sign in, and the trial gate is not a
  pop-up.** Every panel the interface exposes — query assistant, schema,
  history, charts, upload, Sessions, Approvals and PDF reports — is served by
  an unguarded route (`/api/ask`, `/api/run`, `/api/public/schema`, or the
  `/api/visitor/*` namespace), so nothing on page load and no button in the
  workspace can raise the sign-in dialog. The owner routes
  (`/api/sessions`, `/api/approvals`, `/api/report`, `/api/upload`,
  `/api/imports`) still answer `401` to a visitor and remain available to
  scripts holding `APP_API_KEY`. The access-code dialog opens only when
  `/api/ask` answers `403 FREE_TRIAL_EXHAUSTED`.

> If a real secret has ever been committed, rotate it and purge it from
> history — the repository should contain placeholder values only.

## Screenshots

No screenshots are included in this repository. Add them under a `docs/`
folder (for example `docs/screenshot-workspace.png`) and embed them here.

## Contributors

**Abdellah Zerrouki** — project author and sole contributor. All 14 commits
in this repository share a single Git author (handle `Abdoutiix92`).

Responsibilities: application design and implementation (FastAPI routes,
authentication and visitor access, SQL validation and read-only execution,
LLM provider abstraction, sessions/history, approvals, uploads, reports,
provenance and observability), the frontend workspace, the test suite, and
the Vercel deployment configuration.

## License

No license file is present in this repository. No license is currently
specified; all rights are reserved by the author unless a license is added.
