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

from app import access, approvals, history, observability, uploads
from app.access import FreeTrialExhausted
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
    ABOUT_PATH,
    ACCESS_REQUEST_RATE_LIMIT_REQUESTS,
    ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    ACCESS_SESSION_TTL_SECONDS,
    ACCESS_UNLOCK_RATE_LIMIT_REQUESTS,
    ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS,
    APP_API_KEY,
    ASK_RATE_LIMIT_REQUESTS,
    ASK_RATE_LIMIT_WINDOW_SECONDS,
    AUTH_SESSION_TTL_SECONDS,
    CONTACT_PATH,
    DATABASE_PATH,
    GEMINI_API_KEY,
    INDEX_PATH,
    LLM_CONCURRENCY_RETRY_AFTER_SECONDS,
    LOGIN_RATE_LIMIT_REQUESTS,
    LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    MAX_PROMPT_CHARS,
    NVIDIA_API_KEY,
    NVIDIA_MODEL,
    OWNER_CONTACT_EMAIL,
    PRIVACY_PATH,
    PUBLIC_DIR,
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
    rate_limit_visitor,
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

# Added last, so it is the outermost layer and sees the response that is
# actually sent - including the ones built by an exception handler,
# which is the only place a charged free attempt can be written for a
# request that failed after the provider was called. See app/access.py.
app.add_middleware(
    access.TrialCounterMiddleware
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


# The free trial running out is a business answer, not a malfunction:
# the visitor gets a specific, actionable status that the frontend keys
# on, plus the reference id every other answer carries. Handled here
# because the body has to be flat - detail and message side by side - and
# an HTTPException would bury both inside FastAPI's {"detail": {...}}.
# The provider is never reached on this path, so nothing is spent.
@app.exception_handler(FreeTrialExhausted)
async def free_trial_exhausted_handler(
    request: Request, exc: FreeTrialExhausted
):
    return JSONResponse(
        status_code=403,
        content=access.free_trial_payload(
            observability.request_id_of(request)
        ),
    )


# A rejected App Access Code is answered the same way: flat, machine
# readable, and identical whatever the caller got wrong.
@app.exception_handler(access.InvalidAccessCode)
async def invalid_access_code_handler(
    request: Request, exc: access.InvalidAccessCode
):
    return JSONResponse(
        status_code=401,
        content=access.invalid_code_payload(
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

# The visitor surface, deliberately on its own router with NO
# router-level guard, because these three routes are the way a visitor
# arrives:
#
#   GET  /api/public/schema  read-only column metadata, no secrets
#   POST /api/access/unlock  exchange an App Access Code for a session
#   POST /api/access/request collect an email to return with a contact
#
# Keeping them here rather than on the guarded router is the whole
# point: the guard cannot be relaxed to let a visitor in, because it is
# not attached to these routes at all. /api/ask sits here too and is
# gated per request by require_ask_access, which accepts an admin
# session, APP_API_KEY, an access cookie or one free attempt - see
# app/access.py.
visitor_router = APIRouter(
    prefix="/api",
    tags=["api"],
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


class AccessUnlock(BaseModel):
    code: str = ""


class AccessRequest(BaseModel):
    email: str = ""


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
# VISITOR ACCESS (PUBLIC BY NECESSITY)
# ============================================================
# A visitor with no credential at all still has to be able to reach the
# application, see the schema and unlock with a code, so these routes
# are public by construction and live on visitor_router, which carries
# no guard. Nothing here widens the guarded router.
#
# Neither endpoint may be turned into an oracle: /api/access/unlock
# answers 401 with one fixed wording whatever was wrong, and
# /api/access/request stores nothing and sends nothing, because this
# deployment has no mail transport to send with.

_access_unlock_limiter = rate_limit(
    scope="access-unlock",
    limit=ACCESS_UNLOCK_RATE_LIMIT_REQUESTS,
    window_seconds=ACCESS_UNLOCK_RATE_LIMIT_WINDOW_SECONDS,
    pre_auth=True,
)

_access_request_limiter = rate_limit(
    scope="access-request",
    limit=ACCESS_REQUEST_RATE_LIMIT_REQUESTS,
    window_seconds=ACCESS_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    pre_auth=True,
)


def _looks_like_email(value):
    """A shape check, not a validation service.

    Enough to catch a typo before the caller is told the request was
    received. It cannot tell a deliverable address from an undeliverable
    one, and it is not used to send anything.
    """

    if not value or len(value) > 254:
        return False

    local, separator, domain = value.partition("@")

    return bool(
        separator
        and local
        and domain
        and "." in domain
        and " " not in value
        and "\n" not in value
    )


@visitor_router.post("/access/unlock")
def unlock_access(
    response: Response,
    request: Request,
    payload: Optional[AccessUnlock] = None,
    _limited: None = Depends(_access_unlock_limiter),
):

    # 503 when no signing secret is configured: fail closed rather than
    # issue a cookie nobody can verify.
    access.signing_secret()

    if not access.access_code_matches(
        payload.code if payload else ""
    ):

        raise access.InvalidAccessCode() from None

    access.set_access_cookie(response)

    # The counter is meaningless once the code is known, so it is
    # dropped rather than left to expire on its own.
    access.clear_trial_cookie(response)

    # The code itself is never echoed, and neither is anything derived
    # from it: the response is a flag and a lifetime.
    return {
        "unlocked": True,
        "expires_in": int(ACCESS_SESSION_TTL_SECONDS),
    }


@visitor_router.post("/access/request")
def request_access(
    payload: AccessRequest,
    _limited: None = Depends(_access_request_limiter),
):

    # Bounded, and never reflected back: the 422 handler already refuses
    # to echo submitted input, and this message carries no value.
    if not _looks_like_email(payload.email.strip()):

        raise HTTPException(
            status_code=400,
            detail="Enter a valid email address.",
        )

    # Honest about what happened. The address is accepted, validated and
    # discarded: there is no mail transport in this deployment, and no
    # durable store to write it to. Saying "request sent" would be a
    # lie the visitor acts on. When the operator configures a contact
    # address, that is what the visitor is told to use instead.
    return {
        "accepted": True,
        "notification_sent": False,
        "owner_contact": OWNER_CONTACT_EMAIL or None,
        "detail": (
            "No email was sent: this deployment has no mail "
            "transport. Contact the owner directly to request "
            "an App Access Code."
        ),
    }


# The visitor router is mounted at the very end of this module, next to
# the guarded one, so it carries every visitor route defined here.


# ============================================================
# HOME (PUBLIC)
# ============================================================

@app.get("/")
def home():

    return FileResponse(
        INDEX_PATH
    )


# ============================================================
# INFORMATION PAGES (PUBLIC)
# ============================================================
# About, Contact and Privacy are static documents shipped with the
# deployment, exactly like index.html. They are public on purpose:
# they explain the product and how it handles data, and they contain
# no application state, no configuration and no secret. Each is a
# self-contained HTML file served read-only, so adding them cannot
# change any existing route, guard or API behaviour.

@app.get("/about")
def about():

    return FileResponse(
        ABOUT_PATH
    )


@app.get("/contact")
def contact():

    return FileResponse(
        CONTACT_PATH
    )


@app.get("/privacy")
def privacy():

    return FileResponse(
        PRIVACY_PATH
    )


# ============================================================
# BRAND ASSETS (PUBLIC)
# ============================================================
# The pages reference the logo at /tiix-logo-symbol.png. On the
# deployment platform that URL is served from public/ at the site
# root; this route serves the identical existing file locally so the
# reference resolves in both environments. It returns an existing
# read-only image and changes no application behaviour.

@app.get("/tiix-logo-symbol.png")
def logo_symbol():

    return FileResponse(
        os.path.join(PUBLIC_DIR, "tiix-logo-symbol.png")
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

def _schema_payload(request):
    """The read-only schema body, shared by both schema routes.

    get_schema() returns table, column and foreign-key metadata only:
    names and types, never a row, a value, a path or a credential. That
    is what makes the /api/public/schema copy below safe to serve to a
    visitor - the frontend cannot build a prompt without it, so serving
    it is what removes the sign-in from the first page load.
    """

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

    return _schema_payload(request)


# The same body, without the guard. Kept as a separate path rather than
# by relaxing /api/schema, because /api/schema is part of the documented
# security contract: it stays behind APP_API_KEY, and every existing
# caller of it is unaffected. The frontend uses this copy.
@visitor_router.get("/public/schema")
def public_schema(request: Request):

    return _schema_payload(request)


# ============================================================
# MAIN AI ENDPOINT (OWNER SESSION, APP API KEY, ACCESS CODE,
# OR A FREE ATTEMPT)
# ============================================================
# The costliest route in the application, and the only one that calls
# a provider. The controls in front of it, and their order, are
# deliberate:
#
#   Access -> Rate limiting -> Validation -> Concurrency -> Provider
#
# Access is require_ask_access (app/access.py): the admin session, then
# APP_API_KEY, then an access cookie, then one free attempt, and a 403
# for a visitor who has spent them. It runs first and costs nothing, so
# an exhausted visitor never reaches the limiter, the provider or the
# budget behind either.
#
# The rate limiter is per caller over time, so it is the right place
# to stop one client spending the allowance of another. The cap in
# app/llm_concurrency.py is per instance at one instant, so it is the
# right place to stop the instance itself being overwhelmed. Running
# the limiter first means a caller who is already over their limit is
# told so without any provider capacity being consulted at all.
#
# The two controls are not substitutes: neither bounds the other, and
# only the edge layer in front of the function is global. Neither is a
# substitute for the trial counter either - the counter bounds a
# visitor over days, the limiter bounds a client over seconds.

# Answered when the instance is at its concurrency cap. Says nothing
# about the cap, the in-flight count, the configuration or the
# provider: a saturation response must not become a way to measure
# this instance's load.
_CONCURRENCY_DETAIL = (
    "The assistant is busy right now. "
    "Please wait a moment and try again."
)

@visitor_router.post(
    "/ask",
    dependencies=[
        Depends(
            rate_limit_visitor(
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

        # The one place an attempt is charged. Everything above was
        # free: an empty prompt, an over-long prompt or an unknown
        # session returns before this line. It is deliberately AFTER the
        # checks and immediately BEFORE the provider call, so what is
        # counted is what was actually sent upstream - and it holds even
        # if the provider then fails, because that call was still paid
        # for. The counter is published on the request and written by
        # TrialCounterMiddleware, so a 429 or a 500 cannot silently drop
        # the charge.
        access.charge_trial_attempt(request)

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


# Both routers are mounted only after every handler is registered, so
# each route inherits its router's own dependencies and none of them
# can run before them. The guarded router is included first purely to
# keep the two visually adjacent; the two prefixes do not overlap.
app.include_router(api)
app.include_router(visitor_router)


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