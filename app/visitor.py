# ============================================================
# ANONYMOUS VISITOR IDENTITY
# ============================================================
# Every browser that opens the application is quietly given a signed,
# HttpOnly visitor cookie holding a random, opaque id. Uploaded files
# and their imports are then stored under a directory named after that
# id, so one visitor's data is never reachable by another.
#
# WHAT THIS IS NOT
#   It is NOT a credential and NOT an authentication bypass. It grants
#   nothing on the guarded router: sessions, approvals and reports still
#   require APP_API_KEY. It only gives an anonymous uploader a namespace
#   of their own. The visitor id is not a secret either - it is random
#   and unguessable, but if it leaks the most that leaks is the ability
#   to read the files that browser itself uploaded.
#
# WHY A SIGNED COOKIE AND NOT A ROW IN A DATABASE
#   Same reason as the trial counter in app/access.py: the runtime is
#   stateless (read-only filesystem, cold starts, several instances with
#   no shared memory), so there is nowhere to keep a server-side session
#   table. The id travels with the client inside a signed, expiring
#   token and needs no server-side state.
#
# THE HONEST LIMIT, STATED PLAINLY
#   Clearing cookies or opening a private window produces a new visitor
#   id, so that browser loses access to files it uploaded earlier. The
#   files remain on disk until the sandbox is reclaimed; they are simply
#   no longer addressable by any cookie. Isolation between visitors is
#   unaffected: a new id is just another empty namespace.
# ============================================================

import hmac
import re
import secrets
import time

from fastapi import HTTPException, Request, Response

import app.config as config
from app.access import signing_secret
from app.auth import sign_payload

# Format: "<version>.<visitor_id>.<expiry>.<hmac-sha256>". The signature
# covers the version, the id and the expiry together, so none of the
# three can be altered independently.
_COOKIE_VERSION = "vv1"

# A visitor id is random hex minted below. The pattern is deliberately
# narrow - no separator, dot or drive colon - so a value that could ever
# be a path fragment is rejected before any filesystem call, exactly like
# the import-id rule in app/uploads.py.
_VISITOR_ID_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")

VISITOR_ID_STATE_KEY = "visitor_id"


def new_visitor_id():
    """A fresh opaque id. 32 hex characters, well inside the pattern."""

    return secrets.token_hex(16)


def valid_visitor_id(value):
    return bool(value and _VISITOR_ID_RE.match(value))


def create_visitor_token(visitor_id, secret=None, now=None):
    """Sign ``visitor_id`` with an expiry, exactly like a session token."""

    if not valid_visitor_id(visitor_id):
        raise ValueError("Invalid visitor id.")

    resolved = secret if secret is not None else signing_secret()

    expires_at = (
        int(now if now is not None else time.time())
        + int(config.VISITOR_SESSION_TTL_SECONDS)
    )

    signature = sign_payload(
        f"{_COOKIE_VERSION}:{visitor_id}:{expires_at}",
        resolved,
    )

    return (
        f"{_COOKIE_VERSION}.{visitor_id}."
        f"{expires_at}.{signature}"
    )


def verify_visitor_token(token, secret=None, now=None):
    """Return the visitor id a token names, or None if unusable.

    None means "treat this as a new visitor" and is the safe direction:
    a forged or expired cookie is answered with a fresh, empty namespace
    rather than an id an attacker chose.
    """

    if not token:
        return None

    try:

        resolved = (
            secret if secret is not None else signing_secret()
        )

    except HTTPException:

        return None

    parts = token.split(".")

    if len(parts) != 4:
        return None

    version, visitor_id, raw_expiry, signature = parts

    if version != _COOKIE_VERSION or not signature:
        return None

    if not valid_visitor_id(visitor_id):
        return None

    try:

        expires_at = int(raw_expiry)

    except ValueError:

        return None

    current = int(now if now is not None else time.time())

    if expires_at <= current:
        return None

    expected = sign_payload(
        f"{version}:{visitor_id}:{expires_at}", resolved
    )

    if not hmac.compare_digest(signature, expected):
        return None

    return visitor_id


def visitor_id_from_request(request):
    """The verified visitor id on this request, or None.

    Read-only: it never mints one, so a route that only needs to know
    whether a visitor is already known (the /api/ask ownership check)
    can ask without creating a cookie as a side effect.
    """

    return verify_visitor_token(
        request.cookies.get(config.VISITOR_COOKIE_NAME)
    )


def set_visitor_cookie(response, visitor_id=None):
    """Attach (or refresh) the visitor cookie and return the id it holds.

    HttpOnly, so JavaScript can neither read nor forge it. The cookie is
    not a secret and is not marked as one.
    """

    resolved = visitor_id or new_visitor_id()

    response.set_cookie(
        key=config.VISITOR_COOKIE_NAME,
        value=create_visitor_token(resolved),
        max_age=int(config.VISITOR_SESSION_TTL_SECONDS),
        httponly=True,
        secure=bool(config.AUTH_COOKIE_SECURE),
        samesite="lax",
        path="/",
    )

    return resolved


def require_visitor(request: Request, response: Response):
    """FastAPI dependency: the caller's visitor id, minting one if needed.

    A browser that has no usable cookie is given one on the response
    that this request produces. When an endpoint can fail after this
    runs (a rejected upload, a rate limit), the freshly minted cookie is
    simply not delivered: nothing was stored under it yet, so the next
    request quietly mints another. Identity is only ever established by
    a response that actually succeeds.

    Fails closed (503) when no signing secret is configured, through
    signing_secret(): an unsigned visitor id would be forgeable content
    in a cookie, which is the one thing this module refuses to issue.
    """

    existing = visitor_id_from_request(request)

    if existing:

        request.state.visitor_id = existing
        return existing

    minted = set_visitor_cookie(response)

    request.state.visitor_id = minted
    return minted
