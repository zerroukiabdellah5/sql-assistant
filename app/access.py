# ============================================================
# VISITOR ACCESS - FREE TRIAL AND APP ACCESS CODE
# ============================================================
# The demo door. A visitor arrives with no credential at all, gets
# TRIAL_MAX_ATTEMPTS questions answered, and can then trade an App
# Access Code for a bounded session.
#
# WHAT THIS IS NOT
#   It is NOT a substitute for APP_API_KEY. That secret still guards
#   sessions, uploads, imports, approvals, reports and /api/version, and
#   it never reaches the browser. This module only widens /api/ask and
#   adds the two public access endpoints; it never loosens require_auth.
#
# WHY A SIGNED COOKIE AND NOT A ROW IN A DATABASE
#   The runtime is stateless: a read-only filesystem, no shared store,
#   cold starts, several instances. A counter held in memory or in
#   app_meta.db would reset on every cold start and differ between
#   instances, so "5 attempts" would mean "up to 5 per instance per
#   warm period". The count therefore travels with the client inside a
#   signed, expiring token, exactly like the admin session cookie in
#   app/auth.py, and needs no server-side state at all.
#
# THE HONEST LIMITS, STATED PLAINLY
#   * The count is client-held, so clearing cookies or opening a private
#     window produces a fresh visitor with a fresh allowance. Preventing
#     that needs shared storage, which this deployment does not have.
#   * Two genuinely parallel requests from the same visitor can both read
#     the same count and both spend it, so the ceiling is "about" 5
#     under concurrency. The rate limiter is what bounds the abuse that
#     creates; the counter is a courtesy budget, not a quota.
#   * The token is signed, not encrypted: a visitor can read their own
#     count. They cannot change it, because the signature covers it.
#
# WHY THE SIGNING KEY IS NOT APP_ACCESS_CODE
#   The access code is short and human-typed ("1234"). A visitor holds
#   it, so it is never a secret from the visitor, and a signature keyed
#   on it could be recomputed offline by anyone holding their own copy -
#   which would let anyone mint a "0 attempts used" cookie at will. The
#   signing key is therefore derived from APP_API_KEY (or an explicit
#   ACCESS_SIGNING_SECRET), which the visitor never sees, and it fails
#   closed with 503 when neither is configured.
#
# RULES ENFORCED HERE
#   * the access code is compared in constant time;
#   * neither the access code nor the signing key is ever logged,
#     returned, or placed in the frontend;
#   * the trial cookie is written only once the request has been
#     accepted for execution, so a rejected prompt costs nothing.
# ============================================================

import hmac
import time

from http.cookies import SimpleCookie

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import app.config as config
from app.auth import (
    configured_secret,
    credentials_match,
    create_session_token,
    presented_credential,
    sign_payload,
    verify_session_token,
)

_TRIAL_TOKEN_VERSION = "tv1"

# Keys this module publishes on request.state, which is backed by the
# ASGI scope, so the route, the rate limiter and the response middleware
# all read the same values.
ACCESS_TYPE_STATE_KEY = "access_type"
TRIAL_TOKEN_STATE_KEY = "trial_token"
TRIAL_CHARGE_STATE_KEY = "trial_charge"

# Domain separation for the derived signing key. The visitor cookies
# are signed with a key derived through this label, so a signature
# produced for a visitor token can never be replayed as a signature
# made with the raw APP_API_KEY, and rotating one does not rotate the
# other.
_SIGNING_LABEL = "tiix-access-signing-v1"

# Machine-readable code the frontend keys on to decide between "show
# the access modal" and "show an ordinary error". A wording change
# cannot silently change that behaviour.
TRIAL_EXHAUSTED_CODE = "FREE_TRIAL_EXHAUSTED"

# Deliberately vague and identical whatever went wrong: it must not
# reveal whether a code is close, how long one is, or what the right
# value looks like.
_INVALID_CODE_DETAIL = "Invalid App Access Code."

# Number of attempts a visitor is allowed before /api/ask answers 403.
def max_attempts():
    return int(config.TRIAL_MAX_ATTEMPTS)


def remaining_attempts(request):
    """Attempts still available to this visitor. Never negative."""

    try:
        return max(0, max_attempts() - used_attempts(request))
    except Exception:

        # A caller without any trial state has used nothing.
        return max_attempts()


# ------------------------------------------------------------
# SIGNING KEY
# ------------------------------------------------------------

def signing_secret():
    """Return the key used to sign visitor cookies, or fail closed.

    Read from the config module on every call so a rotated secret takes
    effect without a reload. 503 rather than "allow" when nothing is
    configured: unsigned visitor cookies would make the whole gate
    forgeable.
    """

    base = (config.ACCESS_SIGNING_SECRET or "").strip()

    if not base:

        # Raises 503 when APP_API_KEY is missing too.
        base = configured_secret()

    return sign_payload(_SIGNING_LABEL, base)


# ------------------------------------------------------------
# THE APP ACCESS CODE
# ------------------------------------------------------------

