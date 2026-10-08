# ============================================================
# APPLICATION RATE LIMITING
# ============================================================
# This limiter is "best-effort per-instance defense-in-depth".
#
# WHAT IT IS NOT, and this matters:
#   * Process memory is NOT shared between Vercel instances. Each
#     warm instance counts independently, so the real ceiling is
#     (configured limit x number of live instances).
#   * A cold start empties the state, granting a fresh allowance.
#   * It is NOT a global quota and NOT a daily budget. Nothing here
#     can bound total NVIDIA spend across the deployment.
#
# Its honest purpose is narrower: stop one client from hammering a
# warm instance, keep a single request from monopolising the shared
# AnyIO threadpool, and produce correct 429 + Retry-After semantics.
#
# A Vercel edge rate-limit rule is the layer that WOULD be global,
# because it is enforced before the function is invoked. That rule
# does not exist yet; this module is not a substitute for it.
#
# Design rules enforced here:
#   * no raw credential, session token, or IP is ever stored;
#   * only a truncated SHA-256 digest is kept as a dictionary key;
#   * APP_API_KEY is never used as a key, and never hashed into one;
#   * forwarded headers (x-forwarded-for, x-real-ip) are NOT read,
#     because a client can send any value it likes. Only the
#     socket-level peer address is used, and only for the routes that
#     have no authenticated identity yet;
#   * state is protected by a lock, because sync handlers run
#     concurrently in the AnyIO threadpool;
#   * unexpected failures fail CLOSED with 429, never a 500.
# ============================================================

import hashlib
import logging
import math
import threading
import time

from fastapi import Depends, HTTPException, Request

import app.config as config
from app.access import require_ask_access, trial_token_for
from app.auth import require_auth

# Failures inside the limiter are real operational events, so they go
# through the project logger instead of writing straight to the console:
# one stream the platform collects, one level, one place to search, and
# nothing emitted behind the logging configuration. The messages stay
# fixed literals - no key, token, identity, IP or internal detail is ever
# interpolated.
logger = logging.getLogger("app.ratelimit")

# ------------------------------------------------------------
# FIXED LIMITS FOR THE ROUTES THAT DO NOT NEED OPERATOR CONTROL
# ------------------------------------------------------------
# These are deliberately module-level rather than environment
# variables. Only the three highest-cost surfaces are operator
# tunable; promoting any of these later is a one-line change, and
# ten extra environment variables would be configuration sprawl.

REPORT_RATE_LIMIT_REQUESTS = 10
REPORT_RATE_LIMIT_WINDOW_SECONDS = 60

APPROVAL_RATE_LIMIT_REQUESTS = 10
APPROVAL_RATE_LIMIT_WINDOW_SECONDS = 60

DELETE_RATE_LIMIT_REQUESTS = 10
DELETE_RATE_LIMIT_WINDOW_SECONDS = 60

# Shown to the client. Deliberately says nothing about the limit,
# the window, the caller's identity, or the server's configuration.
_LIMITED_DETAIL = (
    "Too many requests. Please wait a moment and try again."
)

# Identities used when a real per-caller value is unavailable or
# would be unsafe to derive. These are constants, not secrets, and
# are prefixed so they can never collide with a hex digest.
_API_KEY_IDENTITY = "shared:api-key"
_UNKNOWN_HOST_IDENTITY = "shared:unknown-host"


def _digest(raw):
    """Return a short one-way digest safe to use as a state key."""

    return hashlib.sha256(
        str(raw).encode("utf-8")
    ).hexdigest()[:32]


