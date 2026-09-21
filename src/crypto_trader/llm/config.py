"""Centralized LLM provider/model configuration.

Phase 2 introduces named LLM *profiles* so that individual agents never hardcode
a provider or a model. All configuration is environment-driven; secrets are read
from the environment and are never embedded, printed, or logged.

Design notes
------------
* A profile describes *how* to reach a provider (model string, base URL, key env
  var, timeout/retry/rate-limit policy) and is resolved at call time, so tests can
  pass an explicit environment mapping instead of mutating ``os.environ``.
* Precedence for every setting: profile-specific env var > provider-wide env var >
  built-in default. Example: ``DEEPSEEK_CANARY_MODEL`` beats ``DEEPSEEK_MODEL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Mapping

__all__ = [
    "ACTIVE_PROFILE_NAMES",
    "ANALYST_PROFILE_NAME",
    "CANARY_PROFILE_NAME",
    "MARKET_PROFILE_NAME",
    "AVAILABLE_PROFILES",
    "DEEPSEEK_DEFAULT_BASE_URL",
    "DEEPSEEK_DEFAULT_CHAT_MODEL",
    "DEEPSEEK_DEFAULT_REASONER_MODEL",
    "PROVIDER_DEEPSEEK",
    "LLMConfigError",
    "LLMProfile",
    "available_profiles",
    "load_profile",
]


class LLMConfigError(RuntimeError):
    """Raised when LLM configuration is missing, unknown, or invalid.

    Messages never contain credential values; they name the *environment
    variable* that must be supplied instead.
    """


# --- Providers -------------------------------------------------------------

PROVIDER_DEEPSEEK = "deepseek"

# DeepSeek is OpenAI-compatible. Verified against the installed LiteLLM 1.74.9
# (litellm/llms/deepseek/chat/transformation.py): provider "deepseek" reads
# DEEPSEEK_API_KEY (with DEEPSEEK_API_BASE as an optional override) and accepts
# the model ids "deepseek/deepseek-chat" and "deepseek/deepseek-reasoner", which
# are present in the bundled model cost map.
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_CHAT_MODEL = "deepseek/deepseek-chat"
DEEPSEEK_DEFAULT_REASONER_MODEL = "deepseek/deepseek-reasoner"


@dataclass(frozen=True)
class LLMProfile:
    """Immutable description of how to reach one LLM provider configuration."""

    name: str
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 8.0
    # Provider-level throttling policy (shared by every LLM of this provider).
    min_seconds_between_requests: float = 0.0
    requests_per_minute: int | None = None

    @property
    def env_prefix(self) -> str:
        return self.name.upper()

    def resolve_api_key(self, env: Mapping[str, str] | None = None) -> str:
        """Return the API key from the environment, or raise ``LLMConfigError``.

        The returned value is never logged by this library.
        """
        if not self.api_key_env:
            return ""
        source = os.environ if env is None else env
        value = (source.get(self.api_key_env) or "").strip()
        if not value:
            raise LLMConfigError(
                f"Missing required environment variable '{self.api_key_env}' for LLM profile "
                f"'{self.name}' (provider '{self.provider}', model '{self.model}'). "
                "Export it in the environment or add it to a local .env file. "
                "Credential values are never logged."
            )
        return value


# Built-in profile defaults. Nothing here contains a secret value.
_DEFAULT_PROFILES: dict[str, LLMProfile] = {
    "deepseek_canary": LLMProfile(
        name="deepseek_canary",
        provider=PROVIDER_DEEPSEEK,
        model=DEEPSEEK_DEFAULT_CHAT_MODEL,
        api_key_env="DEEPSEEK_API_KEY",
        base_url=DEEPSEEK_DEFAULT_BASE_URL,
        temperature=0.2,
        timeout_seconds=60.0,
        max_attempts=3,
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
        min_seconds_between_requests=0.0,
        requests_per_minute=60,
    ),
    # Analytical role profile (Comprehensive Crypto Analyst). Same provider, model and
    # policy as the canary, so both agents share one provider-level rate limiter; it is
    # a distinct named profile so the analytical role can be tuned independently via
    # DEEPSEEK_ANALYST_* environment overrides.
    "deepseek_analyst": LLMProfile(
        name="deepseek_analyst",
        provider=PROVIDER_DEEPSEEK,
        model=DEEPSEEK_DEFAULT_CHAT_MODEL,
        api_key_env="DEEPSEEK_API_KEY",
        base_url=DEEPSEEK_DEFAULT_BASE_URL,
        temperature=0.2,
        timeout_seconds=60.0,
        max_attempts=3,
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
        min_seconds_between_requests=0.0,
        requests_per_minute=60,
    ),
    # Defined for future use and intentionally NOT active: no agent, task, or runtime
    # path references it (see ACTIVE_PROFILE_NAMES below). Kept because it exercises a
    # second named profile shape (no temperature, longer timeout) at zero runtime cost.
    "deepseek_reasoner": LLMProfile(
        name="deepseek_reasoner",
        provider=PROVIDER_DEEPSEEK,
        model=DEEPSEEK_DEFAULT_REASONER_MODEL,
        api_key_env="DEEPSEEK_API_KEY",
        base_url=DEEPSEEK_DEFAULT_BASE_URL,
        # The reasoner model does not accept temperature; LiteLLM drops it anyway.
        temperature=None,
        timeout_seconds=120.0,
        max_attempts=3,
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
        min_seconds_between_requests=0.0,
        requests_per_minute=60,
    ),
    # Market data role profile (Binance Market Data Analyst). Lower temperature because
    # the role reports fetched market data and validation status rather than reasoning.
    # Same provider and policy, so it shares the provider-level rate limiter.
    "deepseek_market": LLMProfile(
        name="deepseek_market",
        provider=PROVIDER_DEEPSEEK,
        model=DEEPSEEK_DEFAULT_CHAT_MODEL,
        api_key_env="DEEPSEEK_API_KEY",
        base_url=DEEPSEEK_DEFAULT_BASE_URL,
        temperature=0.0,
        timeout_seconds=60.0,
        max_attempts=3,
        backoff_base_seconds=1.0,
        backoff_max_seconds=8.0,
        min_seconds_between_requests=0.0,
        requests_per_minute=60,
    ),
}

#: Name of the profile used by the first migrated (canary) agent.
CANARY_PROFILE_NAME = "deepseek_canary"

#: Name of the profile used by the Comprehensive Crypto Analyst (Phase 2, agent 2).
ANALYST_PROFILE_NAME = "deepseek_analyst"

#: Name of the profile used by the Binance Market Data Analyst (Phase 2, agent 3).
MARKET_PROFILE_NAME = "deepseek_market"

#: Profiles currently wired into a live agent. Any profile that is not listed here
#: is defined for future use and is intentionally NOT active yet - in particular
#: ``deepseek_reasoner`` exists so the registry can express a second model shape
#: (no temperature, longer timeout), but no agent, task, or runtime path uses it.
ACTIVE_PROFILE_NAMES: tuple[str, ...] = (
    CANARY_PROFILE_NAME,
    ANALYST_PROFILE_NAME,
    MARKET_PROFILE_NAME,
)

#: Every profile name known to this build, sorted.
AVAILABLE_PROFILES: tuple[str, ...] = tuple(sorted(_DEFAULT_PROFILES))


def available_profiles() -> tuple[str, ...]:
    """Return the names of all built-in profiles."""
    return AVAILABLE_PROFILES


def _first_env_value(env: Mapping[str, str], names: list[str]) -> str | None:
    for name in names:
        raw = env.get(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
    return None


def _coerce_float(raw: str, *, env_name: str, profile_name: str, minimum: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(
            f"Environment variable {env_name} for LLM profile '{profile_name}' must be a number."
        ) from exc
    if value < minimum:
        raise LLMConfigError(
            f"Environment variable {env_name} for LLM profile '{profile_name}' must be >= {minimum}."
        )
    return value


def _coerce_optional_float(raw: str | None, *, env_name: str, profile_name: str) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(
            f"Environment variable {env_name} for LLM profile '{profile_name}' must be a number."
        ) from exc


def _coerce_int(raw: str, *, env_name: str, profile_name: str, minimum: int = 1) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(
            f"Environment variable {env_name} for LLM profile '{profile_name}' must be an integer."
        ) from exc
    if value < minimum:
        raise LLMConfigError(
            f"Environment variable {env_name} for LLM profile '{profile_name}' must be >= {minimum}."
        )
    return value


def load_profile(name: str, env: Mapping[str, str] | None = None) -> LLMProfile:
    """Resolve a named profile, applying environment overrides.

    Args:
        name: Profile name, e.g. ``"deepseek_canary"``.
        env: Optional environment mapping (defaults to ``os.environ``). Passing an
            explicit mapping keeps configuration loading testable and side-effect free.

    Raises:
        LLMConfigError: If the profile name is unknown or an override is invalid.
    """
    base = _DEFAULT_PROFILES.get(name)
    if base is None:
        raise LLMConfigError(
            f"Unknown LLM profile '{name}'. Available profiles: {', '.join(AVAILABLE_PROFILES)}."
        )

    source = os.environ if env is None else env
    prefix = base.env_prefix
    provider_prefix = base.provider.upper()

    overrides: dict[str, Any] = {}

    model = _first_env_value(source, [f"{prefix}_MODEL", f"{provider_prefix}_MODEL"])
    if model:
        overrides["model"] = model

    api_key_env = _first_env_value(source, [f"{prefix}_API_KEY_ENV", f"{provider_prefix}_API_KEY_ENV"])
    if api_key_env:
        overrides["api_key_env"] = api_key_env

    base_url = _first_env_value(source, [f"{prefix}_BASE_URL", f"{provider_prefix}_BASE_URL"])
    if base_url:
        overrides["base_url"] = base_url

    temperature_name = _first_matching_name(
        source, [f"{prefix}_TEMPERATURE", f"{provider_prefix}_TEMPERATURE"]
    )
    if temperature_name:
        overrides["temperature"] = _coerce_optional_float(
            source[temperature_name], env_name=temperature_name, profile_name=base.name
        )

    max_tokens_name = _first_matching_name(
        source, [f"{prefix}_MAX_TOKENS", f"{provider_prefix}_MAX_TOKENS"]
    )
    if max_tokens_name:
        overrides["max_tokens"] = _coerce_int(
            str(source[max_tokens_name]), env_name=max_tokens_name, profile_name=base.name
        )

    timeout_name = _first_matching_name(
        source, [f"{prefix}_TIMEOUT_SECONDS", f"{provider_prefix}_TIMEOUT_SECONDS"]
    )
    if timeout_name:
        overrides["timeout_seconds"] = _coerce_float(
            str(source[timeout_name]), env_name=timeout_name, profile_name=base.name, minimum=0.1
        )

    attempts_name = _first_matching_name(
        source, [f"{prefix}_MAX_ATTEMPTS", f"{provider_prefix}_MAX_ATTEMPTS"]
    )
    if attempts_name:
        overrides["max_attempts"] = _coerce_int(
            str(source[attempts_name]), env_name=attempts_name, profile_name=base.name
        )

    backoff_base_name = _first_matching_name(
        source, [f"{prefix}_BACKOFF_BASE_SECONDS", f"{provider_prefix}_BACKOFF_BASE_SECONDS"]
    )
    if backoff_base_name:
        overrides["backoff_base_seconds"] = _coerce_float(
            str(source[backoff_base_name]), env_name=backoff_base_name, profile_name=base.name
        )

    backoff_max_name = _first_matching_name(
        source, [f"{prefix}_BACKOFF_MAX_SECONDS", f"{provider_prefix}_BACKOFF_MAX_SECONDS"]
    )
    if backoff_max_name:
        overrides["backoff_max_seconds"] = _coerce_float(
            str(source[backoff_max_name]), env_name=backoff_max_name, profile_name=base.name
        )

    min_interval_name = _first_matching_name(
        source,
        [f"{prefix}_MIN_INTERVAL_SECONDS", f"{provider_prefix}_MIN_INTERVAL_SECONDS"],
    )
    if min_interval_name:
        overrides["min_seconds_between_requests"] = _coerce_float(
            str(source[min_interval_name]), env_name=min_interval_name, profile_name=base.name
        )

    rpm_name = _first_matching_name(
        source, [f"{prefix}_REQUESTS_PER_MINUTE", f"{provider_prefix}_REQUESTS_PER_MINUTE"]
    )
    if rpm_name:
        overrides["requests_per_minute"] = _coerce_int(
            str(source[rpm_name]), env_name=rpm_name, profile_name=base.name
        )

    return replace(base, **overrides)


def _first_matching_name(env: Mapping[str, str], names: list[str]) -> str | None:
    for name in names:
        if env.get(name) is not None and str(env.get(name)).strip() != "":
            return name
    return None
