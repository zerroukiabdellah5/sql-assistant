# ============================================================
# APPLICATION AUTHENTICATION
# ============================================================
# Guards the API surface independently of any perimeter (Vercel
# Authentication, a reverse proxy, ...), so the application stays
# protected when it is deployed elsewhere.
#
# Two credentials are accepted:
#   * the shared secret, sent as "Authorization: Bearer <key>" or
#     "X-API-Key: <key>" (scripts, probes, tests);
#   * an HMAC-signed session cookie issued by /api/auth/login and
#     attached automatically by the browser.
#
# The cookie is deliberately STATELESS. The runtime is stateless
# (read-only filesystem, cold starts), so there is no server-side
# session store to persist, lose, or race on.
#
# Rules enforced here:
#   * fail closed - no configured secret means 503, never "allow";
#   * the secret is never logged, never placed in a response, and
#     never reaches the frontend bundle;
#   * comparisons are constant time.
# ============================================================

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

import app.config as config

_TOKEN_VERSION = "v1"
_CREDENTIAL_ERROR = "Invalid or missing credentials."
_UNCONFIGURED_ERROR = "Server authentication is not configured."


# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

def configured_secret():
    """Return the configured secret, or fail closed with 503.

    Read from the config module on every call so that a rotated
    secret takes effect without a reload.
    """

    secret = (config.APP_API_KEY or "").strip()

    if not secret:
        raise HTTPException(
            status_code=503,
            detail=_UNCONFIGURED_ERROR,
        )

    return secret


def invalid_credentials():
    """Reject a request that carried no usable credential."""

    raise HTTPException(
        status_code=401,
        detail=_CREDENTIAL_ERROR,
        headers={"WWW-Authenticate": "Bearer"},
    )


def credentials_match(candidate, secret):
    """Constant-time comparison of a presented secret."""

    if not candidate:
        return False

    return secrets.compare_digest(candidate, secret)


def presented_credential(request):
    """Extract a static secret from the request headers, if any."""

    api_key_header = request.headers.get("x-api-key")

    if api_key_header and api_key_header.strip():
        return api_key_header.strip()

    authorization = request.headers.get("authorization") or ""
    scheme, _, token = authorization.partition(" ")

    if scheme.lower() == "bearer" and token.strip():
        return token.strip()

    return None


# ------------------------------------------------------------
# SESSION TOKENS (SIGNED, EXPIRING, STATELESS)
# ------------------------------------------------------------

def _sign(payload, secret):
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_payload(payload, secret):
    """Sign an arbitrary string with the shared HMAC helper.

    Exposed so app/access.py derives its visitor cookies from the same
    primitive as the admin session instead of duplicating the crypto.
    Callers choose their own payload format and are responsible for
    including everything that must be covered by the signature.
    """

    return _sign(payload, secret)


def create_session_token(secret=None, ttl_seconds=None):
    """Return a signed session token that expires after the TTL.

    Format: "<version>.<expiry>.<hmac-sha256>". The token carries
    no identity and no secret material.
    """

    resolved = secret if secret is not None else configured_secret()
    ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else config.AUTH_SESSION_TTL_SECONDS
    )
    expires_at = int(time.time()) + int(ttl)
    signature = _sign(f"{_TOKEN_VERSION}:{expires_at}", resolved)

    return f"{_TOKEN_VERSION}.{expires_at}.{signature}"


def verify_session_token(token, secret=None, now=None):
    """True when the token is well formed, unexpired and correctly signed."""

    if not token:
        return False

    resolved = secret if secret is not None else configured_secret()
    parts = token.split(".")

    if len(parts) != 3:
        return False

    version, raw_expiry, signature = parts

    if version != _TOKEN_VERSION or not signature:
        return False

    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False

    current = int(time.time() if now is None else now)

    if expires_at <= current:
        return False

    expected = _sign(f"{_TOKEN_VERSION}:{expires_at}", resolved)

    return hmac.compare_digest(signature, expected)


# ------------------------------------------------------------
# COOKIES
# ------------------------------------------------------------

def set_session_cookie(response, token):
    """Attach the session cookie. HttpOnly so JavaScript can never read it."""

    response.set_cookie(
        key=config.AUTH_COOKIE_NAME,
        value=token,
        max_age=int(config.AUTH_SESSION_TTL_SECONDS),
        httponly=True,
        secure=bool(config.AUTH_COOKIE_SECURE),
        samesite="lax",
        path="/",
    )

    return response


def clear_session_cookie(response):
    """Expire the session cookie in the browser."""

    response.delete_cookie(
        key=config.AUTH_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=bool(config.AUTH_COOKIE_SECURE),
        samesite="lax",
    )

    return response


# ------------------------------------------------------------
# LOGIN / LOGOUT
# ------------------------------------------------------------

def login_response(response, secret=None):
    """Issue a fresh session cookie. The secret is never returned."""

    resolved = secret if secret is not None else configured_secret()

    return set_session_cookie(
        response,
        create_session_token(secret=resolved),
    )


def logout_response(response):
    """Drop the session cookie."""

    return clear_session_cookie(response)


# ------------------------------------------------------------
# ROUTER-LEVEL DEPENDENCY
# ------------------------------------------------------------

def require_auth(request: Request):
    """Accept a session cookie or the static secret, otherwise reject.

    Intended to be attached once to a router:

        api = APIRouter(dependencies=[Depends(require_auth)])

    so no individual route has to remember the check.
    """

    secret = configured_secret()

    token = request.cookies.get(config.AUTH_COOKIE_NAME)

    if verify_session_token(token, secret=secret):
        return "session"

    if credentials_match(presented_credential(request), secret):
        return "api-key"

    invalid_credentials()
