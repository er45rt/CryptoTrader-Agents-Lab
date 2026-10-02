"""Durable production account-equity chain reconstruction (G5b).

This module is read-only: it never contacts a venue and never appends to the journal. It validates
the cross-event invariants that a single AccountEquityRecorded event cannot prove by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from crypto_trader.events import ACCOUNT_EQUITY_EVENT_TYPE, AccountEquityRecorded
from crypto_trader.persistence import EventJournal

from .errors import ProductionStateError

__all__ = ["EquityChainState", "load_equity_chain"]


@dataclass(frozen=True)
class EquityChainState:
    """The latest fully validated active equity chain."""

    baseline_event_id: UUID
    last_event_id: UUID
    last_kind: str
    symbol: str
    environment: str
    venue_account_id: str
    base_asset: str
    quote_asset: str
    peak_equity_quote: Decimal
    last_equity_quote: Decimal
    fence_term: int
    last_occurred_at: datetime


def _state_from(event: AccountEquityRecorded) -> EquityChainState:
    payload = event.payload
    return EquityChainState(
        baseline_event_id=payload.baseline_event_id,
        last_event_id=event.event_id,
        last_kind=payload.kind,
        symbol=event.symbol or "",
        environment=payload.environment,
        venue_account_id=payload.venue_account_id,
        base_asset=payload.base_asset,
        quote_asset=payload.quote_asset,
        peak_equity_quote=payload.peak_equity_quote,
        last_equity_quote=payload.equity_quote,
        fence_term=payload.fence_term,
        last_occurred_at=event.occurred_at,
    )


def load_equity_chain(
    journal: EventJournal,
    *,
    symbol: str,
    environment: str,
    venue_account_id: str,
    base_asset: str,
    quote_asset: str,
) -> EquityChainState:
    """Rebuild and validate the latest account-equity chain, failing closed on any gap."""

    events = tuple(
        event
        for event in journal.read_range()
        if event.event_type == ACCOUNT_EQUITY_EVENT_TYPE
    )
    if not events:
        raise ProductionStateError("canonical risk state has no durable account-equity baseline.")

    current: EquityChainState | None = None
    for raw in events:
        if not isinstance(raw, AccountEquityRecorded):
            raise ProductionStateError("account-equity journal event decoded to an unexpected contract.")
        event = raw
        payload = event.payload

        if payload.kind == "baseline":
            if current is not None:
                raise ProductionStateError(
                    "a second baseline cannot replace an active equity chain; use rebaseline."
                )
            current = _state_from(event)
            continue

        if current is None:
            raise ProductionStateError(
                f"account-equity {payload.kind} appears before a durable baseline."
            )
        if payload.previous_event_id != current.last_event_id:
            raise ProductionStateError("account-equity chain previous_event_id is discontinuous.")
        if event.occurred_at < current.last_occurred_at:
            raise ProductionStateError("account-equity chain time moved backwards.")
        if payload.fence_term < current.fence_term:
            raise ProductionStateError("account-equity chain fence term regressed.")
        if (
            event.symbol != current.symbol
            or payload.environment != current.environment
            or payload.venue_account_id != current.venue_account_id
            or payload.base_asset != current.base_asset
            or payload.quote_asset != current.quote_asset
        ):
            raise ProductionStateError("account-equity event changed immutable chain scope.")

        if payload.kind == "rebaseline":
            if payload.peak_equity_quote < current.peak_equity_quote:
                raise ProductionStateError(
                    "account-equity rebaseline cannot lower the durable high-water mark."
                )
            current = _state_from(event)
            continue

        if payload.kind != "peak":
            raise ProductionStateError(f"unsupported account-equity event kind {payload.kind!r}.")
        if payload.baseline_event_id != current.baseline_event_id:
            raise ProductionStateError("account-equity peak changed baseline identity.")
        if payload.peak_equity_quote <= current.peak_equity_quote:
            raise ProductionStateError(
                "account-equity peak must strictly exceed the previous durable high-water mark."
            )
        current = _state_from(event)

    if current is None:  # pragma: no cover - events is non-empty
        raise ProductionStateError("account-equity chain could not be reconstructed.")
    expected = (symbol, environment, venue_account_id, base_asset, quote_asset)
    actual = (
        current.symbol,
        current.environment,
        current.venue_account_id,
        current.base_asset,
        current.quote_asset,
    )
    if actual != expected:
        raise ProductionStateError(
            "active account-equity chain does not match the configured account/instrument scope."
        )
    return current
