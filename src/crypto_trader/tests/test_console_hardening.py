"""Phase 2 hardening tests: console encoding safety and profile activation state.

Offline only: the provider transport is stubbed, and console streams are replaced
with fakes that reproduce a Windows cp1252 console.
"""

import logging
import sys
import traceback

import pytest

import crewai.llm as crewai_llm_module
from crypto_trader.llm import (
    ACTIVE_PROFILE_NAMES,
    CANARY_PROFILE_NAME,
    LLMProviderError,
    ProviderRateLimiter,
    ResilientLLM,
    ascii_safe,
    available_profiles,
    is_console_encoding_error,
    load_profile,
    recover_original_error,
    tolerant_console_writes,
)

PANEL_TEXT = (
    "\u274c LLM Call Failed\n"
    "\u250c\u2500\u2500\u2500\u2510\n"
    "\u2502 x \u2502\n"
    "\u2514\u2500\u2500\u2500\u2518\n"
)
NON_ASCII_PROVIDER_TEXT = "\u4f60\u597d \u2716 provider exploded"


class StrictConsoleStream:
    """Mimics a Windows cp1252 console: strict encoding plus ``reconfigure``."""

    def __init__(self, encoding: str = "cp1252", errors: str = "strict"):
        self.encoding = encoding
        self.errors = errors
        self.writes: list[str] = []
        self.reconfigure_calls: list[str | None] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        text.encode(self.encoding, self.errors)  # raises under strict + non-encodable text
        return len(text)

    def flush(self) -> None:
        return None

    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None:
        self.reconfigure_calls.append(errors)
        if errors is not None:
            self.errors = errors


class ConsoleProxy:
    """Mimics CrewAI's ``FilteredStream``: reports ``errors`` as None and delegates writes.

    ``io.TextIOBase`` (the proxy's base class) exposes ``errors`` as ``None``, which is
    exactly what made an earlier version of the hardening helper skip the stream.
    """

    def __init__(self, inner: StrictConsoleStream):
        self._original_stream = inner
        self.writes: list[str] = []

    @property
    def encoding(self) -> str:
        return self._original_stream.encoding

    @property
    def errors(self) -> None:
        return None

    def write(self, text: str) -> int:
        self.writes.append(text)
        return self._original_stream.write(text)

    def flush(self) -> None:
        return None


class ProviderBoom(Exception):
    """Stand-in transient provider failure (HTTP 500)."""

    status_code = 500


class ProviderFatal(Exception):
    """Stand-in non-retryable provider failure."""


def _profile(**overrides):
    from dataclasses import replace

    return replace(load_profile(CANARY_PROFILE_NAME, env={}), **overrides)


def _llm(monkeypatch, transport, **profile_overrides):
    monkeypatch.setattr(crewai_llm_module.LLM, "call", transport)
    return ResilientLLM(
        profile=_profile(
            requests_per_minute=None, min_seconds_between_requests=0.0, **profile_overrides
        ),
        api_key="sentinel-not-a-real-key",
        rate_limiter=ProviderRateLimiter(),
        sleeper=lambda _seconds: None,
    )


def _chain_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# --- Console hardening helpers ---

def test_tolerant_console_writes_is_non_fatal_and_restores_handler():
    stream = StrictConsoleStream()

    with tolerant_console_writes([stream]):
        assert stream.write(PANEL_TEXT) == len(PANEL_TEXT)  # no UnicodeEncodeError

    assert stream.errors == "strict"  # handler restored
    with pytest.raises(UnicodeEncodeError):
        stream.write(PANEL_TEXT)


def test_tolerant_console_writes_is_a_noop_when_encoding_can_represent_the_text():
    utf8_stream = StrictConsoleStream(encoding="utf-8")
    already_tolerant = StrictConsoleStream(errors="replace")

    with tolerant_console_writes([utf8_stream, already_tolerant]):
        utf8_stream.write(PANEL_TEXT)
        already_tolerant.write(PANEL_TEXT)

    assert utf8_stream.reconfigure_calls == []
    assert already_tolerant.reconfigure_calls == []


