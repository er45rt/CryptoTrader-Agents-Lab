"""Phase 2 agent #2 migration tests: Comprehensive Crypto Analyst.

Offline only. The factory is monkeypatched for crew-wiring assertions, and the
provider transport is stubbed for the error-handling assertion.
"""

from pathlib import Path

import pytest
import yaml
from crewai.llms.base_llm import BaseLLM

import crewai.llm as crewai_llm_module
import crypto_trader.crew as crew_module
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.llm import (
    ACTIVE_PROFILE_NAMES,
    ANALYST_PROFILE_NAME,
    CANARY_PROFILE_NAME,
    LLMProviderError,
    ProviderRateLimiter,
    ResilientLLM,
    get_llm,
    load_profile,
)
from crypto_trader.tools.binance_public_tool import BinancePublicTool

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
CONFIG_DIR = PACKAGE_DIR / "config"

LEGACY_AGENT_MODEL = "openai/qwen-turbo-latest"
LEGACY_AGENTS = ("binance_data_analyst", "news_and_sentiment_researcher")
MIGRATED_AGENTS = ("trading_plan_strategist", "crypto_analyst")
SENTINEL_KEY = "sentinel-not-a-real-key"


class FakeLLM(BaseLLM):
    def __init__(self, model: str = "fake/deepseek"):
        super().__init__(model=model)

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None):
        return "fake"


class ProviderFatal(Exception):
    """Non-retryable provider failure stand-in."""


@pytest.fixture
def crew_with_fake_factory(monkeypatch):
    recorded: list[str] = []
    fake = FakeLLM()

    def fake_get_llm(profile, **kwargs):
        recorded.append(profile)
        return fake

    monkeypatch.setattr(crew_module, "get_llm", fake_get_llm)
    return CryptoTraderCrew(verbose=False), recorded, fake


# --- Exactly two agents migrated -------------------------------------------

def test_exactly_two_agents_use_the_centralized_factory(crew_with_fake_factory):
    crew, recorded, _ = crew_with_fake_factory

    for builder in (
        crew.binance_data_analyst,
        crew.news_and_sentiment_researcher,
        crew.crypto_analyst,
        crew.trading_plan_strategist,
    ):
        builder()

    assert sorted(recorded) == sorted([CANARY_PROFILE_NAME, ANALYST_PROFILE_NAME])
    assert len(recorded) == len(ACTIVE_PROFILE_NAMES) == 2


def test_analyst_is_wired_through_the_factory(crew_with_fake_factory):
    crew, recorded, fake = crew_with_fake_factory

    analyst = crew.crypto_analyst()

    assert analyst.llm is fake  # injected by the factory
    assert ANALYST_PROFILE_NAME in recorded
    assert ANALYST_PROFILE_NAME == "deepseek_analyst"


def test_strategist_remains_wired_through_the_factory(crew_with_fake_factory):
    crew, recorded, fake = crew_with_fake_factory

    assert crew.trading_plan_strategist().llm is fake
    assert CANARY_PROFILE_NAME in recorded


def test_di_fake_llm_override_still_works_for_analyst():
    fake = FakeLLM()
    recorded = {}

    def builder(profile, api_key):
        recorded["profile"] = profile
        recorded["api_key"] = api_key
        return fake

    result = get_llm(ANALYST_PROFILE_NAME, env={"DEEPSEEK_API_KEY": SENTINEL_KEY}, builder=builder)

    assert result is fake
    assert recorded["profile"].name == ANALYST_PROFILE_NAME
    assert recorded["api_key"] == SENTINEL_KEY


# --- Profile configuration -------------------------------------------------

def test_analyst_profile_targets_deepseek_chat():
    profile = load_profile(ANALYST_PROFILE_NAME, env={})

    assert profile.provider == "deepseek"
    assert profile.model == "deepseek/deepseek-chat"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.api_key_env == "DEEPSEEK_API_KEY"
    assert profile.timeout_seconds > 0
    assert profile.max_attempts >= 2


def test_analyst_does_not_activate_the_reasoner_profile():
    profile = load_profile(ANALYST_PROFILE_NAME, env={})

    assert profile.model != "deepseek/deepseek-reasoner"
    assert "deepseek_reasoner" not in ACTIVE_PROFILE_NAMES


