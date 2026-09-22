"""Phase 11: credential handling, redaction and signing hygiene."""

from pathlib import Path

import pytest

from crypto_trader.execution import binance_testnet as bt
from crypto_trader.execution.binance_testnet import (
    API_KEY_ENV_VAR,
    API_SECRET_ENV_VAR,
    TESTNET_ENV_VARS,
    credentials_from_environment,
    normalize_credential_value,
    redact_query_string,
    sign_query_string,
    signed_query_string,
)
from crypto_trader.execution.binance_testnet import (
    TestnetCredentialsRequiredError as CredentialsRequiredError,
)

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "execution" / "binance_testnet"

FAKE_KEY = "fixture-key-value"
FAKE_SECRET = "fixture-secret-value"


def test_the_only_environment_variables_are_the_two_testnet_names():
    assert TESTNET_ENV_VARS == (API_KEY_ENV_VAR, API_SECRET_ENV_VAR)
    assert API_KEY_ENV_VAR == "BINANCE_TESTNET_API_KEY"
    assert API_SECRET_ENV_VAR == "BINANCE_TESTNET_API_SECRET"


def test_credentials_load_from_the_environment():
    loaded = credentials_from_environment(
        {API_KEY_ENV_VAR: FAKE_KEY, API_SECRET_ENV_VAR: FAKE_SECRET}
    )

    assert loaded.api_key == FAKE_KEY
    assert loaded.venue_label == "binance-spot-testnet"


def test_surrounding_whitespace_is_normalized_on_load():
    """A padded value would be rejected by the venue as -2014 API-key format invalid."""
    loaded = credentials_from_environment(
        {
            API_KEY_ENV_VAR: f"  {FAKE_KEY}\n",
            API_SECRET_ENV_VAR: f"\t{FAKE_SECRET}  ",
        }
    )

    assert loaded.api_key == FAKE_KEY
    assert loaded.with_secret() == FAKE_SECRET


def test_one_layer_of_surrounding_quotes_is_removed_on_load():
    loaded = credentials_from_environment(
        {
            API_KEY_ENV_VAR: f'"{FAKE_KEY}"',
            API_SECRET_ENV_VAR: f"'{FAKE_SECRET}'",
        }
    )

    assert loaded.api_key == FAKE_KEY
    assert loaded.with_secret() == FAKE_SECRET


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("   ", ""),
        ("\n\t", ""),
        ("key", "key"),
        ('"key"', "key"),
        ('""', ""),
    ],
)
def test_normalize_credential_value(raw, expected):
    assert normalize_credential_value(raw) == expected


def test_an_all_whitespace_value_counts_as_missing():
    with pytest.raises(CredentialsRequiredError):
        credentials_from_environment({API_KEY_ENV_VAR: "   ", API_SECRET_ENV_VAR: FAKE_SECRET})


@pytest.mark.parametrize(
    "env",
    [
        {},
        {API_KEY_ENV_VAR: FAKE_KEY},
        {API_SECRET_ENV_VAR: FAKE_SECRET},
        {API_KEY_ENV_VAR: "", API_SECRET_ENV_VAR: ""},
    ],
)
def test_missing_credentials_fail_closed_and_name_only_the_variables(env):
    with pytest.raises(CredentialsRequiredError) as error:
        credentials_from_environment(env)

    message = str(error.value)
    assert API_KEY_ENV_VAR in message or API_SECRET_ENV_VAR in message
    assert FAKE_KEY not in message
    assert FAKE_SECRET not in message


def test_repr_and_str_never_expose_credential_values():
    loaded = credentials_from_environment(
        {API_KEY_ENV_VAR: FAKE_KEY, API_SECRET_ENV_VAR: FAKE_SECRET}
    )

    for rendered in (repr(loaded), str(loaded), f"{loaded}"):
        assert FAKE_KEY not in rendered
        assert FAKE_SECRET not in rendered
        assert "<redacted>" in rendered


def test_redacted_dict_has_shape_but_no_values():
    loaded = credentials_from_environment(
        {API_KEY_ENV_VAR: FAKE_KEY, API_SECRET_ENV_VAR: FAKE_SECRET}
    )

    redacted = loaded.to_redacted_dict()

    assert set(redacted) == {"api_key", "api_secret", "venue", "source"}
    assert FAKE_KEY not in str(redacted)
    assert FAKE_SECRET not in str(redacted)


def test_credentials_are_never_written_to_disk_by_the_package():
    """The adapter must not persist credentials anywhere (no file, no journal, no cache)."""
    forbidden = ("open(", "write_text", "json.dump", "pickle", "shelve", "sqlite3")
    for file in PACKAGE_DIR.glob("*.py"):
        text = file.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{file.name} persists state: {token}"


def test_signing_is_hmac_sha256_and_is_deterministic():
    query = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=0.00100000&timestamp=1767225600000"

    first = sign_query_string(query, FAKE_SECRET)
    second = sign_query_string(query, FAKE_SECRET)

    assert first == second
    assert len(first) == 64
    assert sign_query_string(query, FAKE_SECRET + "x") != first
    signed = signed_query_string({"a": 1}, FAKE_SECRET)
    assert signed.startswith("a=1&signature=")


def test_redaction_masks_signature_and_api_key():
    rendered = redact_query_string("symbol=BTCUSDT&signature=deadbeef&apiKey=secret-value")

    assert "deadbeef" not in rendered
    assert "secret-value" not in rendered
    assert "symbol=BTCUSDT" in rendered
    assert rendered.count("<redacted>") == 2


def test_package_exposes_no_withdrawal_endpoint():
    lowered = "\n".join(
        file.read_text(encoding="utf-8").lower() for file in PACKAGE_DIR.glob("*.py")
    )

    for token in ("/sapi/v1/capital/withdraw", "withdraw/apply", "wapi", "universaltransfer"):
        assert token not in lowered
