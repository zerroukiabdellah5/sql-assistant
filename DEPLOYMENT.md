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

### Visitor access: free attempts and the App Access Code

`/api/ask` is a public route. Anyone may use it, with no credential, for
`TRIAL_MAX_ATTEMPTS` requests per browser (six by default), then must
present `APP_ACCESS_CODE`. The seventh attempt answers
`403 FREE_TRIAL_EXHAUSTED`, which is what opens the access-code dialog in
the frontend.

The rest of the anonymous surface is `/api/public/schema`, the
`/api/access/*` pair and the `/api/visitor/*` demo endpoints (`schema`,
`imports`, `sessions`, `approvals` and `report`). Everything else under
`/api` — including `/api/sessions`, `/api/approvals`, `/api/report`,
`/api/upload`, `/api/imports` and `/api/version` — still requires
`APP_API_KEY`.

| Name | Default | Consequence if unset or wrong |
|---|---|---|
| `APP_ACCESS_CODE` | **unset** | **Set this in production.** There is no fallback value: with the variable missing, `/api/access/unlock` answers `401` for every code, so an unconfigured deployment cannot be unlocked — fail closed rather than accept a value published in the repository. The trial itself keeps working. |
| `ACCESS_SIGNING_SECRET` | derived from `APP_API_KEY` | Falls back to a domain-separated HMAC derived from `APP_API_KEY`, so no extra secret must be managed. Set it explicitly if you rotate `APP_API_KEY`: rotating the fallback invalidates every access and trial cookie, i.e. everyone falls back to the free allowance. |
| `TRIAL_MAX_ATTEMPTS` | `6` | Free requests per browser before the code is demanded. |
| `TRIAL_COOKIE_NAME` | `tiix_trial` | Cookie holding the signed attempt count. |
| `ACCESS_COOKIE_NAME` | `tiix_access` | Cookie holding the signed unlocked state. |
| `ACCESS_SESSION_TTL_SECONDS` | `43200` (12h) | How long an unlocked visitor stays unlocked. |
| `TRIAL_SESSION_TTL_SECONDS` | `2592000` (30d) | How long a partly-used free allowance survives. |
| `VISITOR_COOKIE_NAME` | `tiix_visitor` | Cookie identifying an anonymous uploader. |
| `VISITOR_SESSION_TTL_SECONDS` | `2592000` (30d) | How long an anonymous upload identity survives. |
| `OWNER_CONTACT_EMAIL` | empty | Rendered in the unlock dialog as a `mailto:` link so a visitor whose trial has ended can ask for a code. **Not a mail transport**: `/api/access/request` validates the address, stores nothing, sends nothing, and answers `notification_sent: false`. Honour those requests by hand. |

A production deployment must configure at least `APP_API_KEY`, one
provider key, `APP_ACCESS_CODE` (so the dialog can actually unlock) and
`OWNER_CONTACT_EMAIL` (so the dialog can show a contact). All four are
set in the Vercel project's environment variables; none of them belongs
in git.

`APP_API_KEY` remains required even though `/api/ask` is public: it is
the signing key for the visitor cookies (trial, unlocked access and the
anonymous upload identity), so without it `require_ask_access` and the
`/api/visitor/*` routes raise 503. `/api/public/schema` is the one
endpoint that works with no secret configured at all, so the page still
renders.

The trial counter, the access cookie and the anonymous visitor identity
are all stateless signed cookies, so they survive cold starts and work
across instances exactly like the admin session. Two consequences,
stated plainly:

- **The free allowance is per browser, not per person.** It is enforced
  by an HttpOnly cookie, so clearing cookies or using a private window
  resets it. That is a deliberate trade for having no server-side store
  on a serverless platform; it raises the cost of casual abuse without
  raising it against anyone who is trying.
- **Rotating `ACCESS_SIGNING_SECRET` locks out** every unlocked visitor
  and every part-used allowance until they unlock again.

### Optional, all with safe defaults