def access_code_matches(candidate):
    """Constant-time check of a presented App Access Code."""

    configured = (config.APP_ACCESS_CODE or "").strip()

    if not configured:

        # Nothing configured means nothing to grant.
        return False

    return credentials_match(
        (candidate or "").strip(), configured
    )


def invalid_code_payload(request_id):
    """Client-safe body for a rejected unlock."""

    return {
        "detail": _INVALID_CODE_DETAIL,
        "reference_id": request_id,
    }


def create_access_token(secret=None):
    """Issue a signed, expiring access-session token.

    Reuses the admin session token format and verifier, so there is one
    signing scheme in the application rather than two.
    """

    return create_session_token(
        secret=(
            secret if secret is not None else signing_secret()
        ),
        ttl_seconds=int(config.ACCESS_SESSION_TTL_SECONDS),
    )


def verify_access_token(token, secret=None):
    """True when the access cookie is well formed, live and ours."""

    try:

        return verify_session_token(
            token,
            secret=(
                secret
                if secret is not None
                else signing_secret()
            ),
        )

    except HTTPException:

        # Unconfigured server: no cookie can be trusted.
        return False


# ------------------------------------------------------------
# COOKIES
# ------------------------------------------------------------
# Both are HttpOnly. JavaScript can neither read nor forge the trial
# count, and neither cookie carries any secret material.

def set_access_cookie(response, token=None):
    response.set_cookie(
        key=config.ACCESS_COOKIE_NAME,
        value=token if token is not None else create_access_token(),
        max_age=int(config.ACCESS_SESSION_TTL_SECONDS),
        httponly=True,
        secure=bool(config.AUTH_COOKIE_SECURE),
        samesite="lax",
        path="/",
    )

    return response


def clear_access_cookie(response):
    response.delete_cookie(
        key=config.ACCESS_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=bool(config.AUTH_COOKIE_SECURE),
        samesite="lax",
    )

    return response


def clear_trial_cookie(response):
    """Drop the counter, e.g. once a visitor has unlocked.

    Written directly on the response rather than through the middleware:
    this is a decision made while handling the request, not a charge, and
    it has to land even if the rest of the handler fails afterwards.
    """

    response.delete_cookie(
        key=config.TRIAL_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=bool(config.AUTH_COOKIE_SECURE),
        samesite="lax",
    )

    return response


# ------------------------------------------------------------
# TRIAL COUNTER (SIGNED, EXPIRING, STATELESS)
# ------------------------------------------------------------
# Format: "<version>.<used>.<expiry>.<hmac-sha256>". The signature
# covers the version, the count and the expiry together, so none of the
# three can be altered independently.

def create_trial_token(used, secret=None, now=None):
    """Return a signed token recording ``used`` attempts."""

    resolved = secret if secret is not None else signing_secret()
    expires_at = (
        int(now if now is not None else time.time())
        + int(config.TRIAL_SESSION_TTL_SECONDS)
    )
    signature = sign_payload(
        f"{_TRIAL_TOKEN_VERSION}:{int(used)}:{expires_at}",
        resolved,
    )

    return (
        f"{_TRIAL_TOKEN_VERSION}.{int(used)}."
        f"{expires_at}.{signature}"
    )


