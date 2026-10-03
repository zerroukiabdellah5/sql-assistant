# ============================================================
# APP — THIN ROUTES
# ============================================================
# Routes delegate to production modules. No business logic here.
# ============================================================

import os

from contextlib import asynccontextmanager
from typing import List, Optional

import logging

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import approvals, history, observability, uploads
from app.auth import (
    configured_secret,
    credentials_match,
    invalid_credentials,
    login_response,
    logout_response,
    presented_credential,
    require_auth,
)
from app.config import (
    APP_API_KEY,
    ASK_RATE_LIMIT_REQUESTS,
    ASK_RATE_LIMIT_WINDOW_SECONDS,
    AUTH_SESSION_TTL_SECONDS,
    DATABASE_PATH,
    GEMINI_API_KEY,
    INDEX_PATH,
    LLM_CONCURRENCY_RETRY_AFTER_SECONDS,
    LOGIN_RATE_LIMIT_REQUESTS,
    LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    MAX_PROMPT_CHARS,
    NVIDIA_API_KEY,
    NVIDIA_MODEL,
    UPLOAD_RATE_LIMIT_REQUESTS,
    UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
)
from app.db import execute_sql_with_metadata
from app.llm import (
    GenerationRef,
    LLMConcurrencySaturated,
    ask_active,
    get_provider,
)
from app import correctness, provenance
from app.ratelimit import (
    APPROVAL_RATE_LIMIT_REQUESTS,
    APPROVAL_RATE_LIMIT_WINDOW_SECONDS,
    DELETE_RATE_LIMIT_REQUESTS,
    DELETE_RATE_LIMIT_WINDOW_SECONDS,
    REPORT_RATE_LIMIT_REQUESTS,
    REPORT_RATE_LIMIT_WINDOW_SECONDS,
    rate_limit,
)
from app.reports import build_report_pdf
from app.schema import get_schema

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app):

    # Vercel Functions expose a read-only filesystem, so the runtime
    # metadata store and upload directory may not be creatable there.
    # Startup must not crash: the routes that need them report the
    # failure when they are actually used.
    try:

        history.init_meta_db()

    except Exception:

        logger.warning(
            "startup skipped: metadata store unavailable",
            exc_info=True,
        )

    try:

        if not os.path.exists(uploads._upload_root()):
            os.makedirs(uploads._upload_root(), exist_ok=True)

    except Exception:

        logger.warning(
            "startup skipped: upload directory unavailable",
            exc_info=True,
        )

    yield


# Interactive documentation and the OpenAPI schema are disabled:
# they enumerate every route and payload for anyone who can reach
# the deployment, and the frontend does not use them.
app = FastAPI(
    title="Natural Language to SQL AI Assistant",
    version="4.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# The request-id middleware is the outermost layer the application
# controls: it gives every request an id, echoes it as X-Request-ID and
# writes one access line, before authentication, before the rate limiter
# and before any expensive work. It therefore never replaces either of
# them - the guard and the limiter still run first for the work itself -
# and the id is never used as a rate-limit key. A request rejected by
# either still carries an id, which is exactly when a user needs one.
app.add_middleware(
    observability.RequestContextMiddleware
)


# FastAPI's default 422 body echoes the submitted input back: the
# offending field, its value and a Pydantic message. On a login form that
# is a credential oracle, and elsewhere it reflects prompts, SQL and API
# keys. This handler replaces it with a fixed message plus the reference
# id, so a caller learns that the request was malformed and nothing
# else.
@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
):
    return JSONResponse(
        status_code=422,
        content=observability.validation_error_payload(
            observability.request_id_of(request)
        ),
    )


# The authentication endpoints are PUBLIC by necessity: they are how
# a client proves it holds the secret and obtains a session cookie.
# This router is deliberately kept separate from the router that
# carries the authenticated routes, so the guard can never lock the
# door against its own key.
auth_router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
)

# Every remaining /api/* route lives on this router, and the guard is
# attached ONCE as a router-level dependency. Individual handlers hold
# no authentication code, so a route added here later is protected by
# construction and cannot accidentally ship unauthenticated.
api = APIRouter(
    prefix="/api",
    tags=["api"],
    dependencies=[Depends(require_auth)],
)