class FixedWindowLimiter:
    """Counting limiter over a fixed window, per key.

    State is ``key -> (window_start, count, window_seconds)``. The
    window is stored per entry so that a 300 second login bucket is
    not pruned by a 60 second ask call and vice versa.
    """

    def __init__(self, time_source=None):

        self._lock = threading.Lock()
        self._buckets = {}
        self._time = time_source or time.monotonic

    def allow(self, key, limit, window_seconds):
        """Return ``(allowed, retry_after_seconds)`` for one request.

        Never raises: an unexpected problem denies the request so a
        broken limiter cannot silently disable itself.
        """

        try:

            now = self._time()

        except Exception:

            # Even the clock is unusable. Deny, and say nothing.
            logger.warning("RATE LIMITER CLOCK FAILURE")

            return False, max(1, int(window_seconds))

        try:

            with self._lock:

                entry = self._buckets.get(key)

                if entry is None:

                    # First sighting of this key: start an empty
                    # window and let the shared counting path below
                    # admit this request. Returning early here would
                    # let one extra request through every window.
                    start = now
                    count = 0
                    window = window_seconds

                else:

                    # The current key is inspected BEFORE pruning, so a
                    # corrupt entry denies this request instead of
                    # being silently cleaned away and readmitted.
                    # Unpacking is inside the guard because a truncated
                    # or padded tuple is just another corrupt state.
                    try:

                        start, count, window = entry

                        if (
                            not isinstance(
                                start, (int, float)
                            )
                            or isinstance(start, bool)
                            or not isinstance(count, int)
                            or isinstance(count, bool)
                            or not isinstance(
                                window, (int, float)
                            )
                            or isinstance(window, bool)
                            or window <= 0
                            or count < 0
                        ):

                            raise ValueError("corrupt")

                    except Exception:

                        logger.warning(
                            "RATE LIMITER STATE REJECTED"
                        )

                        return False, max(
                            1, int(window_seconds)
                        )

                    if now - start >= window:

                        # The window elapsed: start a fresh one.
                        start = now
                        count = 0

                self._prune(now)

                if count < limit:

                    self._buckets[key] = (
                        start, count + 1, window
                    )

                    return True, 0

                # Over the limit. The stored count is deliberately not
                # incremented: `limit` already means "full", and
                # letting it grow would leak how long a caller has
                # been blocked.
                self._buckets[key] = (start, count, window)

                retry_after = int(
                    math.ceil(window - (now - start))
                )

                return False, max(
                    1, min(retry_after, int(window))
                )

        except Exception:

            # Fail closed. The message carries no key, token, IP or
            # internal detail, so it is safe to log.
            logger.error("RATE LIMITER FAILURE", exc_info=True)

            return False, max(1, int(window_seconds))

    def _prune(self, now):
        """Drop entries whose window has elapsed, bounding memory."""

        stale = []

        for key, entry in self._buckets.items():

            try:

                start, _count, window = entry

                if now - start >= window:
                    stale.append(key)

            except Exception:

                # Malformed leftover: drop it. The current key was
                # already validated above, so this cannot mask a
                # denial.
                stale.append(key)

        for key in stale:
            self._buckets.pop(key, None)

    def reset(self):
        """Clear all state. Used by the test suite for isolation."""

        with self._lock:
            self._buckets.clear()


# The process-wide limiter. One instance, because a single reset()
# has to be able to isolate the whole suite between tests.
limiter = FixedWindowLimiter()


# ------------------------------------------------------------
# IDENTITY
# ------------------------------------------------------------

def _authenticated_identity(request, auth_type):
    """Derive a limiter identity for a request that already passed
    require_auth.

    ``require_auth`` returns "session" or "api-key". It never returns
    the credential itself, and this function does not change it to do
    so.

    * "session": the cookie is a validated session, so a digest of it
      is a stable, unforgeable identity for the session's lifetime.
    * "api-key": APP_API_KEY is SHARED, so every holder is the same
      principal. They all share one bucket, and the credential is
      never read, hashed, or stored. This is deliberate: deriving a
      key from the secret would put a derivative of it in state, and
      an attacker holding the key could otherwise mint unlimited
      buckets by varying a cookie.
    """

    if auth_type == "session":

        token = request.cookies.get(
            config.AUTH_COOKIE_NAME
        )

        if token:
            return _digest(token)

    return _API_KEY_IDENTITY


def _login_identity(request):
    """Identity for a pre-authentication request (the login routes).

    No session exists yet and no credential has been proven, so the
    only value available is the socket-level peer address. That
    address is hashed before use and is never stored in the clear.

    x-forwarded-for and x-real-ip are deliberately NOT read. Both are
    ordinary client-supplied headers, so trusting them would let an
    attacker mint a fresh bucket per request and disable the limiter
    entirely. On a platform where the peer address is a shared
    internal address this degrades toward over-restriction, which is
    the safe direction; a future edge rule is where authoritative
    client identity belongs.
    """

    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None

    if not host:
        return _UNKNOWN_HOST_IDENTITY

    return _digest(host)


