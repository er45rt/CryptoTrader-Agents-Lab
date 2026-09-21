"""Single source of truth for the expected agent -> LLM profile mapping.

Updated once per Phase 2 migration gate so the boundary tests stay explicit and
consistent instead of hardcoding counts in several files.
"""

from crypto_trader.llm import (
    ANALYST_PROFILE_NAME,
    CANARY_PROFILE_NAME,
    MARKET_PROFILE_NAME,
)

#: Model string still used by agents that have not been migrated yet.
LEGACY_AGENT_MODEL = "openai/qwen-turbo-latest"

#: ``CryptoTraderCrew`` agent method name -> centralized profile it must resolve.
AGENT_PROFILE_MAP = {
    "trading_plan_strategist": CANARY_PROFILE_NAME,
    "crypto_analyst": ANALYST_PROFILE_NAME,
    "binance_data_analyst": MARKET_PROFILE_NAME,
}

#: Agents still on their original YAML-configured model.
LEGACY_AGENT_NAMES = ("news_and_sentiment_researcher",)

MIGRATED_AGENT_NAMES = tuple(AGENT_PROFILE_MAP)
EXPECTED_PROFILE_NAMES = tuple(AGENT_PROFILE_MAP.values())

__all__ = [
    "AGENT_PROFILE_MAP",
    "EXPECTED_PROFILE_NAMES",
    "LEGACY_AGENT_MODEL",
    "LEGACY_AGENT_NAMES",
    "MIGRATED_AGENT_NAMES",
]
