"""Tests for the centralized LLM factory (Phase 2).

Retry/backoff/timeout/rate-limit behavior is exercised by stubbing the underlying
CrewAI transport call, so no provider is contacted and no credential is needed.
"""

import logging
from dataclasses import replace

import pytest
from crewai.llms.base_llm import BaseLLM

import crewai.llm as crewai_llm_module
from crypto_trader.llm import (
    CANARY_PROFILE_NAME,
    LLMConfigError,
    LLMProviderError,
    ProviderRateLimiter,
    ResilientLLM,
    get_llm,
    get_provider_rate_limiter,
    load_profile,
    reset_factory_state,
    set_default_builder,
)
from crypto_trader.llm.factory import compute_backoff_delay, is_transient_provider_error

SENTINEL_KEY = "sentinel-not-a-real-key"
ENV_WITH_KEY = {"DEEPSEEK_API_KEY": SENTINEL_KEY}


class TransientServerError(Exception):
    status_code = 503


class RateLimitExceeded(Exception):
    status_code = 429


class AuthenticationError(Exception):
    """Name matches LiteLLM's non-retryable authentication failure."""

    status_code = 401


class FakeLLM(BaseLLM):
    """Minimal stand-in used to prove dependency injection works."""

    def __init__(self, model: str = "fake/model", tag: str = "fake"):
        super().__init__(model=model)
        self.tag = tag
        self.calls: list = []

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None):
        self.calls.append(messages)
        return "fake-response"


@pytest.fixture(autouse=True)
def _clean_factory_state():
    reset_factory_state()
    yield
    reset_factory_state()


def _profile(**overrides):
    return replace(load_profile(CANARY_PROFILE_NAME, env={}), **overrides)


def _stub_transport(monkeypatch, outcomes):
    """Replace CrewAI's transport call with a scripted sequence of outcomes."""
    calls = {"count": 0}

    def fake_call(self, messages, tools=None, callbacks=None, available_functions=None,
                  from_task=None, from_agent=None):
        index = calls["count"]
        calls["count"] += 1
        outcome = outcomes[min(index, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(crewai_llm_module.LLM, "call", fake_call)
    return calls


def _resilient_llm(monkeypatch, outcomes, **profile_overrides):
    calls = _stub_transport(monkeypatch, outcomes)
    sleeps: list[float] = []
    profile = _profile(requests_per_minute=None, min_seconds_between_requests=0.0, **profile_overrides)
    llm = ResilientLLM(
        profile=profile,
        api_key=SENTINEL_KEY,
        rate_limiter=ProviderRateLimiter(),
        sleeper=sleeps.append,
    )
    return llm, calls, sleeps


# --- Construction and dependency injection ---------------------------------

def test_get_llm_builds_resilient_llm_from_profile():
    llm = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY)
    profile = load_profile(CANARY_PROFILE_NAME, env={})

    assert isinstance(llm, ResilientLLM)
    assert isinstance(llm, BaseLLM)
    assert llm.model == profile.model
    assert llm.base_url == profile.base_url
    assert llm.timeout == profile.timeout_seconds
    assert llm.api_key == SENTINEL_KEY
    assert llm.profile.name == CANARY_PROFILE_NAME


def test_get_llm_accepts_a_profile_object():
    profile = _profile(model="deepseek/deepseek-reasoner", temperature=None)

    llm = get_llm(profile, env=ENV_WITH_KEY)

    assert llm.model == "deepseek/deepseek-reasoner"
    assert llm.profile.provider == "deepseek"


def test_get_llm_applies_overrides():
    llm = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY, overrides={"max_tokens": 400, "timeout_seconds": 12})

    assert llm.max_tokens == 400
    assert llm.timeout == 12


def test_get_llm_rejects_unknown_override():
    with pytest.raises(LLMConfigError) as excinfo:
        get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY, overrides={"not_a_field": 1})

    assert "not_a_field" in str(excinfo.value)


def test_get_llm_missing_api_key_fails_before_construction():
    with pytest.raises(LLMConfigError) as excinfo:
        get_llm(CANARY_PROFILE_NAME, env={})

    assert "DEEPSEEK_API_KEY" in str(excinfo.value)
    assert SENTINEL_KEY not in str(excinfo.value)


def test_get_llm_uses_injected_builder_for_dependency_injection():
    recorded = {}
    fake = FakeLLM()

    def builder(profile, api_key):
        recorded["profile"] = profile
        recorded["api_key"] = api_key
        return fake

    result = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY, builder=builder)

    assert result is fake
    assert recorded["profile"].name == CANARY_PROFILE_NAME
    assert recorded["profile"].model == "deepseek/deepseek-chat"
    assert recorded["api_key"] == SENTINEL_KEY


def test_default_builder_can_be_swapped_and_reset():
    fake = FakeLLM()
    set_default_builder(lambda profile, api_key: fake)

    assert get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY) is fake

    reset_factory_state()
    assert isinstance(get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY), ResilientLLM)


def test_builder_must_return_a_base_llm():
    with pytest.raises(LLMConfigError):
        get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY, builder=lambda profile, api_key: object())


# --- Provider-level rate limiting ------------------------------------------

def test_rate_limiter_is_shared_per_provider_and_not_per_agent():
    first = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY)
    second = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY)

    assert first is not second
    assert first.rate_limiter is second.rate_limiter

    reset_factory_state()
    third = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY)
    assert third.rate_limiter is not first.rate_limiter


