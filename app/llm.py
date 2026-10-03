# ============================================================
# LLM PROVIDER ABSTRACTION
# ============================================================
# A clean provider interface shared by Gemini and NVIDIA. The
# active provider is selected through LLM_PROVIDER ("nvidia" or
# "gemini"); both providers keep their own configuration and can
# be used at any time. Both return the same (sql, explanation)
# contract and consume the same schema-driven prompts.
#
# Each successful generation records the usage metadata its provider
# already returned, through app.token_usage. That is observation only:
# no budget, no limit, no added call, no change to the retry loop or
# to the (sql, explanation) contract. See app/token_usage.py.
#
# A caller may also pass a GenerationRef to generate()/ask_active(),
# which the answering provider fills in with its own name and model.
# That is how app/provenance.py reports the provider that really
# answered instead of the one LLM_PROVIDER appears to select.
#
# Every generate() body runs inside one concurrency permit, so the
# number of provider calls in flight on this instance is bounded. See
# app/llm_concurrency.py. The permit covers the whole retry loop, not
# each attempt: a retried request still holds exactly one slot, so
# the upstream call count per request stays exactly 1 + LLM_RETRIES
# and the cap cannot change retry semantics. A saturated instance is
# rejected with LLMConcurrencySaturated rather than queued, and the
# HTTP layer turns that into 429 + Retry-After.
# ============================================================

import json
import re
import time

from contextlib import contextmanager

from google import genai
from google.genai import types

import app.llm_concurrency as llm_concurrency
import app.token_usage as token_usage

try:
    from openai import (
        APIConnectionError as OpenAIConnectionError,
        APITimeoutError as OpenAITimeoutError,
        AuthenticationError as OpenAIAuthenticationError,
        InternalServerError as OpenAIInternalServerError,
        RateLimitError as OpenAIRateLimitError,
    )
except Exception:
    # openai is declared in requirements.txt; this guard only keeps
    # the module importable if the SDK is not installed yet.
    OpenAIConnectionError = None
    OpenAITimeoutError = None
    OpenAIAuthenticationError = None
    OpenAIInternalServerError = None
    OpenAIRateLimitError = None

from app.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LLM_PROVIDER,
    LLM_RETRIES,
    LLM_RETRY_BACKOFF_CAP_SECONDS,
    LLM_TIMEOUT_SECONDS,
    NVIDIA_API_KEY,
    NVIDIA_BASE_URL,
    NVIDIA_MODEL,
)
from app.prompts import (
    build_system_instruction,
    build_user_message,
)
from app.schema import inspect_database

# Lazy Gemini client: the app (UI + /api/schema) must stay usable even
# when the AI key is missing. Only /api/ask requires a provider.
client = None

if GEMINI_API_KEY:
    client = genai.Client(
        api_key=GEMINI_API_KEY
    )


def parse_provider_payload(raw_text, provider_name="Gemini"):
    """Parse a provider JSON response into (sql, explanation).

    Messages reference the provider that produced the response.
    """

    cleaned = raw_text.strip()

    cleaned = re.sub(
        r"^```(?:json|sql)?",
        "",
        cleaned,
        flags=re.IGNORECASE
    ).strip()

    cleaned = cleaned.replace("```", "").strip()

    payload = None

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                payload = None

    if isinstance(payload, dict):
        sql = str(payload.get("sql") or "").strip()
        explanation = str(payload.get("explanation") or "").strip()
    else:
        sql = cleaned
        explanation = ""

    sql = re.sub(
        r"^```sql",
        "",
        sql,
        flags=re.IGNORECASE
    ).replace("```", "").strip()

    if not sql:
        raise ValueError(
            f"{provider_name} did not return a SQL query."
        )

    if sql.startswith("{"):
        raise ValueError(
            f"{provider_name} returned an incomplete response. "
            "Please try again."
        )

    if not explanation:
        # No invented explanation. A missing explanation is reported as
        # missing, which the UI already handles by omitting the section.
        # Writing "This query reads the requested rows." here would
        # claim something about the query that no model actually said.
        explanation = ""

    return sql, explanation


def parse_gemini_payload(raw_text):
    return parse_provider_payload(raw_text, provider_name="Gemini")