def test_tolerant_console_writes_unwraps_crewai_stream_proxy():
    """Regression: the proxy hides the real handler behind ``errors=None``."""
    inner = StrictConsoleStream()
    proxy = ConsoleProxy(inner)

    with tolerant_console_writes([proxy]):
        assert proxy.write(PANEL_TEXT) == len(PANEL_TEXT)

    assert inner.errors == "strict"  # real stream handler restored


def test_non_ascii_panel_through_crewai_style_proxy_is_not_masked(monkeypatch):
    inner_out, inner_err = StrictConsoleStream(), StrictConsoleStream()
    monkeypatch.setattr(sys, "stdout", ConsoleProxy(inner_out))
    monkeypatch.setattr(sys, "stderr", ConsoleProxy(inner_err))

    def transport(self, messages, **kwargs):
        sys.stdout.write(PANEL_TEXT)
        raise ProviderFatal(NON_ASCII_PROVIDER_TEXT)

    llm = _llm(monkeypatch, transport, max_attempts=1)

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    assert excinfo.value.cause_type == "ProviderFatal"
    assert "UnicodeEncodeError" not in _chain_text(excinfo.value)
    assert inner_out.errors == "strict"  # handler restored on the real stream


def test_ascii_safe_strips_non_encodable_characters():
    assert ascii_safe(NON_ASCII_PROVIDER_TEXT).isascii()
    assert ascii_safe(None) == "None"


def test_encoding_error_detection_and_recovery_helpers():
    encoding_error = UnicodeEncodeError("charmap", "\u274c", 0, 1, "character maps to <undefined>")

    assert is_console_encoding_error(encoding_error) is True
    assert is_console_encoding_error(ValueError("nope")) is False
    assert recover_original_error(encoding_error) is None  # nothing attached


# --- Provider failures are never masked ---

def test_non_ascii_panel_output_does_not_mask_provider_failure(monkeypatch):
    """cp1252 console + non-ASCII error panel => typed provider failure, not encoding error."""
    stdout, stderr = StrictConsoleStream(), StrictConsoleStream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    def transport(self, messages, **kwargs):
        sys.stdout.write(PANEL_TEXT)  # mimics CrewAI's console formatter
        raise ProviderFatal(NON_ASCII_PROVIDER_TEXT)

    llm = _llm(monkeypatch, transport, max_attempts=1)

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    error = excinfo.value
    assert error.cause_type == "ProviderFatal"
    assert isinstance(error.__cause__, ProviderFatal)
    assert "UnicodeEncodeError" not in _chain_text(error)
    assert stdout.errors == "strict"  # restored after the call
    assert any("\u274c" in chunk for chunk in stdout.writes)  # panel reached the stream


def test_encoding_failure_with_provider_context_is_reported_as_provider_error(monkeypatch, caplog):
    def transport(self, messages, **kwargs):
        try:
            raise ProviderFatal("real provider failure")
        except ProviderFatal:
            raise UnicodeEncodeError("charmap", "\u274c", 0, 1, "character maps to <undefined>")

    llm = _llm(monkeypatch, transport, max_attempts=1)

    with caplog.at_level(logging.WARNING, logger="crypto_trader.llm"):
        with pytest.raises(LLMProviderError) as excinfo:
            llm.call("prompt")

    error = excinfo.value
    assert error.cause_type == "ProviderFatal"
    assert isinstance(error.__cause__, ProviderFatal)  # original provider error preserved
    # The surfaced traceback shows the provider failure as the cause...
    assert "ProviderFatal" in _chain_text(error)
    # ...while the encoding failure is retained programmatically and reported in the log.
    assert isinstance(error.__context__, UnicodeEncodeError)
    assert "UnicodeEncodeError" in caplog.text
    assert "ProviderFatal" in caplog.text
    assert llm.last_call_metadata["reporting_failure"] is True


