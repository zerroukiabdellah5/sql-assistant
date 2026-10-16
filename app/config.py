# ============================================================
# CONFIGURATION
# ============================================================
# Centralized environment configuration. This module is the
# single place that reads from the environment / .env file.
# Never print or expose secret values.
# ============================================================

import os
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ------------------------------------------------------------
# DATA FILES
# ------------------------------------------------------------
# store.db is the application data file (read-only by the runtime).
# app_meta.db holds sessions/messages/approvals (metadata only).
# uploads/ holds sandboxed imports; never mixed with store.db.
# ------------------------------------------------------------

DATABASE_PATH = os.path.join(BASE_DIR, "store.db")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# Public informational pages. Static, self-contained HTML documents
# served read-only alongside the main workspace. They contain no
# secrets and no application logic.
ABOUT_PATH = os.path.join(BASE_DIR, "about.html")
CONTACT_PATH = os.path.join(BASE_DIR, "contact.html")
PRIVACY_PATH = os.path.join(BASE_DIR, "privacy.html")

# Brand assets referenced by the HTML pages (for example the header
# logo at /tiix-logo-symbol.png). Vercel serves this directory at the
# site root; the application must serve the same URLs locally so the
# references resolve everywhere.
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

# Writable paths. store.db and index.html ship with the deployment and
# are only ever read, so they stay in the project directory.
# app_meta.db and uploads/ are written at runtime, and Vercel Functions
# expose a read-only filesystem, so there they fall back to /tmp.
# Precedence: explicit env override > Vercel /tmp > local project dir.
VERCEL_TMP_DIR = "/tmp"


def _writable_path(env_name, name):
    override = os.getenv(env_name)
    if override:
        return override
    if os.getenv("VERCEL"):
        return VERCEL_TMP_DIR + "/" + name
    return os.path.join(BASE_DIR, name)


META_DB_PATH = _writable_path("META_DB_PATH", "app_meta.db")
UPLOAD_DIR = _writable_path("UPLOAD_DIR", "uploads")

# ------------------------------------------------------------
# SECRETS (never log, never expose)
# ------------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# ------------------------------------------------------------
# APPLICATION AUTHENTICATION
# ------------------------------------------------------------
# APP_API_KEY is the shared secret guarding the API surface. It is
# deliberately NOT defaulted: a deployment that forgets it must fail
# closed (503) rather than serve the API unauthenticated. Set it in
# the local .env and in the deployment environment variables. Its
# value is never logged and never returned in a response.
APP_API_KEY = os.getenv("APP_API_KEY")

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "tiix_session")


def _positive_int_env(name, default):
    """Read a positive integer from the environment, else fall back."""

    raw = os.getenv(name)

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        return default

    return value if value > 0 else default


def _flag_env(name, default=False):
    """Read a boolean flag from the environment, else fall back."""

    raw = os.getenv(name)

    if raw is None:
        return default

    return raw.strip().lower() in ("1", "true", "yes", "on")


def _non_negative_int_env(name, default):
    """Read a non-negative integer from the environment, else fall back.

    Unlike _positive_int_env, zero is a meaningful value here (it
    disables the feature), so it is accepted; a negative or malformed
    value falls back rather than being clamped silently.
    """

    raw = os.getenv(name)

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        return default

    return value if value >= 0 else default


# 12 hours: long enough to get through a working session, short
# enough that a leaked cookie stops being useful quickly.
AUTH_SESSION_TTL_SECONDS = _positive_int_env(
    "AUTH_SESSION_TTL_SECONDS",
    43200,
)

# Secure on the deployment platform. Relaxed on localhost so the
# cookie still works over plain http during development.
AUTH_COOKIE_SECURE = _flag_env(
    "AUTH_COOKIE_SECURE",
    default=bool(os.getenv("VERCEL")),
)

# ------------------------------------------------------------
# VISITOR ACCESS (FREE TRIAL + APP ACCESS CODE)
# ------------------------------------------------------------
# The demo path. A visitor reaches the app with no credential at all,
# gets TRIAL_MAX_ATTEMPTS questions answered, and can then trade an
# App Access Code for an ACCESS_SESSION_TTL_SECONDS session.
#
# APP_API_KEY is deliberately NOT this gate: it is the owner/admin
# secret guarding sessions, uploads, approvals and reports, and it
# never leaves the server. Mixing the two would mean shipping the
# admin secret to every visitor, which is the one thing this design
# refuses to do.
#
# APP_ACCESS_CODE IS a secret (it grants paid provider calls) and it is
# deliberately NOT defaulted. An unset code must fail closed - nobody can
# unlock - rather than fall back to a value published in this repository,
# which would let anyone who reads the source straight through the gate.
# Set it in the deployment environment, never in a committed file.

