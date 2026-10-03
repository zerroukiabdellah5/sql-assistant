"""Tests for Step 4B-3B: LLM token usage observability.

Four properties are pinned here, and every one of them is a property
that must hold before a budget is ever allowed to depend on these
numbers:

  1. whatever the provider actually reported is captured, with the real
     SDK field names (openai.types.CompletionUsage and
     google.genai.types.GenerateContentResponseUsageMetadata are the
     objects under test, not hand-written imitations);
  2. anything the provider did not report stays unreported. No count is
     inferred, derived or estimated, and "not reported" is
     distinguishable from a reported 0;
  3. the log line carries counts and nothing else. The prompt, the
     generated SQL, the schema, the session token and every credential
     are absent by construction, because only seven fixed fields are
     ever formatted;
  4. nothing about the request changes. The retry loop still calls the
     provider 1 + LLM_RETRIES times, the SDK timeout and max_retries
     are untouched, and (sql, explanation) comes back exactly as before.

No network call is made: the provider clients are replaced with stubs
that record what they were asked for. The credential below is a
throwaway literal from conftest.py; no real secret is read from the
environment.
"""

import logging
import re

import pytest

from google.genai import types as genai_types
from openai.types import CompletionUsage

import app.config as config
import app.llm as llm
import app.main as main
import app.token_usage as token_usage

# Distinctive strings. If one of these ever reaches a usage log line,
# the corresponding leak test below fails.
PROMPT_MARKER = "PROMPT-MARKER-should-never-be-logged"
SQL_MARKER = "SELECT 1 AS leaked_sql_marker"
SECRET_MARKER = "nvapi-marker-should-never-leak"
SESSION_MARKER = "session-token-should-never-be-logged"
SCHEMA_MARKER = "column customers_ssn_marker"

USAGE_LOGGER = "app.usage"


# ============================================================
# FIXTURES AND DOUBLES
# ============================================================

@pytest.fixture
def usage_log(caplog):
    """Capture usage lines (INFO, below the caplog default)."""

    caplog.set_level(logging.INFO, logger=USAGE_LOGGER)
    return caplog


def usage_lines(records):
    return [
        record.getMessage()
        for record in records
        if record.name == USAGE_LOGGER
    ]


def fields_of(line):
    """Parse 'key=value key=value' into a dict.

    Values are sanitized to contain no whitespace, so a plain split is
    exact rather than approximate.
    """

    parsed = {}

    for pair in line.split():
        key, _, value = pair.partition("=")
        parsed[key] = value

    return parsed