def test_encoding_failure_without_provider_context_is_reported_honestly(monkeypatch):
    def transport(self, messages, **kwargs):
        raise UnicodeEncodeError("charmap", "\u274c", 0, 1, "character maps to <undefined>")

    llm = _llm(monkeypatch, transport, max_attempts=1)

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    error = excinfo.value
    assert error.cause_type == "UnicodeEncodeError"
    assert str(error).isascii()
    assert llm.last_call_metadata["success"] is False
    assert llm.last_call_metadata["reporting_failure"] is True


def test_transient_provider_failure_preserves_original_exception_instance(monkeypatch):
    original = ProviderBoom(NON_ASCII_PROVIDER_TEXT)

    def transport(self, messages, **kwargs):
        raise original

    llm = _llm(monkeypatch, transport, max_attempts=2)

    with pytest.raises(LLMProviderError) as excinfo:
        llm.call("prompt")

    error = excinfo.value
    assert error.__cause__ is original  # same instance: no information lost
    assert error.attempts == 2
    assert error.cause_type == "ProviderBoom"
    assert str(error).isascii()  # our own message can never fail to print


def test_our_error_and_log_output_stay_ascii(monkeypatch, caplog):
    def transport(self, messages, **kwargs):
        raise ProviderBoom(NON_ASCII_PROVIDER_TEXT)

    llm = _llm(monkeypatch, transport, max_attempts=2)

    with caplog.at_level(logging.DEBUG, logger="crypto_trader.llm"):
        with pytest.raises(LLMProviderError) as excinfo:
            llm.call("prompt")

    assert str(excinfo.value).isascii()
    for record in caplog.records:
        assert record.getMessage().isascii(), record.getMessage()


# --- deepseek_reasoner activation state ---

def test_reasoner_profile_is_defined_but_not_active():
    assert "deepseek_reasoner" in available_profiles()
    assert "deepseek_reasoner" not in ACTIVE_PROFILE_NAMES

    profile = load_profile("deepseek_reasoner", env={})
    assert profile.model == "deepseek/deepseek-reasoner"
    assert profile.temperature is None
    assert profile.api_key_env == "DEEPSEEK_API_KEY"


def test_active_profiles_match_the_migrated_agents():
    from crypto_trader.tests.agent_expectations import EXPECTED_PROFILE_NAMES

    assert ACTIVE_PROFILE_NAMES == EXPECTED_PROFILE_NAMES
    assert CANARY_PROFILE_NAME in ACTIVE_PROFILE_NAMES
    assert "deepseek_reasoner" not in ACTIVE_PROFILE_NAMES


def test_reasoner_profile_is_not_referenced_by_runtime_code():
    from pathlib import Path

    package_dir = Path(__file__).resolve().parent.parent
    runtime_files = [path for path in package_dir.rglob("*.py") if "tests" not in path.parts]

    referencing = [
        path.name
        for path in runtime_files
        if "deepseek_reasoner" in path.read_text(encoding="utf-8") and path.name != "config.py"
    ]

    assert referencing == [], f"runtime modules must not use the inactive profile: {referencing}"


def test_active_profiles_are_wired_in_crew_source():
    from pathlib import Path

    from crypto_trader.tests.agent_expectations import AGENT_PROFILE_MAP

    crew_source = (Path(__file__).resolve().parent.parent / "crew.py").read_text(encoding="utf-8")

    constants = {
        "deepseek_canary": "CANARY_PROFILE_NAME",
        "deepseek_analyst": "ANALYST_PROFILE_NAME",
        "deepseek_market": "MARKET_PROFILE_NAME",
    }
    for profile_name in AGENT_PROFILE_MAP.values():
        assert f"get_llm({constants[profile_name]})" in crew_source