# ============================================================
# REQUEST MODELS
# ============================================================

class HistoryTurn(BaseModel):
    prompt: str
    sql: str = ""


class QueryRequest(BaseModel):
    prompt: str
    history: List[HistoryTurn] = Field(default_factory=list)
    session_id: Optional[str] = None


class SessionCreate(BaseModel):
    title: str = "New session"


class ApprovalCreate(BaseModel):
    kind: str
    sql: str
    context: str = ""


class ReportRequest(BaseModel):
    title: str = "SQL Report"
    question: str = ""
    sql: str = ""
    explanation: str = ""
    data: List[dict] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False

    # Provenance, echoed back from the /api/ask response so the exported
    # document carries the same source facts as the screen it came
    # from. Every one of these is a value the client already holds and
    # none is trusted: they go through the same sanitising helpers the
    # response itself used, and a value that does not have the right
    # shape is dropped rather than printed.
    source_database: str = ""
    source_sha256: str = ""
    model: str = ""


class AuthLogin(BaseModel):
    password: str = ""


# ============================================================
# AUTHENTICATION (PUBLIC BY NECESSITY)
# ============================================================
# The secret is accepted from the request body (typed by a human in
# the browser) or from the standard headers (scripts, probes). It is
# compared in constant time, never logged, and never echoed back:
# a successful login returns a status flag and an HttpOnly cookie,
# so the credential itself never reaches frontend JavaScript.

# The login limiter is declared once and shared by both login
# routes. It must run BEFORE the credential comparison, because the
# shared APP_API_KEY has no per-user identity and no lockout of its
# own, so this limiter IS the brute-force control. A 429 here is
# deliberately indistinguishable from a 401 in wording: it must not
# reveal whether the server holds a secret at all.
_login_limiter = rate_limit(
    scope="login",
    limit=LOGIN_RATE_LIMIT_REQUESTS,
    window_seconds=LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    pre_auth=True,
)


@auth_router.post("/login")
def login(
    response: Response,
    request: Request,
    payload: Optional[AuthLogin] = None,
    _limited: None = Depends(_login_limiter),
):

    # 503 when the server has no secret configured: fail closed.
    secret = configured_secret()

    presented = (payload.password.strip() if payload else "") or (
        presented_credential(request)
    )

    if not credentials_match(presented, secret):
        invalid_credentials()

    login_response(response, secret=secret)

    return {
        "authenticated": True,
        "expires_in": int(AUTH_SESSION_TTL_SECONDS),
    }


# The browser never gets the secret: it is typed into a native HTML form,
# the browser posts it straight to the server, and the server answers with
# a redirect plus the existing HttpOnly session cookie. No JavaScript reads
# the field, calls this route, or ever sees the credential. This is the same
# authentication as /login (same compare, same token, same cookie helper),
# exposed a second time only in the transport a browser can use.

@auth_router.post("/login-form")
def login_form(
    request: Request,
    password: str = Form(...),
    _limited: None = Depends(_login_limiter),
):

    # 503 when the server has no secret configured: fail closed.
    secret = configured_secret()

    if not credentials_match(password.strip(), secret):
        invalid_credentials()

    # 303 so the browser follows up with a GET and never re-posts
    # the credential.
    redirect = RedirectResponse(
        url="/",
        status_code=303,
    )

    login_response(redirect, secret=secret)

    return redirect


@auth_router.post("/logout")
def logout(response: Response):

    logout_response(response)

    return {
        "authenticated": False,
    }


app.include_router(auth_router)


# ============================================================
# HOME (PUBLIC)
# ============================================================

@app.get("/")
def home():

    return FileResponse(
        INDEX_PATH
    )


# ============================================================
# HEALTH (PUBLIC)
# ============================================================
# Kept public so platform liveness probes keep working without a
# credential. It reports nothing but a status flag and a boolean.

@app.get("/health")
def health():

    return {
        "status": "ok",
        "database_exists": os.path.exists(
            DATABASE_PATH
        )
    }


# ============================================================
# VERSION / SCHEMA (PROTECTED: attached to the guarded router)
# ============================================================