def _is_quota_exhausted(exception):
    """True when the provider refused because a quota/rate limit is hit.

    A quota answer is terminal for this request. Retrying it multiplies
    load against a limit that is already exhausted and cannot improve
    availability, so it is classified separately from a genuine
    transient failure below.
    """

    if getattr(exception, "code", None) == 429:
        return True

    if getattr(exception, "status_code", None) == 429:
        return True

    if OpenAIRateLimitError is not None and isinstance(
        exception, OpenAIRateLimitError
    ):
        return True

    # Catch a quota error that arrives without a usable status, so the
    # string markers below cannot reclassify it as transient.
    text = str(exception).lower()

    return "429" in text or "quota exceeded" in text


def _is_transient_error(exception):
    """True when the failure is likely worth a retry.

    Covers Gemini markers plus OpenAI/NVIDIA transient error classes
    and HTTP 5xx. Quota exhaustion (HTTP 429) is deliberately NOT
    transient: see _is_quota_exhausted.
    """

    if _is_quota_exhausted(exception):
        return False

    text = str(exception).lower()

    if any(
        marker in text
        for marker in [
            "unavailable",
            "high demand",
            "503",
        ]
    ):
        return True

    if getattr(exception, "code", None) == 503:
        return True

    if getattr(exception, "status_code", None) in (500, 502, 503, 504):
        return True

    transient_types = (
        OpenAIConnectionError,
        OpenAITimeoutError,
        OpenAIInternalServerError,
    )

    if any(
        error_type is not None
        and isinstance(exception, error_type)
        for error_type in transient_types
    ):
        return True

    return False


def _backoff_seconds(attempt):
    """Bounded linear backoff between application-level attempts.

    attempt is 1-based. The result is clamped so that raising
    LLM_RETRIES cannot make a single request wait without limit.
    """

    return min(
        1.5 * attempt,
        LLM_RETRY_BACKOFF_CAP_SECONDS,
    )


def _duration_ms(started_at):
    """Elapsed milliseconds, rounded to a whole number.

    Measured across the whole generate() call, so it includes reading
    the schema and building the prompt as well as every provider
    attempt. That is the latency a user actually waited, and it is
    reported as a count, never used as a limit.
    """

    return int(
        round((time.perf_counter() - started_at) * 1000)
    )


_SECRET_PATTERNS = (
    re.compile(r"nvapi-[A-Za-z0-9_\-]+", re.IGNORECASE),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)(api[-_]?key\"?\s*[:=]\s*)\S+"),
)


def _safe_upstream_detail(exception, limit=300):
    """Return a short, secret-free excerpt of the upstream error body.

    The upstream body is the only thing that explains a non-2xx answer
    (retired model, bad request, ...). Without it every failure collapses
    into one opaque message and cannot be diagnosed from the UI.
    Redaction keeps a reflected credential out of the response.
    """

    body = getattr(exception, "body", None)
    text = None

    if isinstance(body, str):
        text = body
    elif isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else None
        for source in (error, body):
            if not isinstance(source, dict):
                continue
            for field in ("detail", "message", "error"):
                value = source.get(field)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
            if text:
                break
        if not text:
            try:
                text = json.dumps(body)
            except (TypeError, ValueError):
                text = str(body)

    if not text:
        response = getattr(exception, "response", None)
        if response is not None:
            try:
                text = response.text
            except Exception:
                text = None

    if not text:
        text = getattr(exception, "message", None)

    if not text:
        return ""

    text = str(text)

    if NVIDIA_API_KEY:
        text = text.replace(NVIDIA_API_KEY, "<redacted>")

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)

    text = " ".join(text.split())

    if len(text) > limit:
        text = text[:limit] + "..."

    return text


