"""Offline analysis wiring; no execution-domain integration or model requests."""
import socket

from crewai.llms.base_llm import BaseLLM
import pytest

import crypto_trader.crew as crew_module
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.tools.binance_futures_public_tool import BinanceFuturesPublicTool
from crypto_trader.tools.binance_public_tool import BinancePublicTool
from crypto_trader.tools import derivatives_schema as ds


class FakeLLM(BaseLLM):
    def __init__(self):
        super().__init__(model="fake/local")

    def call(self, *args, **kwargs):
        raise AssertionError("model requests forbidden in wiring test")


@pytest.fixture
def offline_crew(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    fake = FakeLLM()
    monkeypatch.setattr(crew_module, "get_llm", lambda *args, **kwargs: fake)
    return CryptoTraderCrew(verbose=False)


def test_futures_tool_attached_only_to_market_analyst(offline_crew):
    assert [type(tool) for tool in offline_crew.binance_data_analyst().tools] == [BinancePublicTool, BinanceFuturesPublicTool]
    for agent in (offline_crew.news_and_sentiment_researcher(), offline_crew.crypto_analyst(), offline_crew.trading_plan_strategist()):
        assert not any(isinstance(tool, BinanceFuturesPublicTool) for tool in agent.tools)


def test_derivatives_task_is_in_analysis_context_and_crew_sequence(offline_crew):
    derivative = offline_crew.fetch_derivatives_snapshot_task()
    assert derivative in offline_crew.analyze_crypto_data_task().context
    tasks = offline_crew.crew().tasks
    assert tasks.index(derivative) < tasks.index(offline_crew.analyze_crypto_data_task())
    assert derivative.guardrail is None


def test_advisory_and_confidence_rules_are_in_actual_agent_prompts(offline_crew):
    for agent in (offline_crew.binance_data_analyst(), offline_crew.crypto_analyst()):
        prompt = agent.backstory.lower()
        assert "unavailable" in prompt and "reduce confidence" in prompt
        assert "primary trade thesis" in prompt and "suspect" in prompt
        assert "exploratory_only" in prompt and "adoption" in prompt
    description = offline_crew.fetch_derivatives_snapshot_task().description
    assert "current_funding_context" in description and "settled_funding_history" in description
    assert "available_at" in description and "replay_eligible" in description


def test_tool_manager_shares_one_futures_tool(offline_crew):
    other = CryptoTraderCrew(verbose=False)
    assert offline_crew.tool_manager.get_futures_tool() is other.tool_manager.get_futures_tool()


def test_actual_analysis_prompts_state_strict_ab_is_not_yet_eligible(offline_crew):
    expected = f"Strict A/B adoption gate: {ds.STRICT_AB_ADOPTION_STATUS}"
    prompts = [offline_crew.binance_data_analyst().backstory, offline_crew.crypto_analyst().backstory,
               offline_crew.fetch_derivatives_snapshot_task().description,
               offline_crew.analyze_crypto_data_task().description]
    for prompt in prompts:
        assert expected in prompt
        assert "replay_eligible: true" in prompt
        assert "not A/B adoption evidence" in prompt
        assert "several weeks" in prompt and "multiple market regimes" in prompt
        assert "returns" in prompt and "drawdown" in prompt
