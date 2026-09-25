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
# QUERY / PROMPT LIMITS
# ------------------------------------------------------------

MAX_ROWS = 2000
MAX_PROMPT_CHARS = 2000
MAX_HISTORY_TURNS = 10
MAX_HISTORY_FIELD_CHARS = 1000
MAX_SESSION_MESSAGES_CONTEXT = 20

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