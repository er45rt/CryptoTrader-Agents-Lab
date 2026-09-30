"""Mainnet adapter over the shared fail-closed Binance Spot symbol-filter contract.

The parsing and deterministic validation logic lives in
crypto_trader.execution.exchange_filters so mainnet and testnet cannot drift. This module
preserves the Phase 15 public API and translates neutral validation failures into
SymbolFilterError.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from crypto_trader.execution.exchange_filters import (
    MIN_NOTIONAL_FILTER_NAMES,
    TRADING_STATUS,
    ExchangeFilterValidationError,
    ExchangeSymbolFilters,
    parse_exchange_filters,
    validate_limit_order,
)

from .errors import SymbolFilterError

__all__ = [
    "MIN_NOTIONAL_FILTER_NAMES",
    "TRADING_STATUS",
    "SymbolFilters",
    "parse_symbol_filters",
]


@dataclass(frozen=True)
class SymbolFilters(ExchangeSymbolFilters):
    """Mainnet-compatible view of the shared venue filter contract."""

    def validate_order(self, *, price: Decimal, quantity: Decimal) -> Decimal:
        """Validate one LIMIT order using the shared fail-closed rules."""
        try:
            return validate_limit_order(self, price=price, quantity=quantity)
        except ExchangeFilterValidationError as exc:
            raise SymbolFilterError(str(exc)) from exc


def parse_symbol_filters(body: Any, symbol: str) -> SymbolFilters:
    """Parse one symbol using the shared contract and preserve the mainnet typed error."""
    try:
        parsed = parse_exchange_filters(body, symbol)
    except ExchangeFilterValidationError as exc:
        raise SymbolFilterError(str(exc)) from exc
    return SymbolFilters(
        symbol=parsed.symbol,
        status=parsed.status,
        base_asset=parsed.base_asset,
        quote_asset=parsed.quote_asset,
        tick_size=parsed.tick_size,
        step_size=parsed.step_size,
        min_quantity=parsed.min_quantity,
        max_quantity=parsed.max_quantity,
        min_notional=parsed.min_notional,
    )
