"""Canonical production RiskState derivation from durable and authoritative evidence (G5b).

The pure :func:`derive_risk_state` performs no I/O. :class:`ProductionRiskStateSource` is the
fenced production boundary that refreshes strict venue evidence and durably advances equity peaks
before Hard Risk consumes the resulting snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from uuid import UUID, uuid4

from crypto_trader.breakglass import BreakGlassControl, OwnershipStateError, WriterGuard
from crypto_trader.events import (
    ACCOUNT_EQUITY_EVENT_TYPE,
    EXECUTION_FILL_EVENT_TYPE,
    VENUE_TRADE_EVENT_TYPE,
    AccountEquityRecorded,
    AccountEquityRecordPayload,
    EventEnvelope,
    ExecutionFillRecorded,
    VenueTradeRecorded,
)
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.binance_testnet import (
    BinanceSpotTestnetClient, MarkEvidence, TestnetOrder, VenueAccountSnapshot,
)
from crypto_trader.persistence import EventJournal, JournalError, JournalRevisionMismatchError
from crypto_trader.recovery import OrderStateReconstructor, RecoveryError
from crypto_trader.risk import RiskStateSnapshot

from .equity import EquityChainState
from .errors import ProductionStateError

__all__ = ["ProductionRiskStateSource", "derive_risk_state"]

ZERO = Decimal("0")
_ACCOUNTING_PRECISION = 34


def _fail(message: str) -> ProductionStateError:
    return ProductionStateError(f"canonical risk state unavailable: {message}")


def _event_key(event: EventEnvelope) -> tuple[int, int, str]:
    return (
        int(event.occurred_at.timestamp() * 1_000_000_000),
        0 if event.sequence_number is None else event.sequence_number,
        str(event.event_id),
    )


def _require_aware(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _fail(f"{name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _worse_signed(current: Decimal, buys: Decimal, sells: Decimal) -> Decimal:
    buy_case = current + buys
    sell_case = current - sells
    if abs(sell_case) > abs(buy_case):
        return sell_case
    return buy_case


def derive_risk_state(
    journal_events: Sequence[EventEnvelope],
    *,
    chain: EquityChainState,
    account: VenueAccountSnapshot,
    mark: MarkEvidence,
    instrument: InstrumentSpec,
    now: datetime,
    max_account_age_seconds: float = 5.0,
    max_mark_age_seconds: float = 5.0,
    venue_open_orders: Sequence[TestnetOrder] | None = None,
) -> RiskStateSnapshot:
    """Derive one deterministic fail-closed production risk snapshot.

    The active equity root absorbs all activity before its durable baseline/rebaseline. Post-root
    fills are reconstructed from authoritative execution.fill.recorded facts and must reconcile
    exactly to the venue's current free+locked base/quote balances.
    """

    now = _require_aware("now", now)
    for name, value in (
        ("max_account_age_seconds", max_account_age_seconds),
        ("max_mark_age_seconds", max_mark_age_seconds),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise _fail(f"{name} must be a positive number.")
    if account.fetched_at > now:
        raise _fail("account evidence is timestamped in the future.")
    if mark.observed_at > now:
        raise _fail("mark evidence is timestamped in the future.")
    if (now - account.fetched_at).total_seconds() > float(max_account_age_seconds):
        raise _fail("account evidence is stale.")
    if (now - mark.observed_at).total_seconds() > float(max_mark_age_seconds):
        raise _fail("mark evidence is stale.")
    if mark.symbol != instrument.symbol:
        raise _fail("mark symbol does not match the configured instrument.")
    if (
        chain.symbol != instrument.symbol
        or chain.base_asset != instrument.base_currency
        or chain.quote_asset != instrument.quote_currency
    ):
        raise _fail("equity chain scope does not match the configured instrument.")
    if account.venue_account_id != chain.venue_account_id:
        raise _fail("account identity does not match the active equity chain.")

    try:
        base_total = account.balance(chain.base_asset).total
        quote_total = account.balance(chain.quote_asset).total
    except KeyError as exc:
        raise _fail("strict account evidence is missing the configured base or quote asset.") from exc

    ordered = tuple(sorted(journal_events, key=_event_key))
    by_id = {event.event_id: event for event in ordered}
    if len(by_id) != len(ordered):
        raise _fail("journal contains duplicate event identities.")

    raw_root = by_id.get(chain.baseline_event_id)
    if not isinstance(raw_root, AccountEquityRecorded):
        raise _fail("active equity root is missing or decoded to the wrong contract.")
    root = raw_root
    root_payload = root.payload
    if root.event_type != ACCOUNT_EQUITY_EVENT_TYPE or root_payload.kind not in {
        "baseline",
        "rebaseline",
    }:
        raise _fail("active equity root is not a baseline/rebaseline event.")
    if (
        root.symbol != chain.symbol
        or root_payload.environment != chain.environment
        or root_payload.venue_account_id != chain.venue_account_id
        or root_payload.base_asset != chain.base_asset
        or root_payload.quote_asset != chain.quote_asset
    ):
        raise _fail("active equity root scope disagrees with the validated chain.")

    reconstructor = OrderStateReconstructor()
    try:
        for event in ordered:
            reconstructor.handle(event)
    except RecoveryError as exc:
        raise _fail(f"order journal is not reconstructable: {exc}") from exc
    conflicts = reconstructor.conflicts()
    if conflicts:
        raise _fail(f"order reconstruction contains unresolved conflicts: {conflicts!r}.")

    anchors = reconstructor.attempt_anchor_map()
    anchors_by_order: dict[UUID, object] = {}
    root_ns = int(root.occurred_at.timestamp() * 1_000_000_000)
    post_root_references: set[str] = set()
    for reference, anchor in anchors.items():
        if anchor.order_id in anchors_by_order:
            raise _fail("durable attempt order identity is duplicated.")
        anchors_by_order[anchor.order_id] = anchor
        if anchor.occurred_at_ns == root_ns:
            raise _fail("an order attempt lies exactly on the equity-root boundary.")
        if anchor.occurred_at_ns > root_ns:
            if not anchor.strategy_id:
                raise _fail("a post-root order attempt is missing durable strategy attribution.")
            post_root_references.add(reference)

    for state in reconstructor.open_states():
        anchor = anchors.get(state.order_reference)
        if anchor is None:
            raise _fail("an open order has no durable attempt anchor.")
        if anchor.occurred_at_ns <= root_ns:
            raise _fail("the active equity root was not written from a quiet order state.")

    trade_by_fill: dict[UUID, VenueTradeRecorded] = {}
    for event in ordered:
        if event.event_type != VENUE_TRADE_EVENT_TYPE:
            continue
        if not isinstance(event, VenueTradeRecorded):
            raise _fail("venue trade event decoded to the wrong contract.")
        fill_id = event.payload.fill_event_id
        if fill_id in trade_by_fill:
            raise _fail("multiple venue trade records point to one fill.")
        trade_by_fill[fill_id] = event

    matched_trade_ids: set[UUID] = set()
    expected_base = root_payload.base_balance
    expected_quote = root_payload.quote_balance
    position = root_payload.base_balance
    average_cost = root_payload.mark_price if position > ZERO else ZERO
    realized = ZERO
    strategy_qty: dict[str, Decimal] = defaultdict(lambda: ZERO)
    post_root_fill_count = 0

    with localcontext() as ctx:
        ctx.prec = _ACCOUNTING_PRECISION
        ctx.rounding = ROUND_HALF_EVEN

        for event in ordered:
            if event.event_type != EXECUTION_FILL_EVENT_TYPE:
                continue
            if not isinstance(event, ExecutionFillRecorded):
                raise _fail("fill event decoded to the wrong contract.")
            if event.order_id is None:
                raise _fail("fill event is missing order_id.")
            anchor = anchors_by_order.get(event.order_id)
            if anchor is None:
                raise _fail("a fill cannot be classified relative to the active root without an attempt anchor.")
            if anchor.occurred_at_ns <= root_ns:
                continue
            if anchor.order_reference not in post_root_references:
                raise _fail("post-root fill reference is outside the validated attempt set.")
            if event.symbol != anchor.symbol or event.symbol != instrument.symbol:
                raise _fail("fill symbol disagrees with its durable attempt/instrument.")
            if event.producer != anchor.venue_producer:
                raise _fail("post-root fill producer is not the authoritative venue producer.")
            if event.strategy_id is not None and event.strategy_id != anchor.strategy_id:
                raise _fail("fill strategy attribution disagrees with its durable attempt.")

            payload = event.payload
            fee = payload.fee
            fee_asset = payload.fee_asset
            if fee > ZERO and fee_asset is None:
                raise _fail("non-zero fill fee is missing fee_asset.")
            if fee > ZERO and fee_asset not in {chain.base_asset, chain.quote_asset}:
                raise _fail("fill fee asset has no approved base/quote conversion.")
            base_fee = fee if fee_asset == chain.base_asset else ZERO
            quote_fee = fee if fee_asset == chain.quote_asset else ZERO
            if base_fee > payload.quantity:
                raise _fail("base-asset fee exceeds the fill quantity.")

            corroboration = trade_by_fill.get(event.event_id)
            if corroboration is not None:
                trade = corroboration.payload
                if (
                    corroboration.order_id != event.order_id
                    or corroboration.symbol != event.symbol
                    or corroboration.producer != anchor.venue_producer
                    or trade.venue_reference != anchor.order_reference
                    or trade.quantity != payload.quantity
                    or trade.price != payload.price
                    or trade.commission != payload.fee
                    or trade.commission_asset != payload.fee_asset
                    or trade.quote_quantity != payload.price * payload.quantity
                    or trade.is_buyer != (anchor.side == "buy")
                ):
                    raise _fail("venue trade metadata disagrees with its authoritative fill/attempt.")
                if (
                    corroboration.strategy_id is not None
                    and corroboration.strategy_id != anchor.strategy_id
                ):
                    raise _fail("venue trade strategy attribution disagrees with its attempt.")
                matched_trade_ids.add(corroboration.event_id)

            quantity = payload.quantity
            price = payload.price
            strategy_id = anchor.strategy_id
            if anchor.side == "buy":
                received = quantity - base_fee
                expected_base += received
                expected_quote -= price * quantity + quote_fee
                gross_position = position + quantity
                if gross_position <= ZERO:
                    raise _fail("buy accounting produced a non-positive gross spot position.")
                total_cost = average_cost * position + price * quantity
                gross_average = total_cost / gross_position
                realized -= quote_fee
                if base_fee > ZERO:
                    realized -= gross_average * base_fee
                    total_cost -= gross_average * base_fee
                position = gross_position - base_fee
                average_cost = total_cost / position if position > ZERO else ZERO
                strategy_qty[strategy_id] += received
            elif anchor.side == "sell":
                disposed = quantity + base_fee
                if disposed > position:
                    raise _fail("post-root fills would oversell the spot base position.")
                proceeds = price * quantity - quote_fee
                realized += proceeds - average_cost * disposed
                expected_base -= disposed
                expected_quote += proceeds
                position -= disposed
                strategy_qty[strategy_id] -= disposed
                if strategy_qty[strategy_id] < ZERO:
                    raise _fail(
                        "strategy sell quantity exceeds its durable post-root attributed inventory."
                    )
                if position == ZERO:
                    average_cost = ZERO
            else:
                raise _fail("durable attempt side is unsupported by production accounting.")
            post_root_fill_count += 1

    for trade in trade_by_fill.values():
        if trade.event_id in matched_trade_ids:
            continue
        if trade.order_id is None:
            raise _fail("venue trade metadata is missing order_id.")
        anchor = anchors_by_order.get(trade.order_id)
        if anchor is None:
            raise _fail("venue trade metadata cannot be classified without an attempt anchor.")
        if anchor.occurred_at_ns > root_ns:
            raise _fail("post-root venue trade metadata has no matching fill.")

    if expected_base != base_total or expected_quote != quote_total:
        raise _fail(
            "current venue balances do not reconcile to the active root plus durable post-root fills."
        )
    if position != base_total or position < ZERO:
        raise _fail("derived spot position does not match the venue base balance.")

    open_buy = ZERO
    open_sell = ZERO
    strategy_open_buy: dict[str, Decimal] = defaultdict(lambda: ZERO)
    strategy_open_sell: dict[str, Decimal] = defaultdict(lambda: ZERO)
    strategy_open_sell_quantity: dict[str, Decimal] = defaultdict(lambda: ZERO)
    open_orders = 0
    for state in reconstructor.open_states():
        anchor = anchors[state.order_reference]
        if anchor.occurred_at_ns <= root_ns:
            raise _fail("pre-root order remained open after the quiet equity root.")
        remaining = state.remaining_quantity
        if remaining <= ZERO:
            raise _fail("open order has no positive remaining quantity.")
        risk_price = anchor.limit_price if anchor.limit_price is not None else mark.price
        if risk_price <= ZERO:
            raise _fail("open order has no positive deterministic risk price.")
        notional = remaining * risk_price
        if anchor.side == "buy":
            open_buy += notional
            strategy_open_buy[anchor.strategy_id] += notional
        elif anchor.side == "sell":
            open_sell += notional
            strategy_open_sell[anchor.strategy_id] += notional
            strategy_open_sell_quantity[anchor.strategy_id] += remaining
            if strategy_open_sell_quantity[anchor.strategy_id] > strategy_qty[anchor.strategy_id]:
                raise _fail(
                    "strategy open sell quantity exceeds its durable post-root attributed inventory."
                )
        else:
            raise _fail("open order has unsupported side.")
        open_orders += 1

    if venue_open_orders is not None:
        local_open = {state.order_reference: state for state in reconstructor.open_states()}
        remote_open = {order.client_order_id: order for order in venue_open_orders}
        if len(remote_open) != len(tuple(venue_open_orders)):
            raise _fail("authoritative venue open-order identities are duplicated.")
        if set(local_open) != set(remote_open):
            raise _fail("authoritative venue open orders do not match durable open-order identities.")
        for reference, local in local_open.items():
            anchor = anchors[reference]
            remote = remote_open[reference]
            if remote.remaining_quantity != local.remaining_quantity:
                raise _fail("authoritative venue open-order quantity disagrees with durable state.")
            if remote.side != anchor.side.upper():
                raise _fail("authoritative venue open-order side disagrees with durable attempt.")
            if anchor.order_type == "limit" and remote.price != anchor.limit_price:
                raise _fail("authoritative venue open-order price disagrees with durable attempt.")

    current_notional = position * mark.price
    signed_worst = _worse_signed(current_notional, open_buy, open_sell)
    strategy_exposure: dict[str, Decimal] = {}
    strategy_ids = set(strategy_qty) | set(strategy_open_buy) | set(strategy_open_sell)
    for strategy_id in sorted(strategy_ids):
        current = strategy_qty[strategy_id] * mark.price
        strategy_exposure[strategy_id] = _worse_signed(
            current,
            strategy_open_buy[strategy_id],
            strategy_open_sell[strategy_id],
        )

    equity = base_total * mark.price + quote_total
    if equity <= ZERO:
        raise _fail("current marked equity must be positive.")
    peak = max(chain.peak_equity_quote, equity)
    snapshot_ts = min(account.fetched_at, mark.observed_at)
    if snapshot_ts > now:
        raise _fail("risk evidence snapshot timestamp is in the future.")

    dust = Decimal(1).scaleb(-instrument.size_precision)
    open_positions = 1 if abs(position) >= dust else 0

    return RiskStateSnapshot(
        equity_quote=equity,
        peak_equity_quote=peak,
        realized_pnl_quote=realized,
        gross_exposure_quote=abs(signed_worst),
        net_exposure_quote=signed_worst,
        open_orders=open_orders,
        open_positions=open_positions,
        symbol_exposure_quote={instrument.symbol: signed_worst},
        strategy_exposure_quote=strategy_exposure,
        position_quantity={instrument.symbol: position},
        strategy_position_quantity=dict(strategy_qty),
        strategy_open_sell_quantity=dict(strategy_open_sell_quantity),
        snapshot_ts=snapshot_ts,
    )


class ProductionRiskStateSource:
    """Refresh and persist canonical G5b risk state under the runtime's current writer fence."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        control: BreakGlassControl,
        client: BinanceSpotTestnetClient,
        instrument: InstrumentSpec,
        environment: str,
        venue_account_id: str,
        max_account_age_seconds: float = 5.0,
        max_mark_age_seconds: float = 5.0,
        producer: str = "application.production.equity",
    ) -> None:
        if not isinstance(control, BreakGlassControl):
            raise _fail("risk source requires BreakGlassControl.")
        if type(client) is not BinanceSpotTestnetClient:
            raise _fail("risk source requires the strict Binance testnet client.")
        if not isinstance(instrument, InstrumentSpec):
            raise _fail("risk source requires InstrumentSpec.")
        if environment != client.venue_label:
            raise _fail("risk source environment does not match the venue client.")
        if (
            not isinstance(venue_account_id, str)
            or not venue_account_id.isascii()
            or not venue_account_id.isdigit()
            or venue_account_id.startswith("0")
        ):
            raise _fail("risk source requires a configured canonical venue account id.")
        for name, value in (
            ("max_account_age_seconds", max_account_age_seconds),
            ("max_mark_age_seconds", max_mark_age_seconds),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise _fail(f"{name} must be a positive number.")
        self._journal = journal
        self._control = control
        self._client = client
        self._instrument = instrument
        self._environment = environment
        self._venue_account_id = venue_account_id
        self._max_account_age_seconds = max_account_age_seconds
        self._max_mark_age_seconds = max_mark_age_seconds
        self._producer = producer

    def _stable_events(self):
        try:
            before = self._journal.revision()
            events = tuple(self._journal.read_range())
            after = self._journal.revision()
        except JournalError as exc:
            raise _fail("journal could not be read at one stable revision.") from exc
        if before != after:
            raise _fail("journal changed while canonical risk evidence was captured.")
        return before, events

    def _chain(self, events):
        from .equity import load_equity_chain
        return load_equity_chain(
            self._journal,
            symbol=self._instrument.symbol,
            environment=self._environment,
            venue_account_id=self._venue_account_id,
            base_asset=self._instrument.base_currency,
            quote_asset=self._instrument.quote_currency,
        )

    def __call__(
        self,
        now: datetime,
        *,
        guard: WriterGuard,
        correlation_id: UUID | None = None,
    ) -> tuple[RiskStateSnapshot, datetime]:
        _require_aware("now", now)
        if not isinstance(guard, WriterGuard):
            raise _fail("risk source requires the runtime's current WriterGuard.")
        try:
            guard.require_current()
        except OwnershipStateError as exc:
            raise _fail("runtime writer fence is stale before risk refresh.") from exc

        expected, events = self._stable_events()
        chain = self._chain(events)
        try:
            if self._journal.revision() != expected:
                raise _fail("journal changed while the active equity chain was validated.")
        except JournalError as exc:
            raise _fail("journal revision could not be revalidated.") from exc

        try:
            self._client.synchronize_clock()
            venue_before = self._client.startup_open_orders(self._instrument.symbol)
            account = self._client.startup_account_snapshot(
                self._instrument.base_currency,
                self._instrument.quote_currency,
            )
            mark = self._client.startup_mark_evidence(self._instrument.symbol)
            venue_after = self._client.startup_open_orders(self._instrument.symbol)
            evaluation_now = datetime.fromtimestamp(
                self._client.clock.timestamp_ms() / 1000,
                tz=timezone.utc,
            )
        except Exception as exc:
            raise _fail("authoritative venue risk evidence refresh failed.") from exc

        if tuple((o.client_order_id, o.status, o.remaining_quantity) for o in venue_before) != tuple(
            (o.client_order_id, o.status, o.remaining_quantity) for o in venue_after
        ):
            raise _fail("venue open orders changed during risk evidence refresh.")

        state = derive_risk_state(
            events,
            chain=chain,
            account=account,
            mark=mark,
            instrument=self._instrument,
            now=evaluation_now,
            max_account_age_seconds=self._max_account_age_seconds,
            max_mark_age_seconds=self._max_mark_age_seconds,
            venue_open_orders=venue_after,
        )

        if state.equity_quote <= chain.peak_equity_quote:
            try:
                guard.require_current()
                if self._journal.revision() != expected:
                    raise _fail("journal changed before canonical risk state could be returned.")
            except OwnershipStateError as exc:
                raise _fail("runtime writer fence changed during risk refresh.") from exc
            except JournalError as exc:
                raise _fail("journal revision could not be revalidated before risk return.") from exc
            return state, evaluation_now

        if evaluation_now <= chain.last_occurred_at:
            raise _fail(
                "equity high-water cannot be ordered strictly after the current durable chain."
            )
        peak_event = AccountEquityRecorded(
            event_id=uuid4(),
            occurred_at=evaluation_now,
            correlation_id=correlation_id or uuid4(),
            producer=self._producer,
            symbol=self._instrument.symbol,
            payload=AccountEquityRecordPayload(
                kind="peak",
                environment=self._environment,
                venue_account_id=self._venue_account_id,
                base_asset=self._instrument.base_currency,
                quote_asset=self._instrument.quote_currency,
                base_balance=account.balance(self._instrument.base_currency).total,
                quote_balance=account.balance(self._instrument.quote_currency).total,
                mark_price=mark.price,
                mark_reference=mark.reference,
                mark_event_ts=mark.observed_at,
                equity_quote=state.equity_quote,
                peak_equity_quote=state.equity_quote,
                baseline_event_id=chain.baseline_event_id,
                previous_event_id=chain.last_event_id,
                fence_term=guard.fence.term,
                reason="canonical production equity high-water",
            ),
        )
        try:
            with self._control.ownership_store.exclusive(purpose="risk_state_peak"):
                guard.require_current()
                self._journal.append_batch_if_revision(
                    [peak_event],
                    expected=expected,
                    require_all_new=True,
                )
        except (OwnershipStateError, JournalRevisionMismatchError, JournalError) as exc:
            raise _fail("equity peak could not be durably fenced before risk evaluation.") from exc

        # The returned snapshot already contains the same peak that was just committed. There is no
        # fallible post-commit verification path.
        return state, evaluation_now
