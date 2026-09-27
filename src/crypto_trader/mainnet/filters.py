"""Dynamic symbol-filter validation (Phase 15).

No minimum order value, tick or step is hard-coded anywhere: every value is read from the venue's
``exchangeInfo`` payload and validated. A missing, malformed or internally inconsistent filter
fails closed rather than degrading to a permissive default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import SymbolFilterError

__all__ = [
    "MIN_NOTIONAL_FILTER_NAMES",
    "TRADING_STATUS",
    "SymbolFilters",
    "parse_symbol_filters",
]

TRADING_STATUS = "TRADING"

#: Binance has used both names; either is accepted, neither is assumed.
MIN_NOTIONAL_FILTER_NAMES = ("NOTIONAL", "MIN_NOTIONAL")


def _decimal(value: Any, *, name: str, symbol: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SymbolFilterError(
            f"the {name} filter for {symbol} is not a decimal value; refusing to guess."
        ) from exc
    if parsed.is_nan() or parsed.is_infinite():
        raise SymbolFilterError(f"the {name} filter for {symbol} is not finite.")
    return parsed


@dataclass(frozen=True)
class SymbolFilters:
    """The dynamically-read trading rules for one symbol."""

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

    def validate_order(self, *, price: Decimal, quantity: Decimal) -> Decimal:
        """Validate one hypothetical order against the live filters; returns its notional."""
        if not self.is_trading:
            raise SymbolFilterError(
                f"{self.symbol} is not trading (status={self.status!r}); refusing to plan an order."
            )
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise SymbolFilterError("price must be a finite positive Decimal.")
        if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            raise SymbolFilterError("quantity must be a finite positive Decimal.")
        if self.tick_size > 0 and price % self.tick_size != 0:
            raise SymbolFilterError(
                f"price {price} violates the {self.symbol} tick size {self.tick_size}."
            )
        if self.step_size > 0 and quantity % self.step_size != 0:
            raise SymbolFilterError(
                f"quantity {quantity} violates the {self.symbol} step size {self.step_size}."
            )
        if quantity < self.min_quantity:
            raise SymbolFilterError(
                f"quantity {quantity} is below the {self.symbol} minimum {self.min_quantity}."
            )
        if self.max_quantity > 0 and quantity > self.max_quantity:
            raise SymbolFilterError(
                f"quantity {quantity} exceeds the {self.symbol} maximum {self.max_quantity}."
            )
        notional = quantity * price
        if notional < self.min_notional:
            raise SymbolFilterError(
                f"notional {notional} is below the {self.symbol} minimum {self.min_notional}."
            )
        return notional


def parse_symbol_filters(body: Any, symbol: str) -> SymbolFilters:
    """Parse and validate one symbol's filters from an ``exchangeInfo`` payload."""
    if not isinstance(body, Mapping):
        raise SymbolFilterError("the exchangeInfo response was not a JSON object.")
    symbols = body.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise SymbolFilterError("the exchangeInfo response carried no symbol list.")
    entry = next(
        (item for item in symbols if isinstance(item, Mapping) and item.get("symbol") == symbol),
        None,
    )
    if entry is None:
        raise SymbolFilterError(f"symbol {symbol!r} was not present in the exchangeInfo response.")
    status = entry.get("status")
    if not isinstance(status, str) or not status:
        raise SymbolFilterError(f"the {symbol} status was missing or malformed.")
    raw_filters = entry.get("filters")
    if not isinstance(raw_filters, list) or not raw_filters:
        raise SymbolFilterError(f"the {symbol} filter list was missing or malformed.")
    filters: dict[str, Mapping[str, Any]] = {}
    for item in raw_filters:
        if not isinstance(item, Mapping):
            raise SymbolFilterError(f"a {symbol} filter entry was malformed.")
        filter_type = item.get("filterType")
        if isinstance(filter_type, str) and filter_type:
            filters[filter_type] = item

    price_filter = filters.get("PRICE_FILTER")
    lot_filter = filters.get("LOT_SIZE")
    notional_filter = next(
        (filters[name] for name in MIN_NOTIONAL_FILTER_NAMES if name in filters), None
    )
    for name, present in (
        ("PRICE_FILTER", price_filter is not None),
        ("LOT_SIZE", lot_filter is not None),
        ("NOTIONAL", notional_filter is not None),
    ):
        if not present:
            raise SymbolFilterError(f"the {symbol} {name} filter is missing; refusing to guess.")
    assert price_filter is not None and lot_filter is not None and notional_filter is not None

    tick_size = _decimal(price_filter.get("tickSize"), name="tickSize", symbol=symbol)
    step_size = _decimal(lot_filter.get("stepSize"), name="stepSize", symbol=symbol)
    min_quantity = _decimal(lot_filter.get("minQty", "0"), name="minQty", symbol=symbol)
    max_quantity = _decimal(lot_filter.get("maxQty", "0"), name="maxQty", symbol=symbol)
    min_notional = _decimal(notional_filter.get("minNotional"), name="minNotional", symbol=symbol)

    if tick_size <= 0:
        raise SymbolFilterError(f"the {symbol} tick size must be greater than zero.")
    if step_size <= 0:
        raise SymbolFilterError(f"the {symbol} step size must be greater than zero.")
    if min_quantity < 0 or min_notional < 0 or max_quantity < 0:
        raise SymbolFilterError(f"the {symbol} filters carry negative values.")
    if max_quantity > 0 and max_quantity < min_quantity:
        raise SymbolFilterError(f"the {symbol} max quantity is below its minimum quantity.")

    base_asset = entry.get("baseAsset")
    quote_asset = entry.get("quoteAsset")
    if not isinstance(base_asset, str) or not base_asset:
        raise SymbolFilterError(f"the {symbol} base asset was missing or malformed.")
    if not isinstance(quote_asset, str) or not quote_asset:
        raise SymbolFilterError(f"the {symbol} quote asset was missing or malformed.")

    return SymbolFilters(
        symbol=symbol,
        status=status,
        base_asset=base_asset,
        quote_asset=quote_asset,
        tick_size=tick_size,
        step_size=step_size,
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        min_notional=min_notional,
    )
