"""Phase 15: dynamic symbol filters (no hard-coded minimum order values)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_trader.mainnet import SymbolFilterError, parse_symbol_filters
from crypto_trader.tests import mainnet_fixtures as fx


def test_filters_are_read_from_the_payload_not_hard_coded():
    first = parse_symbol_filters(
        fx.exchange_info_body(tick="0.01", step="0.00001", min_notional="5"), "BTCUSDT"
    )
    second = parse_symbol_filters(
        fx.exchange_info_body(tick="0.10", step="0.00100", min_notional="25"), "BTCUSDT"
    )

    assert first.tick_size == Decimal("0.01")
    assert first.step_size == Decimal("0.00001")
    assert first.min_notional == Decimal("5")
    assert second.tick_size == Decimal("0.10")
    assert second.step_size == Decimal("0.00100")
    assert second.min_notional == Decimal("25")
    assert second.price_precision == 1
    assert second.size_precision == 3


def test_the_legacy_min_notional_filter_name_is_accepted():
    body = fx.exchange_info_body()
    body["symbols"][0]["filters"][2] = {
        "filterType": "MIN_NOTIONAL",
        "minNotional": "10.00000000",
    }

    parsed = parse_symbol_filters(body, "BTCUSDT")

    assert parsed.min_notional == Decimal("10.00000000")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.pop("symbols"),
        lambda body: body.__setitem__("symbols", []),
        lambda body: body["symbols"][0].__setitem__("filters", []),
        lambda body: body["symbols"][0].__setitem__("filters", [{"filterType": "PRICE_FILTER"}]),
        lambda body: body["symbols"][0].__setitem__("status", None),
        lambda body: body["symbols"][0].__setitem__("symbol", "ETHUSDT"),
    ],
)
def test_malformed_or_missing_payloads_fail_closed(mutation):
    body = fx.exchange_info_body()
    mutation(body)

    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(body, "BTCUSDT")


def test_a_missing_notional_filter_fails_closed():
    body = fx.exchange_info_body()
    body["symbols"][0]["filters"] = body["symbols"][0]["filters"][:2]

    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(body, "BTCUSDT")


def test_a_non_decimal_filter_value_fails_closed():
    body = fx.exchange_info_body()
    body["symbols"][0]["filters"][0]["tickSize"] = "not-a-number"

    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(body, "BTCUSDT")


def test_a_non_positive_tick_or_step_fails_closed():
    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(fx.exchange_info_body(tick="0"), "BTCUSDT")
    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(fx.exchange_info_body(step="0"), "BTCUSDT")


def test_an_inconsistent_max_below_min_quantity_fails_closed():
    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(
            fx.exchange_info_body(min_qty="1.00000000", max_qty="0.50000000"), "BTCUSDT"
        )


def test_a_non_trading_symbol_refuses_an_order():
    parsed = parse_symbol_filters(fx.exchange_info_body(status="BREAK"), "BTCUSDT")

    assert parsed.is_trading is False
    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=Decimal("27000.00"), quantity=Decimal("0.01"))


def test_order_validation_uses_the_live_filters():
    parsed = fx.filters()

    notional = parsed.validate_order(price=Decimal("27000.00"), quantity=Decimal("0.01000"))
    assert notional == Decimal("270.00000000")

    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=Decimal("27000.005"), quantity=Decimal("0.01000"))
    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=Decimal("27000.00"), quantity=Decimal("0.000015"))
    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=Decimal("27000.00"), quantity=Decimal("0.000001"))
    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=Decimal("100.00"), quantity=Decimal("0.00001"))

@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_order_validation_rejects_non_finite_price_and_quantity(bad):
    parsed = fx.filters()
    value = Decimal(bad)

    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=value, quantity=Decimal("0.01"))
    with pytest.raises(SymbolFilterError):
        parsed.validate_order(price=Decimal("27000"), quantity=value)


@pytest.mark.parametrize("field", ["baseAsset", "quoteAsset"])
@pytest.mark.parametrize("bad", [None, "", 123])
def test_filter_parser_requires_real_asset_identifiers(field, bad):
    body = fx.exchange_info_body()
    body["symbols"][0][field] = bad

    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(body, "BTCUSDT")

