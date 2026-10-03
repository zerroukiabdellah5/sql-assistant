import httpx2
import pytest

import openai

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

import app.config as config
from app import llm


# ============================================================
# RESPONSE PARSING
# ============================================================

def test_parse_plain_json():
    sql, explanation = llm.parse_gemini_payload(
        '{"sql": "SELECT 1", "explanation": "Returns one."}'
    )
    assert sql == "SELECT 1"
    assert explanation == "Returns one."


def test_parse_fenced_json():
    sql, explanation = llm.parse_gemini_payload(
        '```json\n{"sql": "SELECT * FROM t", "explanation": "All rows."}\n```'
    )
    assert sql == "SELECT * FROM t"


def test_parse_sql_at_root():
    sql, explanation = llm.parse_gemini_payload(
        "SELECT name FROM products"
    )
    assert sql == "SELECT name FROM products"

    # Bare SQL carries no explanation and none is invented for it.
    assert explanation == ""


def test_parse_with_trailing_noise():
    sql, _ = llm.parse_gemini_payload(
        'Here you go:\n{"sql": "SELECT 2 AS x", "explanation": "Two."}\nthanks!'
    )
    assert sql == "SELECT 2 AS x"


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        llm.parse_gemini_payload("   ")


def test_parse_open_brace_raises():
    with pytest.raises(ValueError):
        llm.parse_gemini_payload(
            '{ "sql": "SELECT truncated'
        )


def test_parse_default_explanation():
    """A missing explanation is reported missing, never fabricated.

    The application used to substitute "This query reads the requested
    rows." here. That sentence asserts something about the query which
    no model produced, and a client could not tell it apart from a real
    explanation. The empty string is the state the UI, the report and
    the history already handle.
    """

    sql, explanation = llm.parse_gemini_payload(
        '{"sql": "SELECT 3 AS y"}'
    )
    assert sql == "SELECT 3 AS y"
    assert explanation == ""

    _, blank = llm.parse_gemini_payload(
        '{"sql": "SELECT 3 AS y", "explanation": "   "}'
    )
    assert blank == ""


def test_parse_provider_payload_uses_provider_name():
    with pytest.raises(
        ValueError, match="NVIDIA did not return a SQL query."
    ):
        llm.parse_provider_payload("   ", provider_name="NVIDIA")


# ============================================================
# PROVIDER CONTRACT + SELECTION
# ============================================================

def test_provider_interface_contracts(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "gemini")
    assert llm.GeminiProvider.name == "gemini"
    assert issubclass(llm.GeminiProvider, llm.LLMProvider)
    assert llm.NVIDIAProvider.name == "nvidia"
    assert issubclass(llm.NVIDIAProvider, llm.LLMProvider)
    assert llm.get_provider().name == "gemini"


def test_get_provider_selects_nvidia(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "nvidia")
    assert llm.get_provider().name == "nvidia"


def test_get_provider_selects_gemini(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "gemini")
    assert llm.get_provider().name == "gemini"


def test_get_provider_unknown_raises(monkeypatch):
    monkeypatch.setattr(llm, "LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        llm.get_provider()


def test_nvidia_default_base_url():
    assert llm.NVIDIAProvider.default_base_url.startswith(
        "https://integrate.api.nvidia.com/v1"
    )


# ============================================================
# FAKE OPENAI CLIENT FOR NVIDIA PROVIDER
# ============================================================

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletionsResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.captured = None

    def create(self, **kwargs):
        self.captured = kwargs
        return _FakeCompletionsResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content):
        self.chat = _FakeChat(content)


def _openai_status_error(
    error_class,
    status,
    message="request failed",
    body=None,
):
    response = httpx2.Response(
        status,
        request=httpx2.Request(
            "POST",
            "https://integrate.api.nvidia.com/v1/chat/completions",
        ),
    )
    return error_class(message, response=response, body=body)


def _openai_connection_error():
    return APIConnectionError(
        message="connection refused",
        request=httpx2.Request("POST", "https://integrate.api.nvidia.com/v1"),
    )


def _fake_nvidia_provider():
    provider = llm.NVIDIAProvider(
        model="test-model",
        base_url="https://integrate.api.nvidia.com/v1",
    )
    provider.api_key = "dummy-key"
    return provider


