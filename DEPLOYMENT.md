# Deployment — Vercel / serverless

Reference for deploying this application to Vercel. Names only, no
values: **no secret belongs in this repository.**

---

## 1. What gets deployed

| File | Role |
|---|---|
| `api/index.py` | The Vercel serverless ASGI entrypoint. Exposes the existing FastAPI object as `app`. |
| `vercel.json` | Builds `api/index.py` with `@vercel/python` and routes every request to it. |
| `.python-version` | Interpreter pin (`3.13`). |
| `requirements.txt` | Exact, pinned production dependencies. Generated from `uv.lock`. |
| `app/` | The application. |
| `index.html` | The UI. Served by the `GET /` route, so it ships with the function. |
| `store.db` | The read-only dataset. Tracked in git on purpose. |
| `pyproject.toml`, `main.py`, `database.py` | Project metadata, local dev server, dataset setup script. |

`.vercelignore` keeps secrets, local tooling, runtime state, tests and
the untracked `*_demo/` material out of the upload.

---

## 2. Required environment variables

Set these in the Vercel project's environment variables. **Never in
git.**

### Required

| Name | Consequence if unset |
|---|---|
| `APP_API_KEY` | The application **fails closed**: `configured_secret()` raises 503, so every `/api/*` route answers 503. This is deliberate — see `app/auth.py`. |

This is one shared secret. There is no per-user identity and no
lockout; every holder is the same principal
(`_API_KEY_IDENTITY` in `app/ratelimit.py`).

### One provider, required for `/api/ask`

| Name | Consequence if unset |
|---|---|
| `NVIDIA_API_KEY` | NVIDIA provider unusable. |
| `NVIDIA_MODEL` | NVIDIA provider unusable. **Not defaulted** — it must be provided. |

**or**

| Name | Consequence if unset |
|---|---|
| `GEMINI_API_KEY` | Gemini provider unusable. |

Selected with `LLM_PROVIDER` (`nvidia` or `gemini`, default `nvidia`).
`/api/version` reports only `gemini_configured` / `nvidia_configured`
booleans, never a key.

The UI, `/`, `/health` and `/api/schema` all work with no provider
configured. Only `/api/ask` needs one.

### Set by the platform, verify it is present

| Name | Why it matters |
|---|---|
| `VERCEL` | `app/config.py:_writable_path()` branches on it. When set, `META_DB_PATH` and `UPLOAD_DIR` resolve to `/tmp` instead of the read-only project directory, and `AUTH_COOKIE_SECURE` defaults to `True`. **Without it the application tries to write into the deployment bundle.** |

### Optional, all with safe defaults

`NVIDIA_BASE_URL`, `LLM_PROVIDER`, `AUTH_COOKIE_NAME`,
`AUTH_SESSION_TTL_SECONDS`, `AUTH_COOKIE_SECURE`,
`APP_RATE_LIMIT_ENABLED`, `ASK_RATE_LIMIT_REQUESTS`,
`ASK_RATE_LIMIT_WINDOW_SECONDS`, `LOGIN_RATE_LIMIT_REQUESTS`,
`LOGIN_RATE_LIMIT_WINDOW_SECONDS`, `UPLOAD_RATE_LIMIT_REQUESTS`,
`UPLOAD_RATE_LIMIT_WINDOW_SECONDS`, `LLM_MAX_CONCURRENCY`,
`LLM_CONCURRENCY_RETRY_AFTER_SECONDS`, `LLM_TIMEOUT_SECONDS`,
`LLM_RETRY_BACKOFF_CAP_SECONDS`, `META_DB_PATH`, `UPLOAD_DIR`.

Precedence for the two writable paths is
`explicit env override > /tmp (when VERCEL is set) > project dir`.

---

## 3. Storage: what persists and what does not

`store.db` and `index.html` ship with the deployment and are only ever
read. `app/db.py` opens the database with `?mode=ro`.

Everything the application writes at runtime goes to a writable path,
which on Vercel means `/tmp`:

