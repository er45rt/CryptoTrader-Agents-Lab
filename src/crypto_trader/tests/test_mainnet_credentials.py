"""Phase 15: dedicated mainnet READ-ONLY credentials, never reused from testnet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_trader.mainnet import (
    API_KEY_ENV_VAR,
    API_SECRET_ENV_VAR,
    MAINNET_READONLY_ENV_VARS,
    MainnetReadOnlyCredentialsRequiredError,
    credentials_from_environment,
    normalize_credential_value,
)
from crypto_trader.tests import mainnet_fixtures as fx

MAINNET_DIR = Path(__file__).resolve().parents[1] / "mainnet"


def test_credentials_come_only_from_the_dedicated_variables():
    credentials = credentials_from_environment(
        {API_KEY_ENV_VAR: "key-value", API_SECRET_ENV_VAR: "secret-value"}
    )

    assert credentials.api_key == "key-value"
    assert credentials.venue_label == "binance-spot-mainnet-readonly"
    assert credentials.permission_scope == "read_only"
    assert MAINNET_READONLY_ENV_VARS == (API_KEY_ENV_VAR, API_SECRET_ENV_VAR)
    assert API_KEY_ENV_VAR == "BINANCE_MAINNET_READONLY_API_KEY"
    assert API_SECRET_ENV_VAR == "BINANCE_MAINNET_READONLY_API_SECRET"


def test_missing_credentials_fail_closed_and_name_only_the_variables():
    with pytest.raises(MainnetReadOnlyCredentialsRequiredError) as raised:
        credentials_from_environment({})

    message = str(raised.value)
    assert API_KEY_ENV_VAR in message
    assert API_SECRET_ENV_VAR in message


def test_testnet_credentials_are_never_read_or_reused():
    """A populated testnet environment still fails: the mainnet loader reads only its own names."""
    env = {
        "BINANCE_TESTNET_API_KEY": "testnet-key",
        "BINANCE_TESTNET_API_SECRET": "testnet-secret",
    }

    with pytest.raises(MainnetReadOnlyCredentialsRequiredError):
        credentials_from_environment(env)


def test_credentials_are_redacted_in_every_representation():
    credentials = fx.credentials()
    rendered = " ".join(
        (repr(credentials), str(credentials), json.dumps(credentials.to_redacted_dict()))
    )

    assert fx.FAKE_API_KEY not in rendered
    assert fx.FAKE_API_SECRET not in rendered
    assert credentials.to_redacted_dict()["api_key"] != fx.FAKE_API_KEY
    # The secret is reachable only for signing.
    assert credentials.with_secret() == fx.FAKE_API_SECRET


def test_blank_credential_values_fail_closed():
    with pytest.raises(MainnetReadOnlyCredentialsRequiredError):
        credentials_from_environment({API_KEY_ENV_VAR: "   ", API_SECRET_ENV_VAR: "value"})
    with pytest.raises(MainnetReadOnlyCredentialsRequiredError):
        credentials_from_environment({API_KEY_ENV_VAR: "value", API_SECRET_ENV_VAR: ""})


def test_credential_normalisation_trims_whitespace_and_one_quote_layer():
    assert normalize_credential_value('  "abc"  ') == "abc"
    assert normalize_credential_value("'abc'") == "abc"
    assert normalize_credential_value(None) == ""


def test_the_credentials_module_never_persists_a_value():
    source = (MAINNET_DIR / "credentials.py").read_text(encoding="utf-8")

    for token in ("write_text(", "json.dump(", "pickle", "open(", "os.replace"):
        assert token not in source, f"credentials.py persists state: {token}"
