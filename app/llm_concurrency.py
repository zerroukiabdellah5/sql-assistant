# ============================================================
# LLM CONCURRENCY CAP (BEST-EFFORT, PER INSTANCE)
# ============================================================
# This gate bounds how many provider calls a single warm instance
# has in flight at one moment. It exists because /api/ask is a
# SYNCHRONOUS handler: each request occupies one thread of the
# shared AnyIO threadpool for the whole upstream call, so N slow
# provider calls cost N threads for as long as they take.
#
# WHAT IT IS NOT, and this matters as much as what it is:
#   * It is NOT a global quota and NOT a budget. Process memory is
#     NOT shared between Vercel instances, so each warm instance
#     counts independently and the real ceiling is
#     (LLM_MAX_CONCURRENCY x number of live instances).
#   * A cold start empties the counter, granting a fresh allowance.
#   * It is NOT persisted anywhere. No file, no database, no
#     Redis: the filesystem is read-only on Vercel and nothing here
#     may depend on it.
#   * It bounds concurrency, not spend. Twenty sequential calls all
#     pass. This is the deliberate difference from a token or call
#     budget, and it is why no usage figure is read here.
#
# IT NEVER QUEUES, and that is a safety property rather than a
# preference. Waiting for a slot inside a synchronous threadpool
# worker holds an AnyIO thread while doing nothing, so forty queued
# requests would occupy all forty threads and starve every other
# API route - converting a provider cap into the full application
# outage that app/config.py already sets LLM_TIMEOUT_SECONDS to
# prevent. On Vercel an unbounded wait can also outlive the function
# itself. So a saturated instance REJECTS immediately with 429 and
# Retry-After rather than waiting.
#
# Design rules enforced here:
#   * no identity, credential, session token, IP address or request
#     payload is read, derived, hashed or stored. The cap is
#     instance-wide, so there is no key to collide and nothing to
#     mint a fresh bucket with. It cannot be pointed at a caller;
#   * the request id is an opaque application-generated correlation
#     value (see app.observability.new_request_id), logged so a
#     rejection can be traced to its access line. It is never a
#     key and never a limit input;
#   * state is protected by a lock, because sync handlers run
#     concurrently in the AnyIO threadpool;
#   * unexpected failures fail CLOSED with a rejection, never a
#     silent admission. This matches app/ratelimit.py: a gate that
#     cannot count must not report that it is free;
#   * release() never raises, because it runs from a finally block
#     where an exception would replace the real failure.
# ============================================================

import logging
import threading

import app.config as config

# A child of the "app" logger, so it inherits the handler, the level
# and the format installed by app.observability.configure_logging().
# No second logging setup, and stdout/stderr only, which is what the
# Vercel runtime collects.
logger = logging.getLogger("app.llm_concurrency")

# Used when a caller supplied no request id at all. A fixed constant,
# never a value derived from a request.
_UNKNOWN_REQUEST_ID = "unknown"

# A request id is a 32 character uuid4 hex in production, but
# generate() is callable with any string, so the logged value is
# bounded and printable-filtered rather than trusted.
_MAX_REQUEST_ID_CHARS = 64


def _configured_limit():
    """The live cap, read on every acquisition.

    Read through the module rather than imported by value, so an
    operator change or a test monkeypatch takes effect immediately
    instead of at the next cold start.
    """

    return config.LLM_MAX_CONCURRENCY


def _safe_request_id(value):
    """A bounded, printable-only request id for one log field."""

    if value is None:
        return _UNKNOWN_REQUEST_ID

    cleaned = "".join(
        char if char.isprintable() else "?"
        for char in str(value)
    ).strip()

    if not cleaned:
        return _UNKNOWN_REQUEST_ID

    return cleaned[:_MAX_REQUEST_ID_CHARS]


class LLMConcurrencyCap:
    """A counting gate over the number of in-flight provider calls.

    ``limit_provider`` is a callable returning the current limit. It
    is read on every acquisition rather than captured once, so the
    configured value is always the value in force.
    """

    def __init__(self, limit_provider=None):

        self._lock = threading.Lock()
        self._in_flight = 0
        self._limit_provider = (
            limit_provider or _configured_limit
        )

    def _limit(self):
        """The limit in force, or a raised error if it is unusable.

        bool is rejected explicitly: True is an int in Python and
        would otherwise silently become a limit of one.
        """

        raw = self._limit_provider()

        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw < 1
        ):
            raise ValueError("invalid concurrency limit")

        return raw

    @property
    def limit(self):
        """The configured cap, for logging. Falls back to 0 on error."""

        try:
            return self._limit()

        except Exception:

            logger.error(
                "LLM CONCURRENCY CAP LIMIT FAILURE",
                exc_info=True,
            )
            return 0

    @property
    def in_flight(self):
        """A snapshot of the counter, for logging and tests.

        Best effort only. It reports 0 when the counter cannot be
        read, which is why the rejection line is a diagnostic and
        never an authorisation input; the accompanying ERROR record
        carries the real cause.
        """

        try:

            with self._lock:
                return self._in_flight

        except Exception:

            logger.error(
                "LLM CONCURRENCY CAP READ FAILURE",
                exc_info=True,
            )
            return 0

    def try_acquire(self):
        """Take one slot if one is free. Never blocks, never waits.

        Returns False when the cap is reached and also when the gate
        itself is broken, so a caller has exactly one refusal to
        handle. Checked and incremented under one lock, so concurrent
        callers can never be admitted past the limit.
        """

        try:

            limit = self._limit()

            with self._lock:

                if self._in_flight >= limit:
                    return False

                self._in_flight += 1

                return True

        except Exception:

            # Fail closed. The message is a fixed literal with no
            # limit, count, identity or internal detail in it, so it
            # is safe to log.
            logger.error(
                "LLM CONCURRENCY CAP FAILURE",
                exc_info=True,
            )

            return False

    def release(self):
        """Give one slot back. Never raises.

        An unbalanced release means a bookkeeping bug, and it is
        reported rather than absorbed silently. The counter is left
        untouched in that case: an over-release must not be able to
        invent capacity the gate never granted. Raising here is not
        an option, because this runs from a finally block and the
        exception would replace whatever real failure is unwinding.
        """

        try:

            with self._lock:

                if self._in_flight <= 0:
                    raise ValueError(
                        "permit released more than once"
                    )

                self._in_flight -= 1

        except Exception:

            logger.error(
                "LLM CONCURRENCY CAP RELEASE FAILURE",
                exc_info=True,
            )

    def reset(self):
        """Clear the counter. Used by the test suite for isolation."""

        with self._lock:
            self._in_flight = 0


# The process-wide gate. One instance, for the same reason
# app.ratelimit keeps one limiter: a single reset() has to be able to
# isolate the whole suite between tests.
gate = LLMConcurrencyCap()


def log_saturation(request_id=None):
    """Record one rejection.

    A rejection is a served 429, not a failure, so this is INFO on
    the same "app" logger as the request lines - one stream, one
    place to search. The three fields are the ones an operator needs
    to tell saturation from a slow provider: the configured cap, the
    count that was already in flight, and the id tying this to the
    access line for the same request. No identity, token, IP or
    prompt is available to interpolate in the first place.
    """

    logger.info(
        "llm_concurrency_saturated limit=%s in_flight=%s request_id=%s",
        gate.limit,
        gate.in_flight,
        _safe_request_id(request_id),
    )