@api.get("/version")
def version():

    # Only application metadata. The absolute DATABASE_PATH used to be
    # returned here, which handed any authenticated caller the deployment
    # layout of the server (project root, /tmp fallback, store.db name).
    # The booleans below answer the same operational questions without
    # disclosing a single path, environment variable or secret.
    return {
        "version": "4.0",
        "backend": "PROD",
        "provider": get_provider().name,
        "database_exists": os.path.exists(
            DATABASE_PATH
        ),
        "gemini_configured": bool(GEMINI_API_KEY),
        "nvidia_configured": bool(
            NVIDIA_API_KEY and NVIDIA_MODEL
        )
    }


@api.get("/schema")
def schema(request: Request):

    try:

        return {
            "schema": get_schema()
        }

    except Exception as exc:

        # A sqlite3 message names the database file and the failing
        # query, so it is never returned verbatim.
        raise observability.sanitized_http_exception(
            exc, request
        ) from None


# ============================================================
# MAIN AI ENDPOINT (PROTECTED)
# ============================================================
# The costliest route in the application, and the only one that calls
# a provider. Two independent controls sit in front of it, and their
# order is deliberate:
#
#   Authentication -> Rate limiting -> Concurrency cap -> Provider
#
# The rate limiter is per caller over time, so it is the right place
# to stop one client spending the allowance of another. The cap in
# app/llm_concurrency.py is per instance at one instant, so it is the
# right place to stop the instance itself being overwhelmed. Running
# the limiter first means a caller who is already over their limit is
# told so without any provider capacity being consulted at all.
#
# The two controls are not substitutes: neither bounds the other, and
# only the edge layer in front of the function is global.

# Answered when the instance is at its concurrency cap. Says nothing
# about the cap, the in-flight count, the configuration or the
# provider: a saturation response must not become a way to measure
# this instance's load.
_CONCURRENCY_DETAIL = (
    "The assistant is busy right now. "
    "Please wait a moment and try again."
)

