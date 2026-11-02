"""Shared fail-closed parsing and validation for Binance Spot symbol rules.

This module is deliberately pure: it performs no network I/O and owns no client state. Both the
mainnet read-only path and the Spot Testnet venue-writing path use it so exchange-rule semantics
cannot drift between adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "MIN_NOTIONAL_FILTER_NAMES",
    "TRADING_STATUS",
    "ExchangeFilterValidationError",
    "ExchangeSymbolFilters",
    "parse_exchange_filters",
    "validate_limit_order",
]

TRADING_STATUS = "TRADING"
MIN_NOTIONAL_FILTER_NAMES = ("NOTIONAL", "MIN_NOTIONAL")

class ExchangeFilterValidationError(ValueError):
    """A required venue filter is missing, malformed, inconsistent or ambiguous."""


def _required_text(value: Any, *, name: str, symbol: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExchangeFilterValidationError(
            f"the {name} field for {symbol} was missing or malformed."
        )
    return value


def _decimal(value: Any, *, name: str, symbol: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeFilterValidationError(
            f"the {name} filter for {symbol} is not a decimal value; refusing to guess."
        ) from exc
    if parsed.is_nan() or parsed.is_infinite():
        raise ExchangeFilterValidationError(f"the {name} filter for {symbol} is not finite.")
    return parsed

@dataclass(frozen=True)
class ExchangeSymbolFilters:
    """Normalized Binance Spot trading rules for one symbol."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal

    @property
    def is_trading(self) -> bool:
        return self.status == TRADING_STATUS

    @property
    def price_precision(self) -> int:
        return max(0, -self.tick_size.normalize().as_tuple().exponent)

    @property
    def size_precision(self) -> int:
        return max(0, -self.step_size.normalize().as_tuple().exponent)

    def to_record(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "tick_size": str(self.tick_size),
            "step_size": str(self.step_size),
            "min_quantity": str(self.min_quantity),
            "max_quantity": str(self.max_quantity),
            "min_notional": str(self.min_notional),
        }


def validate_limit_order(
    filters: ExchangeSymbolFilters, *, price: Decimal, quantity: Decimal
) -> Decimal:
    """Validate one LIMIT order against normalized venue rules and return its notional."""
    if not filters.is_trading:
        raise ExchangeFilterValidationError(
            f"{filters.symbol} is not trading (status={filters.status!r}); refusing an order."
        )
    if not isinstance(price, Decimal) or price <= 0:
        raise ExchangeFilterValidationError("price must be a positive Decimal.")
    if not isinstance(quantity, Decimal) or quantity <= 0:
        raise ExchangeFilterValidationError("quantity must be a positive Decimal.")
    if price % filters.tick_size != 0:
        raise ExchangeFilterValidationError(
            f"price {price} violates the {filters.symbol} tick size {filters.tick_size}."
        )
    if quantity % filters.step_size != 0:
        raise ExchangeFilterValidationError(
            f"quantity {quantity} violates the {filters.symbol} step size {filters.step_size}."
        )
    if quantity < filters.min_quantity:
        raise ExchangeFilterValidationError(
            f"quantity {quantity} is below the {filters.symbol} minimum {filters.min_quantity}."
        )
    if quantity > filters.max_quantity:
        raise ExchangeFilterValidationError(
            f"quantity {quantity} exceeds the {filters.symbol} maximum {filters.max_quantity}."
        )
    notional = quantity * price
    if notional < filters.min_notional:
        raise ExchangeFilterValidationError(
            f"notional {notional} is below the {filters.symbol} minimum {filters.min_notional}."
        )
    return notional

