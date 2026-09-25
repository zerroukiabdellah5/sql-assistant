# ============================================================
# LLM PROVIDER ABSTRACTION
# ============================================================
# A clean provider interface shared by Gemini and NVIDIA. The
# active provider is selected through LLM_PROVIDER ("nvidia" or
# "gemini"); both providers keep their own configuration and can
# be used at any time. Both return the same (sql, explanation)
# contract and consume the same schema-driven prompts.
# ============================================================

import json
import re
import time

from google import genai
from google.genai import types

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
        explanation = "This query reads the requested rows."

    return sql, explanation


def parse_gemini_payload(raw_text):
    return parse_provider_payload(raw_text, provider_name="Gemini")


def _is_transient_error(exception):
    """True when the failure is likely worth a retry.

    Covers Gemini markers plus OpenAI/NVIDIA transient error classes
    and HTTP 5xx / 429 status codes.
    """

    text = str(exception).lower()

    if any(
        marker in text
        for marker in [
            "unavailable",
            "high demand",
            "503",
            "429",
            "resource_exhausted",
        ]
    ):
        return True

    if getattr(exception, "code", None) in (429, 503):
        return True

    if getattr(exception, "status_code", None) in (429, 500, 502, 503, 504):
        return True

    transient_types = (
        OpenAIConnectionError,
        OpenAITimeoutError,
        OpenAIInternalServerError,
        OpenAIRateLimitError,
    )

    if any(
        error_type is not None
        and isinstance(exception, error_type)
        for error_type in transient_types
    ):
        return True

    return False


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


class LLMProvider:
    """Interface all providers implement."""

    name = "generic"

    def generate(self, user_prompt, history):
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

    def generate(self, user_prompt, history):

        schema = inspect_database()
        system_instruction = build_system_instruction(schema)
        user_message = build_user_message(user_prompt, history)

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

                return parse_gemini_payload(response.text)

            except Exception as error:

                last_error = error

                if not _is_transient_error(error):
                    raise

                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))

        raise last_error


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

        response = self._get_client().chat.completions.create(
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

        content = ""
        if response.choices:
            content = response.choices[0].message.content or ""

        return content

    def generate(self, user_prompt, history):

        schema = inspect_database()
        system_instruction = build_system_instruction(schema)
        user_message = build_user_message(user_prompt, history)

        attempts = LLM_RETRIES + 1
        last_error = None

        for attempt in range(attempts):

            try:

                response_text = self._call_model(
                    user_message,
                    system_instruction,
                )

                if not response_text:
                    raise ValueError(
                        "NVIDIA returned an empty response."
                    )

                return parse_provider_payload(
                    response_text,
                    provider_name="NVIDIA",
                )

            except RuntimeError:

                raise

            except Exception as error:

                last_error = error

                if not _is_transient_error(error):
                    raise _map_nvidia_error(error)

                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))

        raise _map_nvidia_error(last_error)


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


def ask_active(user_prompt, history):
    return get_provider().generate(user_prompt, history)


def ask_gemini(user_prompt, history):
    return GeminiProvider().generate(user_prompt, history)