def test_rate_limiter_enforces_minimum_interval():
    now = {"t": 0.0}
    slept: list[float] = []
    limiter = ProviderRateLimiter(
        min_seconds_between_requests=2.0,
        clock=lambda: now["t"],
        sleeper=lambda seconds: (slept.append(seconds), now.__setitem__("t", now["t"] + seconds)),
    )

    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 2.0
    assert slept == [2.0]


def test_rate_limiter_enforces_requests_per_minute_cap():
    now = {"t": 0.0}
    slept: list[float] = []
    limiter = ProviderRateLimiter(
        requests_per_minute=2,
        clock=lambda: now["t"],
        sleeper=lambda seconds: (slept.append(seconds), now.__setitem__("t", now["t"] + seconds)),
    )

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # would be the third request inside the same minute

    assert slept == [60.0]


# --- Retry, backoff, timeout, typed errors ---------------------------------

def test_transient_error_is_retried_then_succeeds(monkeypatch):
    llm, calls, sleeps = _resilient_llm(monkeypatch, [TransientServerError("boom"), "OK"])

    assert llm.call("prompt") == "OK"
    assert calls["count"] == 2
    assert sleeps == [1.0]
    assert llm.last_call_metadata["attempts"] == 2
    assert llm.last_call_metadata["retries"] == 1
    assert llm.last_call_metadata["success"] is True


def test_rate_limit_error_is_retried(monkeypatch):
    llm, calls, sleeps = _resilient_llm(monkeypatch, [RateLimitExceeded(), "OK"])

    assert llm.call("prompt") == "OK"
    assert calls["count"] == 2
    assert sleeps == [1.0]


def test_retries_are_bounded_and_backoff_is_exponential_capped(monkeypatch):
    llm, calls, sleeps = _resilient_llm(
        monkeypatch,
        [TransientServerError("always down")],
        max_attempts=4,
        backoff_base_seconds=1.0,
        backoff_max_seconds=2.0,
    )

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    error = excinfo.value
    assert error.attempts == 4
    assert error.transient is True
    assert calls["count"] == 4  # never unbounded
    assert sleeps == [1.0, 2.0, 2.0]
    assert llm.last_call_metadata["success"] is False
    assert llm.last_call_metadata["attempts"] == 4


def test_non_transient_error_is_not_retried(monkeypatch):
    llm, calls, sleeps = _resilient_llm(monkeypatch, [AuthenticationError("bad key")])

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    assert calls["count"] == 1
    assert sleeps == []
    assert excinfo.value.transient is False
    assert excinfo.value.cause_type == "AuthenticationError"


def test_timeout_is_treated_as_transient_and_surfaces_typed_error(monkeypatch):
    llm, calls, sleeps = _resilient_llm(monkeypatch, [TimeoutError("timed out")], max_attempts=2)

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    assert calls["count"] == 2
    assert sleeps == [1.0]
    assert excinfo.value.transient is True
    assert excinfo.value.cause_type == "TimeoutError"
    assert "deepseek" in str(excinfo.value)


def test_provider_error_never_contains_the_api_key(monkeypatch, caplog):
    llm, _, _ = _resilient_llm(monkeypatch, [TransientServerError("down")], max_attempts=1)

    with caplog.at_level(logging.DEBUG, logger="crypto_trader.llm"):
        with pytest.raises(LLMProviderError) as excinfo:
            llm.call("prompt")

    assert SENTINEL_KEY not in str(excinfo.value)
    assert SENTINEL_KEY not in caplog.text


def test_config_timeout_is_used_by_the_llm():
    llm = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY, overrides={"timeout_seconds": 7})
    assert llm.timeout == 7


# --- Shared-state hygiene --------------------------------------------------

def test_instances_do_not_share_conversation_state():
    first = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY)
    second = get_llm(CANARY_PROFILE_NAME, env=ENV_WITH_KEY)

    first.last_call_metadata["marker"] = "first"

    assert first.last_call_metadata is not second.last_call_metadata
    assert "marker" not in second.last_call_metadata
    assert first.rate_limiter is second.rate_limiter  # only throttle state is shared


# --- Classification helpers ------------------------------------------------

@pytest.mark.parametrize(
    "error,expected",
    [
        (TransientServerError(), True),
        (RateLimitExceeded(), True),
        (TimeoutError(), True),
        (ConnectionError(), True),
        (OSError(), True),
        (AuthenticationError(), False),
        (ValueError("programming error"), False),
    ],
)
def test_error_classification(error, expected):
    assert is_transient_provider_error(error) is expected


def test_backoff_helper_is_exponential_and_capped():
    assert compute_backoff_delay(1, 1.0, 8.0) == 1.0
    assert compute_backoff_delay(2, 1.0, 8.0) == 2.0
    assert compute_backoff_delay(3, 1.0, 8.0) == 4.0
    assert compute_backoff_delay(4, 1.0, 8.0) == 8.0
    assert compute_backoff_delay(9, 1.0, 8.0) == 8.0


def test_get_provider_rate_limiter_is_stable_per_configuration():
    profile = _profile(requests_per_minute=30)

    assert get_provider_rate_limiter(profile) is get_provider_rate_limiter(profile)
    assert get_provider_rate_limiter(profile).requests_per_minute == 30