APP_ACCESS_CODE = (os.getenv("APP_ACCESS_CODE") or "").strip()

# Optional dedicated signing secret for the visitor cookies. It is not
# required: app/access.py derives the signing key from APP_API_KEY
# when this is unset, and fails closed when neither is configured. Set
# it only if you want to rotate visitor sessions without rotating the
# admin secret.
ACCESS_SIGNING_SECRET = os.getenv("ACCESS_SIGNING_SECRET")

ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "tiix_access")
TRIAL_COOKIE_NAME = os.getenv("TRIAL_COOKIE_NAME", "tiix_trial")

# Anonymous, per-browser identity for visitor uploads and imports. It is
# NOT a credential: it grants no access to the guarded API and carries no
# secret. It exists only so that a visitor's own uploads live in a
# namespace keyed to their browser instead of a single shared one, which
# is what makes one visitor's imports unreadable to another. See
# app/visitor.py.
VISITOR_COOKIE_NAME = os.getenv("VISITOR_COOKIE_NAME", "tiix_visitor")

# Same window as an authenticated session: long enough to finish a
# working session, short enough that a leaked cookie stops being
# useful quickly.
ACCESS_SESSION_TTL_SECONDS = _positive_int_env(
    "ACCESS_SESSION_TTL_SECONDS",
    43200,
)

# The trial counter outlives an access session on purpose. A visitor
# who clears cookies or opens a private window is treated as a new
# visitor by the signed token as well, so the only thing a shorter
# window would achieve is handing out a second allowance every few
# hours to the same browser.
TRIAL_SESSION_TTL_SECONDS = _positive_int_env(
    "TRIAL_SESSION_TTL_SECONDS",
    2592000,
)

# The anonymous visitor identity lives as long as the trial allowance,
# and for the same reason: a visitor should keep access to the files they
# uploaded for as long as they can keep asking questions about them.
VISITOR_SESSION_TTL_SECONDS = _positive_int_env(
    "VISITOR_SESSION_TTL_SECONDS",
    2592000,
)

# Six free questions per browser: enough to demonstrate the product,
# small enough that the App Access Code is the natural next step. The
# frontend and the refusal message both read this value, so changing it
# here changes the trial everywhere at once.
TRIAL_MAX_ATTEMPTS = _positive_int_env(
    "TRIAL_MAX_ATTEMPTS",
    6,
)

# Shown to a visitor in the access modal. Deliberately configurable
# rather than hardcoded: it is the project's contact address, not a
# property of the code. When unset the modal hides the contact block
# instead of showing a placeholder that looks like a real address.
OWNER_CONTACT_EMAIL = (os.getenv("OWNER_CONTACT_EMAIL") or "").strip()

# ------------------------------------------------------------
# QUERY / PROMPT LIMITS
# ------------------------------------------------------------

MAX_ROWS = 2000
MAX_PROMPT_CHARS = 2000
MAX_HISTORY_TURNS = 10
MAX_HISTORY_FIELD_CHARS = 1000
MAX_SESSION_MESSAGES_CONTEXT = 20

# ------------------------------------------------------------
# RATE LIMITING (BEST-EFFORT, PER INSTANCE)
# ------------------------------------------------------------
# These bound a single client against a single warm instance. They
# are NOT a global quota: Vercel runs multiple instances that do not
# share memory, so the effective ceiling is (limit x instances) and
# a cold start resets the count. A Vercel edge rule is the global
# layer; these values are the in-application floor beneath it.
#
# None of these values is a secret.

APP_RATE_LIMIT_ENABLED = _flag_env(
    "APP_RATE_LIMIT_ENABLED",
    default=True,
)

# /api/ask is the only route that reaches NVIDIA, so it gets the
# tightest operator control.
ASK_RATE_LIMIT_REQUESTS = _positive_int_env(
    "ASK_RATE_LIMIT_REQUESTS",
    20,
)
ASK_RATE_LIMIT_WINDOW_SECONDS = _positive_int_env(
    "ASK_RATE_LIMIT_WINDOW_SECONDS",
    60,
)

# /api/run executes reviewed SQL and is deliberately the cheaper door:
# no provider call and no attempt spent. It has its OWN bucket so a
# caller cannot fold generation and execution into one allowance, and it
# defaults to the same ceiling as generation. An operator can tighten it
# - or widen it - without touching the generation limit, because a run
# reaches only a read-only database while a generation reaches the
# provider.
RUN_RATE_LIMIT_REQUESTS = _positive_int_env(
    "RUN_RATE_LIMIT_REQUESTS",
    ASK_RATE_LIMIT_REQUESTS,
)
RUN_RATE_LIMIT_WINDOW_SECONDS = _positive_int_env(
    "RUN_RATE_LIMIT_WINDOW_SECONDS",
    ASK_RATE_LIMIT_WINDOW_SECONDS,
)

