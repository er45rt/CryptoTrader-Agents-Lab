"""Phase 2 agent #3 migration tests: Binance Market Data Analyst.

Offline only. Verifies the LLM provider path changed while the Binance public tool
wiring and Phase 1 read-only guarantees stayed intact.
"""

from pathlib import Path

import pytest
from crewai.llms.base_llm import BaseLLM

import crewai.llm as crewai_llm_module
import crypto_trader.crew as crew_module
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.llm import (
    ACTIVE_PROFILE_NAMES,
    MARKET_PROFILE_NAME,
    LLMProviderError,
    ProviderRateLimiter,
    ResilientLLM,
    get_llm,
    load_profile,
)
from crypto_trader.tests.agent_expectations import (
    EXPECTED_PROFILE_NAMES,
    LEGACY_AGENT_NAMES,
    MIGRATED_AGENT_NAMES,
)
from crypto_trader.tools.binance_public_tool import BinancePublicTool

PACKAGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PACKAGE_DIR / "config"

SENTINEL_KEY = "sentinel-not-a-real-key"
MARKET_TOOL_NAME = "Binance Market Data Tool (Official Public API)"


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


# --- Agent #3 migrated -----------------------------------------------------

def test_market_agent_is_wired_through_the_factory(crew_with_fake_factory):
    crew, recorded, fake = crew_with_fake_factory

    market_agent = crew.binance_data_analyst()

    assert market_agent.llm is fake
    assert MARKET_PROFILE_NAME in recorded
    assert MARKET_PROFILE_NAME == "deepseek_market"


def test_all_four_agents_use_the_factory_with_distinct_profiles(crew_with_fake_factory):
    crew, recorded, _ = crew_with_fake_factory

    for name in MIGRATED_AGENT_NAMES + LEGACY_AGENT_NAMES:
        getattr(crew, name)()

    assert sorted(recorded) == sorted(EXPECTED_PROFILE_NAMES)
    assert len(set(recorded)) == 4  # one distinct profile per agent


def test_market_profile_configuration():
    profile = load_profile(MARKET_PROFILE_NAME, env={})

    assert profile.provider == "deepseek"
    assert profile.model == "deepseek/deepseek-chat"
    assert profile.base_url == "https://api.deepseek.com"
    assert profile.api_key_env == "DEEPSEEK_API_KEY"
    assert profile.temperature == 0.0
    assert profile.max_attempts >= 2
    assert profile.model != "deepseek/deepseek-reasoner"


def test_all_deepseek_agents_share_one_provider_rate_limiter():
    env = {"DEEPSEEK_API_KEY": SENTINEL_KEY}
    llms = [get_llm(name, env=env) for name in EXPECTED_PROFILE_NAMES]

    # One distinct profile per agent, but a single provider-level limiter for all of them.
    assert len({llm.profile.name for llm in llms}) == len(EXPECTED_PROFILE_NAMES) == 4
    assert len({id(llm.rate_limiter) for llm in llms}) == 1  # provider-level, not per agent


def test_di_fake_llm_override_works_for_market_profile():
    fake = FakeLLM()
    recorded = {}

    def builder(profile, api_key):
        recorded["profile"] = profile
        recorded["api_key"] = api_key
        return fake

    result = get_llm(MARKET_PROFILE_NAME, env={"DEEPSEEK_API_KEY": SENTINEL_KEY}, builder=builder)

    assert result is fake
    assert recorded["profile"].name == MARKET_PROFILE_NAME
    assert recorded["api_key"] == SENTINEL_KEY


def test_market_agent_builds_a_resilient_llm():
    llm = get_llm(MARKET_PROFILE_NAME, env={"DEEPSEEK_API_KEY": SENTINEL_KEY})

    assert isinstance(llm, ResilientLLM)
    assert llm.timeout == load_profile(MARKET_PROFILE_NAME, env={}).timeout_seconds


# --- Binance tool wiring and Phase 1 guarantees unchanged ------------------

def test_market_agent_keeps_the_same_binance_public_tool(crew_with_fake_factory):
    crew, _, _ = crew_with_fake_factory

    tool_names = [tool.name for tool in crew.binance_data_analyst().tools]

    assert tool_names == [MARKET_TOOL_NAME]


def test_binance_tool_still_public_and_read_only():
    tool = BinancePublicTool()

    assert tool.base_url == "https://api.binance.com"
    assert tool.kline_endpoint_path == "/api/v3/klines"
    assert tool.price_ticker_endpoint_path == "/api/v3/ticker/price"
    assert tool.ticker_24hr_endpoint_path == "/api/v3/ticker/24hr"
    assert not hasattr(tool, "rapidapi_key")


def test_no_authenticated_or_trading_binance_capability_exists():
    """Binance-scoped scan.

    Only Binance-relevant code is scanned, so unrelated credentials (for example the
    Serper key handled in ``main.py``) cannot produce a false positive.
    """
    binance_sources = {
        "tools/binance_public_tool.py": (PACKAGE_DIR / "tools" / "binance_public_tool.py"),
        "crew.py": (PACKAGE_DIR / "crew.py"),
    }
    forbidden_tokens = (
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
    )

    for label, path in binance_sources.items():
        lowered = path.read_text(encoding="utf-8").lower()
        for token in forbidden_tokens:
            assert token not in lowered, f"{label}: unexpected Binance capability: {token}"

    # Stronger, explicit check: no runtime module reads a Binance credential.
    for path in PACKAGE_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden_env_var in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SECRET_KEY"):
            assert forbidden_env_var not in text, f"{path.name} reads {forbidden_env_var}"


def test_market_tasks_remain_free_form_without_a_schema_guardrail(crew_with_fake_factory):
    """Structured data comes from the validated Binance tool payload, not a task schema."""
    crew, _, _ = crew_with_fake_factory

    for build in (
        crew.fetch_binance_klines_task,
        crew.fetch_binance_price_ticker_task,
        crew.fetch_binance_24hr_stats_task,
    ):
        task = build()
        assert task.guardrail is None
        assert "JSON string" in task.expected_output


# --- Provider hardening still applies -------------------------------------

def test_provider_error_hardening_applies_to_the_market_llm(monkeypatch):
    original = ProviderFatal("provider exploded \u4f60\u597d")

    def transport(self, messages, **kwargs):
        raise original

    monkeypatch.setattr(crewai_llm_module.LLM, "call", transport)
    llm = ResilientLLM(
        profile=load_profile(MARKET_PROFILE_NAME, env={}),
        api_key=SENTINEL_KEY,
        rate_limiter=ProviderRateLimiter(),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    assert excinfo.value.__cause__ is original
    assert excinfo.value.cause_type == "ProviderFatal"
    assert str(excinfo.value).isascii()


# --- Scope boundaries ------------------------------------------------------

def test_profiles_active_set_matches_migrated_agents():
    assert tuple(ACTIVE_PROFILE_NAMES) == tuple(EXPECTED_PROFILE_NAMES)
    assert "deepseek_reasoner" not in ACTIVE_PROFILE_NAMES


def test_no_unrelated_phase_2_components_exist():
    # NOTE (Phase 4): "execution" is an authorised boundary package now; the rest stay absent.
    # NOTE (Phase 7): "risk" is an authorised production package now; the rest stay absent.
    for name in ("tinyfish", "oms", "simulator", "event_bus", "redis"):
        assert not (PACKAGE_DIR / name).exists(), f"unexpected module/package: {name}"
