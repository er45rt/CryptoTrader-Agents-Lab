"""Tests for centralized LLM profile configuration (Phase 2).

These tests never touch the network and never use a real credential: profile
resolution takes an explicit environment mapping, and the only key values that
appear are obvious sentinels.
"""

import logging

import pytest

from crypto_trader.llm import (
    AVAILABLE_PROFILES,
    CANARY_PROFILE_NAME,
    LLMConfigError,
    available_profiles,
    get_llm,
    load_profile,
)
from crypto_trader.llm.config import (
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_CHAT_MODEL,
    PROVIDER_DEEPSEEK,
)

SENTINEL_KEY = "sentinel-not-a-real-key"


def test_canary_profile_is_registered():
    assert CANARY_PROFILE_NAME == "deepseek_canary"
    assert CANARY_PROFILE_NAME in AVAILABLE_PROFILES
    assert available_profiles() == AVAILABLE_PROFILES
    assert "deepseek_reasoner" in available_profiles()


def test_profile_defaults_are_deepseek_chat_without_embedded_secrets():
    profile = load_profile(CANARY_PROFILE_NAME, env={})

    assert profile.provider == PROVIDER_DEEPSEEK
    assert profile.model == DEEPSEEK_DEFAULT_CHAT_MODEL
    assert profile.base_url == DEEPSEEK_DEFAULT_BASE_URL
    assert profile.api_key_env == "DEEPSEEK_API_KEY"
    assert profile.timeout_seconds > 0
    assert profile.max_attempts >= 2
    assert profile.requests_per_minute and profile.requests_per_minute > 0
    assert not hasattr(profile, "api_key")


def test_unknown_profile_lists_available_profiles():
    with pytest.raises(LLMConfigError) as excinfo:
        load_profile("does_not_exist", env={})

    message = str(excinfo.value)
    assert "does_not_exist" in message
    for name in AVAILABLE_PROFILES:
        assert name in message


def test_provider_wide_env_overrides_defaults():
    profile = load_profile(
        CANARY_PROFILE_NAME,
        env={
            "DEEPSEEK_MODEL": "deepseek/deepseek-reasoner",
            "DEEPSEEK_BASE_URL": "https://example.invalid/v1",
            "DEEPSEEK_TIMEOUT_SECONDS": "12.5",
            "DEEPSEEK_MAX_ATTEMPTS": "5",
            "DEEPSEEK_REQUESTS_PER_MINUTE": "30",
            "DEEPSEEK_MIN_INTERVAL_SECONDS": "0.25",
        },
    )

    assert profile.model == "deepseek/deepseek-reasoner"
    assert profile.base_url == "https://example.invalid/v1"
    assert profile.timeout_seconds == 12.5
    assert profile.max_attempts == 5
    assert profile.requests_per_minute == 30
    assert profile.min_seconds_between_requests == 0.25


def test_profile_specific_env_beats_provider_wide_env():
    profile = load_profile(
        CANARY_PROFILE_NAME,
        env={
            "DEEPSEEK_MODEL": "deepseek/deepseek-reasoner",
            "DEEPSEEK_CANARY_MODEL": "deepseek/deepseek-chat",
            "DEEPSEEK_CANARY_TIMEOUT_SECONDS": "9",
        },
    )

    assert profile.model == "deepseek/deepseek-chat"
    assert profile.timeout_seconds == 9.0


@pytest.mark.parametrize(
    "env,expected_env_name",
    [
        ({"DEEPSEEK_TIMEOUT_SECONDS": "not-a-number"}, "DEEPSEEK_TIMEOUT_SECONDS"),
        ({"DEEPSEEK_MAX_ATTEMPTS": "0"}, "DEEPSEEK_MAX_ATTEMPTS"),
        ({"DEEPSEEK_MAX_ATTEMPTS": "abc"}, "DEEPSEEK_MAX_ATTEMPTS"),
        ({"DEEPSEEK_BACKOFF_BASE_SECONDS": "-1"}, "DEEPSEEK_BACKOFF_BASE_SECONDS"),
    ],
)
def test_invalid_env_overrides_raise_clear_config_error(env, expected_env_name):
    with pytest.raises(LLMConfigError) as excinfo:
        load_profile(CANARY_PROFILE_NAME, env=env)

    assert expected_env_name in str(excinfo.value)


def test_missing_api_key_raises_without_leaking_any_value():
    profile = load_profile(CANARY_PROFILE_NAME, env={})

    with pytest.raises(LLMConfigError) as excinfo:
        profile.resolve_api_key(env={})

    message = str(excinfo.value)
    assert "DEEPSEEK_API_KEY" in message
    assert SENTINEL_KEY not in message


def test_api_key_is_resolved_from_environment():
    profile = load_profile(CANARY_PROFILE_NAME, env={})
    assert profile.resolve_api_key(env={"DEEPSEEK_API_KEY": SENTINEL_KEY}) == SENTINEL_KEY


def test_api_key_value_is_never_logged(caplog):
    with caplog.at_level(logging.DEBUG, logger="crypto_trader.llm"):
        llm = get_llm(CANARY_PROFILE_NAME, env={"DEEPSEEK_API_KEY": SENTINEL_KEY})

    assert llm.api_key == SENTINEL_KEY  # held in memory, ready for the provider
    assert SENTINEL_KEY not in caplog.text
    for record in caplog.records:
        assert SENTINEL_KEY not in record.getMessage()


def test_api_key_env_override_rejects_arbitrary_secret_name():
    with pytest.raises(LLMConfigError, match="allowlist"):
        load_profile(
            CANARY_PROFILE_NAME, env={"DEEPSEEK_CANARY_API_KEY_ENV": "MY_CUSTOM_DEEPSEEK_KEY"}
        )

    profile = load_profile(CANARY_PROFILE_NAME, env={"DEEPSEEK_CANARY_API_KEY_ENV": "DEEPSEEK_API_KEY"})
    assert profile.resolve_api_key(env={"DEEPSEEK_API_KEY": SENTINEL_KEY}) == SENTINEL_KEY