def parse_exchange_filters(body: Any, symbol: str) -> ExchangeSymbolFilters:
    """Parse one symbol from exchangeInfo, rejecting missing or ambiguous venue rules."""
    if not isinstance(body, Mapping):
        raise ExchangeFilterValidationError("the exchangeInfo response was not a JSON object.")
    symbols = body.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ExchangeFilterValidationError("the exchangeInfo response carried no symbol list.")
    entry = next(
        (item for item in symbols if isinstance(item, Mapping) and item.get("symbol") == symbol),
        None,
    )
    if entry is None:
        raise ExchangeFilterValidationError(
            f"symbol {symbol!r} was not present in the exchangeInfo response."
        )

    parsed_symbol = _required_text(entry.get("symbol"), name="symbol", symbol=symbol)
    status = _required_text(entry.get("status"), name="status", symbol=symbol)
    base_asset = _required_text(entry.get("baseAsset"), name="baseAsset", symbol=symbol)
    quote_asset = _required_text(entry.get("quoteAsset"), name="quoteAsset", symbol=symbol)
    if status != TRADING_STATUS:
        raise ExchangeFilterValidationError(
            f"{symbol} is not trading (status={status!r}); refusing to use its rules."
        )

    raw_filters = entry.get("filters")
    if not isinstance(raw_filters, list) or not raw_filters:
        raise ExchangeFilterValidationError(f"the {symbol} filter list was missing or malformed.")
    filters: dict[str, Mapping[str, Any]] = {}
    for item in raw_filters:
        if not isinstance(item, Mapping):
            raise ExchangeFilterValidationError(f"a {symbol} filter entry was malformed.")
        filter_type = item.get("filterType")
        if not isinstance(filter_type, str) or not filter_type:
            raise ExchangeFilterValidationError(
                f"a {symbol} filterType was missing or malformed."
            )
        if filter_type in filters:
            raise ExchangeFilterValidationError(
                f"the {symbol} {filter_type} filter was duplicated; refusing ambiguous rules."
            )
        filters[filter_type] = item

    has_notional = "NOTIONAL" in filters
    has_min_notional = "MIN_NOTIONAL" in filters
    if has_notional and has_min_notional:
        # Project ambiguity policy: Binance exposes distinct NOTIONAL and MIN_NOTIONAL variants.
        # Until an explicit reconciliation contract exists, never choose precedence silently.
        raise ExchangeFilterValidationError(
            f"{symbol} supplied both NOTIONAL and MIN_NOTIONAL; refusing ambiguous rules."
        )

    price_filter = filters.get("PRICE_FILTER")
    lot_filter = filters.get("LOT_SIZE")
    notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
    for name, present in (
        ("PRICE_FILTER", price_filter is not None),
        ("LOT_SIZE", lot_filter is not None),
        ("NOTIONAL", notional_filter is not None),
    ):
        if not present:
            raise ExchangeFilterValidationError(
                f"the {symbol} {name} filter is missing; refusing to guess."
            )
    assert price_filter is not None and lot_filter is not None and notional_filter is not None

    tick_size = _decimal(price_filter.get("tickSize"), name="tickSize", symbol=symbol)
    step_size = _decimal(lot_filter.get("stepSize"), name="stepSize", symbol=symbol)
    min_quantity = _decimal(lot_filter.get("minQty"), name="minQty", symbol=symbol)
    max_quantity = _decimal(lot_filter.get("maxQty"), name="maxQty", symbol=symbol)
    min_notional = _decimal(
        notional_filter.get("minNotional"), name="minNotional", symbol=symbol
    )

    # Project fail-closed policy: Binance may use tickSize == 0 to disable PRICE_FILTER.
    # This runtime intentionally refuses that case instead of inferring disabled-filter semantics.
    if tick_size <= 0:
        raise ExchangeFilterValidationError(
            f"the {symbol} tick size must be greater than zero by project policy."
        )
    if step_size <= 0:
        raise ExchangeFilterValidationError(f"the {symbol} step size must be greater than zero.")
    if min_quantity < 0 or min_notional < 0:
        raise ExchangeFilterValidationError(f"the {symbol} filters carry negative values.")
    if max_quantity <= 0:
        raise ExchangeFilterValidationError(
            f"the {symbol} max quantity must be greater than zero."
        )
    if max_quantity < min_quantity:
        raise ExchangeFilterValidationError(
            f"the {symbol} max quantity is below its minimum quantity."
        )

    return ExchangeSymbolFilters(
        symbol=parsed_symbol,
        status=status,
        base_asset=base_asset,
        quote_asset=quote_asset,
        tick_size=tick_size,
        step_size=step_size,
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        min_notional=min_notional,
    )
