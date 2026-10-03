# ============================================================
# LLM TOKEN USAGE OBSERVABILITY
# ============================================================
# Reads the usage metadata a provider already returns, and writes one
# safe line per successful generation. Observation only: nothing here
# decides anything. There is no budget, no quota, no counter that can
# block a request, and no behaviour depends on the values captured.
#
# WHY A SEPARATE MODULE
#   * provider-specific knowledge lives in one testable place: the
#     OpenAI-compatible field names and the Gemini field names are
#     never mixed up, and neither is imported into the other;
#   * it depends on the standard library only, so the LLM layer stays
#     free of FastAPI, starlette and HTTP objects. A provider can be
#     exercised in a unit test with no Request, no client and no app;
#   * the loggers are children of the "app" logger configured in
#     app.observability, so these lines land on the same stream, with
#     the same format, as the request lines. No second logging setup.
#
# WHAT IS LOGGED
#   exactly seven fields: provider, model, prompt_tokens,
#   completion_tokens, total_tokens, duration_ms, request_id.
#
# WHAT IS NEVER LOGGED
#   the prompt, the generated SQL, the schema, conversation history,
#   session ids, API keys, cookies, headers, or any provider payload.
#   The counts arrive as integers or None and are interpolated as
#   integers or None; no provider string ever reaches the line except
#   provider, model and request_id, and all three pass an allowlist.
#   provider and model are operator configuration read from the
#   environment, not request data, and they are reduced to a bounded
#   token of letters, digits and ".-_/:+" so a configuration value can
#   neither add a field to the line nor append a record. request_id is
#   held to the 32-hex-character form the application generates, so a
#   cookie or session token passed here by mistake degrades to
#   "unknown" instead of being written out.
#
# MISSING USAGE
#   "not reported" is recorded as None and available=False, which is
#   distinct from a reported 0. A count is never inferred, derived or
#   estimated: if the provider did not send it, this module does not
#   invent it, and a missing total stays missing even when the other
#   two are present.
#
# SCOPE
#   one record per successful generate() call, on the attempt that
#   produced the answer. A call that ends in an error is not recorded:
#   a failed attempt returns no usage metadata. That undercount is
#   accepted for now and noted as a known limitation, because a budget
#   is not being enforced in this step.
# ============================================================

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

# A child of the "app" logger, so it inherits the handler, the level
# and the format configured by app.observability.configure_logging().
_LOGGER_NAME = "app.usage"

logger = logging.getLogger(_LOGGER_NAME)

# Provider identifiers. Constants, never derived from a response.
NVIDIA = "nvidia"
GEMINI = "gemini"

# Used when a generation was started outside a request (a script, a
# test, a future CLI). A fixed constant, never a value derived from
# anything.
UNKNOWN_REQUEST_ID = "unknown"

# Bounds keep one hostile or mistaken configuration value from flooding
# a log or forging a record.
MAX_FIELD_CHARS = 120

_UNAVAILABLE = "unavailable"


# ------------------------------------------------------------
# THE RECORD
# ------------------------------------------------------------

@dataclass(frozen=True)
class TokenUsage:
    """Usage metadata for one provider call.

    Counts are Optional[int]: None means "the provider did not report
    this", which is deliberately different from 0.
    """

    provider: str
    model: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    duration_ms: Optional[int] = None

    @property
    def available(self) -> bool:
        """True when the provider reported at least one count."""

        return any(
            value is not None
            for value in (
                self.prompt_tokens,
                self.completion_tokens,
                self.total_tokens,
            )
        )


# ------------------------------------------------------------
# READING A PROVIDER RESPONSE
# ------------------------------------------------------------
# Both extractors accept either an object or a mapping, because an
# OpenAI-compatible endpoint reached through the SDK yields model
# objects while other compatible clients yield plain dicts. Neither
# extractor raises: usage metadata is a convenience, and a response
# that cannot be read must not break a generation that already
# succeeded.

def _read(source: Any, *names: str) -> Any:
    """First present value among names, or None.

    A mapping is read by key, anything else by attribute. The first
    name that exists wins, which is how the Gemini naming
    (prompt_token_count) and the OpenAI naming (prompt_tokens) are both
    honoured from one call site.
    """

    if source is None:
        return None

    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None

    for name in names:

        try:
            value = getattr(source, name, None)
        except Exception:
            # A response object whose attribute raises must not turn a
            # successful generation into a failed one.
            continue

        if value is not None:
            return value

    return None


