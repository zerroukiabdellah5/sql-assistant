import httpx2
import pytest

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

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
    assert explanation


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
    sql, explanation = llm.parse_gemini_payload(
        '{"sql": "SELECT 3 AS y"}'
    )
    assert sql == "SELECT 3 AS y"
    assert explanation


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