"""Inbound translation: engine-independent input -> Nautilus public types (Phase 4).

Public Nautilus API only: no private methods, no monkey-patching, no vendor modification.
Unsupported combinations raise typed errors instead of being coerced.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import (
    AggressorSide,
    BookAction,
    OrderSide,
    RecordFlag,
    TimeInForce,
)
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from ..errors import TranslationError, UnsupportedExecutionRequestError
from ..ports import (
    BookLevel,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
    MarketBookUpdate,
    MarketTrade,
)

__all__ = [
    "SIDE_TO_ENGINE",
    "TIF_TO_ENGINE",
    "build_order",
    "correlation_tags",
    "to_book_deltas",
    "to_price",
    "to_quantity",
    "to_trade_tick",
]

SIDE_TO_ENGINE = {
    ExecutionSide.BUY: OrderSide.BUY,
    ExecutionSide.SELL: OrderSide.SELL,
}

TIF_TO_ENGINE = {
    ExecutionTimeInForce.GTC: TimeInForce.GTC,
    ExecutionTimeInForce.IOC: TimeInForce.IOC,
    ExecutionTimeInForce.FOK: TimeInForce.FOK,
}

_BOOK_ACTION_TO_ENGINE = {
    "ADD": BookAction.ADD,
    "UPDATE": BookAction.UPDATE,
    "DELETE": BookAction.DELETE,
}


def to_quantity(value: Decimal, instrument: InstrumentSpec) -> Quantity:
    """Exact quantity conversion; refuses values the instrument cannot represent."""
    if not isinstance(value, Decimal) or value <= 0:
        raise UnsupportedExecutionRequestError("quantity must be a positive Decimal.")
    if -value.as_tuple().exponent > instrument.size_precision:
        raise UnsupportedExecutionRequestError(
            f"quantity {value} exceeds size precision {instrument.size_precision}."
        )
    return Quantity.from_str(f"{value:.{instrument.size_precision}f}")


def to_price(value: Decimal, instrument: InstrumentSpec) -> Price:
    """Exact price conversion; refuses values the instrument cannot represent."""
    if not isinstance(value, Decimal) or value <= 0:
        raise UnsupportedExecutionRequestError("price must be a positive Decimal.")
    if -value.as_tuple().exponent > instrument.price_precision:
        raise UnsupportedExecutionRequestError(
            f"price {value} exceeds price precision {instrument.price_precision}."
        )
    return Price.from_str(f"{value:.{instrument.price_precision}f}")


def correlation_tags(request: ExecutionRequest) -> list[str]:
    """Correlation metadata carried on the engine order (public ``tags`` field)."""
    return [f"intent:{request.intent_id}", f"corr:{request.correlation_id}"]


def build_order(order_factory, instrument: InstrumentSpec, request: ExecutionRequest):
    """Create the engine order for a validated request using the public order factory."""
    instrument_id = InstrumentId.from_str(f"{instrument.symbol}.{instrument.venue}")
    try:
        side = SIDE_TO_ENGINE[request.side]
        time_in_force = TIF_TO_ENGINE[request.time_in_force]
    except KeyError as exc:  # pragma: no cover - enums are closed
        raise TranslationError(f"unsupported side/time-in-force: {exc}") from exc

    quantity = to_quantity(request.quantity, instrument)
    tags = correlation_tags(request)

    if request.order_type is ExecutionOrderType.MARKET:
        if request.limit_price is not None:
            raise UnsupportedExecutionRequestError("market orders must not carry a limit_price.")
        return order_factory.market(instrument_id, side, quantity, time_in_force=time_in_force, tags=tags)
    if request.order_type is ExecutionOrderType.LIMIT:
        if request.limit_price is None:
            raise UnsupportedExecutionRequestError("limit orders require a limit_price.")
        price = to_price(request.limit_price, instrument)
        return order_factory.limit(instrument_id, side, quantity, price, time_in_force=time_in_force, tags=tags)
    raise TranslationError(f"unsupported order type {request.order_type!r}.")


def to_book_deltas(update: MarketBookUpdate, instrument: InstrumentSpec) -> OrderBookDeltas:
    """Translate an engine-independent book update into Nautilus order-book deltas."""
    instrument_id = InstrumentId.from_str(f"{instrument.symbol}.{instrument.venue}")
    try:
        action = _BOOK_ACTION_TO_ENGINE[update.action]
    except KeyError as exc:
        raise TranslationError(f"unsupported book action {update.action!r}.") from exc

    deltas = []
    for index, level in enumerate(update.levels):
        if level.side not in ("BUY", "SELL"):
            raise TranslationError(f"unsupported book side {level.side!r}.")
        order = BookOrder(
            side=OrderSide.BUY if level.side == "BUY" else OrderSide.SELL,
            price=to_price(level.price, instrument),
            size=to_quantity(level.size, instrument),
            order_id=level.order_id,
        )
        deltas.append(
            OrderBookDelta(
                instrument_id=instrument_id,
                action=action,
                order=order,
                flags=RecordFlag.F_LAST,
                sequence=update.sequence + index,
                ts_event=update.ts_ns,
                ts_init=update.ts_ns,
            )
        )
    if not deltas:
        raise TranslationError("a book update must contain at least one level.")
    return OrderBookDeltas(instrument_id=instrument_id, deltas=deltas)


def to_trade_tick(trade: MarketTrade, instrument: InstrumentSpec) -> TradeTick:
    """Translate an engine-independent trade into a Nautilus trade tick."""
    instrument_id = InstrumentId.from_str(f"{instrument.symbol}.{instrument.venue}")
    if trade.aggressor not in ("BUYER", "SELLER"):
        raise TranslationError(f"unsupported aggressor {trade.aggressor!r}.")
    return TradeTick(
        instrument_id=instrument_id,
        price=to_price(trade.price, instrument),
        size=to_quantity(trade.size, instrument),
        aggressor_side=AggressorSide.SELLER if trade.aggressor == "SELLER" else AggressorSide.BUYER,
        trade_id=TradeId(str(trade.trade_id)),
        ts_event=trade.ts_ns,
        ts_init=trade.ts_ns,
    )