def verify_trial_token(token, secret=None, now=None):
    """Return the recorded attempt count, or None if unusable.

    None means "treat this visitor as new". That is the safe direction:
    a forged or expired cookie earns a fresh allowance rather than
    being read as an unlimited one.
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

    version, raw_used, raw_expiry, signature = parts

    if version != _TRIAL_TOKEN_VERSION or not signature:
        return None

    try:

        used = int(raw_used)
        expires_at = int(raw_expiry)

    except ValueError:

        return None

    current = int(now if now is not None else time.time())

    if used < 0 or expires_at <= current:
        return None

    expected = sign_payload(
        f"{version}:{used}:{expires_at}", resolved
    )

    if not hmac.compare_digest(signature, expected):
        return None

    return used


def used_attempts(request):
    """Attempts this visitor has already spent."""

    used = verify_trial_token(
        request.cookies.get(config.TRIAL_COOKIE_NAME)
    )

    return used if used is not None else 0


def read_access_type(request):
    """How this request earned its way in.

    "session" and "api-key" are the admin/owner paths, unchanged.
    "access-code" is a visitor holding a live access cookie.
    "trial" is a visitor spending one of the free attempts.
    """

    return getattr(request.state, ACCESS_TYPE_STATE_KEY, None)


def trial_token_for(request):
    """The next counter value, stashed by the gate for this request.

    Deliberately only a proposal. It becomes a charge when
    charge_trial_attempt is called, and a charge becomes a cookie when
    TrialCounterMiddleware writes it.
    """

    return getattr(request.state, TRIAL_TOKEN_STATE_KEY, None)


# ------------------------------------------------------------
# EXHAUSTION
# ------------------------------------------------------------

class FreeTrialExhausted(Exception):
    """Raised by the /api/ask gate once the free attempts are gone.

    A dedicated exception rather than an HTTPException because the body
    has to be a FLAT object with a machine-readable ``detail``, and
    FastAPI nests a dict detail one level deeper, where the frontend
    would have to guess which shape it received.
    """


class InvalidAccessCode(Exception):
    """Raised by /api/access/unlock when the presented code is wrong.

    Same reason as FreeTrialExhausted: the body has to be flat. The
    wording is identical whatever was wrong, so the response cannot be
    used to probe the code.
    """


def free_trial_payload(request_id):
    """The client-safe 403 body.

    Carries no secret and nothing about the server. The contact address
    is included only when the operator configured one, and the trial
    total is included because the modal has to state it.
    """

    attempts = max_attempts()

    payload = {
        "detail": TRIAL_EXHAUSTED_CODE,
        "message": (
            f"Your {attempts} free attempts have been used. "
            "Enter an App Access Code to continue."
        ),
        "trial_max_attempts": attempts,
        "reference_id": request_id,
    }

    if config.OWNER_CONTACT_EMAIL:

        payload["owner_contact"] = config.OWNER_CONTACT_EMAIL

    return payload


# ------------------------------------------------------------
# ROUTE-LEVEL DEPENDENCY
# ------------------------------------------------------------

def require_ask_access(request: Request):
    """Gate /api/ask. Accepts four credentials, in this order.

    Order matters and is deliberate:

      1. the admin session cookie  -> owner, unchanged;
      2. APP_API_KEY from a header -> owner, unchanged, for scripts;
      3. a live access cookie      -> visitor with the App Access Code;
      4. one of the free attempts  -> visitor with nothing at all.

    Steps 1 and 2 run first so an owner is never charged an attempt.
    Step 3 runs before the counter so a visitor who unlocked is never
    counted down.

    Step 4 charges nothing yet. It only refuses a visitor who is already
    out, and records the attempt that WOULD be spent, so the rate
    limiter has a stable per-visitor identity from the very first
    request. The charge itself is made later, by the handler, at the
    point where a provider is actually called.
    """

    secret = configured_secret()

    session_token = request.cookies.get(config.AUTH_COOKIE_NAME)

    if verify_session_token(session_token, secret=secret):

        request.state.access_type = "session"
        return "session"

    if credentials_match(presented_credential(request), secret):

        request.state.access_type = "api-key"
        return "api-key"

    access_token = request.cookies.get(config.ACCESS_COOKIE_NAME)

    if verify_access_token(access_token):

        request.state.access_type = "access-code"
        return "access-code"

    used = used_attempts(request)

    if used >= max_attempts():

        # Refused here, before the rate limiter, before validation and
        # long before any provider call: an exhausted visitor costs
        # nothing.
        raise FreeTrialExhausted()

    request.state.access_type = "trial"
    request.state.trial_token = create_trial_token(used + 1)

    return "trial"


# ------------------------------------------------------------
# CHARGING AND DELIVERING THE COUNTER
# ------------------------------------------------------------
# Two separate steps, because they answer different questions.
#
# Charging decides whether the visitor paid. It happens once, in the
# handler, immediately before the provider call: a request rejected as
# empty, over-long or attached to an unknown session returns earlier and
# costs nothing, while a request that WAS sent upstream has been paid
# for whatever the provider then did with it.
#
# Delivering decides whether the browser hears about it, and it cannot
# happen in the handler. FastAPI discards the injected Response
# whenever a handler raises - the error body is built from scratch - so
# a cookie set there never reaches the client on a 429 or a 500. That
# would not be cosmetic: a visitor whose calls kept failing would keep
# every attempt while still spending the budget behind all of them,
# because each charge was computed and then dropped on the floor. The
# middleware writes to the response that is actually sent, so a charged
# attempt is always a charged attempt.

def charge_trial_attempt(request):
    """Mark the proposed attempt as spent. Called once, before the call.

    A no-op for an owner and for a visitor who unlocked, so neither can
    be charged by this code path.
    """

    token = trial_token_for(request)

    if not token:
        return

    request.state.trial_charge = token


def trial_cookie_header(token):
    """The Set-Cookie value for a trial token.

    The counter is written here rather than through Response.set_cookie
    because the middleware has to produce the header on a response the
    exception handler built, where there is no Response object to hang it
    on. The attributes are deliberately identical to the admin session
    cookie's.
    """

    cookie = SimpleCookie()
    cookie[config.TRIAL_COOKIE_NAME] = token
    morsel = cookie[config.TRIAL_COOKIE_NAME]

    morsel["path"] = "/"
    morsel["max-age"] = int(config.TRIAL_SESSION_TTL_SECONDS)
    morsel["httponly"] = True
    morsel["samesite"] = "lax"

    if config.AUTH_COOKIE_SECURE:
        morsel["secure"] = True

    return morsel.OutputString()


class TrialCounterMiddleware:
    """Publish a charged trial counter on the response actually sent."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:

        # Lifespan and websocket traffic carry no request state.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_trial_counter(message: Message) -> None:

            token = (scope.get("state") or {}).get(
                TRIAL_CHARGE_STATE_KEY
            )

            if message["type"] == "http.response.start" and token:

                MutableHeaders(scope=message).append(
                    "set-cookie",
                    trial_cookie_header(token),
                )

            await send(message)

        await self.app(scope, receive, send_with_trial_counter)