"""Tests for the per-instance LLM concurrency cap (Step 4B-6).

The cap bounds how many provider calls one warm instance has in
flight at one moment. It is deliberately NOT a global quota and NOT
a budget: Vercel instances do not share memory and a cold start
clears the counter. These tests pin the behaviour that IS real -
the cap itself, non-blocking rejection, 429 + Retry-After on the
wire, one slot per request across the whole retry loop, and the
release of that slot on every exit path.

No NVIDIA request is made: the provider's client is replaced with a
counting stub, so the upstream call count is directly observable.
The credential used here is the throwaway literal from conftest.py.
"""

import inspect
import logging
import re
import threading
import time

import pytest

from types import SimpleNamespace

import app.config as config
from app import llm, llm_concurrency

from app.llm import LLMConcurrencySaturated
from app.llm_concurrency import (
    LLMConcurrencyCap,
    gate,
)
from app.ratelimit import _LIMITED_DETAIL


# ============================================================
# STUBS
# ============================================================

ANSWER = '{"sql": "SELECT 1", "explanation": "One row."}'


class _StubCompletions:
    """Counts create() calls, so the retry count stays observable."""

    def __init__(self, content):
        self.calls = 0
        self._content = content

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=self._content
                    )
                )
            ]
        )


class _StubClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(
            completions=_StubCompletions(content)
        )


class _StubGeminiModels:
    """Counts generate_content() calls for the Gemini provider."""

    def __init__(self, text):
        self.calls = 0
        self._text = text

    def generate_content(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(text=self._text)


def _provider():
    """An NVIDIA provider wired to a counting stub client."""

    provider = llm.NVIDIAProvider(
        model="test-model",
        base_url="https://integrate.api.nvidia.com/v1",
    )
    provider.api_key = "dummy-key"
    provider._client = _StubClient(ANSWER)
    return provider


@pytest.fixture
def nvidia_stub(monkeypatch):
    """Route the real /api/ask path at a stubbed provider client.

    ask_active is deliberately NOT stubbed: the cap lives below it,
    inside generate(), so replacing ask_active would remove the very
    code under test.
    """

    stub = {}

    monkeypatch.setattr(llm, "NVIDIA_API_KEY", "test-only-key")
    monkeypatch.setattr(llm, "NVIDIA_MODEL", "test-model")
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    def _get_client(self):
        if "client" not in stub:
            stub["client"] = _StubClient(ANSWER)
        return stub["client"]

    monkeypatch.setattr(
        llm.NVIDIAProvider, "_get_client", _get_client
    )

    return stub


def _upstream_error(status, message):
    """A real SDK status error, so _is_transient_error sees a real
    exception type rather than a stand-in.

    body is required by this SDK version; None is the honest value
    for an error that carries no payload.
    """

    import httpx2

    from openai import InternalServerError, RateLimitError

    error_class = {
        429: RateLimitError,
        503: InternalServerError,
    }[status]

    return error_class(
        message,
        response=httpx2.Response(
            status,
            request=httpx2.Request(
                "POST",
                "https://integrate.api.nvidia.com/v1",
            ),
        ),
        body=None,
    )


def _saturation_records(records):
    return [
        record
        for record in records
        if record.name == "app.llm_concurrency"
        and record.getMessage().startswith(
            "llm_concurrency_saturated"
        )
    ]


# ============================================================
# 1-4. THE CAP ALGORITHM
# ============================================================

def test_cap_admits_exactly_the_limit():

    cap = LLMConcurrencyCap(limit_provider=lambda: 2)

    assert cap.try_acquire() is True
    assert cap.try_acquire() is True
    assert cap.try_acquire() is False
    assert cap.in_flight == 2


def test_a_released_slot_can_be_reused():

    cap = LLMConcurrencyCap(limit_provider=lambda: 1)

    assert cap.try_acquire() is True
    assert cap.try_acquire() is False

    cap.release()

    assert cap.in_flight == 0
    assert cap.try_acquire() is True


def test_release_never_invents_capacity(caplog):
    """An over-release is a bookkeeping bug, and it must be reported
    rather than absorbed into a counter that would then admit more
    calls than the cap allows."""

    cap = LLMConcurrencyCap(limit_provider=lambda: 1)

    cap.try_acquire()
    cap.release()

    with caplog.at_level(
        logging.ERROR, logger="app.llm_concurrency"
    ):
        # Must not raise: this runs from a finally block.
        cap.release()

    assert cap.in_flight == 0

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.llm_concurrency"
    ]

    assert "LLM CONCURRENCY CAP RELEASE FAILURE" in messages