@api.post(
    "/ask",
    dependencies=[
        Depends(
            rate_limit(
                scope="ask",
                limit=ASK_RATE_LIMIT_REQUESTS,
                window_seconds=ASK_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
def ask_ai(payload: QueryRequest, request: Request):

    prompt = payload.prompt.strip()

    if not prompt:

        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    if len(prompt) > MAX_PROMPT_CHARS:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Prompt is too long "
                f"(max {MAX_PROMPT_CHARS} characters)."
            )
        )

    session_id = payload.session_id

    if session_id and not history.session_exists(session_id):

        raise HTTPException(
            status_code=404,
            detail="Session not found."
        )

    try:

        if session_id:

            stored_turns = history.build_history_turns(session_id)
            turns = [
                HistoryTurn(prompt=turn["prompt"], sql=turn["sql"])
                for turn in stored_turns
            ]

        else:

            turns = payload.history

        # Filled in by whichever provider answers, so provenance can
        # name the real one instead of the configured one.
        generation = GenerationRef()

        generated_sql, explanation = ask_active(
            prompt,
            turns,

            # Opaque correlation id only. The LLM layer logs it with the
            # token counts and never uses it as a key or a limit. The
            # same value is reused in the provenance block below: one
            # request, one id.
            request_id=observability.request_id_of(request),
            generation=generation,
        )

        data, execution = execute_sql_with_metadata(
            generated_sql
        )

        if session_id:

            history.add_message(
                session_id, "user",
                prompt=prompt,
            )
            history.add_message(
                session_id, "assistant",
                sql=generated_sql,
                explanation=explanation,
                count=len(data),
                truncated=execution.truncated,
            )

        return {
            "success": True,
            "prompt": prompt,
            "sql": generated_sql,
            "explanation": explanation,
            "data": data,
            "count": len(data),
            "truncated": execution.truncated,

            # Additive: everything above is unchanged. Describes how the
            # rows were produced; says nothing about whether they answer
            # the question. Built by a module whose helpers degrade to
            # null, so it cannot fail a query that already succeeded.
            "provenance": provenance.build_provenance(
                database_path=execution.database_path,
                request_id=observability.request_id_of(request),
                provider=generation.provider,
                model=generation.model,
                executed_at=execution.executed_at,
                elapsed=execution.elapsed_ms,
                rows_returned=execution.rows_returned,
                truncated=execution.truncated,
                total_matched=execution.total_matched,
                generated_by_ai=True,
            ),

            # Separate from provenance on purpose. The block above
            # describes how the rows were produced; this one states
            # that nothing checked whether they answer the question.
            # A client that reads only one of the two is misled by
            # neither, which is the whole point of the split. It takes
            # no arguments and cannot fail, so it is as safe to build
            # as it is to read.
            "verification": correctness.build_verification(),
        }

    except HTTPException:

        raise

    except LLMConcurrencySaturated:

        # The instance was already at LLM_MAX_CONCURRENCY provider
        # calls, so nothing was sent upstream and no quota was spent.
        # This is a capacity answer rather than an error: it is a
        # served 429 with Retry-After, and the request id reaches the
        # client through the same middleware as every other status.
        raise HTTPException(
            status_code=429,
            detail=_CONCURRENCY_DETAIL,
            headers={
                "Retry-After": str(
                    int(
                        LLM_CONCURRENCY_RETRY_AFTER_SECONDS
                    )
                )
            },
        ) from None

    except Exception as exc:

        # Provider, SDK, database and filesystem messages all used to be
        # returned verbatim here, which is how an NVIDIA key, a
        # deployment path or a sqlite error reached the browser. Only a
        # hand-written validation failure still says something useful;
        # everything else becomes the generic detail plus the reference
        # id, and the real traceback goes to the server log.
        raise observability.sanitized_http_exception(
            exc, request
        ) from None


# ============================================================
# SESSIONS (PROTECTED)
# ============================================================

@api.get("/sessions")
def list_sessions():

    return {
        "sessions": history.list_sessions()
    }


@api.post("/sessions")
def create_session(request: SessionCreate):

    return history.create_session(request.title.strip() or "New session")


@api.get("/sessions/{session_id}")
def session_detail(session_id: str):

    session = history.get_session(session_id)

    if session is None:

        raise HTTPException(
            status_code=404,
            detail="Session not found."
        )

    return {
        "session": session,
        "messages": history.get_messages(session_id),
    }


@api.delete(
    "/sessions/{session_id}",
    dependencies=[
        Depends(
            rate_limit(
                scope="delete",
                limit=DELETE_RATE_LIMIT_REQUESTS,
                window_seconds=DELETE_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
def delete_session_route(session_id: str):

    if not history.session_exists(session_id):

        raise HTTPException(
            status_code=404,
            detail="Session not found."
        )

    history.delete_session(session_id)

    return {"deleted": session_id}


# ============================================================
# APPROVALS (PROTECTED)
# ============================================================

@api.get("/approvals")
def list_approvals(status: Optional[str] = None):

    if status and status not in (
        approvals.PENDING,
        approvals.APPROVED,
        approvals.REJECTED,
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid approval status."
        )

    return {
        "approvals": approvals.list_approvals(status=status)
    }


@api.post("/approvals")
def create_approval(request: ApprovalCreate):

    if not request.kind.strip() or not request.sql.strip():

        raise HTTPException(
            status_code=400,
            detail="kind and sql are required."
        )

    return approvals.create_approval(
        kind=request.kind.strip(),
        sql=request.sql.strip(),
        context=request.context,
    )


def _decide_approval(approval_id, action):

    if approvals.get_approval(approval_id) is None:

        raise HTTPException(
            status_code=404,
            detail="Approval not found."
        )

    if not action(approval_id):

        raise HTTPException(
            status_code=500,
            detail="Could not update the approval."
        )

    return approvals.get_approval(approval_id)


@api.post(
    "/approvals/{approval_id}/approve",
    dependencies=[
        Depends(
            rate_limit(
                scope="approval",
                limit=APPROVAL_RATE_LIMIT_REQUESTS,
                window_seconds=APPROVAL_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
def approve_approval_route(approval_id: str):

    return _decide_approval(approval_id, approvals.approve_approval)


@api.post(
    "/approvals/{approval_id}/reject",
    dependencies=[
        Depends(
            rate_limit(
                scope="approval",
                limit=APPROVAL_RATE_LIMIT_REQUESTS,
                window_seconds=APPROVAL_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
def reject_approval_route(approval_id: str):

    return _decide_approval(approval_id, approvals.reject_approval)


# ============================================================
# UPLOADS (PROTECTED, SANDBOXED ALWAYS)
# ============================================================

@api.post(
    "/upload",
    dependencies=[
        Depends(
            rate_limit(
                scope="upload",
                limit=UPLOAD_RATE_LIMIT_REQUESTS,
                window_seconds=UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
):

    if not file.filename:

        raise HTTPException(
            status_code=400,
            detail="No file provided."
        )

    content = await file.read()

    try:

        return uploads.import_file(file.filename, content)

    except Exception as exc:

        # The unsupported-extension and sandbox rules keep their own
        # hand-written wording (400). A sqlite3, openpyxl or OS failure
        # - which names the sandbox path and the offending statement - is
        # replaced by the generic detail and logged server-side.
        raise observability.sanitized_http_exception(
            exc, request
        ) from None


@api.get("/imports")
def list_imports():

    return {
        "imports": uploads.list_imports()
    }


@api.get("/imports/{import_id}")
def import_detail(import_id: str):

    imported = uploads.get_import(import_id)

    if imported is None:

        raise HTTPException(
            status_code=404,
            detail="Import not found."
        )

    return imported


@api.delete(
    "/imports/{import_id}",
    dependencies=[
        Depends(
            rate_limit(
                scope="delete",
                limit=DELETE_RATE_LIMIT_REQUESTS,
                window_seconds=DELETE_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
def delete_import_route(import_id: str):

    if not uploads.delete_import(import_id):

        raise HTTPException(
            status_code=404,
            detail="Import not found."
        )

    return {"deleted": import_id}


# ============================================================
# REPORTS (PROTECTED)
# ============================================================

@api.post(
    "/report",
    dependencies=[
        Depends(
            rate_limit(
                scope="report",
                limit=REPORT_RATE_LIMIT_REQUESTS,
                window_seconds=REPORT_RATE_LIMIT_WINDOW_SECONDS,
            )
        )
    ],
)
def generate_report(
    payload: ReportRequest, request: Request
):

    try:

        path = build_report_pdf(
            title=payload.title,
            question=payload.question,
            sql=payload.sql,
            explanation=payload.explanation,
            data=payload.data,
            count=payload.count,
            truncated=payload.truncated,
            schema_text=get_schema(),
            source_database=provenance.source_database(
                payload.source_database
            ),
            source_sha256=provenance.hex_digest(
                payload.source_sha256
            ),
            model=provenance.safe_label(payload.model, 64),
        )

    except Exception as exc:

        # reportlab and sqlite3 both put the filesystem layout and the
        # submitted content into their messages. The previous
        # "Report generation failed: {exc}" returned all of it.
        raise observability.sanitized_http_exception(
            exc, request
        ) from None

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


# The guarded router is mounted only after every protected handler is
# registered, so each one inherits the single require_auth dependency
# and none of them can run before the guard.
app.include_router(api)


# ============================================================
# START SERVER
# ============================================================

def run():

    import uvicorn

    provider = get_provider()
    nvidia_ready = bool(NVIDIA_API_KEY and NVIDIA_MODEL)
    gemini_ready = bool(GEMINI_API_KEY)
    llm_ready = nvidia_ready if provider.name == "nvidia" else gemini_ready

    # Operator-facing startup output, written through the same logger the
    # request lines use. Only configuration state is reported: a secret
    # is reduced to CONFIGURED / NOT CONFIGURED and is never printed.
    logger.info("=" * 70)
    logger.info("STARTING PRODUCTION APP")
    logger.info("VERSION: 4.0")
    logger.info("=" * 70)
    logger.info("DATABASE: %s", DATABASE_PATH)
    logger.info(
        "DATABASE EXISTS: %s", os.path.exists(DATABASE_PATH)
    )
    logger.info("LLM PROVIDER: %s", provider.name)
    logger.info(
        "LLM API: %s",
        "CONFIGURED" if llm_ready else "NOT CONFIGURED",
    )
    logger.info(
        "AUTH: %s",
        "CONFIGURED" if APP_API_KEY else "NOT CONFIGURED",
    )
    logger.info("=" * 70)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False
    )


if __name__ == "__main__":
    run()