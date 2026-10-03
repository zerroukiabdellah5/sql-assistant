# ============================================================
# REQUEST OBSERVABILITY
# ============================================================
# Request IDs, safe request logging, and error sanitisation for the
# whole HTTP surface. Standard library only.
#
# WHAT IT DOES
#   * assigns every request a cryptographically random id, publishes it
#     as request.state.request_id, and echoes it as X-Request-ID on
#     EVERY response: 2xx, 400, 401, 403, 404, 405, 422, 429 and 500
#     alike, whoever produced it;
#   * writes exactly one access line per request, built from five safe
#     fields (method, path, status, duration, request id);
#   * replaces an unexpected exception with a generic message carrying
#     that id, while the real traceback stays on the server.
#
# WHAT IT NEVER LOGS
#   Authorization or X-API-Key headers, cookies, passwords, APP_API_KEY,
#   request bodies, query strings, prompts, generated SQL, database
#   contents, provider keys or response bodies. The access line is
#   assembled from five fields and nothing else, so that property is
#   checkable rather than aspirational, and no header, cookie or body is
#   ever read on this path. The client IP is not read either: forwarded
#   headers are client-supplied, so they cannot be trusted.
#
# ERROR DISCLOSURE
#   Only app.validate.ValidationError carries a message written by this
#   project on purpose for a user ("Only SELECT queries are allowed.").
#   Every other failure - provider, database, filesystem, OS, SDK,
#   anything a third party raised, including a plain ValueError - is
#   logged with its traceback and answered with the generic detail plus
#   the reference id. Unrecognised means hidden: the classifier fails
#   closed.
#
# VERCEL / SERVERLESS
#   ordinary stdlib logging to stdout/stderr, which is what the platform
#   collects. No log file, no Redis, no database: the filesystem is
#   read-only, instances are independent and disposable, and a cold
#   start drops everything in memory. Process-local logs are therefore
#   NOT permanent and must never be treated as an audit trail.
#
# ORDERING
#   the middleware wraps the whole lifecycle for observability only. It
#   necessarily runs before authentication and rate limiting, because a
#   rejected request needs an id too, but it replaces neither of them:
#
#       Authentication -> Rate limiting -> Expensive work
#
#   is unchanged, and the request id is never used as a rate-limit key.
# ============================================================

import logging
import os
import sys
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.validate import ValidationError

# ------------------------------------------------------------
# CONSTANTS
# ------------------------------------------------------------

REQUEST_ID_HEADER = "X-Request-ID"

# Key under which the id is published on request.state. Handlers and
# dependencies read it from there, so the value a route sees and the
# value on the wire are the same object.
REQUEST_ID_STATE_KEY = "request_id"

_ROOT_LOGGER_NAME = "app"
_ACCESS_LOGGER_NAME = "app.request"
_ERROR_LOGGER_NAME = "app.error"

# The only two messages a client may see from an unexpected failure.
# Both carry the reference id so the failure can be reported, and
# neither describes the server: no exception text, no stack trace, no
# filesystem path, no provider, database or OS error.
_INTERNAL_DETAIL = "Request failed. Reference ID: {request_id}"
_VALIDATION_DETAIL = "Invalid request data."

# Used when a caller has no request context at all (startup, a code
# path reached outside the request lifecycle). It is a fixed constant,
# never a value derived from the request.
_UNKNOWN_REQUEST_ID = "unknown"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# A bounded path keeps one hostile request from flooding the log, and a
# bounded method keeps a crafted verb from doing the same.
_MAX_PATH_CHARS = 200
_MAX_METHOD_CHARS = 16


# ------------------------------------------------------------
# LOGGING (STDOUT / STDERR ONLY)
# ------------------------------------------------------------