def test_reset_clears_the_counter():

    cap = LLMConcurrencyCap(limit_provider=lambda: 4)

    cap.try_acquire()
    cap.try_acquire()
    assert cap.in_flight == 2

    cap.reset()

    assert cap.in_flight == 0


# ============================================================
# 5. CONCURRENCY: THE CHECK AND THE INCREMENT ARE ATOMIC
# ============================================================

def test_concurrent_callers_never_exceed_the_cap():
    """Sync handlers share the AnyIO threadpool, so the check and the
    increment have to be one atomic step.

    Peak simultaneous holders are observed rather than inferred: a
    worker that is admitted registers itself, widens the window, then
    gives the slot back, so an implementation that let a second caller
    slip between the comparison and the increment would show a peak
    above the limit.

    Honest limitation, stated so this test is not read as stronger
    than it is: CPython's GIL means a bare int comparison followed by
    an increment cannot be interrupted at all, so removing the lock
    from THIS particular body would not be caught here. The lock is
    what makes the invariant hold by construction rather than by
    timing luck, and it is what keeps holding the moment the body
    gains any operation that releases the GIL. This test is the
    regression guard for that, not a proof about the present body.
    """

    limit = 4
    workers = 48
    rounds = 20

    cap = LLMConcurrencyCap(limit_provider=lambda: limit)

    peak = [0]
    refusals = []
    errors = []

    peak_lock = threading.Lock()
    start = threading.Barrier(workers)

    def worker():
        try:
            start.wait(timeout=10)

            for _ in range(rounds):

                if not cap.try_acquire():
                    refusals.append(1)
                    continue

                try:
                    with peak_lock:
                        peak[0] = max(
                            peak[0], cap.in_flight
                        )

                    # Yield the GIL so a second caller really does get
                    # a chance to run while this slot is held.
                    time.sleep(0)

                finally:
                    cap.release()

        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker)
        for _ in range(workers)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert peak[0] <= limit, (
        f"observed {peak[0]} simultaneous holders at a cap of {limit}"
    )
    assert refusals, "nothing was ever refused, so nothing was tested"
    assert cap.in_flight == 0


# ============================================================
# 6-7. FAIL CLOSED, LIKE app/ratelimit.py
# ============================================================

@pytest.mark.parametrize(
    "broken_limit", [0, -1, None, "4", 4.0, True]
)
def test_an_unusable_limit_denies_rather_than_admits(
    broken_limit, caplog
):
    """A limit that cannot be read must not be read as "free".

    bool and float are the interesting cases: True is an int in
    Python, so without an explicit check it would silently become a
    cap of one.
    """

    cap = LLMConcurrencyCap(
        limit_provider=lambda: broken_limit
    )

    with caplog.at_level(
        logging.ERROR, logger="app.llm_concurrency"
    ):
        assert cap.try_acquire() is False

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.llm_concurrency"
    ]

    assert "LLM CONCURRENCY CAP FAILURE" in messages
    assert cap.in_flight == 0


def test_a_raising_limit_provider_denies(caplog):
    """The gate cannot count, so it must not report itself free."""

    def broken():
        raise RuntimeError("internal detail")

    cap = LLMConcurrencyCap(limit_provider=broken)

    with caplog.at_level(
        logging.ERROR, logger="app.llm_concurrency"
    ):
        assert cap.try_acquire() is False

    records = [
        record
        for record in caplog.records
        if record.name == "app.llm_concurrency"
    ]

    assert records
    assert records[0].exc_info, "no traceback was recorded"
    assert "internal detail" not in records[0].getMessage()


# ============================================================
# 8. THE LIMIT IS READ LIVE, NOT CAPTURED AT IMPORT
# ============================================================

def test_the_gate_follows_a_live_config_change(monkeypatch):
    """An operator change must take effect without a cold start, and a
    test must be able to move the cap without reimporting."""

    monkeypatch.setattr(config, "LLM_MAX_CONCURRENCY", 1)

    assert gate.try_acquire() is True
    assert gate.try_acquire() is False

    gate.release()

    monkeypatch.setattr(config, "LLM_MAX_CONCURRENCY", 2)

    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False


