"""Canary migration boundary tests (Phase 2).

Verifies that the Crypto Trading Plan Strategist resolves its LLM through the
centralized factory with its DeepSeek profile, that migrated agents (per
``agent_expectations``) each use their own profile, that unmigrated agents keep their
existing configuration, and that Phase 1 constraints still hold.
"""

from pathlib import Path

import pytest
import yaml
from crewai.llms.base_llm import BaseLLM

import crypto_trader.crew as crew_module
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.llm import CANARY_PROFILE_NAME, trading_plan_guardrail
from crypto_trader.tests.agent_expectations import (
    EXPECTED_PROFILE_NAMES,
    LEGACY_AGENT_NAMES,
    MIGRATED_AGENT_NAMES,
)
from crypto_trader.tools.binance_public_tool import BinancePublicTool

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "src" / "crypto_trader" / "config"
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"

class FakeLLM(BaseLLM):
    """Injected stand-in so the canary test never contacts DeepSeek."""

    def __init__(self):
        super().__init__(model="fake/deepseek-canary")

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None):
        return "fake"


@pytest.fixture
def crew_with_canary_monkeypatched(monkeypatch):
    """Crew instance whose strategist LLM comes from a recording fake factory."""
    recorded: list[str] = []
    fake = FakeLLM()

    def fake_get_llm(profile, **kwargs):
        recorded.append(profile)
        return fake

    monkeypatch.setattr(crew_module, "get_llm", fake_get_llm)
    return CryptoTraderCrew(verbose=False), recorded, fake


# --- Only the canary uses the factory --------------------------------------

def test_strategist_resolves_its_llm_through_the_factory(crew_with_canary_monkeypatched):
    crew, recorded, fake = crew_with_canary_monkeypatched

    strategist = crew.trading_plan_strategist()

    assert CANARY_PROFILE_NAME in recorded
    assert strategist.llm is fake


def test_all_agents_resolve_through_the_factory(crew_with_canary_monkeypatched):
    """End state of Phase 2: every agent resolves its LLM from the factory."""
    crew, recorded, fake = crew_with_canary_monkeypatched

    for name in MIGRATED_AGENT_NAMES:
        assert getattr(crew, name)().llm is fake

    assert sorted(recorded) == sorted(EXPECTED_PROFILE_NAMES)
    assert LEGACY_AGENT_NAMES == ()  # no agent is left on the legacy configuration


def test_agents_yaml_only_migrated_agents_drop_hardcoded_model():
    agents = yaml.safe_load((CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8"))

    for name in MIGRATED_AGENT_NAMES:
        assert "llm" not in agents[name], f"{name} must not hardcode a provider/model"
    # Final Phase 2 state: no agent carries an agent-level model configuration any more.
    assert not any("llm" in config for config in agents.values())


def test_no_agent_contains_provider_specific_branching():
    source = (PACKAGE_DIR / "crew.py").read_text(encoding="utf-8")

    for forbidden in ("deepseek", "openai/", "if provider", "isinstance(llm"):
        assert forbidden not in source, f"provider-specific logic leaked into crew.py: {forbidden}"


def test_canary_profile_is_deepseek(crew_with_canary_monkeypatched):
    assert CANARY_PROFILE_NAME == "deepseek_canary"


# --- Guardrail wiring on the canary task only ------------------------------

def test_strategist_task_wires_guardrail_with_bounded_repair(crew_with_canary_monkeypatched):
    crew, _, _ = crew_with_canary_monkeypatched

    task = crew.develop_trading_plan_task()

    assert task.guardrail is trading_plan_guardrail
    assert task.guardrail_max_retries == 1


def test_only_the_canary_task_has_a_guardrail(crew_with_canary_monkeypatched):
    crew, _, _ = crew_with_canary_monkeypatched

    for task in (
        crew.fetch_binance_klines_task(),
        crew.fetch_binance_price_ticker_task(),
        crew.fetch_binance_24hr_stats_task(),
        crew.research_news_and_sentiment_task(),
    ):
        assert task.guardrail is None


# --- Phase 1 boundaries still hold -----------------------------------------

def test_binance_tool_remains_public_and_read_only():
    tool = BinancePublicTool()

    assert tool.base_url == "https://api.binance.com"
    assert tool.kline_endpoint_path == "/api/v3/klines"
    assert tool.price_ticker_endpoint_path == "/api/v3/ticker/price"
    assert tool.ticker_24hr_endpoint_path == "/api/v3/ticker/24hr"
    assert not hasattr(tool, "rapidapi_key")
    assert not hasattr(tool, "rapidapi_host")


def test_binance_source_has_no_authenticated_or_trading_endpoints():
    source = (PACKAGE_DIR / "tools" / "binance_public_tool.py").read_text(encoding="utf-8")
    lowered = source.lower()

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
        assert forbidden not in lowered, f"unexpected Binance capability: {forbidden}"

    assert "requests.get" in lowered  # public GET only


def test_no_phase_three_components_were_introduced():
    for name in ("tinyfish", "oms", "risk", "simulator", "execution", "event_bus", "redis"):
        assert not (PACKAGE_DIR / name).exists(), f"unexpected module/package: {name}"

    # Scan runtime modules only; the test modules necessarily name these terms.
    for path in PACKAGE_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "tinyfish" not in text, f"TinyFish reference in {path}"
        assert "import redis" not in text, f"Redis reference in {path}"


def test_news_agent_still_uses_serper_tools_only(crew_with_canary_monkeypatched):
    crew, _, _ = crew_with_canary_monkeypatched

    tool_names = sorted(tool.name for tool in crew.news_and_sentiment_researcher().tools)

    assert tool_names == ["Read website content", "Search the internet with Serper"]