def _visitor_identity(request, access_type):
    """Derive a limiter identity for a request that passed
    require_ask_access.

    /api/ask is reachable without any credential, so the identity has
    to be derivable from whatever the caller happens to hold:

    * "api-key": APP_API_KEY is SHARED, so every holder is one
      principal and shares one bucket, exactly as above. The credential
      is never read, hashed, or stored.
    * "session" / "access-code": the cookie is a validated signed
      token, so a digest of it is a stable, unforgeable identity for
      its lifetime - and, unlike the shared secret, it is per visitor.
    * "trial": the digest of the visitor's own signed trial token, so
      one visitor's allowance cannot be spent by another. On a first
      visit there is no cookie yet, so the token the request is about
      to write is used instead; that keeps the identity stable from
      the very first request.

    Every value here is a truncated digest. No token, cookie, IP or
    secret reaches the limiter's state in the clear.
    """

    if access_type == "api-key":
        return _API_KEY_IDENTITY

    if access_type == "trial":
        token = trial_token_for(request)
        token = token or request.cookies.get(
            config.TRIAL_COOKIE_NAME
        )
    else:
        cookie_name = {
            "session": config.AUTH_COOKIE_NAME,
            "access-code": config.ACCESS_COOKIE_NAME,
        }.get(access_type)
        token = (
            request.cookies.get(cookie_name) if cookie_name else None
        )

    if token:
        return _digest(token)

    # A visitor whose identity could not be read from a cookie shares
    # the hashed peer address, the same degradation as the login
    # routes: over-restriction rather than under-restriction.
    return _login_identity(request)


# ------------------------------------------------------------
# DEPENDENCY FACTORY
# ------------------------------------------------------------

def rate_limit(scope, limit, window_seconds, pre_auth=False):
    """Build a FastAPI dependency enforcing one limit.

    ``scope`` keeps the buckets of different routes separate, so the
    same caller cannot spend the /api/ask allowance on /api/report.

    ``pre_auth=True`` is used only by the login routes: there the
    limiter must run BEFORE any credential comparison, because the
    whole point is to slow an online guessing attack against the
    shared APP_API_KEY.

    For protected routes require_auth is declared as a
    sub-dependency. The router already guards them, so an
    unauthenticated caller is rejected with 401 and never reaches
    the counter; the return value is reused here purely to learn
    which kind of authenticated identity applies. That keeps
    authentication strictly ahead of the limiter.
    """

    if pre_auth:

        def dependency(request: Request):

            _enforce(
                _login_identity(request),
                scope,
                limit,
                window_seconds,
            )

        return dependency

    def dependency(
        request: Request,
        auth_type: str = Depends(require_auth),
    ):

        _enforce(
            _authenticated_identity(request, auth_type),
            scope,
            limit,
            window_seconds,
        )

    return dependency


def rate_limit_visitor(scope, limit, window_seconds):
    """Build a dependency limiting a route that needs no APP_API_KEY.

    /api/ask is reachable by a visitor with no credential, so the
    generic ``rate_limit`` factory cannot be used: it takes its
    identity from require_auth, which would reject exactly the callers
    this route now serves. Everything else is identical - the same
    process-wide limiter, the same scope isolation, the same 429 plus
    Retry-After, and the same ordering.

    require_ask_access is declared as a sub-dependency so access is
    settled BEFORE the counter is touched, in the same
    authentication-then-limiting order as every protected route. It is
    read only: an exhausted visitor is refused by it first and never
    spends any of this bucket.
    """

    def dependency(
        request: Request,
        access_type: str = Depends(require_ask_access),
    ):

        _enforce(
            _visitor_identity(request, access_type),
            scope,
            limit,
            window_seconds,
        )

    return dependency


def _enforce(identity, scope, limit, window_seconds):
    """Apply the limit, or raise 429 with a Retry-After header."""

    if not config.APP_RATE_LIMIT_ENABLED:

        # Operational kill switch. Authentication is unaffected.
        return

    key = f"{scope}:{identity}"

    allowed, retry_after = limiter.allow(
        key, limit, window_seconds
    )

    if not allowed:

        raise HTTPException(
            status_code=429,
            detail=_LIMITED_DETAIL,
            headers={
                "Retry-After": str(int(retry_after))
            },
        )