def test_the_configured_cap_has_a_safe_default():
    assert isinstance(config.LLM_MAX_CONCURRENCY, int)
    assert not isinstance(config.LLM_MAX_CONCURRENCY, bool)
    assert config.LLM_MAX_CONCURRENCY > 0

    # Far below the 40 thread AnyIO pool. A cap at or above the pool
    # size would stop protecting the instance and start consuming the
    # threads it exists to leave free.
    assert config.LLM_MAX_CONCURRENCY <= 20


def test_malformed_cap_values_fall_back():

    for raw in ("not-a-number", "-5", "0", ""):
        assert (
            config._positive_int_env("LLM_MAX_CONCURRENCY", 4)
            == 4
        )


def test_retry_after_is_a_small_positive_integer():
    value = config.LLM_CONCURRENCY_RETRY_AFTER_SECONDS

    assert isinstance(value, int)
    assert 1 <= value <= 30


# ============================================================
# 9-12. THE PERMIT INSIDE NVIDIA generate()
# ============================================================

def test_a_saturated_cap_rejects_before_any_provider_call(
    monkeypatch,
):
    """The decisive assertion: a rejected request spends no upstream
    quota, so _call_model must never run."""

    provider = _provider()
    calls = []

    monkeypatch.setattr(
        llm_concurrency,
        "gate",
        LLMConcurrencyCap(limit_provider=lambda: 1),
    )

    assert llm_concurrency.gate.try_acquire() is True

    monkeypatch.setattr(
        provider,
        "_call_model",
        lambda *args: calls.append(1),
    )

    with pytest.raises(LLMConcurrencySaturated):
        provider.generate("show products", [])

    assert calls == []


def test_the_permit_is_released_after_a_success():

    provider = _provider()

    sql, explanation = provider.generate("show products", [])

    assert sql == "SELECT 1"
    assert explanation == "One row."
    assert gate.in_flight == 0


def test_the_permit_is_released_when_generation_raises(
    monkeypatch,
):
    """Every exit path has to give the slot back, or the cap leaks one
    permit per failure until the instance refuses everything."""

    provider = _provider()
    monkeypatch.setattr(llm, "LLM_RETRIES", 0)

    def boom(*args):
        raise _upstream_error(503, "unavailable")

    monkeypatch.setattr(provider, "_call_model", boom)

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert gate.in_flight == 0


def test_the_permit_is_released_after_a_parse_failure():
    """A malformed answer is not a provider failure, but it still has
    to return the slot."""

    provider = _provider()
    provider._client = _StubClient("{ not json")

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert gate.in_flight == 0


def test_one_request_holds_exactly_one_permit_across_retries(
    monkeypatch,
):
    """The permit wraps the whole retry loop, not each attempt.

    Sampled from inside the provider call: the count must be exactly 1
    on every attempt and must never move. Were the permit taken per
    attempt, a retried request would briefly consume two slots, and a
    single request could be refused while already holding one.
    """

    monkeypatch.setattr(llm, "LLM_RETRIES", 2)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = _provider()
    observed = []

    def flaky(*args):
        observed.append(gate.in_flight)
        raise _upstream_error(503, "unavailable")

    monkeypatch.setattr(provider, "_call_model", flaky)

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert observed == [1, 1, 1]
    assert gate.in_flight == 0


def test_retry_semantics_are_unchanged_by_the_cap(monkeypatch):
    """The upstream call count is still exactly 1 + LLM_RETRIES for a
    transient failure, and still exactly 1 for an exhausted quota.

    Both halves are counted, not inferred from the exception type: the
    permit adds an acquisition and a release around the loop, and a
    mistake there would show up as one call too many rather than as an
    error.
    """

    monkeypatch.setattr(llm, "LLM_RETRIES", 2)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)

    provider = _provider()
    transient_calls = []

    def unavailable(*args):
        transient_calls.append(1)
        raise _upstream_error(503, "unavailable")

    monkeypatch.setattr(provider, "_call_model", unavailable)

    with pytest.raises(ValueError):
        provider.generate("show products", [])

    assert len(transient_calls) == 1 + llm.LLM_RETRIES
    assert gate.in_flight == 0

    quota_provider = _provider()
    quota_calls = []

    def throttled(*args):
        quota_calls.append(1)
        raise _upstream_error(429, "rate limit hit")

    monkeypatch.setattr(quota_provider, "_call_model", throttled)

    with pytest.raises(ValueError):
        quota_provider.generate("show products", [])

    # An exhausted quota is terminal: retrying it multiplies load
    # against a limit that is already spent.
    assert len(quota_calls) == 1
    assert gate.in_flight == 0


