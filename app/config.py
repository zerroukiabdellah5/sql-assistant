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
# APP_ACCESS_CODE IS a secret (it grants paid provider calls) but it is
# deliberately defaulted so the demo works out of the box. Change it
# before exposing the deployment to anyone.

APP_ACCESS_CODE = os.getenv("APP_ACCESS_CODE", "1234")

# Optional dedicated signing secret for the visitor cookies. It is not
# required: app/access.py derives the signing key from APP_API_KEY
# when this is unset, and fails closed when neither is configured. Set
# it only if you want to rotate visitor sessions without rotating the
# admin secret.
ACCESS_SIGNING_SECRET = os.getenv("ACCESS_SIGNING_SECRET")

ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "tiix_access")
TRIAL_COOKIE_NAME = os.getenv("TRIAL_COOKIE_NAME", "tiix_trial")

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

TRIAL_MAX_ATTEMPTS = _positive_int_env(
    "TRIAL_MAX_ATTEMPTS",
    5,
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