# The login routes are the only brute-force surface for the shared
# APP_API_KEY, so the window is deliberately long.
LOGIN_RATE_LIMIT_REQUESTS = _positive_int_env(
    "LOGIN_RATE_LIMIT_REQUESTS",
    10,
)
LOGIN_RATE_LIMIT_WINDOW_SECONDS = _positive_int_env(
    "LOGIN_RATE_LIMIT_WINDOW_SECONDS",
    300,
)

# An upload buffers up to 25 MB in memory before it is validated, so
# it is expensive per request and worth a matching ceiling.
UPLOAD_RATE_LIMIT_REQUESTS = _positive_int_env(
    "UPLOAD_RATE_LIMIT_REQUESTS",
    10,
)
UPLOAD_RATE_LIMIT_WINDOW_SECONDS = _positive_int_env(
    "UPLOAD_RATE_LIMIT_WINDOW_SECONDS",
    60,
)

# /api/access/unlock is the same brute-force surface as the login
# routes, so it gets the same deliberately long window.
ACCESS_UNLOCK_RATE_LIMIT_REQUESTS = _positive_int_env(
    "ACCESS_UNLOCK_RATE_LIMIT_REQUESTS",
    10,
)
ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS = _positive_int_env(
    "ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS",
    300,
)

# /api/access/request is unauthenticated, takes no expensive work and
# stores nothing, so it is bounded mainly to stop a client from
# looping on it.
ACCESS_REQUEST_RATE_LIMIT_REQUESTS = _positive_int_env(
    "ACCESS_REQUEST_RATE_LIMIT_REQUESTS",
    10,
)
ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS = _positive_int_env(
    "ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS",
    300,
)

# ------------------------------------------------------------
# CONNECTIONS
# ------------------------------------------------------------

DB_BUSY_TIMEOUT_MS = 1500

# ------------------------------------------------------------
# SCHEMA INSPECTION LIMITS
# ------------------------------------------------------------

MAX_SCHEMA_TABLES = 40
MAX_SCHEMA_COLUMNS = 40

# ------------------------------------------------------------
# UPLOADS
# ------------------------------------------------------------

MAX_UPLOAD_MB = 25
MAX_IMPORT_ROWS = 5000
MAX_SQL_SCRIPT_STATEMENTS = 100
ALLOWED_DATABASE_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
ALLOWED_SQL_EXTENSIONS = (".sql",)
ALLOWED_EXCEL_EXTENSIONS = (".xlsx",)
ALLOWED_CSV_EXTENSIONS = (".csv",)

# ------------------------------------------------------------
# UPLOAD STORAGE
# ------------------------------------------------------------
# Where an uploaded import lives between the request that stored it and
# the request that queries it. See app/storage.py.
#
# STORAGE_BACKEND selects the provider:
#
#   "local" (default) - a filesystem store, perfect for development and
#                       tests and the WRONG answer on Vercel, where each
#                       function instance has its own /tmp and a
#                       read-only filesystem.
#   "r2"              - Cloudflare R2 over its S3-compatible API, the
#                       durable backend; see the R2 section below.
#
# Any other value, "r2" with incomplete credentials, or "local" while
# STORAGE_REQUIRE_PERSISTENT is set, fails closed with a 503 rather than
# silently writing to ephemeral storage. See DEPLOYMENT.md.
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()

# Set this in production to make the app refuse to upload at all while
# only ephemeral storage is configured. It turns a silent, hard-to-see
# data-loss bug into a loud, immediate 503.
STORAGE_REQUIRE_PERSISTENT = _flag_env(
    "STORAGE_REQUIRE_PERSISTENT",
    default=False,
)

# How long an import is kept before the next import in its namespace
# sweeps it away. Zero disables expiry. The default matches the visitor
# cookie lifetime, so a visitor's files live as long as the identity
# that owns them and no longer.
UPLOAD_TTL_SECONDS = _non_negative_int_env(
    "UPLOAD_TTL_SECONDS",
    2592000,
)


# ------------------------------------------------------------
# UPLOAD STORAGE: CLOUDFLARE R2 (S3-COMPATIBLE)
# ------------------------------------------------------------
# The durable backend, selected with STORAGE_BACKEND=r2. It is an S3
# implementation of the ObjectStore/MetadataStore interfaces in
# app/storage.py, talking to R2 over its S3-compatible endpoint.
#
# ALL FOUR of R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
# and R2_BUCKET must be present or the backend is treated as NOT
# configured: get_stores() then fails closed and every storage path
# answers 503. The application never silently falls back to the local
# filesystem, because on Vercel that is a per-instance /tmp and the
# upload would appear to succeed and then vanish.
#
# None of these values is ever logged or returned in a response; only
# storage.status() (a backend name and two booleans) is exposed.
#
#   R2_ENDPOINT  optional; defaults to
#                https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
#   R2_REGION    optional; defaults to "auto" (correct for R2)
#   R2_PREFIX    optional key prefix, so one bucket can be shared with
#                another application without collisions