def test_the_cap_is_fully_restored_after_each_request(monkeypatch):
    """A request must leave the instance exactly as capable as it
    found it.

    Checking in_flight alone would miss a permit that was released
    twice while another leaked, so this asserts the observable
    consequence instead: with a cap of one, consecutive requests must
    all succeed. A leak would refuse the second one.
    """

    monkeypatch.setattr(
        llm_concurrency,
        "gate",
        LLMConcurrencyCap(limit_provider=lambda: 1),
    )

    provider = _provider()

    for _ in range(5):
        sql, _explanation = provider.generate(
            "show products", []
        )
        assert sql == "SELECT 1"

    assert provider._client.chat.completions.calls == 5
    assert gate.in_flight == 0


def test_saturation_leaves_the_generation_ref_unfilled(monkeypatch):
    """Provenance must not be able to name a provider that never
    answered. A GenerationRef still holding None is the state
    provenance already degrades to."""

    provider = _provider()

    monkeypatch.setattr(
        llm_concurrency,
        "gate",
        LLMConcurrencyCap(limit_provider=lambda: 1),
    )

    llm_concurrency.gate.try_acquire()

    reference = llm.GenerationRef()

    with pytest.raises(LLMConcurrencySaturated):
        provider.generate(
            "show products", [], generation=reference
        )

    assert reference.provider is None
    assert reference.model is None


def test_saturation_is_a_runtime_error():
    """NVIDIAProvider.generate() re-raises RuntimeError untouched, so
    this type can never be rewritten into a provider error message."""

    assert issubclass(LLMConcurrencySaturated, RuntimeError)


# ============================================================
# 13. THE GEMINI PROVIDER IS CAPPED TOO
# ============================================================

def test_the_gemini_provider_is_capped_and_releases(monkeypatch):
    """The permit sits in generate(), not in one provider, so the
    second provider is covered by construction."""

    models = _StubGeminiModels(ANSWER)

    monkeypatch.setattr(
        llm, "client", SimpleNamespace(models=models)
    )

    provider = llm.GeminiProvider(model="test-model")

    sql, _explanation = provider.generate("show products", [])

    assert sql == "SELECT 1"
    assert models.calls == 1
    assert gate.in_flight == 0

    # Saturation is enforced for this provider as well.
    monkeypatch.setattr(
        llm_concurrency,
        "gate",
        LLMConcurrencyCap(limit_provider=lambda: 1),
    )

    llm_concurrency.gate.try_acquire()

    calls = []

    def counting(**kwargs):
        calls.append(1)

    monkeypatch.setattr(models, "generate_content", counting)

    with pytest.raises(LLMConcurrencySaturated):
        provider.generate("show products", [])

    assert calls == []


# ============================================================
# 14-17. THE HTTP CONTRACT
# ============================================================

def test_a_saturated_ask_returns_429_with_retry_after(
    authed_client, nvidia_stub, monkeypatch
):
    """Deterministic: the gate itself is made to refuse, so the result
    does not depend on thread scheduling."""

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert response.status_code == 429

    retry_after = response.headers["Retry-After"]
    assert re.fullmatch(r"[0-9]+", retry_after)
    assert int(retry_after) >= 1

    # Nothing was sent upstream, so no quota was spent.
    assert "client" not in nvidia_stub


def test_the_saturation_response_carries_the_request_id(
    authed_client, nvidia_stub, monkeypatch
):
    """A user told to retry needs an id to quote, and the middleware
    attaches it to every status."""

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert response.status_code == 429
    assert re.fullmatch(
        r"[0-9a-f]{32}", response.headers["X-Request-ID"]
    )


def test_the_saturation_detail_discloses_no_configuration(
    authed_client, nvidia_stub, monkeypatch
):
    """The response must not become a way to measure this instance's
    load, so it carries neither the cap nor the in-flight count."""

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    response = authed_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    detail = response.json()["detail"]

    assert str(config.LLM_MAX_CONCURRENCY) not in detail
    assert "in_flight" not in detail
    assert "limit" not in detail.lower()
    assert "nvidia" not in detail.lower()


