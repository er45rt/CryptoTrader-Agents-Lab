"""Patch 1: mainnet and Spot Testnet share one fail-closed exchange-filter contract."""

from __future__ import annotations

from copy import deepcopy

import pytest

from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.execution.binance_testnet import TestnetSymbolError as SymbolError
from crypto_trader.mainnet import SymbolFilterError, parse_symbol_filters
from crypto_trader.tests import testnet_fixtures as fx


def _testnet_parse(body: dict):
    response = fx.response(200, deepcopy(body))
    client = fx.client(ScriptedTransport(responses=[response]))
    return client.symbol_rules(fx.SYMBOL)


def test_same_exchange_info_has_the_same_normalized_result_in_both_paths():
    body = deepcopy(fx.exchange_info_response().body)

    mainnet = parse_symbol_filters(deepcopy(body), fx.SYMBOL)
    testnet = _testnet_parse(body)

    assert mainnet.to_record() == testnet.to_record()

@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body["symbols"][0].pop("status"),
        lambda body: body["symbols"][0].__setitem__("status", "BREAK"),
        lambda body: body["symbols"][0].pop("baseAsset"),
        lambda body: body["symbols"][0].pop("quoteAsset"),
        lambda body: body["symbols"][0]["filters"][1].pop("maxQty"),
        lambda body: body["symbols"][0]["filters"].append(
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"}
        ),
        lambda body: body["symbols"][0]["filters"].append(
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"}
        ),
    ],
)
def test_same_invalid_exchange_info_fails_closed_in_both_paths(mutation):
    body = deepcopy(fx.exchange_info_response().body)
    mutation(body)

    with pytest.raises(SymbolFilterError):
        parse_symbol_filters(deepcopy(body), fx.SYMBOL)
    with pytest.raises(SymbolError):
        _testnet_parse(body)
