"""Phase 15: the strict signed read-only permission contract."""

from __future__ import annotations

import pytest

from crypto_trader.mainnet import (
    REQUIRED_PERMISSION_FIELDS,
    PermissionSchemaError,
    ReadOnlyPermissionError,
    parse_api_restrictions,
    validate_read_only_profile,
)
from crypto_trader.tests import mainnet_fixtures as fx

WRITABLE_FIELDS = [name for name, expected in REQUIRED_PERMISSION_FIELDS.items() if not expected]


def test_a_read_only_profile_parses_and_validates():
    restrictions = parse_api_restrictions(fx.restrictions_body())

    assert restrictions.is_read_only is True
    assert restrictions.enable_reading is True
    assert restrictions.writable_capabilities == ()
    record = restrictions.to_record()
    assert record["is_read_only"] is True
    assert "ipRestrict" in record["observed_fields"]


@pytest.mark.parametrize("missing", sorted(REQUIRED_PERMISSION_FIELDS))
def test_a_missing_required_field_fails_closed(missing: str):
    body = fx.restrictions_body()
    del body[missing]

    with pytest.raises(PermissionSchemaError):
        parse_api_restrictions(body)


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, []])
def test_a_non_boolean_required_field_fails_closed(value):
    body = fx.restrictions_body()
    body["enableSpotAndMarginTrading"] = value

    with pytest.raises(PermissionSchemaError):
        parse_api_restrictions(body)


@pytest.mark.parametrize("field", WRITABLE_FIELDS)
def test_an_enabled_writable_capability_fails_closed(field: str):
    body = fx.restrictions_body()
    body[field] = True

    with pytest.raises(ReadOnlyPermissionError):
        parse_api_restrictions(body)


def test_reading_is_required():
    with pytest.raises(ReadOnlyPermissionError):
        parse_api_restrictions(fx.restrictions_body(enableReading=False))


def test_an_unexpected_writable_capability_fails_closed():
    with pytest.raises(ReadOnlyPermissionError):
        parse_api_restrictions(fx.restrictions_body(enableInternalTransfer=True))


def test_an_unexpected_writable_field_of_the_wrong_type_fails_closed():
    with pytest.raises(PermissionSchemaError):
        parse_api_restrictions(fx.restrictions_body(enableInternalTransfer="yes"))


def test_benign_extra_fields_are_recorded_not_rejected():
    restrictions = parse_api_restrictions(
        fx.restrictions_body(ipRestrict=True, createTime=1_700_000_000_000)
    )

    assert restrictions.is_read_only is True
    assert "createTime" in restrictions.observed_fields


def test_a_non_object_response_fails_closed():
    with pytest.raises(PermissionSchemaError):
        parse_api_restrictions(["not", "an", "object"])
    with pytest.raises(PermissionSchemaError):
        parse_api_restrictions(None)


def test_validate_requires_a_parsed_profile():
    with pytest.raises(PermissionSchemaError):
        validate_read_only_profile({"enableReading": True})  # type: ignore[arg-type]
