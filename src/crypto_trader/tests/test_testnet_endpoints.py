"""Phase 11: testnet-only endpoint policy (mainnet is rejected everywhere)."""

from pathlib import Path

import pytest

from crypto_trader.execution.binance_testnet import (
    MAINNET_HOST_MARKERS,
    SPOT_TESTNET_REST_BASE,
    assert_no_mainnet_reference,
    is_testnet_host,
    require_testnet_base_url,
)
# Aliased so pytest does not mistake these error classes for test classes.
from crypto_trader.execution.binance_testnet import (
    MainnetForbiddenError as MainnetError,
    TestnetEndpointError as EndpointError,
)

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "execution" / "binance_testnet"


def test_the_only_declared_base_url_is_the_spot_testnet_host():
    assert SPOT_TESTNET_REST_BASE == "https://testnet.binance.vision"
    assert is_testnet_host(SPOT_TESTNET_REST_BASE) is True
    assert require_testnet_base_url(SPOT_TESTNET_REST_BASE) == SPOT_TESTNET_REST_BASE


@pytest.mark.parametrize(
    "url",
    [
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api-gcp.binance.com",
        "https://fapi.binance.com",
        "https://dapi.binance.com",
        "https://api.binance.us",
        "https://api.binance.com/api/v3/order",
    ],
)
def test_mainnet_endpoints_are_rejected_outright(url):
    with pytest.raises(MainnetError):
        require_testnet_base_url(url)
    assert is_testnet_host(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://testnet.binance.vision",  # https only
        "https://example.com",
        "https://testnet.binance.vision.evil.example",
        "not-a-url",
    ],
)
def test_non_testnet_endpoints_are_rejected(url):
    with pytest.raises(EndpointError):
        require_testnet_base_url(url)


def test_mainnet_reference_guard_is_fail_closed():
    for marker in MAINNET_HOST_MARKERS:
        with pytest.raises(MainnetError):
            assert_no_mainnet_reference(f"https://{marker}")
    assert_no_mainnet_reference(SPOT_TESTNET_REST_BASE)


def test_no_mainnet_host_appears_outside_the_blocklist_definition():
    """The blocklist itself is the one authorised place a mainnet name may appear."""
    offenders: list[str] = []
    for file in PACKAGE_DIR.glob("*.py"):
        if file.name == "endpoints.py":
            continue
        text = file.read_text(encoding="utf-8")
        for marker in MAINNET_HOST_MARKERS:
            if marker in text:
                offenders.append(f"{file.name}:{marker}")
    assert offenders == [], f"mainnet hostnames leaked outside the blocklist: {offenders}"
