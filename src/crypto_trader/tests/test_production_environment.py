"""Phase 14: explicit environment selection, mainnet-disabled-by-default, endpoint confusion."""

from __future__ import annotations

from pathlib import Path

import pytest

from crypto_trader.execution.binance_testnet import (
    SPOT_TESTNET_REST_BASE,
    MainnetForbiddenError,
    require_testnet_base_url,
)
from crypto_trader.production import (
    DEFAULT_ENVIRONMENT,
    ENVIRONMENT_ENV_VAR,
    MAINNET_ENABLED,
    EnvironmentEndpointMismatchError,
    MainnetDisabledError,
    ProductionConfigError,
    TradingEnvironment,
    assert_endpoint_matches_environment,
    endpoint_for,
    parse_environment,
    resolve_environment,
)

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PRODUCTION_DIR = PACKAGE_DIR / "production"

#: Synthetic, non-secret host used to prove cross-environment refusal (never contacted).
MAINNET_HOST = "https://api.binance.com"


def test_the_default_environment_is_testnet_with_the_documented_host():
    decision = resolve_environment(environ={})

    assert DEFAULT_ENVIRONMENT == "testnet"
    assert decision.environment is TradingEnvironment.TESTNET
    assert decision.base_url == SPOT_TESTNET_REST_BASE
    assert decision.is_testnet is True


def test_production_is_disabled_by_default_in_this_phase():
    assert MAINNET_ENABLED is False
    assert parse_environment("mainnet") is TradingEnvironment.MAINNET


def test_unknown_environment_values_fail_closed_rather_than_guessing():
    with pytest.raises(ProductionConfigError):
        parse_environment("banana")
    with pytest.raises(ProductionConfigError):
        parse_environment("")
    with pytest.raises(ProductionConfigError):
        parse_environment(123)  # type: ignore[arg-type]


def test_the_environment_can_be_selected_explicitly_by_configuration():
    decision = resolve_environment(environ={ENVIRONMENT_ENV_VAR: "testnet"})

    assert decision.environment is TradingEnvironment.TESTNET
    assert decision.requested == "testnet"


@pytest.mark.parametrize("alias", ["mainnet", "live", "production"])
def test_a_production_configuration_value_is_refused_before_anything_else(alias: str):
    with pytest.raises(MainnetDisabledError):
        resolve_environment(environ={ENVIRONMENT_ENV_VAR: alias})


def test_an_owner_flag_cannot_enable_production_in_this_phase():
    """Even an explicit authorization flag plus a base URL cannot reach a production endpoint."""
    with pytest.raises(MainnetDisabledError):
        resolve_environment("mainnet", mainnet_authorized=True, mainnet_base_url=MAINNET_HOST)


def test_a_production_request_fails_before_any_credential_or_network_activity(monkeypatch):
    calls: list[str] = []

    def exploded(self, request):  # pragma: no cover - must never be reached
        calls.append("send")
        raise AssertionError("a production request must not reach a transport")

    from crypto_trader.execution.binance_testnet import RequestsTransport

    monkeypatch.setattr(RequestsTransport, "send", exploded)
    with pytest.raises(MainnetDisabledError):
        resolve_environment("mainnet")
    assert calls == []


def test_testnet_cannot_silently_become_production():
    with pytest.raises(EnvironmentEndpointMismatchError):
        assert_endpoint_matches_environment(TradingEnvironment.TESTNET, MAINNET_HOST)
    with pytest.raises(MainnetForbiddenError):
        require_testnet_base_url(MAINNET_HOST)


def test_a_production_environment_refuses_a_testnet_host():
    with pytest.raises(EnvironmentEndpointMismatchError):
        assert_endpoint_matches_environment(TradingEnvironment.MAINNET, SPOT_TESTNET_REST_BASE)


def test_the_testnet_endpoint_is_the_documented_host():
    assert endpoint_for(TradingEnvironment.TESTNET) == SPOT_TESTNET_REST_BASE
    assert (
        assert_endpoint_matches_environment(TradingEnvironment.TESTNET, SPOT_TESTNET_REST_BASE)
        == SPOT_TESTNET_REST_BASE
    )


def test_the_environment_selector_is_not_a_credential_variable():
    assert ENVIRONMENT_ENV_VAR == "CRYPTO_TRADER_ENV"
    for token in ("KEY", "SECRET", "TOKEN", "PASSWORD"):
        assert token not in ENVIRONMENT_ENV_VAR


def test_the_production_package_invents_no_production_url_of_its_own():
    """The production path requires an owner-supplied host; it hard-codes no production URL."""
    source = "\n".join(
        file.read_text(encoding="utf-8").lower()
        for file in PRODUCTION_DIR.rglob("*.py")
        if "__pycache__" not in file.parts
    )

    for token in ("api.binance.com", "binance.us", "/sapi/", "fapi."):
        assert token not in source, f"the production package hard-codes {token}"
