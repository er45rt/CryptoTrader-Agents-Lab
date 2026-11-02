"""Offline gate proof: ambient opt-in/credentials alone cannot start live Testnet tests.

This file NEVER creates an adapter, makes network requests or sends venue orders.
"""
import sys

import pytest

from crypto_trader.tests import test_testnet_live_integration as live


def _ambient_testnet_credentials(monkeypatch):
    monkeypatch.setenv("CRYPTO_TRADER_RUN_TESTNET", "1")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "fake-never-used")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "fake-never-used")


@pytest.mark.parametrize("argv", [
    ["pytest", "-q"],
    ["pytest", "-m", "testnet"],
    ["pytest", "--allow-live-testnet=1"],
])
def test_ambient_credentials_do_not_activate_live_testnet(monkeypatch, argv):
    _ambient_testnet_credentials(monkeypatch)
    monkeypatch.setattr(sys, "argv", argv)
    assert live.live_run_enabled() is False


def test_inherited_pytest_addopts_is_not_direct_cli_authorization(monkeypatch):
    _ambient_testnet_credentials(monkeypatch)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--allow-live-testnet")
    monkeypatch.setattr(sys, "argv", ["pytest", "-q"])
    assert live.live_run_enabled() is False


def test_explicit_live_cli_requires_both_flag_and_credentials(monkeypatch):
    _ambient_testnet_credentials(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["pytest", "--allow-live-testnet", "-q"])
    assert live.live_run_enabled() is True
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET")
    assert live.live_run_enabled() is False
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "fake-never-used")
    monkeypatch.setenv("CRYPTO_TRADER_RUN_TESTNET", "0")
    assert live.live_run_enabled() is False
