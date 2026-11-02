"""Pure deterministic planning for pre-start Binance trade backfill (G4).

This module performs no network I/O and no persistence writes. It reconciles an already captured
journal view with strict per-order Binance order/trade evidence and returns the exact event batch
that a separately fenced maintenance writer may append atomically.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import hashlib
from json import dumps as encode_json
from typing import TypeAlias
from uuid import UUID

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    VENUE_TRADE_EVENT_TYPE,
    EventEnvelope,
    ExecutionFillRecorded,
    FillRecordPayload,
    VenueTradeRecorded,
    VenueTradeRecordPayload,
)
from crypto_trader.recovery import OrderStateReconstructor, RecoveryError

from .errors import TradeBackfillError
from .models import STATUS_TO_STATE, TestnetOrder, VenueTrade
from .translate import (
    canonical_fill_event_id,
    legacy_fill_event_ids,
    venue_trade_event_id,
)

BackfillEvent: TypeAlias = ExecutionFillRecorded | VenueTradeRecorded

__all__ = [
    "BackfillEvent",
    "TradeBackfillPlan",
    "plan_trade_backfill",
    "trade_backfill_plan_fingerprint",
    "trade_backfill_source_revision",
]


@dataclass(frozen=True)
class TradeBackfillPlan:
    """An immutable, side-effect-free event plan bound to one exact journal view."""

    events: tuple[BackfillEvent, ...]
    references: tuple[str, ...]
    source_entries: tuple[tuple[UUID, str], ...]
    source_fingerprint: str
    plan_fingerprint: str

    @property
    def source_event_ids(self) -> tuple[UUID, ...]:
        return tuple(event_id for event_id, _digest in self.source_entries)

    @property
    def is_empty(self) -> bool:
        return not self.events

    @property
    def fill_count(self) -> int:
        return sum(event.event_type == EXECUTION_FILL_EVENT_TYPE for event in self.events)

    @property
    def venue_trade_count(self) -> int:
        return sum(event.event_type == VENUE_TRADE_EVENT_TYPE for event in self.events)


def _fail(message: str) -> TradeBackfillError:
    return TradeBackfillError(message)


def _validate_fee_assets(values: Collection[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise _fail("allowed fee assets must be a collection of asset codes, not a string.")
    assets = frozenset(values)
    if not assets:
        raise _fail("trade backfill requires at least one allowed fee asset.")
    for asset in assets:
        if (
            not isinstance(asset, str)
            or not asset.isascii()
            or asset != asset.upper()
            or not asset.isalnum()
        ):
            raise _fail("allowed fee assets must be uppercase ASCII alphanumeric codes.")
    return assets


def _existing_fill_matches_trade(event: EventEnvelope, trade: VenueTrade) -> None:
    if event.event_type != EXECUTION_FILL_EVENT_TYPE:
        raise _fail("matched fill identity decoded to the wrong event type.")
    payload = event.payload
    if payload.quantity != trade.quantity or payload.price != trade.price:
        raise _fail("durable fill economics disagree with authoritative venue trade evidence.")
    if payload.fee != trade.commission:
        raise _fail("durable fill fee disagrees with authoritative venue trade evidence.")
    if trade.commission != 0:
        if payload.fee_asset != trade.commission_asset:
            raise _fail("durable fill fee asset disagrees with authoritative venue trade evidence.")
    elif payload.fee_asset not in {None, trade.commission_asset}:
        raise _fail("durable zero-fee fill carries a conflicting fee asset.")
    # Historical runtime fills always wrote liquidity="taker", even for maker executions.
    # Do not reinterpret that legacy field as authoritative venue evidence. The exact maker/taker
    # role is durably captured by the linked venue.trade.recorded event planned below.


def _event_ns(event: EventEnvelope) -> int:
    return int(event.occurred_at.timestamp() * 1_000_000_000)


def _journal_replay_key(event: EventEnvelope) -> tuple[int, int, str]:
    return (
        _event_ns(event),
        0 if event.sequence_number is None else event.sequence_number,
        str(event.event_id),
    )


def _payload_digest(event: EventEnvelope) -> str:
    return hashlib.sha256(event.to_json().encode("utf-8")).hexdigest()


def _revision_fingerprint(entries: Sequence[tuple[UUID, str]]) -> str:
    material = tuple((str(event_id), digest) for event_id, digest in entries)
    return hashlib.sha256(
        encode_json(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def trade_backfill_source_revision(
    journal_events: Sequence[EventEnvelope],
) -> tuple[tuple[tuple[UUID, str], ...], str]:
    """Return exact event-json digests matching the persistence JournalRevision format."""
    entries = tuple(
        sorted(
            ((event.event_id, _payload_digest(event)) for event in journal_events),
            key=lambda item: str(item[0]),
        )
    )
    return entries, _revision_fingerprint(entries)


def trade_backfill_plan_fingerprint(
    *,
    events: Sequence[BackfillEvent],
    references: Sequence[str],
    source_fingerprint: str,
) -> str:
    """Bind the exact planned events, references and source revision into one digest."""
    material = {
        "source_fingerprint": source_fingerprint,
        "references": tuple(references),
        "events": tuple((str(event.event_id), _payload_digest(event)) for event in events),
    }
    return hashlib.sha256(
        encode_json(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_trade_values(trade: VenueTrade) -> None:
    for label, value, allow_zero in (
        ("price", trade.price, False),
        ("quantity", trade.quantity, False),
        ("quote_quantity", trade.quote_quantity, False),
        ("commission", trade.commission, True),
    ):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise _fail(f"venue trade {label} must be a finite Decimal.")
        if value < 0 or (not allow_zero and value == 0):
            raise _fail(f"venue trade {label} has an invalid sign.")
    if (
        not isinstance(trade.commission_asset, str)
        or not trade.commission_asset.isascii()
        or trade.commission_asset != trade.commission_asset.upper()
        or not trade.commission_asset.isalnum()
    ):
        raise _fail("venue trade commission asset must be uppercase ASCII alphanumeric.")
    if trade.trade_time.tzinfo is None or trade.trade_time.utcoffset() is None:
        raise _fail("venue trade time must be timezone-aware.")


def _prove_replay(
    journal_events: Sequence[EventEnvelope],
    planned: Sequence[BackfillEvent],
    *,
    orders: Mapping[str, TestnetOrder],
    anchors,
) -> None:
    rebuilt = OrderStateReconstructor()
    try:
        for event in sorted((*journal_events, *planned), key=_journal_replay_key):
            rebuilt.handle(event)
    except RecoveryError as exc:
        raise _fail(f"planned trade backfill would make journal replay fail: {exc}") from exc
    conflicts = rebuilt.conflicts()
    if conflicts:
        raise _fail(f"planned trade backfill would create replay conflicts: {conflicts!r}.")
    for reference, order in orders.items():
        state = rebuilt.state_for(reference)
        anchor = anchors[reference]
        if state is None:
            raise _fail(f"planned trade backfill lost reconstructed state for {reference!r}.")
        expected_remaining = anchor.quantity - order.executed_quantity
        if (
            state.filled_quantity != order.executed_quantity
            or state.remaining_quantity != expected_remaining
        ):
            raise _fail(
                f"planned trade backfill replay does not match authoritative quantities for {reference!r}."
            )


def plan_trade_backfill(
    journal_events: Sequence[EventEnvelope],
    *,
    orders: Mapping[str, TestnetOrder],
    trades_by_reference: Mapping[str, Sequence[VenueTrade]],
    allowed_fee_assets: Collection[str],
) -> TradeBackfillPlan:
    """Plan missing fill/trade events from authoritative per-order venue evidence.

    Coverage policy is intentionally external: the caller decides which durable attempted orders
    must be included. For every supplied reference this planner requires one durable attempt anchor,
    one strict order snapshot and one complete myTrades sequence. It never guesses missing values.
    """

    targets = tuple(sorted(orders))
    if set(trades_by_reference) != set(targets):
        raise _fail("trade evidence coverage must exactly match supplied order evidence.")
    fee_assets = _validate_fee_assets(allowed_fee_assets)

    events_by_id = {event.event_id: event for event in journal_events}
    if len(events_by_id) != len(journal_events):
        raise _fail("journal evidence contains duplicate event identities.")

    rebuilt = OrderStateReconstructor()
    try:
        for event in sorted(journal_events, key=_journal_replay_key):
            rebuilt.handle(event)
    except RecoveryError as exc:
        raise _fail(f"durable order history is not reconstructable: {exc}") from exc
    conflicts = rebuilt.conflicts()
    if conflicts:
        raise _fail(f"durable order history contains unresolved conflicts: {conflicts!r}.")
    anchors = rebuilt.attempt_anchor_map()

    planned: list[BackfillEvent] = []
    planned_ids = set()
    venue_order_owners: dict[str, str] = {}

    for reference in targets:
        anchor = anchors.get(reference)
        if anchor is None:
            raise _fail(f"trade backfill reference {reference!r} has no durable attempt anchor.")
        if anchor.correlation_id is None:
            raise _fail(f"trade backfill reference {reference!r} has no durable correlation id.")

        order = orders[reference]
        trades = tuple(trades_by_reference[reference])
        if order.client_order_id != reference:
            raise _fail("order evidence client identity does not match the requested reference.")
        if order.symbol != anchor.symbol:
            raise _fail("order evidence symbol does not match the durable attempt anchor.")
        if order.original_quantity != anchor.quantity:
            raise _fail("order evidence original quantity does not match the durable attempt anchor.")
        if order.venue_order_id is None:
            raise _fail("order evidence is missing the venue order id.")
        if (
            not order.venue_order_id.isascii()
            or not order.venue_order_id.isdigit()
            or order.venue_order_id.startswith("0")
        ):
            raise _fail("venue order id must be a canonical positive ASCII integer.")
        owner = venue_order_owners.get(order.venue_order_id)
        if owner is not None and owner != reference:
            raise _fail("the same venue order id cannot belong to multiple durable references.")
        venue_order_owners[order.venue_order_id] = reference
        if order.executed_quantity < 0 or order.executed_quantity > anchor.quantity:
            raise _fail("order evidence executed quantity is outside the durable quantity bounds.")
        if STATUS_TO_STATE.get(order.status) is not order.state:
            raise _fail("order evidence status and lifecycle state disagree.")
        if (
            (order.status == "FILLED" and order.executed_quantity != anchor.quantity)
            or (order.status in {"NEW", "REJECTED"} and order.executed_quantity != 0)
            or (
                order.status == "PARTIALLY_FILLED"
                and not Decimal("0") < order.executed_quantity < anchor.quantity
            )
            or (
                order.status in {"CANCELED", "PENDING_CANCEL", "EXPIRED", "EXPIRED_IN_MATCH"}
                and order.executed_quantity >= anchor.quantity
            )
        ):
            raise _fail("order evidence status and executed quantity are inconsistent.")

        side = anchor.side
        if side not in {"buy", "sell"}:
            raise _fail("durable attempt side is not supported by trade backfill.")
        expected_is_buyer = side == "buy"

        trade_ids: list[int] = []
        total_quantity = Decimal("0")
        total_quote = Decimal("0")
        previous_trade_ns: int | None = None
        for trade in trades:
            _validate_trade_values(trade)
            if trade.quote_quantity != trade.price * trade.quantity:
                raise _fail("venue trade quote quantity must equal price multiplied by quantity.")
            trade_ns = int(trade.trade_time.timestamp() * 1_000_000_000)
            if trade_ns < anchor.occurred_at_ns:
                raise _fail("venue trade time precedes the durable submission attempt.")
            if previous_trade_ns is not None and trade_ns < previous_trade_ns:
                raise _fail("venue trade time moved backwards relative to trade-id order.")
            previous_trade_ns = trade_ns
            if trade.symbol != anchor.symbol or trade.venue_order_id != order.venue_order_id:
                raise _fail("venue trade scope does not match the durable order identity.")
            if trade.is_buyer is not expected_is_buyer:
                raise _fail("venue trade buyer/seller role contradicts the durable attempt side.")
            if trade.commission_asset not in fee_assets:
                raise _fail(
                    f"venue trade fee asset {trade.commission_asset!r} has no approved quote conversion."
                )
            if (
                not trade.trade_id.isascii()
                or not trade.trade_id.isdigit()
                or trade.trade_id.startswith("0")
            ):
                raise _fail("venue trade id must be a canonical positive ASCII integer.")
            trade_ids.append(int(trade.trade_id))
            total_quantity += trade.quantity
            total_quote += trade.quote_quantity

        if trade_ids != sorted(trade_ids) or len(trade_ids) != len(set(trade_ids)):
            raise _fail("venue trade ids must be strictly ordered and unique.")
        if total_quantity != order.executed_quantity:
            raise _fail("venue trade quantity sum does not equal authoritative executedQty.")
        if total_quote != order.cummulative_quote_quantity:
            raise _fail("venue trade quote sum does not equal authoritative cummulativeQuoteQty.")
        if order.executed_quantity == 0 and trades:
            raise _fail("zero-executed order unexpectedly contains venue trades.")
        if order.executed_quantity > 0 and not trades:
            raise _fail("executed order is missing authoritative venue trades.")

        existing_fills = tuple(
            event
            for event in journal_events
            if event.event_type == EXECUTION_FILL_EVENT_TYPE and event.order_id == anchor.order_id
        )
        existing_trade_events = tuple(
            event
            for event in journal_events
            if event.event_type == VENUE_TRADE_EVENT_TYPE and event.order_id == anchor.order_id
        )
        matched_fill_ids = set()
        matched_trade_event_ids = set()
        cumulative_quantity = Decimal("0")

        for trade_index, trade in enumerate(trades, start=1):
            cumulative_quantity += trade.quantity
            remaining = anchor.quantity - cumulative_quantity
            if remaining < 0:
                raise _fail("venue trades exceed the durable original order quantity.")

            canonical_fill_id = canonical_fill_event_id(reference, trade.trade_id)
            candidate_ids = {canonical_fill_id} | legacy_fill_event_ids(
                reference, trade.trade_id, len(existing_fills)
            )
            fill_matches = tuple(
                event for event in existing_fills if event.event_id in candidate_ids
            )
            if len(fill_matches) > 1:
                raise _fail("multiple durable fills map to one authoritative venue trade.")

            expected_liquidity = "maker" if trade.is_maker else "taker"
            for candidate_id in candidate_ids:
                candidate = events_by_id.get(candidate_id)
                if candidate is not None and (
                    candidate.event_type != EXECUTION_FILL_EVENT_TYPE
                    or candidate.order_id != anchor.order_id
                ):
                    raise _fail(
                        "a canonical or legacy fill identity collides with another journal event."
                    )
            if fill_matches:
                fill_event = fill_matches[0]
                if (
                    fill_event.order_id != anchor.order_id
                    or fill_event.symbol != anchor.symbol
                    or fill_event.producer != anchor.venue_producer
                ):
                    raise _fail("durable fill identity/provenance disagrees with the attempt anchor.")
                _existing_fill_matches_trade(fill_event, trade)
                fill_event_id = fill_event.event_id
                matched_fill_ids.add(fill_event_id)
            else:
                fill_event_id = canonical_fill_id
                if fill_event_id in events_by_id:
                    raise _fail("planned fill event id collides with another journal event.")
                fill_event = ExecutionFillRecorded(
                    event_id=fill_event_id,
                    occurred_at=trade.trade_time,
                    correlation_id=anchor.correlation_id,
                    producer=anchor.venue_producer,
                    strategy_id=anchor.strategy_id,
                    order_id=anchor.order_id,
                    symbol=anchor.symbol,
                    sequence_number=trade_index,
                    payload=FillRecordPayload(
                        quantity=trade.quantity,
                        price=trade.price,
                        fee=trade.commission,
                        fee_asset=trade.commission_asset,
                        remaining_quantity=remaining,
                        liquidity=expected_liquidity,
                    ),
                )
                if fill_event.event_id in planned_ids:
                    raise _fail("planned event identities must be unique.")
                planned_ids.add(fill_event.event_id)
                planned.append(fill_event)

            trade_event_id = venue_trade_event_id(
                anchor.symbol, order.venue_order_id, trade.trade_id
            )
            if trade_event_id in events_by_id and not any(
                event.event_id == trade_event_id for event in existing_trade_events
            ):
                raise _fail("planned venue-trade event id collides with another journal event.")
            trade_event = VenueTradeRecorded(
                event_id=trade_event_id,
                occurred_at=trade.trade_time,
                correlation_id=anchor.correlation_id,
                causation_id=fill_event_id,
                producer=anchor.venue_producer,
                strategy_id=anchor.strategy_id,
                order_id=anchor.order_id,
                symbol=anchor.symbol,
                payload=VenueTradeRecordPayload(
                    venue_reference=reference,
                    venue_order_id=order.venue_order_id,
                    venue_trade_id=trade.trade_id,
                    fill_event_id=fill_event_id,
                    price=trade.price,
                    quantity=trade.quantity,
                    quote_quantity=trade.quote_quantity,
                    commission=trade.commission,
                    commission_asset=trade.commission_asset,
                    trade_time=trade.trade_time,
                    is_buyer=trade.is_buyer,
                    is_maker=trade.is_maker,
                ),
            )
            existing_trade = tuple(
                event
                for event in existing_trade_events
                if event.event_id == trade_event.event_id
            )
            if len(existing_trade) > 1:
                raise _fail("duplicate venue-trade metadata identities exist in the journal.")
            if existing_trade:
                if existing_trade[0] != trade_event:
                    raise _fail("durable venue-trade metadata conflicts with authoritative evidence.")
                matched_trade_event_ids.add(trade_event.event_id)
            else:
                if trade_event.event_id in planned_ids:
                    raise _fail("planned event identities must be unique.")
                planned_ids.add(trade_event.event_id)
                planned.append(trade_event)

        if {event.event_id for event in existing_fills} != matched_fill_ids:
            raise _fail("durable fill history contains a fill not represented by authoritative myTrades.")
        if {event.event_id for event in existing_trade_events} != matched_trade_event_ids:
            raise _fail(
                "durable venue-trade metadata contains a trade not represented by authoritative myTrades."
            )

    _prove_replay(
        journal_events,
        planned,
        orders=orders,
        anchors=anchors,
    )
    source_entries, source_fingerprint = trade_backfill_source_revision(journal_events)
    planned_events = tuple(planned)
    return TradeBackfillPlan(
        events=planned_events,
        references=targets,
        source_entries=source_entries,
        source_fingerprint=source_fingerprint,
        plan_fingerprint=trade_backfill_plan_fingerprint(
            events=planned_events,
            references=targets,
            source_fingerprint=source_fingerprint,
        ),
    )
