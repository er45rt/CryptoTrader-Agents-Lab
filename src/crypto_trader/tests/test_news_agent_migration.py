"""Phase 2 agent #4 migration tests: News and Sentiment Researcher.

Offline only. Verifies the LLM provider path was centralized while the existing
Serper search + scrape tool wiring stayed exactly as it was (no TinyFish, no provider swap).
"""

from pathlib import Path

import pytest
from crewai.llms.base_llm import BaseLLM

import crewai.llm as crewai_llm_module
import crypto_trader.crew as crew_module
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.llm import (
    ACTIVE_PROFILE_NAMES,
    LLMProviderError,
    NEWS_PROFILE_NAME,
    ProviderRateLimiter,
    ResilientLLM,
    get_llm,
    load_profile,
)
from crypto_trader.tests.agent_expectations import (
    AGENT_PROFILE_MAP,
    EXPECTED_PROFILE_NAMES,
    LEGACY_AGENT_NAMES,
    MIGRATED_AGENT_NAMES,
)
from crypto_trader.tools.binance_public_tool import BinancePublicTool

PACKAGE_DIR = Path(__file__).resolve().parent.parent
SENTINEL_KEY = "sentinel-not-a-real-key"

EXPECTED_NEWS_TOOLS = ["Read website content", "Search the internet with Serper"]


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


# --- Agent #4 migrated -----------------------------------------------------

def test_news_agent_is_wired_through_the_factory(crew_with_fake_factory):
    crew, recorded, fake = crew_with_fake_factory

    news_agent = crew.news_and_sentiment_researcher()

    assert news_agent.llm is fake
    assert NEWS_PROFILE_NAME in recorded
    assert NEWS_PROFILE_NAME == "deepseek_news"


def test_all_four_agents_use_centralized_profiles(crew_with_fake_factory):
    crew, recorded, fake = crew_with_fake_factory

    for name in MIGRATED_AGENT_NAMES:
        assert getattr(crew, name)().llm is fake

    assert sorted(recorded) == sorted(EXPECTED_PROFILE_NAMES)
    assert len(set(recorded)) == 4
    assert LEGACY_AGENT_NAMES == ()


def test_each_role_uses_its_own_named_profile(crew_with_fake_factory):
    crew, recorded, _ = crew_with_fake_factory

    for name in (
        "binance_data_analyst",
        "news_and_sentiment_researcher",
        "crypto_analyst",
        "trading_plan_strategist",
    ):
        getattr(crew, name)()

    assert set(recorded) == set(AGENT_PROFILE_MAP.values())
    assert len(recorded) == len(AGENT_PROFILE_MAP) == 4


def test_news_profile_configuration():
    profile = load_profile(NEWS_PROFILE_NAME, env={})

    assert profile.provider == "deepseek"
    assert profile.model == "deepseek/deepseek-chat"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.api_key_env == "DEEPSEEK_API_KEY"
    assert profile.max_attempts >= 2
    assert profile.model != "deepseek/deepseek-reasoner"


def test_all_four_profiles_share_one_provider_rate_limiter():
    env = {"DEEPSEEK_API_KEY": SENTINEL_KEY}
    llms = [get_llm(name, env=env) for name in EXPECTED_PROFILE_NAMES]

    assert len({llm.profile.name for llm in llms}) == 4
    assert len({id(llm.rate_limiter) for llm in llms}) == 1  # provider-level limiting


def test_di_fake_llm_override_works_for_news_profile():
    fake = FakeLLM()
    recorded = {}

    def builder(profile, api_key):
        recorded["profile"] = profile
        recorded["api_key"] = api_key
        return fake

    result = get_llm(NEWS_PROFILE_NAME, env={"DEEPSEEK_API_KEY": SENTINEL_KEY}, builder=builder)

    assert result is fake
    assert recorded["profile"].name == NEWS_PROFILE_NAME


def test_news_agent_builds_a_resilient_llm():
    llm = get_llm(NEWS_PROFILE_NAME, env={"DEEPSEEK_API_KEY": SENTINEL_KEY})

    assert isinstance(llm, ResilientLLM)
    assert llm.timeout == load_profile(NEWS_PROFILE_NAME, env={}).timeout_seconds


# --- Tool wiring preserved -------------------------------------------------

def test_news_agent_keeps_its_existing_search_and_scrape_tools(crew_with_fake_factory):
    crew, _, _ = crew_with_fake_factory

    tool_names = sorted(tool.name for tool in crew.news_and_sentiment_researcher().tools)

    assert tool_names == EXPECTED_NEWS_TOOLS


def test_no_tinyfish_or_replacement_news_provider_exists():
    assert not (PACKAGE_DIR / "tinyfish").exists()
    assert not (PACKAGE_DIR / "tools" / "tinyfish_tool.py").exists()

    for path in PACKAGE_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        assert "tinyfish" not in path.read_text(encoding="utf-8").lower()


def test_news_task_remains_free_form_research_prose(crew_with_fake_factory):
    """The research summary is consumed as LLM context (and later by the analyst), not parsed."""
    crew, _, _ = crew_with_fake_factory

    task = crew.research_news_and_sentiment_task()

    assert task.guardrail is None
    assert "markdown" in task.expected_output.lower()


# --- Phase 1 / hardening boundaries ---------------------------------------

def test_binance_stays_public_and_read_only():
    tool = BinancePublicTool()

    assert tool.base_url == "https://api.binance.com"
    assert [tool.kline_endpoint_path, tool.price_ticker_endpoint_path, tool.ticker_24hr_endpoint_path] == [
        "/api/v3/klines",
        "/api/v3/ticker/price",
        "/api/v3/ticker/24hr",
    ]


def test_provider_error_hardening_applies_to_the_news_llm(monkeypatch):
    original = ProviderFatal("provider exploded \u4f60\u597d")

    def transport(self, messages, **kwargs):
        raise original

    monkeypatch.setattr(crewai_llm_module.LLM, "call", transport)
    llm = ResilientLLM(
        profile=load_profile(NEWS_PROFILE_NAME, env={}),
        api_key=SENTINEL_KEY,
        rate_limiter=ProviderRateLimiter(),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    assert excinfo.value.__cause__ is original
    assert str(excinfo.value).isascii()


def test_active_profiles_cover_all_four_agents():
    assert tuple(ACTIVE_PROFILE_NAMES) == tuple(EXPECTED_PROFILE_NAMES)
    assert len(ACTIVE_PROFILE_NAMES) == 4
    assert "deepseek_reasoner" not in ACTIVE_PROFILE_NAMES


def test_no_other_phase_2_components_exist():
    # NOTE (Phase 4): "execution" is an authorised boundary package now; the rest stay absent.
    # NOTE (Phase 7): "risk" is an authorised production package now; the rest stay absent.
    for name in ("oms", "simulator", "event_bus", "redis"):
        assert not (PACKAGE_DIR / name).exists(), f"unexpected module/package: {name}"
