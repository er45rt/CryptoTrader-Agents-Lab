"""Centralized LLM layer for crypto_trader.

Agents obtain their model through :func:`get_llm` using a named profile; no agent
hardcodes a provider or model string. See :mod:`crypto_trader.llm.config` for
profiles and :mod:`crypto_trader.llm.factory` for construction, retry, timeout, and
rate-limiting policy.
"""

from .config import (
    ACTIVE_PROFILE_NAMES,
    ANALYST_PROFILE_NAME,
    AVAILABLE_PROFILES,
    CANARY_PROFILE_NAME,
    LLMConfigError,
    LLMProfile,
    available_profiles,
    load_profile,
)
from .console import (
    ascii_safe,
    is_console_encoding_error,
    recover_original_error,
    tolerant_console_writes,
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
    "ACTIVE_PROFILE_NAMES",
    "ANALYST_PROFILE_NAME",
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
    "ascii_safe",
    "build_resilient_llm",
    "get_llm",
    "get_provider_rate_limiter",
    "is_console_encoding_error",
    "load_profile",
    "parse_trading_plan",
    "recover_original_error",
    "reset_factory_state",
    "set_default_builder",
    "tolerant_console_writes",
    "trading_plan_guardrail",
]