`NVIDIA_BASE_URL`, `LLM_PROVIDER`, `AUTH_COOKIE_NAME`,
`AUTH_SESSION_TTL_SECONDS`, `AUTH_COOKIE_SECURE`,
`APP_RATE_LIMIT_ENABLED`, `ASK_RATE_LIMIT_REQUESTS`,
`ASK_RATE_LIMIT_WINDOW_SECONDS`, `LOGIN_RATE_LIMIT_REQUESTS`,
`LOGIN_RATE_LIMIT_WINDOW_SECONDS`, `UPLOAD_RATE_LIMIT_REQUESTS`,
`UPLOAD_RATE_LIMIT_WINDOW_SECONDS`, `ACCESS_UNLOCK_RATE_LIMIT_REQUESTS`,
`ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS`,
`ACCESS_REQUEST_RATE_LIMIT_REQUESTS`,
`ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS`,
`LLM_MAX_CONCURRENCY`,
`LLM_CONCURRENCY_RETRY_AFTER_SECONDS`, `LLM_TIMEOUT_SECONDS`,
`LLM_RETRY_BACKOFF_CAP_SECONDS`, `META_DB_PATH`, `UPLOAD_DIR`,
`STORAGE_BACKEND`, `STORAGE_REQUIRE_PERSISTENT`, `UPLOAD_TTL_SECONDS`,
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`,
`R2_ENDPOINT`, `R2_REGION`, `R2_PREFIX`.

Both visitor cookies inherit `AUTH_COOKIE_SECURE`, so they cannot
disagree with the admin session about whether the deployment is https.

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
| Imported uploads (owner) | `databases/…` under `UPLOAD_DIR`, or an R2 key | `app/uploads.py`, through `app/storage.py` |
| Imported uploads (anonymous, per visitor) | `visitors/<id>/…` under `UPLOAD_DIR`, or an R2 key | `app/uploads.py` (via `app/visitor.py`), through `app/storage.py` |
| Import metadata records | `_meta/<scope>/<id>.json` under `UPLOAD_DIR`, or an R2 object | `app/storage.py` (`LocalMetadataStore` / `S3MetadataStore`) |
| Generated PDF reports | `/tmp/uploads/reports/` | `app/reports.py:60,307` |

### What this means

- **Sessions are not durable.** A session created on one function
  instance is not visible to another. A cold start loses them.
  Approvals behave the same way: an approval recorded on one instance
  may not exist on the next.
- **Uploads go through a storage layer with two implemented backends.**
  `app/storage.py` splits imports into an object (the file) and a
  metadata record (its owner, kind, tables and object key); a lookup goes
  through the record, so an import resolves only if it was recorded in
  the same namespace.
  - `STORAGE_BACKEND=local` (the default) keeps both under `UPLOAD_DIR`,
    i.e. `/tmp` on Vercel: **not durable across instances and not shared
    between them**, so an import can still vanish between the upload and
    the query that uses it. Local is for development and tests.
  - `STORAGE_BACKEND=r2` uses **Cloudflare R2** over its S3-compatible
    API (via `boto3`), which is durable and shared across instances. It
    requires `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
    and `R2_BUCKET` (optionally `R2_ENDPOINT`, `R2_REGION`, `R2_PREFIX`);
    an incomplete configuration is treated as **not configured**.
  Per-visitor isolation is enforced by the application — a signed cookie
  selects the scope, the record's key begins with that scope, and the
  record's scope is re-checked on every lookup — so it holds regardless
  of backend.
- **An unusable backend fails closed, not silently.** Any
  `STORAGE_BACKEND` other than `local`/`r2`, an `r2` backend with
  incomplete credentials, or `local` while `STORAGE_REQUIRE_PERSISTENT`
  is set, makes every storage path answer `503` with a fixed body plus
  the request id. The application never quietly falls back to `/tmp`.
  Set `STORAGE_REQUIRE_PERSISTENT=1` in production to turn silent data
  loss into an immediate, visible failure.
- **Imports now expire.** `UPLOAD_TTL_SECONDS` (default 30 days, `0`
  disables) is swept by the next import in the same namespace — there is
  no scheduler — so abandoned uploads no longer accumulate forever.
- **A report is generated and returned inside one invocation.**
  `build_report_pdf()` writes the file and the route streams it back
  immediately, so report generation needs no persistence. This is the
  one write path that is correct on a serverless filesystem.

### The durable backend (Cloudflare R2) and what is still unverified

Making uploads reliable in production needs **two** things the platform
does not provide, kept behind the interfaces in `app/storage.py`:

1. a shared **object store** for the files, and
2. a shared **metadata store** for the records.

Both are provided by the **R2 backend** (`STORAGE_BACKEND=r2`), which
uses R2's S3-compatible API through `boto3` (imported lazily, so local
development and the test suite never need it installed). Objects keep the
same keys the local backend uses; metadata records are JSON objects under
`_meta/<scope>/<id>.json`, listed with `ListObjectsV2`. `boto3` is a
declared runtime dependency and is present in `requirements.txt`.

Required variables (set them in the Vercel project, never in Git):

| Variable | Meaning |
|---|---|
| `STORAGE_BACKEND` | `r2` to use this backend |
| `STORAGE_REQUIRE_PERSISTENT` | `1` in production, to fail loudly if it is lost |
| `R2_ACCOUNT_ID` | R2 account id; the endpoint is derived from it |
| `R2_ACCESS_KEY_ID` | S3 access key |
| `R2_SECRET_ACCESS_KEY` | S3 secret (**secret**) |
| `R2_BUCKET` | bucket name |
| `R2_ENDPOINT` | optional; defaults to `https://<account>.r2.cloudflarestorage.com` |
| `R2_REGION` | optional; defaults to `auto` |
| `R2_PREFIX` | optional key prefix, to share a bucket with another app |