def _count(value: Any) -> Optional[int]:
    """Coerce a provider count to a non-negative int, or None.

    A bool is not a count (True would silently become 1), a negative
    number is not a count, and an unparsable value is reported as
    absent rather than guessed at.
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value >= 0 else None

    if isinstance(value, float):
        if value != value or value in (
            float("inf"), float("-inf")
        ):
            return None
        if value < 0 or value != int(value):
            return None
        return int(value)

    if isinstance(value, str):
        text = value.strip()
        if not text.lstrip("-").isdigit():
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    return None


def extract_openai_usage(
    response: Any,
    model: str = "",
    duration_ms: Optional[int] = None,
) -> TokenUsage:
    """Usage from an OpenAI-compatible chat completion response.

    Reads response.usage.prompt_tokens / completion_tokens /
    total_tokens. The input_tokens / output_tokens aliases some
    compatible endpoints send are accepted as well.

    Returns a record with available=False and every count None when the
    response carries no usage at all, which is a normal outcome for an
    OpenAI-compatible endpoint and must never be an error.
    """

    usage = _read(response, "usage")

    return TokenUsage(
        provider=NVIDIA,
        model=model,
        prompt_tokens=_count(
            _read(usage, "prompt_tokens", "input_tokens")
        ),
        completion_tokens=_count(
            _read(usage, "completion_tokens", "output_tokens")
        ),
        total_tokens=_count(_read(usage, "total_tokens")),
        duration_ms=_count(duration_ms),
    )


def extract_gemini_usage(
    response: Any,
    model: str = "",
    duration_ms: Optional[int] = None,
) -> TokenUsage:
    """Usage from a Gemini generate_content response.

    The google-genai SDK exposes usage_metadata with
    prompt_token_count / candidates_token_count / total_token_count.
    usage is accepted as a fallback name because the field has been
    spelled both ways across SDK generations, and neither spelling is
    invented here: only an attribute the response really carries is
    read.

    Gemini reports a candidate count rather than a completion count.
    The two are mapped onto the same field so the record shape does
    not depend on the provider; candidates_token_count is what the API
    bills and returns for the generated text.

    Returns a record with available=False when the response exposes no
    usage metadata. No count is derived from the response length or
    from any estimate.
    """

    metadata = _read(response, "usage_metadata", "usage")

    return TokenUsage(
        provider=GEMINI,
        model=model,
        prompt_tokens=_count(
            _read(
                metadata,
                "prompt_token_count",
                "prompt_tokens",
            )
        ),
        completion_tokens=_count(
            _read(
                metadata,
                "candidates_token_count",
                "completion_tokens",
            )
        ),
        total_tokens=_count(
            _read(metadata, "total_token_count", "total_tokens")
        ),
        duration_ms=_count(duration_ms),
    )


# ------------------------------------------------------------
# THE LOG LINE
# ------------------------------------------------------------

def safe_field(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """A bounded single-token field value.

    Shared by every module that puts operator configuration into a log
    line or a response body. There is one implementation of this rule on
    purpose: a second copy would be a second answer to "what may leave
    the process".

    Only provider and model pass through here, and both are operator
    configuration rather than request data. The character allowlist is
    stricter than "printable": a value containing a space or an equals
    sign is reduced to separate words, so it cannot add a field to the
    line or forge a second record, and the length bound stops it
    flooding the log.
    """

    allowed = "abcdefghijklmnopqrstuvwxyz"
    allowed += "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    allowed += "0123456789"
    allowed += ".-_/:+"

    cleaned = "".join(
        character if character in allowed else " "
        for character in str(value or "")
    )

    cleaned = " ".join(cleaned.split())

    if len(cleaned) > limit:
        cleaned = cleaned[:limit]

    return cleaned or "-"


def safe_request_id(value: Any) -> str:
    """A request id, or the word for its absence.

    Also shared: app.provenance returns request ids to clients through
    this same rule, so what a client sees in a log line and what it sees
    in a response body can never disagree.

    The application generates request ids as 32 lowercase hex
    characters, so anything else is not a request id. Refusing to
    reproduce it means a caller that passes a cookie, a session token
    or any other credential by mistake still cannot get it into a
    response: the field degrades to "unknown" instead.
    """

    text = str(value or "")

    if text == UNKNOWN_REQUEST_ID:
        return UNKNOWN_REQUEST_ID

    if len(text) == 32 and all(
        character in "0123456789abcdef" for character in text
    ):
        return text

    return UNKNOWN_REQUEST_ID


def _count_field(value: Optional[int]) -> str:
    """A count, or the literal word for its absence.

    None is rendered as "unavailable" rather than 0, so a log search
    cannot mistake a provider that reported nothing for a provider
    that reported a free request.
    """

    return _UNAVAILABLE if value is None else str(value)


def usage_fields(
    usage: TokenUsage,
    request_id: Optional[str] = None,
) -> dict:
    """The exact data behind the log line, for tests and callers."""

    return {
        "provider": safe_field(usage.provider, 32),
        "model": safe_field(usage.model),
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "duration_ms": usage.duration_ms,
        "request_id": safe_request_id(request_id),
        "available": usage.available,
    }


def format_usage_line(
    usage: TokenUsage,
    request_id: Optional[str] = None,
) -> str:
    """One line, seven fields, built from usage_fields() alone."""

    fields = usage_fields(usage, request_id)

    return (
        f"provider={fields['provider']} "
        f"model={fields['model']} "
        f"prompt_tokens={_count_field(fields['prompt_tokens'])} "
        f"completion_tokens={_count_field(fields['completion_tokens'])} "
        f"total_tokens={_count_field(fields['total_tokens'])} "
        f"duration_ms={_count_field(fields['duration_ms'])} "
        f"request_id={fields['request_id']}"
    )


def log_usage(
    usage: TokenUsage,
    request_id: Optional[str] = None,
) -> None:
    """Write the usage line.

    Observation only: this function returns None, records nothing, and
    raises nothing. A logging problem must not be able to fail a
    generation that already produced an answer.
    """

    # Importing app.observability applies the project logging
    # configuration. It is done here rather than at module scope so
    # this module stays importable, and unit-testable, without FastAPI
    # and without depending on import order: a script that uses a
    # provider still gets the handler instead of silently dropping
    # INFO records.
    try:
        from app import observability  # noqa: F401
    except Exception:
        pass

    try:
        line = format_usage_line(usage, request_id)
    except Exception:
        # Even formatting is not allowed to break a completed request.
        return

    logger.info("%s", line)