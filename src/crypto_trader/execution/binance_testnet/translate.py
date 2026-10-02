"""Translate Testnet order state into our Phase 3 contracts (Phase 11).

Same discipline as the Phase 4 Nautilus outbound adapter: an explicit, total mapping, deterministic
identities (``uuid5``) so replay is stable, and no venue/engine object ever escaping this layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid5

from crypto_trader.events import (
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderStateChanged,
    OrderStateChangePayload,
)
from crypto_trader.recovery.models import LocalOrderState, OrderStateName

from ..ports import OrderLifecycleState
from .models import TestnetOrder

__all__ = [
    "PRODUCER",
    "canonical_fill_event_id",
    "venue_trade_event_id",
    "deterministic_order_uuid",
    "deterministic_fill_event_uuid",
    "lifecycle_state_name",
    "testnet_order_to_phase3_events",
    "to_local_order_state",
]

PRODUCER = "infrastructure.binance_testnet"

_ORDER_NAMESPACE = UUID("9f1b7c34-5d2e-4a83-b6c1-7e0d4f2a9b55")

#: Our lifecycle vocabulary -> the recovery/domain state vocabulary (explicit, total).
_LIFECYCLE_TO_STATE_NAME: dict[OrderLifecycleState, OrderStateName] = {
    OrderLifecycleState.CREATED: OrderStateName.CREATED,
    OrderLifecycleState.SUBMITTED: OrderStateName.SUBMITTED,
    OrderLifecycleState.ACCEPTED: OrderStateName.ACCEPTED,
    OrderLifecycleState.PARTIALLY_FILLED: OrderStateName.PARTIALLY_FILLED,
    OrderLifecycleState.FILLED: OrderStateName.FILLED,
    OrderLifecycleState.CANCELED: OrderStateName.CANCELED,
    OrderLifecycleState.REJECTED: OrderStateName.REJECTED,
    OrderLifecycleState.EXPIRED: OrderStateName.EXPIRED,
}


def legacy_fill_event_ids(client_order_id: str, trade_id: str, count: int) -> set[UUID]:
    """Compatibility with journals written before stable trade-key deduplication.

    Legacy runtime appended fills sequentially. A persisted fill at index N necessarily had
    at least N preceding fill entries for that order. Never rewrite those stored envelopes.
    """
    return {uuid5(_ORDER_NAMESPACE, f"{client_order_id}:fill:{index}:{trade_id}")
            for index in range(count)}


def canonical_fill_event_id(client_order_id: str, trade_id: str) -> UUID:
    """Stable fill identity used by all post-migration Binance fill writers."""
    if not client_order_id or not trade_id:
        raise ValueError("client order id and venue trade id are required.")
    return uuid5(_ORDER_NAMESPACE, f"{client_order_id}:trade:{trade_id}")


def venue_trade_event_id(symbol: str, venue_order_id: str, trade_id: str) -> UUID:
    """Stable identity for one venue trade metadata record."""
    if not symbol or not venue_order_id or not trade_id:
        raise ValueError("symbol, venue order id and venue trade id are required.")
    return uuid5(_ORDER_NAMESPACE, f"{symbol}:venue-trade:{venue_order_id}:{trade_id}")


def deterministic_order_uuid(client_order_id: str) -> UUID:
    """Stable mapping from the client order id (the idempotency key) to a domain order id."""
    if not client_order_id:
        raise ValueError("a client order id is required.")
    return uuid5(_ORDER_NAMESPACE, client_order_id)


def deterministic_fill_event_uuid(client_order_id: str, trade_id: str) -> UUID:
    """Backward-compatible alias for :func:`canonical_fill_event_id`."""
    return canonical_fill_event_id(client_order_id, trade_id)


def lifecycle_state_name(state: OrderLifecycleState) -> OrderStateName:
    """Map our execution lifecycle state onto the recovery/domain state vocabulary."""
    mapped = _LIFECYCLE_TO_STATE_NAME.get(state)
    if mapped is None:
        raise ValueError(f"unmappable lifecycle state {state!r}.")
    return mapped


def to_local_order_state(
    order: TestnetOrder,
    *,
    correlation_id: UUID,
    intent_id: UUID,
    submission_attempted: bool = True,
) -> LocalOrderState:
    """Express a venue order as our recovery/domain local state."""
    state_name = lifecycle_state_name(order.state)
    return LocalOrderState(
        order_id=deterministic_order_uuid(order.client_order_id),
        order_reference=order.client_order_id,
        intent_id=intent_id,
        correlation_id=correlation_id,
        symbol=order.symbol,
        state=state_name,
        filled_quantity=order.executed_quantity,
        remaining_quantity=order.remaining_quantity,
        admitted_locally=True,
        submission_attempted=submission_attempted,
    )


def testnet_order_to_phase3_events(
    order: TestnetOrder,
    *,
    correlation_id: UUID,
    occurred_at: datetime | None = None,
    reason: str | None = None,
) -> tuple[OrderStateChanged, ...]:
    """Build the Phase 3 lifecycle event plus one fill event per newly reported fill."""
    if not order.client_order_id:
        raise ValueError("a client order id is required to build lifecycle events.")
    stamp = occurred_at or order.transact_time
    if stamp is None:
        raise ValueError("a stable venue timestamp is required for an order observation.")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware.")
    order_id = deterministic_order_uuid(order.client_order_id)
    state = order.state.value
    terminal_reason = reason
    if state in {"canceled", "rejected", "expired"} and not (terminal_reason or "").strip():
        terminal_reason = f"venue_status_{order.status.lower()}"

    events: list[OrderStateChanged] = [
        OrderStateChanged(
            event_id=uuid5(
                _ORDER_NAMESPACE,
                f"{order.client_order_id}:state:{state}:{order.status}:"
                f"{stamp.astimezone(timezone.utc).isoformat()}:{order.executed_quantity}:"
                f"{order.cummulative_quote_quantity}:{terminal_reason}",
            ),
            occurred_at=stamp,
            correlation_id=correlation_id,
            producer=PRODUCER,
            order_id=order_id,
            symbol=order.symbol,
            payload=OrderStateChangePayload(state=state, reason=terminal_reason),
        )
    ]
    for fill in order.fills:
        if not fill.trade_id:
            raise ValueError("a stable venue trade id is required to journal a fill.")
        events.append(
            ExecutionFillRecorded(
                event_id=deterministic_fill_event_uuid(order.client_order_id, fill.trade_id),
                occurred_at=stamp,
                correlation_id=correlation_id,
                producer=PRODUCER,
                order_id=order_id,
                symbol=order.symbol,
                payload=FillRecordPayload(
                    quantity=fill.quantity,
                    price=fill.price,
                    fee=fill.commission,
                    fee_asset=fill.commission_asset,
                    remaining_quantity=order.remaining_quantity,
                    liquidity="taker",
                ),
            )
        )
    return tuple(events)