def r2_settings():
    """The R2 configuration, or None while it is incomplete.

    Read at call time rather than import time, so a rotated credential
    or a test override takes effect immediately, and so a partially
    configured backend is a clean "not configured" instead of a
    half-built client. Returns a dict; never log it, it carries the
    secret access key.
    """

    required = {
        "account_id": (os.getenv("R2_ACCOUNT_ID") or "").strip(),
        "access_key_id": (
            os.getenv("R2_ACCESS_KEY_ID") or ""
        ).strip(),
        "secret_access_key": (
            os.getenv("R2_SECRET_ACCESS_KEY") or ""
        ).strip(),
        "bucket": (os.getenv("R2_BUCKET") or "").strip(),
    }

    if not all(required.values()):
        return None

    endpoint = (os.getenv("R2_ENDPOINT") or "").strip()

    if not endpoint:
        endpoint = (
            "https://"
            + required["account_id"]
            + ".r2.cloudflarestorage.com"
        )

    return {
        **required,
        "endpoint": endpoint,
        "region": (os.getenv("R2_REGION") or "auto").strip()
        or "auto",
        "prefix": (os.getenv("R2_PREFIX") or "").strip().strip("/"),
    }


# ------------------------------------------------------------
# LLM
# ------------------------------------------------------------

# Active provider selection. "nvidia" and "gemini" are supported.
# NVIDIA is the default because the app has switched to it.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "nvidia").strip().lower()

# NVIDIA OpenAI-compatible endpoint. Official default:
# https://integrate.api.nvidia.com/v1
NVIDIA_BASE_URL = os.getenv(
    "NVIDIA_BASE_URL",
    "https://integrate.api.nvidia.com/v1",
)

# NVIDIA model ID. Deliberately NOT defaulted: the project does not
# define one, so it must be provided via .env (NVIDIA_MODEL).
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL")

GEMINI_MODEL = "gemini-3.8-flash"
LLM_RETRIES = 2

# Explicit upstream timeout, in seconds.
#
# The OpenAI-compatible SDK defaults to a 600 second read/write/pool
# timeout, which is unusable here: /api/ask is a SYNCHRONOUS handler,
# so a slow upstream call occupies one thread of the shared AnyIO
# pool (40 by default) for the whole duration. Forty such calls
# starve every other API route, so a slow or hung provider becomes a
# full application outage rather than a slow query.
#
# 60 seconds stays generous for a schema-driven completion while
# keeping the worst case bounded at (1 + LLM_RETRIES) * 60 seconds.
LLM_TIMEOUT_SECONDS = _positive_int_env(
    "LLM_TIMEOUT_SECONDS",
    60,
)

# Cap on a single inter-attempt backoff wait. LLM_RETRIES is operator
# configurable, so the linear backoff is clamped to stay bounded no
# matter how high that value is set.
LLM_RETRY_BACKOFF_CAP_SECONDS = _positive_int_env(
    "LLM_RETRY_BACKOFF_CAP_SECONDS",
    5,
)

# ------------------------------------------------------------
# LLM CONCURRENCY (BEST-EFFORT, PER INSTANCE)
# ------------------------------------------------------------
# How many provider calls one warm instance may have in flight at
# once. See app/llm_concurrency.py: this counts concurrent calls, it
# does NOT bound spend, and it is not a global quota - Vercel
# instances do not share memory, so the real ceiling is
# (LLM_MAX_CONCURRENCY x live instances) and a cold start resets it.
#
# 4 is deliberately well under the 40-thread AnyIO pool. The cap
# exists to bound upstream pressure; if it were set near the pool
# size it would stop protecting the instance and start consuming the
# threads it is meant to leave free for every other route. It also
# has to stay above realistic concurrency: with the default ask
# allowance of 20 requests per 60 seconds, a handful of parallel
# users never reaches it, so ordinary traffic is unaffected.
LLM_MAX_CONCURRENCY = _positive_int_env(
    "LLM_MAX_CONCURRENCY",
    4,
)

# Retry-After sent with a 429 when the cap is saturated. A fixed small
# value on purpose: the time until a slot frees is not knowable (a
# call in flight may end in milliseconds or minutes), so any estimate
# would be a guess. A short value invites a prompt retry without
# telling the client a time that may not be true.
LLM_CONCURRENCY_RETRY_AFTER_SECONDS = _positive_int_env(
    "LLM_CONCURRENCY_RETRY_AFTER_SECONDS",
    1,
)