def _map_nvidia_error(exception):
    """Convert an NVIDIA SDK exception into a clean app-level error.

    Never includes the API key or raw request data. Our own
    ValueError/RuntimeError messages are passed through untouched.
    """

    if isinstance(exception, (ValueError, RuntimeError)):
        return exception

    if OpenAIAuthenticationError is not None and isinstance(
        exception, OpenAIAuthenticationError
    ):
        return ValueError(
            "NVIDIA authentication failed. "
            "Check that NVIDIA_API_KEY is valid and try again."
        )

    if OpenAIRateLimitError is not None and isinstance(
        exception, OpenAIRateLimitError
    ):
        return ValueError(
            "NVIDIA API rate limit or quota exceeded. "
            "Please wait a moment and try again."
        )

    if (
        OpenAIConnectionError is not None
        and isinstance(exception, (OpenAIConnectionError, OpenAITimeoutError))
    ):
        return ValueError(
            "Could not reach the NVIDIA API. "
            "Please check your connection and try again."
        )

    status = getattr(exception, "status_code", None)

    if status in (500, 502, 503, 504):
        return ValueError(
            "The NVIDIA API is temporarily unavailable. "
            "Please try again later."
        )

    if status == 401:
        return ValueError(
            "NVIDIA authentication failed. "
            "Check that NVIDIA_API_KEY is valid and try again."
        )

    if status == 429:
        return ValueError(
            "NVIDIA API rate limit or quota exceeded. "
            "Please wait a moment and try again."
        )

    # Permanent, non-retryable model failures. Retrying cannot help, so
    # say what actually happened instead of "try again".
    if status == 410:
        return ValueError(
            "The configured NVIDIA model is no longer available "
            "(upstream returned HTTP 410 Gone). "
            "Update NVIDIA_MODEL in .env to a supported model."
        )

    if status == 404:
        return ValueError(
            "The configured NVIDIA model was not found by the NVIDIA API "
            "(upstream returned HTTP 404). "
            "Update NVIDIA_MODEL in .env to a supported model."
        )

    detail = _safe_upstream_detail(exception)

    prefix = (
        f"NVIDIA API request failed (HTTP {status})"
        if status
        else f"NVIDIA API request failed ({type(exception).__name__})"
    )

    if detail:
        return ValueError(
            f"{prefix}. Upstream said: {detail}"
        )

    return ValueError(
        f"{prefix}. Please try again."
    )


class GenerationRef:
    """The provider and model that actually produced one answer.

    A caller that wants to describe where a result came from creates
    one of these, passes it to generate(), and reads it afterwards. The
    provider fills it in only after the response has parsed into a
    usable (sql, explanation), so a field left as None means no answer
    was produced, never "the configured provider, probably".

    It is an out-parameter on purpose. The alternative, returning a
    third value, would change the (sql, explanation) contract that every
    caller and test depends on; a module-level "last generation" would
    be wrong as soon as two requests overlapped.
    """

    __slots__ = ("provider", "model")

    def __init__(self, provider=None, model=None):
        self.provider = provider
        self.model = model

    def record(self, provider, model):
        self.provider = provider
        self.model = model


class LLMConcurrencySaturated(RuntimeError):
    """This instance already has LLM_MAX_CONCURRENCY calls in flight.

    A capacity answer, not a provider failure: no call was made, no
    quota was spent, and retrying cannot help while the cap is full.
    The HTTP layer maps it to 429 with Retry-After.

    RuntimeError is deliberate. NVIDIAProvider.generate() re-raises
    RuntimeError untouched before _map_nvidia_error sees it, so this
    can never be rewritten into an ordinary provider error - and,
    because the permit is taken outside that method's try block, no
    clause inside the retry loop can intercept it either.
    """


@contextmanager
def _llm_permit(request_id=None):
    """Hold one concurrency slot for the duration of a generation.

    Fail fast by design. Waiting for a free slot inside a
    synchronous threadpool worker would hold an AnyIO thread while
    doing nothing, so a queue deep enough to help would starve every
    other route instead of protecting them. Refusing immediately is
    the only outcome that cannot make the instance worse.

    The release is in a finally block, so it runs on success, on a
    provider failure, on an exhausted retry sequence and on a parse
    failure alike. Nothing can escape this block holding a slot.
    """

    if not llm_concurrency.gate.try_acquire():

        llm_concurrency.log_saturation(request_id)

        raise LLMConcurrencySaturated(
            "The LLM concurrency cap is saturated on this instance."
        )

    try:
        yield

    finally:
        llm_concurrency.gate.release()