def test_nvidia_generate_returns_contract(monkeypatch):
    provider = _fake_nvidia_provider()
    fake_client = _FakeClient(
        'Sure!\n```json\n{"sql": "SELECT * FROM products", '
        '"explanation": "All products."}\n```'
    )
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    sql, explanation = provider.generate("Show me products", [])

    assert sql == "SELECT * FROM products"
    assert explanation == "All products."

    call = fake_client.chat.completions.captured
    assert call["model"] == "test-model"
    assert call["temperature"] == 1.0
    assert call["top_p"] == 0.95
    assert call["max_tokens"] == 1200
    assert call["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["system", "user"]


def test_nvidia_missing_api_key(monkeypatch):
    provider = _fake_nvidia_provider()
    provider.api_key = None
    with pytest.raises(
        RuntimeError, match="NVIDIA_API_KEY is not configured"
    ):
        provider.generate("show products", [])


def test_nvidia_missing_model(monkeypatch):
    provider = _fake_nvidia_provider()
    provider.model = None
    with pytest.raises(
        RuntimeError, match="NVIDIA_MODEL is not configured"
    ):
        provider.generate("show products", [])


# ============================================================
# NVIDIA ERROR MAPPING (no secrets in messages)
# ============================================================

def test_nvidia_authentication_error_mapped(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_status_error(
            AuthenticationError, 401, "Invalid API key: sk-leaked-secret"
        )

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    message = str(excinfo.value)
    assert "authentication" in message.lower()
    assert "sk-leaked-secret" not in message
    assert "dummy-key" not in message


def test_nvidia_rate_limit_error_mapped(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_status_error(RateLimitError, 429, "rate limit hit")

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    message = str(excinfo.value)
    assert ("rate limit" in message.lower()) or ("quota" in message.lower())


def test_nvidia_server_error_mapped(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_status_error(InternalServerError, 503, "unavailable")

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    assert "temporarily unavailable" in str(excinfo.value).lower()


def test_nvidia_connection_error_mapped(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_connection_error()

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    assert "could not reach" in str(excinfo.value).lower()


# ============================================================
# RETIRED / UNKNOWN MODEL (permanent upstream failures)
# ============================================================

def test_nvidia_gone_retired_model_mapped(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_status_error(
            APIStatusError,
            410,
            "Error code: 410",
            body={
                "type": "about:blank",
                "title": "Gone",
                "status": 410,
                "detail": (
                    "The model 'test-model' has reached its end of life "
                    "and is no longer available."
                ),
            },
        )

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    message = str(excinfo.value)
    assert "no longer available" in message
    assert "NVIDIA_MODEL" in message


def test_nvidia_retired_model_is_not_retried(monkeypatch):
    calls = []

    def boom(user_message, system_instruction):
        calls.append(1)
        raise _openai_status_error(
            APIStatusError,
            410,
            "Error code: 410",
            body={"detail": "model is no longer available"},
        )

    monkeypatch.setattr(llm, "LLM_RETRIES", 2)
    provider = _fake_nvidia_provider()
    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert len(calls) == 1


def test_nvidia_missing_model_404_mapped(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_status_error(
            APIStatusError,
            404,
            "Error code: 404",
            body={"detail": "Model 'test-model' not found."},
        )

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    assert "NVIDIA_MODEL" in str(excinfo.value)


def test_nvidia_unknown_status_surfaces_status_and_detail(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)
    provider = _fake_nvidia_provider()

    def boom(user_message, system_instruction):
        raise _openai_status_error(
            APIStatusError,
            400,
            "Error code: 400",
            body={"detail": "max_tokens exceeds the model limit."},
        )

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    message = str(excinfo.value)
    assert "HTTP 400" in message
    assert "max_tokens exceeds the model limit." in message


def test_nvidia_upstream_detail_redacts_secrets(monkeypatch):
    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "nvapi-supersecretvalue123")

    detail = llm._safe_upstream_detail(
        _openai_status_error(
            APIStatusError,
            400,
            "Error code: 400",
            body={
                "detail": (
                    "rejected key nvapi-supersecretvalue123 "
                    "with Authorization: Bearer nvapi-supersecretvalue123"
                )
            },
        )
    )

    assert "supersecretvalue123" not in detail
    assert "<redacted>" in detail
    assert detail.startswith("rejected key")


def test_nvidia_upstream_detail_truncates():
    detail = llm._safe_upstream_detail(
        _openai_status_error(
            APIStatusError,
            400,
            body={"detail": "x" * 5000},
        ),
        limit=120,
    )

    assert len(detail) <= 123
    assert detail.endswith("...")


def test_nvidia_upstream_detail_missing_body_is_empty():
    error = _openai_status_error(APIStatusError, 400, body=None)
    error.message = ""
    assert llm._safe_upstream_detail(error) == ""


# ============================================================
# STEP 4B-1: BLAST-RADIUS HARDENING
# ============================================================
# These tests stand in for openai.OpenAI so the real SDK never opens a
# socket. They drive the production _get_client() path, which is what
# makes the constructor arguments and the call count trustworthy.
# The credential below is a throwaway literal; no real key is used.


class _StubCompletions:
    """Counts create() calls so amplification is directly observable."""

    def __init__(self, content=None, error=None):
        self.calls = 0
        self._content = content
        self._error = error

    def create(self, **kwargs):
        self.calls += 1

        if self._error is not None:
            raise self._error

        return _FakeCompletionsResponse(self._content)


class _StubOpenAI:
    """Replacement for openai.OpenAI, capturing constructor kwargs."""

    def __init__(self, content=None, error=None, **kwargs):
        self.kwargs = kwargs
        self.chat = self
        self.completions = _StubCompletions(
            content=content, error=error
        )


def _provider_using_stub(monkeypatch, content=None, error=None):
    """Install a stubbed OpenAI and return (provider, stub)."""

    stub = {}

    def factory(**kwargs):
        stub["client"] = _StubOpenAI(
            content=content, error=error, **kwargs
        )
        return stub["client"]

    monkeypatch.setattr(openai, "OpenAI", factory)
    monkeypatch.setattr(
        llm.time, "sleep", lambda seconds: None
    )

    provider = _fake_nvidia_provider()
    provider._get_client()

    return provider, stub["client"]


# ------------------------------------------------------------
# A + B. THE SDK DEFAULTS ARE OVERRIDDEN EXPLICITLY
# ------------------------------------------------------------

def test_sdk_retries_are_disabled(monkeypatch):
    _, client = _provider_using_stub(monkeypatch)

    # The SDK default is 2. Left implicit it would multiply with the
    # application retry loop.
    assert client.kwargs["max_retries"] == 0


def test_explicit_timeout_reaches_the_client(monkeypatch):
    _, client = _provider_using_stub(monkeypatch)

    assert client.kwargs["timeout"] == config.LLM_TIMEOUT_SECONDS


def test_timeout_is_not_the_sdk_default(monkeypatch):
    """Guard against the 600 second SDK default coming back."""

    _, client = _provider_using_stub(monkeypatch)

    # openai's DEFAULT_TIMEOUT uses a 600 second read timeout.
    assert client.kwargs["timeout"] < 600


def test_configured_timeout_override_is_honoured(monkeypatch):
    monkeypatch.setattr(llm, "LLM_TIMEOUT_SECONDS", 12.5)
    _, client = _provider_using_stub(monkeypatch)

    assert client.kwargs["timeout"] == 12.5


# ------------------------------------------------------------
# C. RETRY AMPLIFICATION IS BOUNDED TO 1 + LLM_RETRIES
# ------------------------------------------------------------

def test_transient_503_is_retried_exactly_llm_retries_plus_one(
    monkeypatch,
):
    monkeypatch.setattr(llm, "LLM_RETRIES", 2)

    provider, client = _provider_using_stub(
        monkeypatch,
        error=_openai_status_error(
            InternalServerError, 503, "upstream unavailable"
        ),
    )

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    # Exactly 1 + LLM_RETRIES. It must never be 9, which is what the
    # hidden SDK retry layer used to produce.
    assert client.completions.calls == 3
    assert client.completions.calls != 9


def test_amplification_stays_bounded_at_high_retry_counts(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 4)

    provider, client = _provider_using_stub(
        monkeypatch,
        error=_openai_connection_error(),
    )

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert client.completions.calls == 5


# ------------------------------------------------------------
# D. AN EXHAUSTED QUOTA IS NEVER RETRIED
# ------------------------------------------------------------

def test_429_is_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 2)

    provider, client = _provider_using_stub(
        monkeypatch,
        error=_openai_status_error(
            RateLimitError, 429, "rate limit hit"
        ),
    )

    with pytest.raises(ValueError) as excinfo:
        provider.generate("show products", [])

    # One call only: retrying an exhausted quota cannot help and
    # multiplies load against the limit.
    assert client.completions.calls == 1
    assert "rate limit or quota exceeded" in str(
        excinfo.value
    )


def test_quota_classification_covers_status_code_and_class(
    monkeypatch,
):
    assert llm._is_quota_exhausted(
        _openai_status_error(RateLimitError, 429, "slow down")
    ) is True
    assert llm._is_transient_error(
        _openai_status_error(RateLimitError, 429, "slow down")
    ) is False


def test_5xx_remains_transient(monkeypatch):
    """Retry behaviour for genuine transient failures is preserved."""

    for status in (500, 502, 503, 504):
        error = _openai_status_error(
            APIStatusError, status, "upstream unavailable"
        )
        assert llm._is_transient_error(error) is True, status

    assert llm._is_transient_error(
        _openai_connection_error()
    ) is True


# ------------------------------------------------------------
# E + F + G. EXISTING BEHAVIOUR IS INTACT
# ------------------------------------------------------------

def test_successful_generation_still_works_through_real_client_path(
    monkeypatch,
):
    monkeypatch.setattr(llm, "LLM_RETRIES", 2)

    provider, client = _provider_using_stub(
        monkeypatch,
        content=(
            '```json\n{"sql": "SELECT * FROM products", '
            '"explanation": "All products."}\n```'
        ),
    )

    sql, explanation = provider.generate(
        "Show me products", []
    )

    assert sql == "SELECT * FROM products"
    assert explanation == "All products."
    assert client.completions.calls == 1
    assert client.kwargs["max_retries"] == 0


def test_malformed_response_is_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 2)

    provider, client = _provider_using_stub(
        monkeypatch, content="{"
    )

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    # A parse failure is deterministic, so retrying only wastes quota.
    assert client.completions.calls == 1


def test_non_transient_400_is_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 2)

    provider, client = _provider_using_stub(
        monkeypatch,
        error=_openai_status_error(
            APIStatusError, 400, "bad request"
        ),
    )

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert client.completions.calls == 1


# ------------------------------------------------------------
# 5. BACKOFF STAYS BOUNDED
# ------------------------------------------------------------

def test_backoff_is_bounded_for_any_retry_count(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRY_BACKOFF_CAP_SECONDS", 5)

    waits = [llm._backoff_seconds(n) for n in range(1, 50)]

    assert waits[:3] == [1.5, 3.0, 4.5]
    assert max(waits) <= 5
    assert all(wait <= 5 for wait in waits)


def test_no_real_sleep_happens_in_a_bounded_retry(monkeypatch):
    monkeypatch.setattr(llm, "LLM_RETRIES", 2)

    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)

    provider = _fake_nvidia_provider()
    provider._client = _StubOpenAI(
        error=_openai_status_error(
            InternalServerError, 503, "unavailable"
        )
    )

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    # Bounded: at most one wait between each of the two retries.
    assert len(slept) == 2
    assert sum(slept) <= (
        2 * config.LLM_RETRY_BACKOFF_CAP_SECONDS
    )


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

def test_llm_timeout_has_a_safe_configured_value():
    assert isinstance(config.LLM_TIMEOUT_SECONDS, int)
    assert config.LLM_TIMEOUT_SECONDS > 0
    # Comfortably below the SDK's 600 second default, and bounded
    # enough to keep a synchronous handler from tying up a thread.
    assert config.LLM_TIMEOUT_SECONDS <= 300


def test_llm_timeout_falls_back_when_malformed(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "not-a-number")
    assert config._positive_int_env(
        "LLM_TIMEOUT_SECONDS", 60
    ) == 60

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "-5")
    assert config._positive_int_env(
        "LLM_TIMEOUT_SECONDS", 60
    ) == 60
