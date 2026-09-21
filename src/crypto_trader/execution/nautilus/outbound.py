"""Outbound translation: Nautilus lifecycle state -> our representation / Phase 3 events.

Mapping is explicit and total: an engine state we do not recognise raises
:class:`TranslationError` instead of being coerced. Engine objects never leave this layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid5

from crypto_trader.events import (
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderStateChanged,
    OrderStateChangePayload,
)

from ..errors import TranslationError
from ..ports import ExecutionObservation, OrderLifecycleState

__all__ = [
    "ENGINE_STATUS_TO_STATE",
    "deterministic_order_uuid",
    "observation_from_fill",
    "observation_from_order",
    "state_from_engine_status",
    "to_phase3_fill_event",
    "to_phase3_order_state_event",
]

#: Explicit, closed mapping from engine status names to our lifecycle states.
ENGINE_STATUS_TO_STATE: dict[str, OrderLifecycleState] = {
    "SUBMITTED": OrderLifecycleState.SUBMITTED,
    "ACCEPTED": OrderLifecycleState.ACCEPTED,
    "PARTIALLY_FILLED": OrderLifecycleState.PARTIALLY_FILLED,
    "FILLED": OrderLifecycleState.FILLED,
    "CANCELED": OrderLifecycleState.CANCELED,
    "REJECTED": OrderLifecycleState.REJECTED,
    "EXPIRED": OrderLifecycleState.EXPIRED,
}

_ORDER_UUID_NAMESPACE = UUID("6f2c1e2a-9a1e-4c7f-9a6c-2f1b6d4e5a10")

PHASE3_PRODUCER = "infrastructure.nautilus"


def deterministic_order_uuid(order_reference: str) -> UUID:
    """Stable mapping from an engine order reference to a domain order UUID.

    Deterministic (uuid5) so the same engine order always maps to the same domain order id,
    and so replaying identical input yields identical domain events.
    """
    if not order_reference:
        raise TranslationError("order reference must not be empty.")
    return uuid5(_ORDER_UUID_NAMESPACE, order_reference)


def state_from_engine_status(status_name: str) -> OrderLifecycleState:
    """Translate an engine status name, failing visibly on anything unmapped."""
    try:
        return ENGINE_STATUS_TO_STATE[status_name]
    except KeyError as exc:
        raise TranslationError(
            f"engine order status {status_name!r} has no truthful mapping to our lifecycle "
            f"states ({', '.join(sorted(ENGINE_STATUS_TO_STATE))})."
        ) from exc


def _decimal_or_none(value) -> Decimal | None:
    if value is None or str(value) == "":
        return None
    return Decimal(str(value))


def observation_from_order(
    order,
    *,
    intent_id: UUID,
    correlation_id: UUID,
    symbol: str,
    producer: str = PHASE3_PRODUCER,
) -> ExecutionObservation:
    """Build an engine-independent observation from the public order object."""
    status_name = getattr(getattr(order, "status", None), "name", None)
    if status_name is None:
        raise TranslationError("order object did not expose a status name.")
    state = state_from_engine_status(str(status_name))
    return ExecutionObservation(
        order_reference=str(order.client_order_id),
        intent_id=intent_id,
        correlation_id=correlation_id,
        symbol=symbol,
        state=state,
        filled_quantity=_decimal_or_none(getattr(order, "filled_qty", None)) or Decimal("0"),
        remaining_quantity=_decimal_or_none(getattr(order, "leaves_qty", None)) or Decimal("0"),
        ts_ns=int(getattr(order, "ts_last", 0) or 0),
        producer=producer,
    )


def observation_from_fill(
    event,
    *,
    intent_id: UUID,
    correlation_id: UUID,
    symbol: str,
    filled_quantity: Decimal,
    remaining_quantity: Decimal,
    state: OrderLifecycleState,
    producer: str = PHASE3_PRODUCER,
) -> ExecutionObservation:
    """Build an engine-independent observation from a fill event."""
    last_qty = _decimal_or_none(getattr(event, "last_qty", None))
    last_px = _decimal_or_none(getattr(event, "last_px", None))
    if last_qty is None or last_px is None:
        raise TranslationError("fill event did not expose last_qty/last_px.")
    if last_qty <= 0 or last_px <= 0:
        raise TranslationError("fill event carried a non-positive quantity or price.")
    commission = getattr(event, "commission", None)
    fee = _decimal_or_none(getattr(commission, "amount", None)) if commission is not None else None
    fee_asset = getattr(getattr(commission, "currency", None), "code", None)
    return ExecutionObservation(
        order_reference=str(event.client_order_id),
        intent_id=intent_id,
        correlation_id=correlation_id,
        symbol=symbol,
        state=state,
        filled_quantity=Decimal(filled_quantity),
        remaining_quantity=Decimal(remaining_quantity),
        last_fill_quantity=last_qty,
        last_fill_price=last_px,
        fee=fee,
        fee_asset=str(fee_asset) if fee_asset else None,
        ts_ns=int(getattr(event, "ts_event", 0)),
        producer=producer,
    )


def _occurred_at(ts_ns: int) -> datetime:
    if ts_ns <= 0:
        raise TranslationError("observation has no simulated timestamp; refusing to guess one.")
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)


def to_phase3_order_state_event(
    observation: ExecutionObservation, *, sequence_number: int | None = None
) -> OrderStateChanged:
    """Translate an observation into the Phase 3 ``order.state.changed`` contract."""
    if observation.state is None:
        raise TranslationError("observation has no lifecycle state to publish.")
    return OrderStateChanged(
        occurred_at=_occurred_at(observation.ts_ns),
        correlation_id=observation.correlation_id,
        producer=observation.producer,
        order_id=deterministic_order_uuid(observation.order_reference),
        symbol=observation.symbol,
        sequence_number=sequence_number,
        payload=OrderStateChangePayload(state=observation.state.value),
    )


def to_phase3_fill_event(observation: ExecutionObservation) -> ExecutionFillRecorded:
    """Translate a fill observation into the Phase 3 ``execution.fill.recorded`` contract."""
    if observation.last_fill_quantity is None or observation.last_fill_price is None:
        raise TranslationError("observation is not a fill; cannot publish a fill event.")
    return ExecutionFillRecorded(
        occurred_at=_occurred_at(observation.ts_ns),
        correlation_id=observation.correlation_id,
        producer=observation.producer,
        order_id=deterministic_order_uuid(observation.order_reference),
        symbol=observation.symbol,
        payload=FillRecordPayload(
            quantity=observation.last_fill_quantity,
            price=observation.last_fill_price,
            fee=observation.fee if observation.fee is not None else Decimal("0"),
            fee_asset=observation.fee_asset,
            remaining_quantity=observation.remaining_quantity,
        ),
    )