Credentials are read from the environment and are never logged or
returned; only `storage.status()` (a backend name and two booleans) is
exposed. No presigned URL is generated in this phase.

**Concurrency and ownership.** There is no read-modify-write: `save()`
PUTs one whole record to one key, and S3 PUT is atomic, so two concurrent
uploads (distinct random ids) touch different keys and cannot lose each
other's write. Isolation is structural — a record's key begins with its
scope, and the scope is re-checked on every read — so one visitor cannot
address another's records.

**What is NOT verified.** The R2 backend is covered by tests against a
**stubbed** S3 client (`tests/test_r2_storage.py`), which proves the code
and the fail-closed behaviour, not the provider. `boto3` and the backend
selection have **not** been exercised against a real bucket here. The
opt-in test `tests/test_r2_integration.py` talks to a real bucket and is
skipped unless dedicated `R2_TEST_*` credentials are configured; until it
is run green against a real bucket, **no claim is made that R2 works in
production**. Deadlines, ACLs, signing and eventual-consistency timing are
exactly the things a stub cannot model.

`ObjectStore.materialize(key)` returns a local path for a key. The local
backend returns the stored path; the R2 backend downloads the object to a
per-instance cache under `UPLOAD_DIR/_cache` (the SQLite read path needs a
real file), pruned by `UPLOAD_TTL_SECONDS`. That cache is still on `/tmp`,
so it is per-instance and rebuilt on demand — it is a cache, not a store.

### The 4.5 MB request-body ceiling

Vercel rejects a request body larger than **4.5 MB** with `413
FUNCTION_PAYLOAD_TOO_LARGE` **before** the function runs. The configured
`MAX_UPLOAD_MB=25` is therefore unreachable in production; the platform
is the real ceiling. The current upload route also buffers the whole body
(the size check runs after `await file.read()`), so it is not streamed.

Raising the limit safely needs a **direct-to-object-storage upload**:
the server issues a short-lived, namespace-scoped upload authorization,
the browser uploads straight to storage, and the server records the
resulting object key. That design is documented here but **not
implemented in this phase** — the current upload-size behaviour is
unchanged. Permanent storage credentials must never be handed to the
browser, and no presigned URL is generated by the current code.

- **The metadata database (`app_meta.db`) is still not durable.** Only
  uploads go through the storage layer. Sessions, messages and approvals
  remain on per-instance `/tmp`; wiring them to a shared store is a
  separate step not covered by R2.

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
| Access codes and free attempts | `app/access.py` | **Stateless** — signed HMAC tokens in HttpOnly cookies, same properties and same trade-off. |
| Anonymous upload identity | `app/visitor.py` | **Stateless** — a signed HMAC token in an HttpOnly cookie; the namespace is derived from it, not stored. |

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
- Whether `VERCEL` is injected into the deployed functions.
- Memory and CPU sizing for a 25 MB buffered upload plus ReportLab
  rendering.
- Cold-start latency from importing the provider SDKs.

### Confirmed platform limits (from Vercel's documentation)

- **Request/response body ≤ 4.5 MB.** A larger body is rejected with
  `413 FUNCTION_PAYLOAD_TOO_LARGE` before the function runs, so the
  effective upload ceiling is 4.5 MB, not `MAX_UPLOAD_MB`.
- **The filesystem is read-only; `/tmp` is writable scratch space of up
  to 500 MB per function instance**, local to that instance and not
  shared. This is why the local storage backend is not durable on
  Vercel.

Both are platform facts the code already assumes (the `/tmp` fallback in
`app/config.py:_writable_path()` and the per-instance caveat throughout
this document); they are recorded here so the assumptions are visible
rather than folklore.

**Database support:** Only SQLite (.db/.sqlite/.sqlite3) and uploaded sandbox data are supported. PostgreSQL, MySQL/MariaDB, Oracle, and remote database connections are not supported yet. Uploads (.sql, .xlsx, .csv) are used read-only. Imports are written through `app/storage.py`, which has two backends: `local` (filesystem, per-instance `/tmp` on Vercel and **not durable**) and `r2` (Cloudflare R2 over its S3-compatible API, durable and shared, configured with `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`). Selecting an unimplemented backend, configuring `r2` without credentials, or requiring persistence with only `local`, fails closed (`503`) rather than silently degrading. A Vercel function caps a request body at 4.5 MB regardless of `MAX_UPLOAD_MB`; raising it needs a direct-to-object-storage upload flow (not implemented in this phase). Anonymous uploads are isolated per browser by a signed HttpOnly visitor cookie (each `/api/visitor/*` route resolves only inside that visitor's own namespace); owner uploads remain application-wide and behind the application access key.