class LLMProvider:
    """Interface all providers implement."""

    name = "generic"

    def generate(
        self,
        user_prompt,
        history,
        request_id=None,
        generation=None,
    ):
        """Return (sql, explanation).

        request_id is an opaque correlation string supplied by the
        caller. It is optional, so every existing call site keeps
        working, and it is used for one log field and nothing else:
        never as a key, never as a limit input.

        generation is an optional GenerationRef. When supplied, the
        provider records its own name and model in it after a
        successful parse, so a caller can report the provider that
        really answered instead of guessing from configuration.
        """

        raise NotImplementedError


class GeminiProvider(LLMProvider):

    name = "gemini"
    model = GEMINI_MODEL

    def __init__(self, model=None):
        self.model = model or GEMINI_MODEL

    def _call_model(self, contents, system_instruction):

        if client is None:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured. "
                "Set it in .env to enable AI query generation."
            )

        return client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0,
                max_output_tokens=1200,
                response_mime_type="application/json"
            )
        )

    def generate(
        self,
        user_prompt,
        history,
        request_id=None,
        generation=None,
    ):

        # Outermost statement on purpose: the permit is taken before
        # the schema is read, the prompt is built and the retry loop
        # begins, and it is held across every attempt inside that
        # loop. One request is therefore exactly one slot, however
        # many times it has to be retried.
        with _llm_permit(request_id):

            started_at = time.perf_counter()

            schema = inspect_database()
            system_instruction = build_system_instruction(
                schema
            )
            user_message = build_user_message(
                user_prompt, history
            )

            attempts = LLM_RETRIES + 1
            last_error = None

            for attempt in range(attempts):

                try:

                    response = self._call_model(
                        user_message,
                        system_instruction,
                    )

                    if not response.text:
                        raise ValueError(
                            "Gemini returned an empty response."
                        )

                    parsed = parse_gemini_payload(
                        response.text
                    )

                    self._record_usage(
                        response,
                        started_at,
                        request_id,
                    )

                    _record_generation(generation, self)

                    return parsed

                except Exception as error:

                    last_error = error

                    if not _is_transient_error(error):
                        raise

                    if attempt < attempts - 1:
                        time.sleep(
                            _backoff_seconds(attempt + 1)
                        )

            raise last_error

    def _record_usage(
        self, response, started_at, request_id
    ):
        """Record token usage for one completed generation.

        Called only after the answer parsed successfully, and never
        inside the retry loop, so it cannot change how many times the
        provider is called. The whole body is guarded: by this point the
        answer already exists, so no failure inside observability may be
        allowed to turn a completed generation into a 500.
        """

        try:

            token_usage.log_usage(
                token_usage.extract_gemini_usage(
                    response,
                    model=self.model,
                    duration_ms=_duration_ms(started_at),
                ),
                request_id,
            )

        except Exception:
            return


