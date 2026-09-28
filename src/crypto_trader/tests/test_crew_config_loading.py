"""Regression proof for CrewAI YAML configuration loading (review finding D1)."""

from collections.abc import Mapping

from crypto_trader.crew import CryptoTraderCrew


def test_crewbase_resolves_yaml_config_paths_to_mappings(monkeypatch):
    # CrewBase loads YAML before it maps task/agent references. Disable only that later mapping
    # here so this regression proof needs no API keys, tool construction, or network-capable LLMs.
    monkeypatch.setattr(CryptoTraderCrew, "map_all_agent_variables", lambda self: None)
    monkeypatch.setattr(CryptoTraderCrew, "map_all_task_variables", lambda self: None)

    instance = CryptoTraderCrew(verbose=False)

    assert isinstance(instance.agents_config, Mapping)
    assert isinstance(instance.tasks_config, Mapping)
    assert {
        "binance_data_analyst",
        "news_and_sentiment_researcher",
        "crypto_analyst",
        "trading_plan_strategist",
    } <= set(instance.agents_config)
    assert {
        "fetch_binance_klines_task",
        "fetch_binance_price_ticker_task",
        "fetch_binance_24hr_stats_task",
        "research_news_and_sentiment_task",
        "analyze_crypto_data_task",
        "develop_trading_plan_task",
    } <= set(instance.tasks_config)