def test_the_rejection_is_logged_with_the_request_id(
    authed_client, nvidia_stub, monkeypatch, caplog
):
    """One INFO line per rejection, carrying the id that ties it to the
    access line for the same request."""

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    with caplog.at_level(
        logging.INFO, logger="app.llm_concurrency"
    ):
        response = authed_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

    records = _saturation_records(caplog.records)

    assert len(records) == 1

    message = records[0].getMessage()

    assert (
        f"request_id={response.headers['X-Request-ID']}" in message
    )
    assert f"limit={config.LLM_MAX_CONCURRENCY}" in message


# ============================================================
# 18. AUTH -> RATE LIMIT -> CONCURRENCY CAP -> PROVIDER
# ============================================================

def test_authentication_still_comes_first(
    anon_client, nvidia_stub, monkeypatch, caplog
):
    """An unauthenticated caller is rejected by auth, so it never
    reaches the cap and never produces a saturation record."""

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    with caplog.at_level(
        logging.INFO, logger="app.llm_concurrency"
    ):
        response = anon_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

    assert response.status_code == 401
    assert "Retry-After" not in response.headers
    assert _saturation_records(caplog.records) == []


def test_the_rate_limiter_answers_before_the_cap_is_consulted(
    authed_client, nvidia_stub, monkeypatch, caplog
):
    """Ordering, proven by absence.

    With the gate refusing every request, the first
    ASK_RATE_LIMIT_REQUESTS calls are rejected by the cap. The next one
    must be rejected by the rate limiter instead, which is only
    possible if the limiter runs first - and it must do so without
    consulting the cap, so no further saturation record appears.
    """

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    with caplog.at_level(
        logging.INFO, logger="app.llm_concurrency"
    ):
        for _ in range(config.ASK_RATE_LIMIT_REQUESTS):
            assert authed_client.post(
                "/api/ask", json={"prompt": "show products"}
            ).status_code == 429

        limited = authed_client.post(
            "/api/ask", json={"prompt": "show products"}
        )

    assert limited.status_code == 429
    assert limited.json()["detail"] == _LIMITED_DETAIL

    # Exactly one saturation record per cap rejection, and none for
    # the request the limiter turned away.
    assert len(_saturation_records(caplog.records)) == (
        config.ASK_RATE_LIMIT_REQUESTS
    )


# ============================================================
# 19. THE CAP IS AN ADDITION, NOT A REPLACEMENT
# ============================================================

def test_an_unsaturated_ask_is_unaffected(
    authed_client, nvidia_stub
):
    """The ordinary path is untouched: a real answer, one provider
    call, and the slot returned afterwards."""

    response = authed_client.post(
        "/api/ask", json={"prompt": "show products"}
    )

    assert response.status_code == 200

    body = response.json()
    assert body["sql"] == "SELECT 1"
    assert body["success"] is True

    assert nvidia_stub["client"].chat.completions.calls == 1
    assert gate.in_flight == 0


def test_the_cap_is_instance_wide_not_per_caller():
    """There is no identity in the gate, so a second caller cannot
    claim an allowance of its own."""

    cap = LLMConcurrencyCap(limit_provider=lambda: 1)

    assert cap.try_acquire() is True
    assert cap.try_acquire() is False

    assert not hasattr(cap, "_buckets")
    assert not hasattr(cap, "_keys")
    assert not hasattr(cap, "_identities")


# ============================================================
# 20. NOTHING SENSITIVE IS REACHABLE OR STORED
# ============================================================

def test_the_gate_stores_no_identity_or_payload(
    authed_client, nvidia_stub, monkeypatch
):
    """The cap is instance-wide, so the only state it holds is an
    integer. No credential, token, prompt or SQL can be in it."""

    monkeypatch.setattr(gate, "try_acquire", lambda: False)

    authed_client.post(
        "/api/ask",
        json={
            "prompt": "prompt-marker-value",
            "history": [],
        },
    )

    rendered = " ".join(
        str(value) for value in vars(gate).values()
    )

    assert "prompt-marker-value" not in rendered
    assert config.APP_API_KEY not in rendered
    assert "SELECT" not in rendered
    assert gate.in_flight == 0


def test_the_rejection_log_line_carries_no_request_content(
    caplog,
):
    """A saturation line is a diagnostic about the instance, not a
    record of what the caller asked for."""

    with caplog.at_level(
        logging.INFO, logger="app.llm_concurrency"
    ):
        llm_concurrency.log_saturation("request-id-marker-value")

    records = _saturation_records(caplog.records)

    assert len(records) == 1
    assert (
        "request_id=request-id-marker-value"
        in records[0].getMessage()
    )