class _StreamHandler(logging.Handler):
    """Write records to the CURRENT stdout or stderr.

    The stream is resolved on every emit instead of being bound in the
    constructor, so the lines land in whatever is capturing output: the
    Vercel runtime, a local terminal, or a test's capture.

    ERROR and above go to stderr and everything else to stdout, so a
    platform log viewer separates real failures from ordinary request
    lines without a second handler.
    """

    def emit(self, record):
        try:
            stream = (
                sys.stderr
                if record.levelno >= logging.ERROR
                else sys.stdout
            )
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


def configure_logging(level=None):
    """Install one stdout/stderr handler on the application logger.

    Idempotent by construction: a second call (lifespan startup after
    import, a reload, another test) reuses the handler it finds instead
    of adding another, so no line is ever duplicated.
    """

    logger = logging.getLogger(_ROOT_LOGGER_NAME)

    resolved = str(
        level or os.getenv("APP_LOG_LEVEL") or "INFO"
    ).upper()

    logger.setLevel(resolved)

    if not any(
        getattr(handler, "_tiix_observability", False)
        for handler in logger.handlers
    ):
        handler = _StreamHandler()
        handler._tiix_observability = True
        handler.setFormatter(
            logging.Formatter(
                _LOG_FORMAT, datefmt=_DATE_FORMAT
            )
        )
        logger.addHandler(handler)

    return logger


# Configured on import rather than in the lifespan: the serverless
# runtime imports the module to serve, and the handler is what makes the
# request and error lines visible there at all.
configure_logging()


# ------------------------------------------------------------
# REQUEST IDS
# ------------------------------------------------------------

def new_request_id():
    """A fresh, unguessable identifier for one request.

    uuid4 is built on os.urandom, so an id cannot be predicted from the
    ones already issued and therefore cannot be forged or enumerated.
    It carries no identity: nothing about the caller, the credential or
    the payload is encoded in it.
    """

    return uuid.uuid4().hex


def request_id_of(request):
    """The id assigned to this request, or a fixed placeholder.

    Never raises and never invents a value: if the middleware did not
    run, the caller gets a constant so a log line is still well formed.
    """

    request_id = None

    try:
        request_id = getattr(
            request.state, REQUEST_ID_STATE_KEY, None
        )
    except Exception:
        request_id = None

    return str(request_id) if request_id else _UNKNOWN_REQUEST_ID


# ------------------------------------------------------------
# CLIENT-SAFE MESSAGES
# ------------------------------------------------------------

def internal_error_detail(request_id):
    """The generic 500 body. Says nothing about the failure."""

    return _INTERNAL_DETAIL.format(request_id=request_id)


def validation_error_payload(request_id):
    """The sanitized 422 body.

    Neither the submitted values nor the failing field names come back.
    Echoing them would hand an attacker a free oracle: a probe of
    /api/auth/login-form could read back whatever it posted, including a
    password guess, and any endpoint would reflect the prompt or SQL the
    caller sent.
    """

    return {
        "detail": _VALIDATION_DETAIL,
        "reference_id": request_id,
    }


def is_user_facing_error(exception):
    """True only for a hand-written, deliberate validation failure.

    ValidationError is raised by this project's own validation code with
    a message written to be shown to a user, and it is the only thing
    this module trusts. Everything else is treated as an unexpected
    internal failure - including a plain ValueError, since that type is
    also what JSON decoding, SDKs and the standard library raise, and
    their messages carry paths, keys and upstream payloads.
    """

    return isinstance(exception, ValidationError)


# ------------------------------------------------------------
# SERVER-SIDE LOGGING OF FAILURES
# ------------------------------------------------------------

def log_unexpected_exception(exception, request_id):
    """Record the real failure, with its traceback, on the server.

    The message carries the reference id, the exception class and
    nothing else: the detail lives in the traceback attached to the
    record, which is server-side by construction and never leaves the
    process. The client is told the reference id only.
    """

    logging.getLogger(_ERROR_LOGGER_NAME).error(
        "unexpected_error request_id=%s type=%s",
        request_id,
        type(exception).__name__,
        exc_info=exception,
    )