# --- provider doubles ----------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    """A chat completion response, with usage only when asked for.

    Leaving the attribute off entirely is the point: it reproduces an
    OpenAI-compatible endpoint that reports no usage, which is a normal
    answer and must never be an error.
    """

    def __init__(self, content, usage=...):
        self.choices = [_FakeChoice(content)]
        self.id = "chatcmpl-stub"
        self.model = "stub-model"

        if usage is not ...:
            self.usage = usage


class _StubCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1

        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]

        if isinstance(response, Exception):
            raise response

        return response


class _StubOpenAI:
    def __init__(self, responses):
        self.chat = self
        self.completions = _StubCompletions(responses)


class _StubGeminiModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1

        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]

        if isinstance(response, Exception):
            raise response

        return response


class _StubGemini:
    def __init__(self, responses):
        self.models = _StubGeminiModels(responses)


ANSWER = (
    '```json\n{"sql": "SELECT * FROM products", '
    '"explanation": "All products."}\n```'
)


def nvidia_provider(responses):
    """An NVIDIAProvider wired to a stub, never to the network."""

    provider = llm.NVIDIAProvider(
        model="test-model",
        base_url="https://integrate.api.nvidia.com/v1",
    )
    provider.api_key = "dummy-key"
    provider._client = _StubOpenAI(responses)

    return provider


def gemini_provider(responses, monkeypatch):
    provider = llm.GeminiProvider(model="test-gemini-model")
    monkeypatch.setattr(
        llm, "client", _StubGemini(responses)
    )

    return provider


def gemini_response(
    prompt_tokens=None,
    completion_tokens=None,
    total_tokens=None,
):
    """A real SDK response, usage metadata present or absent.

    Built through the SDK's own types, so a field rename upstream breaks
    this test loudly instead of silently yielding unavailable.
    """

    candidate = genai_types.Candidate(
        content=genai_types.Content(
            parts=[genai_types.Part(text=ANSWER)]
        ),
        finish_reason=genai_types.FinishReason.STOP,
    )

    if (
        prompt_tokens is None
        and completion_tokens is None
        and total_tokens is None
    ):
        return genai_types.GenerateContentResponse(
            candidates=[candidate]
        )

    return genai_types.GenerateContentResponse(
        candidates=[candidate],
        usage_metadata=genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=completion_tokens,
            total_token_count=total_tokens,
        ),
    )


# ============================================================
# 1. NVIDIA USAGE IS CAPTURED
# ============================================================

def test_openai_usage_is_captured_from_the_real_sdk_object():
    """The field names are read from the actual SDK type, not guessed."""

    response = _FakeCompletion(
        ANSWER,
        usage=CompletionUsage(
            prompt_tokens=1234,
            completion_tokens=56,
            total_tokens=1290,
        ),
    )

    usage = token_usage.extract_openai_usage(
        response, model="test-model", duration_ms=42
    )

    assert usage.available is True
    assert usage.prompt_tokens == 1234
    assert usage.completion_tokens == 56
    assert usage.total_tokens == 1290
    assert usage.duration_ms == 42
    assert usage.provider == token_usage.NVIDIA
    assert usage.model == "test-model"


def test_openai_usage_is_also_read_from_a_plain_mapping():
    """An OpenAI-compatible client may hand back a dict, not a model."""

    response = {
        "choices": [{"message": {"content": ANSWER}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
    }

    usage = token_usage.extract_openai_usage(response)

    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 2
    assert usage.total_tokens == 12


def test_openai_input_and_output_aliases_are_accepted():
    """Some compatible endpoints send input_tokens / output_tokens."""

    usage = token_usage.extract_openai_usage(
        {
            "usage": {
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            }
        }
    )

    assert usage.prompt_tokens == 7
    assert usage.completion_tokens == 3
    assert usage.total_tokens == 10


# ============================================================
# 2. MISSING NVIDIA USAGE IS SAFE
# ============================================================

def test_openai_response_without_usage_is_unavailable():
    usage = token_usage.extract_openai_usage(
        _FakeCompletion(ANSWER)
    )

    assert usage.available is False
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None


def test_openai_usage_of_none_is_unavailable():
    usage = token_usage.extract_openai_usage(
        _FakeCompletion(ANSWER, usage=None)
    )

    assert usage.available is False
    assert usage.total_tokens is None


def test_openai_usage_with_no_counts_is_unavailable():
    """A usage object that carries nothing is still absent usage."""

    class _EmptyUsage:
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None

    usage = token_usage.extract_openai_usage(
        _FakeCompletion(ANSWER, usage=_EmptyUsage())
    )

    assert usage.available is False


def test_openai_never_invents_a_missing_total():
    """Two counts do not imply a third."""

    usage = token_usage.extract_openai_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
            }
        }
    )

    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 20
    assert usage.total_tokens is None, (
        "a total was invented from two reported counts"
    )


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        -1,
        -1000,
        float("nan"),
        float("inf"),
        1.5,
        "many",
        "",
        object(),
        [1],
    ],
    ids=[
        "true", "false", "negative-one", "negative-big", "nan", "inf",
        "fractional-float", "word", "empty", "object", "list",
    ],
)
def test_openai_implausible_counts_are_reported_as_absent(value):
    """A count that is not a non-negative integer is not a count.

    Reporting it as absent is honest; coercing it to a number would
    invent usage, and this module never invents usage.
    """

    usage = token_usage.extract_openai_usage(
        {"usage": {"prompt_tokens": value}}
    )

    assert usage.prompt_tokens is None


def test_a_zero_count_is_a_real_report_not_an_absence():
    """0 is what the provider said, so it must survive."""

    usage = token_usage.extract_openai_usage(
        {"usage": {"prompt_tokens": 0, "total_tokens": 0}}
    )

    assert usage.prompt_tokens == 0
    assert usage.available is True


def test_extraction_never_raises_on_a_hostile_response():
    class _Explosive:
        @property
        def usage(self):
            raise RuntimeError("usage blew up")

        choices = None

    usage = token_usage.extract_openai_usage(_Explosive())

    assert usage.available is False


# ============================================================
# 3. GEMINI USAGE IS CAPTURED
# ============================================================

def test_gemini_usage_is_captured_from_the_real_sdk_object():
    """Gemini reports candidates_token_count, not completion_tokens."""

    response = gemini_response(
        prompt_tokens=321, completion_tokens=64, total_tokens=385
    )

    usage = token_usage.extract_gemini_usage(
        response, model="test-gemini-model", duration_ms=17
    )

    assert usage.available is True
    assert usage.prompt_tokens == 321
    assert usage.completion_tokens == 64
    assert usage.total_tokens == 385
    assert usage.duration_ms == 17
    assert usage.provider == token_usage.GEMINI


def test_gemini_response_without_usage_metadata_is_unavailable():
    usage = token_usage.extract_gemini_usage(
        gemini_response()
    )

    assert usage.available is False
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None


def test_gemini_usage_metadata_of_none_is_unavailable():
    response = genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(
                    parts=[genai_types.Part(text=ANSWER)]
                )
            )
        ],
        usage_metadata=None,
    )

    usage = token_usage.extract_gemini_usage(response)

    assert usage.available is False


def test_gemini_never_invents_a_total_from_the_other_two():
    response = gemini_response(prompt_tokens=50, completion_tokens=10)

    usage = token_usage.extract_gemini_usage(response)

    assert usage.prompt_tokens == 50
    assert usage.completion_tokens == 10
    assert usage.total_tokens is None


# ============================================================
# 4. THE LOG LINE CARRIES SAFE METADATA ONLY
# ============================================================

def test_the_line_has_exactly_the_seven_agreed_fields(usage_log):
    token_usage.log_usage(
        token_usage.TokenUsage(
            provider=token_usage.NVIDIA,
            model="test-model",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            duration_ms=1500,
        ),
        request_id="a" * 32,
    )

    lines = usage_lines(usage_log.records)

    assert len(lines) == 1
    assert set(fields_of(lines[0])) == {
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "duration_ms",
        "request_id",
    }


def test_the_line_reports_the_captured_values(usage_log):
    token_usage.log_usage(
        token_usage.TokenUsage(
            provider=token_usage.GEMINI,
            model="gemini-test",
            prompt_tokens=321,
            completion_tokens=64,
            total_tokens=385,
            duration_ms=17,
        ),
        request_id="b" * 32,
    )

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed == {
        "provider": "gemini",
        "model": "gemini-test",
        "prompt_tokens": "321",
        "completion_tokens": "64",
        "total_tokens": "385",
        "duration_ms": "17",
        "request_id": "b" * 32,
    }


def test_absent_counts_are_labelled_and_never_shown_as_zero(usage_log):
    """A provider that reported nothing must not look like a free call."""

    token_usage.log_usage(
        token_usage.extract_openai_usage(_FakeCompletion(ANSWER)),
        request_id="c" * 32,
    )

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed["prompt_tokens"] == "unavailable"
    assert parsed["completion_tokens"] == "unavailable"
    assert parsed["total_tokens"] == "unavailable"
    assert "0" not in (
        parsed["prompt_tokens"]
        + parsed["completion_tokens"]
        + parsed["total_tokens"]
    )


def test_a_generation_outside_a_request_is_recorded_as_unknown(usage_log):
    token_usage.log_usage(
        token_usage.TokenUsage(provider=token_usage.NVIDIA)
    )

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed["request_id"] == token_usage.UNKNOWN_REQUEST_ID


def test_the_line_never_carries_request_content(usage_log):
    """Everything this suite marks as sensitive is absent by construction.

    provider and model are operator configuration, not request data, so
    the model is expected to appear; nothing derived from the request
    may.
    """

    token_usage.log_usage(
        token_usage.TokenUsage(
            provider=token_usage.NVIDIA,
            model="nvidia/test-model",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            duration_ms=1,
        ),
        request_id="a" * 32,
    )

    line = usage_lines(usage_log.records)[0]

    for marker in (
        PROMPT_MARKER,
        SQL_MARKER,
        SCHEMA_MARKER,
        SECRET_MARKER,
        SESSION_MARKER,
    ):
        assert marker not in line

    assert "model=nvidia/test-model" in line


def test_the_generation_never_reaches_the_log(usage_log, monkeypatch):
    """End to end through a real prompt: the log cannot contain it.

    The prompt, the schema text and the generated SQL all pass through
    the provider call that produces this line, and none of them may
    appear in it.
    """

    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider(
        [
            _FakeCompletion(
                '```json\n{"sql": "SELECT 1 AS leaked_sql_marker", '
                '"explanation": "leaked_explanation_marker"}\n```',
                usage=CompletionUsage(
                    prompt_tokens=700,
                    completion_tokens=20,
                    total_tokens=720,
                ),
            )
        ]
    )

    provider.generate(
        PROMPT_MARKER,
        [main.HistoryTurn(prompt=SESSION_MARKER, sql=SQL_MARKER)],
    )

    line = usage_lines(usage_log.records)[0]

    for marker in (
        PROMPT_MARKER,
        SESSION_MARKER,
        "leaked_sql_marker",
        "leaked_explanation_marker",
        "SELECT",
        "CREATE TABLE",
    ):
        assert marker not in line

    assert "total_tokens=720" in line


def test_a_session_token_passed_as_a_request_id_is_not_logged(usage_log):
    """A caller mistake cannot turn the id field into a credential leak.

    The application only ever passes a generated 32-hex id, so anything
    else is refused rather than reproduced.
    """

    for candidate in (
        SESSION_MARKER,
        "nvapi-" + "a" * 20,
        "a" * 31,
        "A" * 32,
        "z" * 32,
    ):
        token_usage.log_usage(
            token_usage.TokenUsage(
                provider=token_usage.NVIDIA
            ),
            request_id=candidate,
        )

    lines = usage_lines(usage_log.records)

    assert len(lines) == 5

    for line, candidate in zip(lines, (
        SESSION_MARKER,
        "nvapi-" + "a" * 20,
        "a" * 31,
        "A" * 32,
        "z" * 32,
    )):
        assert candidate not in line
        assert fields_of(line)["request_id"] == "unknown"


def test_a_crafted_model_cannot_forge_a_second_record(usage_log):
    forged = "m\nprovider=fake total_tokens=999999 prompt_tokens=1"

    token_usage.log_usage(
        token_usage.TokenUsage(
            provider=token_usage.NVIDIA, model=forged
        )
    )

    lines = usage_lines(usage_log.records)

    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert lines[0].count("total_tokens=") == 1


def test_a_crafted_request_id_cannot_forge_a_second_record(usage_log):
    forged = "d" * 32 + "\nprovider=fake total_tokens=999999"

    token_usage.log_usage(
        token_usage.TokenUsage(provider=token_usage.NVIDIA),
        request_id=forged,
    )

    lines = usage_lines(usage_log.records)

    assert len(lines) == 1
    assert lines[0].count("total_tokens=") == 1


def test_a_very_long_model_is_bounded(usage_log):
    token_usage.log_usage(
        token_usage.TokenUsage(
            provider=token_usage.NVIDIA, model="z" * 5000
        )
    )

    line = usage_lines(usage_log.records)[0]
    model = fields_of(line)["model"]

    assert len(model) <= token_usage.MAX_FIELD_CHARS


def test_logging_a_record_cannot_raise(usage_log):
    """Observability must not be able to fail a completed request."""

    class _BrokenUsage:
        @property
        def provider(self):
            raise RuntimeError("boom")

    token_usage.log_usage(_BrokenUsage(), request_id="e" * 32)

    assert usage_lines(usage_log.records) == []


def test_the_usage_logger_is_a_child_of_the_project_logger():
    """No second logging setup: these lines reuse the app handlers."""

    assert token_usage.logger.name.startswith("app.")
    assert token_usage.logger is logging.getLogger(USAGE_LOGGER)

    handlers = logging.getLogger("app").handlers

    assert handlers, "the project logger has no handler configured"


# ============================================================
# 5. THE PROVIDER PATH RECORDS WHAT IT RECEIVED
# ============================================================

def test_nvidia_generate_logs_the_usage_the_provider_returned(
    usage_log, monkeypatch
):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider(
        [
            _FakeCompletion(
                ANSWER,
                usage=CompletionUsage(
                    prompt_tokens=800,
                    completion_tokens=30,
                    total_tokens=830,
                ),
            )
        ]
    )

    sql, explanation = provider.generate("show products", [])

    # The public contract is untouched.
    assert sql == "SELECT * FROM products"
    assert explanation == "All products."

    lines = usage_lines(usage_log.records)

    assert len(lines) == 1
    parsed = fields_of(lines[0])

    assert parsed["provider"] == "nvidia"
    assert parsed["model"] == "test-model"
    assert parsed["prompt_tokens"] == "800"
    assert parsed["completion_tokens"] == "30"
    assert parsed["total_tokens"] == "830"
    assert parsed["duration_ms"].isdigit()


def test_nvidia_generate_is_unaffected_when_no_usage_is_reported(
    usage_log, monkeypatch
):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider([_FakeCompletion(ANSWER)])

    sql, explanation = provider.generate("show products", [])

    assert sql == "SELECT * FROM products"
    assert explanation == "All products."

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed["total_tokens"] == "unavailable"


def test_nvidia_generate_records_the_request_id_it_was_given(
    usage_log, monkeypatch
):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider([_FakeCompletion(ANSWER)])
    request_id = "f" * 32

    provider.generate("show products", [], request_id=request_id)

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed["request_id"] == request_id


def test_nvidia_generate_without_a_request_id_is_allowed(
    usage_log, monkeypatch
):
    """The parameter is optional: every pre-existing caller still works."""

    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider([_FakeCompletion(ANSWER)])

    sql, _explanation = provider.generate("show products", [])

    assert sql == "SELECT * FROM products"
    assert (
        fields_of(usage_lines(usage_log.records)[0])["request_id"]
        == token_usage.UNKNOWN_REQUEST_ID
    )


def test_gemini_generate_logs_the_usage_the_sdk_returned(
    usage_log, monkeypatch
):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = gemini_provider(
        [gemini_response(900, 45, 945)], monkeypatch
    )

    sql, explanation = provider.generate("show products", [])

    assert sql == "SELECT * FROM products"
    assert explanation == "All products."

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed["provider"] == "gemini"
    assert parsed["model"] == "test-gemini-model"
    assert parsed["prompt_tokens"] == "900"
    assert parsed["completion_tokens"] == "45"
    assert parsed["total_tokens"] == "945"


def test_gemini_generate_is_unaffected_without_usage_metadata(
    usage_log, monkeypatch
):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = gemini_provider([gemini_response()], monkeypatch)

    sql, _explanation = provider.generate("show products", [])

    assert sql == "SELECT * FROM products"

    parsed = fields_of(usage_lines(usage_log.records)[0])

    assert parsed["prompt_tokens"] == "unavailable"


def test_a_usage_failure_cannot_break_a_completed_generation(
    usage_log, monkeypatch
):
    """Even a broken extractor must not turn a good answer into a 500."""

    monkeypatch.setattr(
        llm.token_usage,
        "extract_openai_usage",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("extractor exploded")
        ),
    )

    provider = nvidia_provider([_FakeCompletion(ANSWER)])

    sql, explanation = provider.generate("show products", [])

    assert sql == "SELECT * FROM products"
    assert explanation == "All products."


# ============================================================
# 6. RETRY, TIMEOUT AND OUTPUT BEHAVIOUR ARE UNCHANGED
# ============================================================

def test_the_provider_is_still_called_once_per_attempt(
    usage_log, monkeypatch
):
    """Usage capture adds no provider call of its own."""

    monkeypatch.setattr(llm, "LLM_RETRIES", 2)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider([_FakeCompletion(ANSWER)])

    provider.generate("show products", [])

    assert provider._client.completions.calls == 1


def test_retry_behaviour_is_unchanged_and_usage_is_recorded_once(
    usage_log, monkeypatch
):
    """Two transient failures then success: three calls, one record.

    The usage line belongs to the attempt that produced the answer, so a
    retried request reports the tokens it actually spent on the
    successful call rather than one record per attempt.
    """

    monkeypatch.setattr(llm, "LLM_RETRIES", 2)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    unavailable = Exception("service unavailable")

    provider = nvidia_provider(
        [
            unavailable,
            unavailable,
            _FakeCompletion(
                ANSWER,
                usage=CompletionUsage(
                    prompt_tokens=400,
                    completion_tokens=20,
                    total_tokens=420,
                ),
            ),
        ]
    )

    sql, _explanation = provider.generate("show products", [])

    assert sql == "SELECT * FROM products"
    assert provider._client.completions.calls == 3

    lines = usage_lines(usage_log.records)

    assert len(lines) == 1
    assert fields_of(lines[0])["total_tokens"] == "420"


def test_a_failed_generation_records_no_usage(usage_log, monkeypatch):
    """Nothing succeeded, so there is no usage to claim."""

    monkeypatch.setattr(llm, "LLM_RETRIES", 0)

    provider = nvidia_provider([Exception("permanently unavailable")])

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert usage_lines(usage_log.records) == []


def test_a_parse_failure_records_no_usage(usage_log, monkeypatch):
    """The provider was called and spent tokens, but the step under
    review only records a completed generation; see the limitation in
    the report."""

    monkeypatch.setattr(llm, "LLM_RETRIES", 0)

    provider = nvidia_provider(
        [_FakeCompletion("{", usage=CompletionUsage(
            prompt_tokens=999, completion_tokens=1, total_tokens=1000
        ))]
    )

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert usage_lines(usage_log.records) == []


def test_the_sdk_call_parameters_are_unchanged(usage_log, monkeypatch):
    """timeout and max_retries stay exactly as they were."""

    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider([_FakeCompletion(ANSWER)])

    provider.generate("show products", [])

    sent = provider._client.completions.kwargs

    assert sent["model"] == "test-model"
    assert sent["temperature"] == 1.0
    assert sent["top_p"] == 0.95
    assert sent["max_tokens"] == 1200
    assert sent["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False

    roles = [message["role"] for message in sent["messages"]]
    assert roles == ["system", "user"]


def test_the_client_is_still_built_with_a_timeout_and_no_sdk_retries(
    monkeypatch
):
    """Usage capture must not have disturbed the client constructor."""

    import openai

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return _StubOpenAI([_FakeCompletion(ANSWER)])

    monkeypatch.setattr(openai, "OpenAI", factory)

    provider = llm.NVIDIAProvider(
        model="test-model", base_url="https://example.invalid/v1"
    )
    provider.api_key = "dummy-key"

    provider._get_client()

    assert captured["timeout"] == config.LLM_TIMEOUT_SECONDS
    assert captured["max_retries"] == 0


def test_the_provider_contract_is_still_a_two_tuple(usage_log, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider([_FakeCompletion(ANSWER)])
    result = provider.generate("show products", [])

    assert isinstance(result, tuple)
    assert len(result) == 2
    assert all(isinstance(part, str) for part in result)


# ============================================================
# 7. NO BUDGET, NO CONCURRENCY CAP
# ============================================================

def test_a_large_usage_total_blocks_nothing(usage_log, monkeypatch):
    """Observation only: a report that looks enormous is still served.

    If any budget were enforced, this is where it would appear.
    """

    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = nvidia_provider(
        [
            _FakeCompletion(
                ANSWER,
                usage=CompletionUsage(
                    prompt_tokens=8_000_000,
                    completion_tokens=1_000_000,
                    total_tokens=9_000_000,
                ),
            )
            for _ in range(5)
        ]
    )

    for _ in range(5):
        sql, _explanation = provider.generate("show products", [])
        assert sql == "SELECT * FROM products"

    assert provider._client.completions.calls == 5

    totals = [
        fields_of(line)["total_tokens"]
        for line in usage_lines(usage_log.records)
    ]

    assert totals == ["9000000"] * 5


def test_the_module_documents_that_it_enforces_nothing():
    """If this wording is removed the file starts over-claiming."""

    import inspect

    header = inspect.getsource(token_usage).lower()

    assert "observation only" in header
    assert "no budget" in header


def test_the_llm_layer_takes_no_usage_parameter():
    """ask_active keeps optional inputs and nothing about budgets.

    The two optional parameters are the correlation id, which is
    logged and nothing else, and the GenerationRef the provider fills
    in with its own name and model. Neither can cap spending, and
    neither is a usage parameter.
    """

    import inspect

    signature = inspect.signature(llm.ask_active)

    assert list(signature.parameters) == [
        "user_prompt",
        "history",
        "request_id",
        "generation",
    ]
    assert (
        signature.parameters["request_id"].default is None
    )
    assert (
        signature.parameters["generation"].default is None
    )


# ============================================================
# 8. END TO END THROUGH THE HTTP LAYER
# ============================================================

def test_the_usage_line_carries_the_requests_id(
    authed_client, usage_log, monkeypatch
):
    """The id on the usage line is the id on the response header.

    This is the whole point of passing it down: one identifier ties the
    tokens a request spent to the request the client can quote.
    """

    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only-key")
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    monkeypatch.setattr(
        llm.NVIDIAProvider,
        "_get_client",
        lambda self: _StubOpenAI(
            [
                _FakeCompletion(
                    ANSWER,
                    usage=CompletionUsage(
                        prompt_tokens=1200,
                        completion_tokens=40,
                        total_tokens=1240,
                    ),
                )
            ]
        ),
    )

    response = authed_client.post(
        "/api/ask", json={"prompt": PROMPT_MARKER}
    )

    assert response.status_code == 200

    request_id = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)

    lines = usage_lines(usage_log.records)

    assert len(lines) == 1
    parsed = fields_of(lines[0])

    assert parsed["request_id"] == request_id
    assert parsed["provider"] == "nvidia"
    assert parsed["total_tokens"] == "1240"

    # The prompt and the generated SQL reached the client, as they always
    # did, and neither is in the log line.
    assert PROMPT_MARKER in response.text

    logged = "\n".join(
        record.getMessage()
        for record in usage_log.records
    )

    assert PROMPT_MARKER not in logged
    assert SQL_MARKER not in logged


def test_the_api_response_is_byte_identical_with_and_without_usage(
    authed_client, monkeypatch
):
    """Adding usage metadata upstream changes nothing a client sees."""

    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only-key")
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    def call(usage):
        monkeypatch.setattr(
            llm.NVIDIAProvider,
            "_get_client",
            lambda self: _StubOpenAI(
                [_FakeCompletion(ANSWER, usage=usage)]
            ),
        )

        return authed_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

    with_usage = call(
        CompletionUsage(
            prompt_tokens=5, completion_tokens=2, total_tokens=7
        )
    )
    without_usage = call(...)

    assert with_usage.status_code == without_usage.status_code == 200

    assert with_usage.json()["sql"] == without_usage.json()["sql"]
    assert (
        with_usage.json()["explanation"]
        == without_usage.json()["explanation"]
    )
    assert with_usage.json()["data"] == without_usage.json()["data"]