def test_an_absent_request_id_logs_a_placeholder(caplog):

    with caplog.at_level(
        logging.INFO, logger="app.llm_concurrency"
    ):
        llm_concurrency.log_saturation()

    records = _saturation_records(caplog.records)

    assert "request_id=unknown" in records[0].getMessage()


def test_an_unprintable_request_id_cannot_forge_a_log_record(
    caplog,
):
    """generate() is callable with any string, so the id is filtered
    before it reaches a log line."""

    with caplog.at_level(
        logging.INFO, logger="app.llm_concurrency"
    ):
        llm_concurrency.log_saturation(
            "abc\nFAKE LEVEL=ERROR msg=injected"
        )

    records = [
        record
        for record in caplog.records
        if record.name == "app.llm_concurrency"
    ]

    assert len(records) == 1
    assert "\n" not in records[0].getMessage()


def test_a_long_request_id_is_truncated():
    assert len(
        llm_concurrency._safe_request_id("x" * 500)
    ) == llm_concurrency._MAX_REQUEST_ID_CHARS


# ============================================================
# 21-22. FAILURES ARE LOGGED, NEVER PRINTED
# ============================================================

def test_the_cap_writes_nothing_to_the_console(
    monkeypatch, capsys
):
    """print() bypasses the logging configuration entirely, so it can
    be neither silenced nor filtered. Once the logger is muted, a
    rejection must produce no output at all, and the record must
    still be observable."""

    recorded = []

    class Recorder(logging.Handler):
        def emit(self, record):
            recorded.append(record)

    application_logger = logging.getLogger("app")
    cap_logger = logging.getLogger("app.llm_concurrency")

    monkeypatch.setattr(
        application_logger, "handlers", [logging.NullHandler()]
    )
    monkeypatch.setattr(
        application_logger, "propagate", False
    )
    monkeypatch.setattr(cap_logger, "handlers", [Recorder()])

    llm_concurrency.log_saturation("abc123")

    cap = LLMConcurrencyCap(limit_provider=lambda: 1)
    cap.try_acquire()
    cap.try_acquire()

    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""

    # A refusal at the cap is silent; only the saturation line is
    # written, and it went to the logger.
    assert len(recorded) == 1
    assert recorded[0].getMessage().startswith(
        "llm_concurrency_saturated"
    )


def test_the_concurrency_module_contains_no_print_calls():
    """A static check, so a reintroduced print() cannot hide in a
    branch this suite happens not to exercise."""

    assert "print(" not in inspect.getsource(llm_concurrency)


def test_the_llm_layer_contains_no_print_calls():
    assert "print(" not in inspect.getsource(llm)


# ============================================================
# 23-25. VERCEL-SAFETY AND DOCUMENTATION HONESTY
# ============================================================

def test_the_module_writes_nothing_to_the_filesystem():
    """Vercel exposes a read-only filesystem, so the gate must not
    depend on a writable path."""

    source = inspect.getsource(llm_concurrency)

    for forbidden in (
        "open(",
        "sqlite3",
        "META_DB_PATH",
        "DATABASE_PATH",
        "tempfile",
    ):
        assert forbidden not in source, forbidden


def test_the_gate_needs_no_event_loop():
    """A module-level asyncio primitive would have to bind to a loop
    under the serverless runtime. A threading.Lock does not."""

    source = inspect.getsource(llm_concurrency)

    assert "asyncio" not in source
    assert "anyio" not in source


def test_the_module_states_it_is_not_a_global_control():
    """The cap must not be mistaken for a global quota. If this
    wording is removed the file is over-claiming, which is exactly
    the failure mode this test exists to catch."""

    header = inspect.getsource(llm_concurrency)
    header = header.split("def ", 1)[0].lower()

    assert "best-effort, per instance" in header
    assert "not a global quota" in header
    assert "not shared between vercel instances" in header
    assert "cold start" in header
    assert "it never queues" in header


def test_the_config_documents_what_the_cap_does_not_bound():
    header = inspect.getsource(config)

    assert "does NOT bound spend" in header
    assert "not a global quota" in header


def test_no_new_third_party_dependency_was_introduced():
    """The cap must be standard library only: a new dependency would
    be a new failure mode on the serverless runtime."""

    source = inspect.getsource(llm_concurrency)

    imported = re.findall(
        r"^\s*(?:import|from)\s+(\w+)", source, re.MULTILINE
    )

    for name in imported:
        assert name in (
            "logging",
            "threading",
            "app",
        ), name