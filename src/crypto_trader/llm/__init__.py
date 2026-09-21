"""Centralized LLM layer for crypto_trader.

Agents obtain their model through :func:`get_llm` using a named profile; no agent
hardcodes a provider or model string. See :mod:`crypto_trader.llm.config` for
profiles and :mod:`crypto_trader.llm.factory` for construction, retry, timeout, and
rate-limiting policy.
"""

from .config import (
    AVAILABLE_PROFILES,
    CANARY_PROFILE_NAME,
    LLMConfigError,
    LLMProfile,
    available_profiles,
    load_profile,
)
from .factory import (
    LLMProviderError,
    ProviderRateLimiter,
    ResilientLLM,
    build_resilient_llm,
    get_llm,
    get_provider_rate_limiter,
    reset_factory_state,
    set_default_builder,
)
from .schemas import (
    StructuredOutputError,
    TakeProfitTarget,
    TradingPlan,
    parse_trading_plan,
    trading_plan_guardrail,
)

__all__ = [
    "AVAILABLE_PROFILES",
    "CANARY_PROFILE_NAME",
    "LLMConfigError",
    "LLMProfile",
    "LLMProviderError",
    "ProviderRateLimiter",
    "ResilientLLM",
    "StructuredOutputError",
    "TakeProfitTarget",
    "TradingPlan",
    "available_profiles",
    "build_resilient_llm",
    "get_llm",
    "get_provider_rate_limiter",
    "load_profile",
    "parse_trading_plan",
    "reset_factory_state",
    "set_default_builder",
    "trading_plan_guardrail",
]
