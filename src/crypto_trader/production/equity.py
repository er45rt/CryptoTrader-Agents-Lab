"""Durable production account-equity chain reconstruction (G5b).

The loader is read-only and validates cross-event invariants. The pre-start baseline writer is a
separate fenced maintenance boundary that collects strict venue/market evidence and appends only
the first account-equity root.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from crypto_trader.breakglass import BreakGlassControl, BreakGlassError
from crypto_trader.events import (
    ACCOUNT_EQUITY_EVENT_TYPE,
    AccountEquityRecorded,
    AccountEquityRecordPayload,
)
from crypto_trader.execution.binance_testnet import BinanceSpotTestnetClient
from crypto_trader.execution.ports import InstrumentSpec
from crypto_trader.persistence import (
    EventJournal,
    JournalError,
    JournalRevision,
    JournalRevisionMismatchError,
)
from crypto_trader.recovery import OrderStateReconstructor, RecoveryError
from crypto_trader.validity import MarketStateProvider

from .errors import ProductionStateError

__all__ = [
    "AccountEquityBaselineReport",
    "AccountEquityBaselineWriter",
    "EquityChainState",
    "load_equity_chain",
]


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
    root_occurred_at: datetime
    root_base_balance: Decimal
    root_quote_balance: Decimal
    root_mark_price: Decimal
    root_mark_reference: str


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
        root_occurred_at=event.occurred_at,
        root_base_balance=payload.base_balance,
        root_quote_balance=payload.quote_balance,
        root_mark_price=payload.mark_price,
        root_mark_reference=payload.mark_reference,
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
        if event.producer != "application.production.equity":
            raise ProductionStateError("account-equity event has an unauthorized producer.")
        if payload.equity_quote != payload.quote_balance + payload.base_balance * payload.mark_price:
            raise ProductionStateError(
                "account-equity event does not reconcile balances and mark to equity."
            )
        if payload.kind in {"baseline", "rebaseline", "peak"} and (
            payload.peak_equity_quote != payload.equity_quote
        ):
            raise ProductionStateError(
                "account-equity root/peak event has inconsistent peak and current equity."
            )

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
            # Explicit operator rebaseline starts a new loss/drawdown window for the same
            # account/instrument scope. The previous high-water mark remains auditable through
            # previous_event_id, but the active peak intentionally resets to the observed equity.
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
        current = replace(
            current,
            last_event_id=event.event_id,
            last_kind=payload.kind,
            peak_equity_quote=payload.peak_equity_quote,
            last_equity_quote=payload.equity_quote,
            fence_term=payload.fence_term,
            last_occurred_at=event.occurred_at,
        )

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


@dataclass(frozen=True)
class AccountEquityBaselineReport:
    """Evidence returned after one durable baseline root is committed."""

    event_id: UUID
    fence_term: int
    venue_account_id: str
    equity_quote: Decimal
    journal_fingerprint: str
    observed_at: datetime


class AccountEquityBaselineWriter:
    """Create the first G5b equity-chain root under the normal-writer fence.

    This is a pre-start maintenance operation. It never creates a rebaseline and refuses to run
    when any equity-chain event already exists.
    """

    def __init__(
        self,
        *,
        journal: EventJournal,
        control: BreakGlassControl,
        client: BinanceSpotTestnetClient,
        market_state: MarketStateProvider,
        instrument: InstrumentSpec,
        environment: str,
        venue_account_id: str,
        max_account_age_seconds: float,
        max_mark_age_seconds: float,
        clock: Callable[[], datetime],
        owner_id: str = "normal:equity-baseline-maintenance",
    ) -> None:
        if not isinstance(control, BreakGlassControl):
            raise ProductionStateError("equity baseline requires BreakGlassControl.")
        if type(client) is not BinanceSpotTestnetClient:
            raise ProductionStateError("equity baseline requires the strict Binance testnet client.")
        if not isinstance(market_state, MarketStateProvider):
            raise ProductionStateError("equity baseline requires a MarketStateProvider.")
        if not isinstance(instrument, InstrumentSpec):
            raise ProductionStateError("equity baseline requires an InstrumentSpec.")
        if not isinstance(environment, str) or not environment.strip():
            raise ProductionStateError("equity baseline requires an explicit environment label.")
        if environment != client.venue_label:
            raise ProductionStateError("equity baseline environment does not match the venue client.")
        if (
            not isinstance(venue_account_id, str)
            or not venue_account_id.isascii()
            or not venue_account_id.isdigit()
            or venue_account_id.startswith("0")
        ):
            raise ProductionStateError(
                "equity baseline requires a configured canonical venue account id."
            )
        for name, value in (
            ("max_account_age_seconds", max_account_age_seconds),
            ("max_mark_age_seconds", max_mark_age_seconds),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ProductionStateError(f"{name} must be a positive number.")
        if not callable(clock):
            raise ProductionStateError("equity baseline requires an explicit clock.")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ProductionStateError("equity baseline requires a non-empty owner id.")

        self._journal = journal
        self._control = control
        self._client = client
        self._market_state = market_state
        self._instrument = instrument
        self._environment = environment
        self._venue_account_id = venue_account_id
        self._max_account_age_seconds = float(max_account_age_seconds)
        self._max_mark_age_seconds = float(max_mark_age_seconds)
        self._clock = clock
        self._owner_id = owner_id.strip()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ProductionStateError("equity baseline clock must return a timezone-aware datetime.")
        return value.astimezone(timezone.utc)

    def _capture_ready_revision(self) -> JournalRevision:
        try:
            before = self._journal.revision()
            events = tuple(self._journal.read_range())
            after = self._journal.revision()
        except JournalError as exc:
            raise ProductionStateError(
                "equity baseline could not capture a stable journal revision."
            ) from exc
        if before != after:
            raise ProductionStateError("equity baseline journal changed while readiness was checked.")
        if any(event.event_type == ACCOUNT_EQUITY_EVENT_TYPE for event in events):
            raise ProductionStateError(
                "equity baseline already exists; G5b does not create an automatic rebaseline."
            )
        foreign_symbols = sorted(
            {
                event.symbol
                for event in events
                if event.symbol is not None and event.symbol != self._instrument.symbol
            }
        )
        if foreign_symbols:
            raise ProductionStateError(
                "equity baseline requires a dedicated single-symbol journal."
            )

        rebuilt = OrderStateReconstructor()
        try:
            for event in events:
                rebuilt.handle(event)
        except RecoveryError as exc:
            raise ProductionStateError(
                "equity baseline cannot reconstruct durable order history."
            ) from exc
        if rebuilt.conflicts():
            raise ProductionStateError(
                "equity baseline refuses unresolved durable order-history conflicts."
            )
        if rebuilt.open_states():
            raise ProductionStateError(
                "equity baseline requires zero open orders in the application journal."
            )
        return after

    @staticmethod
    def _require_age(
        label: str,
        *,
        observed_at: datetime,
        now: datetime,
        max_age_seconds: float,
    ) -> None:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ProductionStateError(f"{label} timestamp must be timezone-aware.")
        age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            raise ProductionStateError(f"{label} timestamp is in the future.")
        if age > max_age_seconds:
            raise ProductionStateError(f"{label} evidence is stale.")

    def create(
        self,
        *,
        correlation_id: UUID | None = None,
        reason: str = "canonical production account-equity baseline",
    ) -> AccountEquityBaselineReport:
        """Collect fresh, bracketed venue evidence and atomically append the first root."""

        expected = self._capture_ready_revision()
        try:
            if self._control.ownership_store.read() is not None:
                raise ProductionStateError(
                    "equity baseline requires no pre-existing writer lease; stop and resolve ownership first."
                )
            guard = self._control.claim_normal_writer(
                owner_id=self._owner_id,
                reason_code="equity_baseline_maintenance",
            )
        except BreakGlassError as exc:
            raise ProductionStateError(
                "equity baseline refused: normal writer lease is unavailable."
            ) from exc

        # No ownership lock is held across network or market-data reads.
        try:
            self._client.synchronize_clock()
            before_orders = self._client.startup_open_orders(self._instrument.symbol)
            if before_orders:
                raise ProductionStateError(
                    "equity baseline requires zero authoritative venue open orders."
                )
            account = self._client.startup_account_snapshot(
                self._instrument.base_currency,
                self._instrument.quote_currency,
            )
            mark = self._market_state.latest(self._instrument.symbol)
            if mark is None:
                raise ProductionStateError("equity baseline has no market observation.")
            after_orders = self._client.startup_open_orders(self._instrument.symbol)
            if after_orders:
                raise ProductionStateError(
                    "equity baseline requires zero authoritative venue open orders after snapshot."
                )
        except ProductionStateError:
            raise
        except Exception as exc:
            raise ProductionStateError(
                "equity baseline venue/market evidence collection failed closed."
            ) from exc

        if account.venue_account_id != self._venue_account_id:
            raise ProductionStateError(
                "equity baseline venue account id does not match configured account id."
            )
        if mark.symbol != self._instrument.symbol:
            raise ProductionStateError("equity baseline market observation changed symbol.")

        # Freshness is evaluated against the explicit production clock. Binance clock
        # synchronization protects signed-request timestamps; it must not silently redefine the
        # application's evidence-age boundary.
        now = self._now()
        self._require_age(
            "account",
            observed_at=account.fetched_at,
            now=now,
            max_age_seconds=self._max_account_age_seconds,
        )
        self._require_age(
            "mark",
            observed_at=mark.market_event_ts,
            now=now,
            max_age_seconds=self._max_mark_age_seconds,
        )

        base_balance = account.balance(self._instrument.base_currency).total
        quote_balance = account.balance(self._instrument.quote_currency).total
        equity = quote_balance + base_balance * mark.price
        if equity <= 0:
            raise ProductionStateError("equity baseline requires positive account equity.")

        event_id = uuid4()
        event = AccountEquityRecorded(
            event_id=event_id,
            occurred_at=now,
            correlation_id=correlation_id or uuid4(),
            producer="application.production.equity",
            symbol=self._instrument.symbol,
            payload=AccountEquityRecordPayload(
                kind="baseline",
                environment=self._environment,
                venue_account_id=self._venue_account_id,
                base_asset=self._instrument.base_currency,
                quote_asset=self._instrument.quote_currency,
                base_balance=base_balance,
                quote_balance=quote_balance,
                mark_price=mark.price,
                mark_reference=mark.market_reference,
                mark_event_ts=mark.market_event_ts,
                equity_quote=equity,
                peak_equity_quote=equity,
                baseline_event_id=event_id,
                previous_event_id=None,
                fence_term=guard.fence.term,
                reason=reason,
            ),
        )

        try:
            with self._control.ownership_store.exclusive(purpose="equity_baseline"):
                guard.require_current()
                committed = self._journal.append_batch_if_revision(
                    [event],
                    expected=expected,
                    require_all_new=True,
                )
        except (BreakGlassError, JournalRevisionMismatchError, JournalError) as exc:
            raise ProductionStateError(
                "equity baseline refused: fence or journal revision changed before commit."
            ) from exc

        return AccountEquityBaselineReport(
            event_id=event.event_id,
            fence_term=guard.fence.term,
            venue_account_id=account.venue_account_id,
            equity_quote=equity,
            journal_fingerprint=committed.after.fingerprint,
            observed_at=now,
        )