def test_analyst_and_strategist_share_one_provider_rate_limiter():
    env = {"DEEPSEEK_API_KEY": SENTINEL_KEY}

    analyst_llm = get_llm(ANALYST_PROFILE_NAME, env=env)
    strategist_llm = get_llm(CANARY_PROFILE_NAME, env=env)

    assert isinstance(analyst_llm, ResilientLLM)
    assert analyst_llm.profile.name != strategist_llm.profile.name
    assert analyst_llm.rate_limiter is strategist_llm.rate_limiter  # provider-level, not per agent


# --- Legacy agents unchanged ----------------------------------------------

def test_legacy_agents_keep_their_previous_configuration(crew_with_fake_factory):
    crew, _, _ = crew_with_fake_factory

    for name in LEGACY_AGENTS:
        agent = getattr(crew, name)()
        assert agent.llm.model == LEGACY_AGENT_MODEL, f"{name} must stay on the legacy model"


def test_agents_yaml_hardcodes_only_the_unmigrated_agents():
    agents = yaml.safe_load((CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8"))

    for name in MIGRATED_AGENTS:
        assert "llm" not in agents[name], f"{name} must not hardcode a provider/model"
    for name in LEGACY_AGENTS:
        assert agents[name]["llm"] == LEGACY_AGENT_MODEL


def test_crew_source_contains_no_provider_branching_or_reasoner_use():
    source = (PACKAGE_DIR / "crew.py").read_text(encoding="utf-8")

    for forbidden in ("deepseek", "openai/", "deepseek_reasoner", "if provider"):
        assert forbidden not in source, f"provider detail leaked into crew.py: {forbidden}"


# --- Structured-output boundary -------------------------------------------

def test_analyst_task_remains_free_form_without_a_schema_guardrail(crew_with_fake_factory):
    """The analyst's Markdown report is consumed as LLM context, not machine-parsed."""
    crew, _, _ = crew_with_fake_factory

    analyst_task = crew.analyze_crypto_data_task()

    assert analyst_task.guardrail is None
    assert "Markdown" in analyst_task.expected_output


def test_analyst_output_flows_to_the_strategist_as_text_context(crew_with_fake_factory):
    crew, _, _ = crew_with_fake_factory

    analyst_task = crew.analyze_crypto_data_task()
    strategist_task = crew.develop_trading_plan_task()

    assert [task.id for task in strategist_task.context] == [analyst_task.id]
    # The analyst itself consumes the four upstream market/news tasks.
    assert len(analyst_task.context) == 4


# --- Error handling hardening still applies -------------------------------

def test_provider_error_hardening_applies_to_the_analyst_llm(monkeypatch):
    original = ProviderFatal("provider exploded \u4f60\u597d")

    def transport(self, messages, **kwargs):
        raise original

    monkeypatch.setattr(crewai_llm_module.LLM, "call", transport)
    llm = ResilientLLM(
        profile=load_profile(ANALYST_PROFILE_NAME, env={}),
        api_key=SENTINEL_KEY,
        rate_limiter=ProviderRateLimiter(),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    assert excinfo.value.__cause__ is original  # original provider error preserved
    assert excinfo.value.cause_type == "ProviderFatal"
    assert str(excinfo.value).isascii()


# --- Phase 1 / scope boundaries -------------------------------------------

def test_binance_stays_public_and_read_only():
    tool = BinancePublicTool()

    assert tool.base_url == "https://api.binance.com"
    assert tool.kline_endpoint_path == "/api/v3/klines"
    assert tool.price_ticker_endpoint_path == "/api/v3/ticker/price"
    assert tool.ticker_24hr_endpoint_path == "/api/v3/ticker/24hr"
    assert not hasattr(tool, "rapidapi_key")


def test_binance_source_has_no_authenticated_or_trading_capability():
    source = (PACKAGE_DIR / "tools" / "binance_public_tool.py").read_text(encoding="utf-8").lower()

    for forbidden in (
        "api_key",
        "api_secret",
        "hmac",
        "signature",
        "x-mbx-apikey",
        "listenkey",
        "recvwindow",
        "requests.post",
        "/order",
        "api/v3/account",
        "create_order",
        "cancel_order",
    ):
        assert forbidden not in source


def test_no_new_phase_2_components_were_added():
    for name in ("tinyfish", "oms", "risk", "simulator", "execution", "event_bus", "redis"):
        assert not (PACKAGE_DIR / name).exists(), f"unexpected module/package: {name}"