def sanitized_http_exception(
    exception,
    request,
    *,
    validation_status_code=400,
):
    """Translate a caught exception into a client-safe HTTPException.

    A ValidationError keeps its hand-written message, because hiding
    "Only SELECT queries are allowed." would make the application
    unusable while telling the user nothing. Anything else is logged
    with its traceback and answered with the generic detail plus the
    reference id, so an SDK, provider, database or OS message can never
    reach a client.

    Raises nothing: the caller does the raising, which keeps the
    sanitisation impossible to forget at the raise site.
    """

    if is_user_facing_error(exception):
        return HTTPException(
            status_code=validation_status_code,
            detail=str(exception),
        )

    request_id = request_id_of(request)

    log_unexpected_exception(exception, request_id)

    return HTTPException(
        status_code=500,
        detail=internal_error_detail(request_id),
    )


# ------------------------------------------------------------
# ACCESS LOG LINE
# ------------------------------------------------------------

def _clean_log_value(value, limit):
    """Printable-only and bounded.

    A value that reached the log unfiltered could forge an extra record
    with a newline in it, or flood the log with one huge field. Neither
    is worth more than the two lines this costs.
    """

    cleaned = "".join(
        char if char.isprintable() else "?"
        for char in str(value or "-")
    )

    return cleaned[:limit]


def _safe_path(path):
    """The URL path alone, hardened for a log file.

    The query string is never part of this value: it can carry a token,
    a prompt or a session id.
    """

    return _clean_log_value(path or "/", _MAX_PATH_CHARS)


def access_log_line(
    method, path, status_code, duration_ms, request_id
):
    """One line of safe request metadata.

    Deliberately exactly five fields. No header, no cookie, no body, no
    query string, no IP, no prompt, no SQL.
    """

    return (
        f"{method} {path} "
        f"status={status_code} "
        f"duration_ms={duration_ms} "
        f"request_id={request_id}"
    )


def _duration_ms(started_at):
    return int(round((time.perf_counter() - started_at) * 1000))


# ------------------------------------------------------------
# MIDDLEWARE
# ------------------------------------------------------------

class RequestContextMiddleware:
    """Give every request an id, publish it, and log safe metadata.

    Wraps the entire request lifecycle without taking part in
    authentication or rate limiting. It adds an identifier, one response
    header and one log line, and nothing else.

    The id is assigned before anything else runs, so a request rejected
    by the authentication guard, the rate limiter or a 404 lookup still
    carries one, and the client can quote it in a report.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:

        # Lifespan and websocket traffic has no HTTP request id.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()

        # Published on the scope, which is what backs request.state, so
        # every handler, dependency and exception handler downstream
        # sees the same id that goes out on the header.
        scope.setdefault("state", {})[
            REQUEST_ID_STATE_KEY
        ] = request_id

        method = _clean_log_value(
            scope.get("method", "-"), _MAX_METHOD_CHARS
        )
        path = _safe_path(scope.get("path", "/"))
        started_at = time.perf_counter()

        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started

            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])

                # Whoever produced the response - a handler, the
                # authentication guard, the limiter, the router's 404 or
                # 405, the 422 handler, or the 500 below - the id is
                # attached here, exactly once, in one place.
                MutableHeaders(scope=message)[
                    REQUEST_ID_HEADER
                ] = request_id

            await send(message)

        try:
            await self.app(
                scope, receive, send_with_request_id
            )

        except Exception as exc:

            log_unexpected_exception(exc, request_id)

            if response_started:
                # The body is already on the wire, so a new response
                # cannot be sent. Propagate and let the server report
                # the truncated transfer.
                raise

            response = JSONResponse(
                status_code=500,
                content={
                    "detail": internal_error_detail(request_id)
                },
            )
            await response(
                scope, receive, send_with_request_id
            )

        finally:
            logging.getLogger(_ACCESS_LOGGER_NAME).info(
                "%s",
                access_log_line(
                    method,
                    path,
                    status_code,
                    _duration_ms(started_at),
                    request_id,
                ),
            )