| Data | Path on Vercel | Written by |
|---|---|---|
| Sessions, messages, approvals | `/tmp/app_meta.db` | `app/history.py:62,75` (`CREATE TABLE IF NOT EXISTS` on every connect) |
| Imported uploads (`.db`, `.sql`, `.xlsx`) | `/tmp/uploads/` | `app/uploads.py:38,87` |
| Generated PDF reports | `/tmp/uploads/reports/` | `app/reports.py:60,307` |

### What this means

- **Sessions are not durable.** A session created on one function
  instance is not visible to another. A cold start loses them.
  Approvals behave the same way: an approval recorded on one instance
  may not exist on the next.
- **Uploads are not durable.** `/api/imports` lists whatever the
  serving instance happens to hold. An import can vanish between the
  upload and the query that uses it, and uploads are never garbage
  collected — they disappear only when the instance's filesystem does.
- **A report is generated and returned inside one invocation.**
  `build_report_pdf()` writes the file and the route streams it back
  immediately, so report generation needs no persistence. This is the
  one write path that is correct on a serverless filesystem.
- **Nothing here is a database.** No external storage is wired up.
  Adding one is a separate step; nothing in this repository pretends
  otherwise.

The `/tmp` lifetime itself is platform behaviour. It is writable and
per-instance — that much the code depends on — but exactly how long it
survives must be confirmed against Vercel's own documentation before
this is relied upon.

### In-memory state is per-instance too

| State | Module | Scope |
|---|---|---|
| Rate-limit buckets | `app/ratelimit.py` | Per instance, resets on cold start |
| LLM in-flight counter | `app/llm_concurrency.py` | Per instance, resets on cold start |
| Authentication sessions | `app/auth.py` | **Stateless** — a signed HMAC token in an HttpOnly cookie. Survives cold starts and works across instances. No server-side session store. |

The rate limiter and the concurrency cap are **per-instance
defense-in-depth, not global quotas.** The real ceilings are
`limit x live instances`. A global limit needs a Vercel edge / WAF
rule, which does not exist yet.

---

## 4. Dependencies

`requirements.txt` is generated, not hand-written:

```
uv lock
uv export --frozen --no-hashes --no-emit-project --no-dev \
    --format requirements-txt > requirements.txt
```

`--no-dev` is what keeps `pytest` and its exclusive dependencies out
of the production environment. They live in `[dependency-groups] dev`
in `pyproject.toml`.

Versions are pinned; artifact hashes are not, because the
platform-specific wheels cannot be verified from this repository.
`uv.lock` records the sha256 of every artifact.

---

## 5. Function duration

`maxDuration` is deliberately **not** set in `vercel.json`. The
worst case for `/api/ask` is computed below, but the ceiling it has
to fit under depends on the Vercel plan and runtime configuration,
which cannot be established from this repository.

Worst case, from the current constants:

```
LLM_RETRIES                 = 2            app/config.py:211
LLM_TIMEOUT_SECONDS          = 60           app/config.py:224
LLM_RETRY_BACKOFF_CAP_SECONDS = 5           app/config.py:232

attempts          = LLM_RETRIES + 1                = 3
provider calls    = 3 x 60s                        = 180 s
backoff           = min(1.5*1, 5) + min(1.5*2, 5)  = 1.5 + 3.0 = 4.5 s
                                                     ---------
                                          total  ~184.5 s
```

Backoff formula: `app/llm.py:226-237`.

Two things follow. The value to configure must exceed ~185 s plus
schema inspection, validation and database time. And the bound only
holds for the NVIDIA path, which passes `LLM_TIMEOUT_SECONDS` to the
SDK (`app/llm.py:659`); the Gemini path (`app/llm.py:521-533`) sets no
timeout, so its worst case is bounded by SDK defaults rather than by
this application.

---

## 6. Not yet verified

Confirm against Vercel's documentation before relying on these:

- Maximum function duration for the chosen plan, and the
  `functions.maxDuration` value to set.
- Whether `/tmp` persists for the full invocation on the target
  runtime.
- Whether `VERCEL` is injected into the deployed functions.
- Memory and CPU sizing for a 25 MB buffered upload plus ReportLab
  rendering.
- Cold-start latency from importing the provider SDKs.