class NVIDIAProvider(LLMProvider):
    """OpenAI-compatible NVIDIA provider (integrate.api.nvidia.com)."""

    name = "nvidia"
    default_base_url = "https://integrate.api.nvidia.com/v1"

    def __init__(self, model=None, base_url=None):
        self.model = model or NVIDIA_MODEL
        self.base_url = base_url or NVIDIA_BASE_URL or self.default_base_url
        self.api_key = NVIDIA_API_KEY
        self._client = None

    def is_configured(self):
        return bool(self.api_key and self.model)

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,

                # Explicit, because the SDK default is a 600 second
                # timeout. Left unset, one slow upstream call holds a
                # shared AnyIO thread and can starve other routes.
                timeout=LLM_TIMEOUT_SECONDS,

                # Explicit, because the SDK default is 2. A hidden
                # retry layer here multiplies with the loop in
                # generate(): a single /api/ask would reach the
                # provider (1 + LLM_RETRIES) * 3 times. This module is
                # the ONLY retry layer, so the upstream call count per
                # request is exactly 1 + LLM_RETRIES.
                max_retries=0,
            )
        return self._client

    def _build_messages(self, user_message, system_instruction):
        return [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_message},
        ]

    def _call_model(self, user_message, system_instruction):

        if not self.api_key:
            raise RuntimeError(
                "NVIDIA_API_KEY is not configured. "
                "Set it in .env to enable AI query generation."
            )

        if not self.model:
            raise RuntimeError(
                "NVIDIA_MODEL is not configured. "
                "Set it in .env to enable AI query generation."
            )

        # The response object is returned rather than its text, because
        # it is the only object that carries usage metadata. One call to
        # the provider, exactly as before.
        return self._get_client().chat.completions.create(
            model=self.model,
            messages=self._build_messages(
                user_message,
                system_instruction,
            ),
            temperature=1.0,
            top_p=0.95,
            max_tokens=1200,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": False,
                },
            },
        )

    @staticmethod
    def _content_from(response):
        """The assistant text of a completion response.

        Kept equivalent to the inline extraction this replaced, so the
        empty-answer path is unchanged.
        """

        choices = getattr(response, "choices", None)

        if not choices:
            return ""

        return choices[0].message.content or ""

    def generate(
        self,
        user_prompt,
        history,
        request_id=None,
        generation=None,
    ):

        # See GeminiProvider.generate: one permit per request, taken
        # outside the retry loop and held across every attempt, so a
        # retried request still costs exactly one slot.
        with _llm_permit(request_id):

            started_at = time.perf_counter()

            schema = inspect_database()
            system_instruction = build_system_instruction(
                schema
            )
            user_message = build_user_message(
                user_prompt, history
            )

            attempts = LLM_RETRIES + 1
            last_error = None

            for attempt in range(attempts):

                try:

                    response = self._call_model(
                        user_message,
                        system_instruction,
                    )

                    response_text = self._content_from(
                        response
                    )

                    if not response_text:
                        raise ValueError(
                            "NVIDIA returned an empty response."
                        )

                    parsed = parse_provider_payload(
                        response_text,
                        provider_name="NVIDIA",
                    )

                    self._record_usage(
                        response,
                        started_at,
                        request_id,
                    )

                    _record_generation(generation, self)

                    return parsed

                except RuntimeError:

                    # The provider's own configuration errors (no API
                    # key, no model) are already app-level messages
                    # and pass through untouched.
                    raise

                except Exception as error:

                    last_error = error

                    if not _is_transient_error(error):
                        raise _map_nvidia_error(error)

                    if attempt < attempts - 1:
                        time.sleep(
                            _backoff_seconds(attempt + 1)
                        )

            raise _map_nvidia_error(last_error)

    def _record_usage(
        self, response, started_at, request_id
    ):
        """Record token usage for one completed generation.

        Called only after the answer parsed successfully, and never
        inside the retry loop, so it cannot change how many times the
        provider is called. The whole body is guarded: by this point the
        answer already exists, so no failure inside observability may be
        allowed to turn a completed generation into a 500.
        """

        try:

            token_usage.log_usage(
                token_usage.extract_openai_usage(
                    response,
                    model=self.model,
                    duration_ms=_duration_ms(started_at),
                ),
                request_id,
            )

        except Exception:
            return


def _record_generation(generation, provider):
    """Note which provider instance answered, if the caller asked.

    Guarded for the same reason as _record_usage: the answer already
    exists, so observability must never turn it into an error. Nothing
    is recorded when generation is None, which is how every existing
    call site keeps its current behaviour.
    """

    try:

        if generation is None:
            return

        generation.record(
            getattr(provider, "name", None),
            getattr(provider, "model", None),
        )

    except Exception:
        return


def get_provider():
    """Return the active provider selected by LLM_PROVIDER.

    - "nvidia"  -> NVIDIAProvider
    - "gemini"  -> GeminiProvider
    """

    name = (LLM_PROVIDER or "nvidia").strip().lower()

    if name == "nvidia":
        return NVIDIAProvider()

    if name == "gemini":
        return GeminiProvider()

    raise ValueError(
        f"Unknown LLM_PROVIDER '{name}'. "
        "Use 'nvidia' or 'gemini'."
    )


def ask_active(
    user_prompt,
    history,
    request_id=None,
    generation=None,
):
    """Generate with the configured provider.

    request_id is optional and opaque: the HTTP layer passes its
    correlation id so usage can be tied to the request that spent the
    tokens, and any caller may omit it.

    generation is an optional GenerationRef filled in with the provider
    and model that answered. The return value is unchanged.
    """

    return get_provider().generate(
        user_prompt,
        history,
        request_id=request_id,
        generation=generation,
    )


def ask_gemini(
    user_prompt,
    history,
    request_id=None,
    generation=None,
):
    return GeminiProvider().generate(
        user_prompt,
        history,
        request_id=request_id,
        generation=generation,
    )