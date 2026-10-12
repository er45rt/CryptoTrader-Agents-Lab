"""Production runtime wrapper hosting the Nautilus adapter (Phase 4).

Explicit start/stop, observable lifecycle, dependency injectable, no global singleton.
The engine loop never performs model inference or blocking network work: callbacks only
translate engine state into our representations.

Offline only: a simulated backtest venue with no exchange connectivity whatsoever.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any, Callable, Sequence
from uuid import UUID, uuid5

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.model.currencies import Currency
from nautilus_trader.model.data import OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import AccountType, BookType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from ..errors import RuntimeStateError, UnsupportedExecutionRequestError
from ..ports import (
    ExecutionCapability,
    ExecutionObservation,
    ExecutionRequest,
    ExecutionRuntimeState,
    InstrumentSpec,
    MarketBookUpdate,
    MarketTrade,
    OrderLifecycleState,
)
from . import inbound, outbound
from .guard import (
    QueueAwareSettings,
    require_queue_aware_settings,
    require_supported_engine_version,
    tested_queue_aware_settings,
)

__all__ = ["NautilusExecutionRuntime"]

_NS_PER_MS = 1_000_000
_EVENT_NAMESPACE = UUID("bd6f93ac-3f6b-4f87-866b-749ad0d50d8e")
_BASE_TS_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z, deterministic simulated base time


class _TranslationStrategy(Strategy):
    """Submits queued orders on the first market event and records translated observations."""

    def __init__(
        self,
        config: StrategyConfig,
        *,
        instrument: InstrumentSpec,
        runtime: "NautilusExecutionRuntime",
    ) -> None:
        super().__init__(config)
        self._instrument = instrument
        self._runtime = runtime

    def on_start(self) -> None:
        instrument_id = InstrumentId.from_str(f"{self._instrument.symbol}.{self._instrument.venue}")
        self.subscribe_order_book_deltas(instrument_id)
        self.subscribe_trade_ticks(instrument_id)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        self._runtime._submit_queued(self, int(deltas.ts_init))

    def on_trade_tick(self, tick: TradeTick) -> None:
        self._runtime._submit_queued(self, int(tick.ts_init))

    # --- lifecycle callbacks: translate only, never infer ---

    def _record_from_order(self, label: str, event) -> None:
        self._runtime._record_order_event(self, label, event)

    def on_order_submitted(self, event) -> None:
        self._record_from_order("order_submitted", event)

    def on_order_accepted(self, event) -> None:
        self._record_from_order("order_accepted", event)

    def on_order_updated(self, event) -> None:
        self._record_from_order("order_updated", event)

    def on_order_canceled(self, event) -> None:
        self._record_from_order("order_canceled", event)

    def on_order_rejected(self, event) -> None:
        self._record_from_order("order_rejected", event)

    def on_order_expired(self, event) -> None:
        self._record_from_order("order_expired", event)

    def on_order_filled(self, event) -> None:
        self._runtime._record_fill_event(self, event)


class NautilusExecutionRuntime:
    """Hosts the Nautilus adapter behind the engine-independent execution port."""

    #: Explicit Stage-3 capability: the offline simulator never writes to an external venue.
    capability = ExecutionCapability.NON_WRITING

    def __init__(
        self,
        instrument: InstrumentSpec,
        *,
        queue_aware: bool = True,
        queue_settings: QueueAwareSettings | None = None,
        producer: str = outbound.PHASE3_PRODUCER,
        latency_model: Any | None = None,
        fill_model: Any | None = None,
    ) -> None:
        self._instrument = instrument
        self._queue_aware = queue_aware
        self._queue_settings = queue_settings or tested_queue_aware_settings()
        self._producer = producer
        self._latency_model = latency_model
        self._fill_model = fill_model
        self._state = ExecutionRuntimeState.STOPPED
        self._engine: BacktestEngine | None = None
        self._strategy: _TranslationStrategy | None = None
        self._queued: deque[tuple[ExecutionRequest, int | None]] = deque()
        self._temporal_mode = False
        self._order_state_counts: dict[str, int] = {}
        self._seen_native_fill_ids: set[UUID] = set()
        self._seen_native_state_keys: set[tuple[UUID, str, int, str]] = set()
        self._pending: dict[str, ExecutionRequest] = {}
        self._observations: list[ExecutionObservation] = []
        self._phase3_events: list[Any] = []
        self._completed_runs = 0
        self._engine_version: str | None = None

    # --- lifecycle ---

    def start(self) -> None:
        if self._state is ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("runtime is already running.")
        self._engine_version = require_supported_engine_version()
        if self._queue_aware:
            require_queue_aware_settings(self._queue_settings)
        self._observations.clear()
        self._phase3_events.clear()
        self._queued.clear()
        self._pending.clear()
        self._order_state_counts.clear()
        self._seen_native_fill_ids.clear()
        self._seen_native_state_keys.clear()
        self._temporal_mode = False
        self._engine = self._build_engine()
        self._strategy = _TranslationStrategy(
            StrategyConfig(strategy_id="phase4-execution"),
            instrument=self._instrument,
            runtime=self,
        )
        self._engine.add_strategy(self._strategy)
        self._state = ExecutionRuntimeState.RUNNING

    def submit(self, request: ExecutionRequest) -> str:
        """Validate and queue an engine-independent request (fail closed)."""
        if self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("runtime must be started before submitting requests.")
        if self._temporal_mode:
            raise RuntimeStateError("untimed submit is forbidden in temporal simulation.")
        request.validate_for(self._instrument)
        reference = f"REQ-{request.intent_id.hex[:12]}"
        self._queued.append((request, None))
        return reference

    def enable_temporal(self) -> None:
        """Enable fail-closed time-gated submission without changing the shared port."""
        if self._state is not ExecutionRuntimeState.RUNNING or self._queued:
            raise RuntimeStateError("temporal mode requires a running, empty runtime.")
        self._temporal_mode = True

    def submit_timed(self, request: ExecutionRequest, *, decided_ns: int) -> str:
        if self._state is not ExecutionRuntimeState.RUNNING or not self._temporal_mode:
            raise RuntimeStateError("timed submit requires an active temporal runtime.")
        if type(decided_ns) is not int or decided_ns <= 0:
            raise RuntimeStateError("decision timestamp must be positive nanoseconds.")
        request.validate_for(self._instrument)
        self._queued.append((request, decided_ns))
        return f"REQ-{request.intent_id.hex[:12]}"

    def run_until(self, *, end_ns: int) -> None:
        """Advance the same preloaded simulated engine, preserving venue state."""
        if not self._temporal_mode or self._engine is None:
            raise RuntimeStateError("run_until requires temporal simulation.")
        if type(end_ns) is not int or end_ns <= 0:
            raise RuntimeStateError("invalid simulation boundary.")
        self._engine.run(end=end_ns, streaming=True)

    def has_queued_orders(self) -> bool:
        """Whether approved requests still await a native submission callback."""
        return bool(self._queued)

    def has_unsettled_orders(self) -> bool:
        """Conservatively check in-flight simulated orders before risk snapshots."""
        if self._queued:
            return True
        terminal = {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.EXPIRED,
        }
        latest = {
            item.intent_id: item.state
            for item in self._observations
            if item.state is not None
        }
        return any(
            latest.get(request.intent_id) not in terminal
            for request in self._pending.values()
        )

    def end_temporal(self) -> None:
        if not self._temporal_mode or self._engine is None:
            raise RuntimeStateError("temporal simulation is not active.")
        self._engine.end()

    def feed_market_data(
        self,
        *,
        book_updates: Sequence[MarketBookUpdate],
        trades: Sequence[MarketTrade],
    ) -> None:
        """Translate engine-independent market data and load it into the engine."""
        if self._engine is None:
            raise RuntimeStateError("runtime must be started before market data is supplied.")
        data: list[Any] = [inbound.to_book_deltas(update, self._instrument) for update in book_updates]
        data.extend(inbound.to_trade_tick(trade, self._instrument) for trade in trades)
        data.sort(key=lambda item: int(getattr(item, "ts_event", 0)))
        self._engine.add_data(data)

    def run(self) -> None:
        """Run the simulated engine to completion."""
        if self._engine is None or self._state is not ExecutionRuntimeState.RUNNING:
            raise RuntimeStateError("runtime must be started before it can run.")
        self._engine.run()
        self._completed_runs += 1

    def observations(self) -> Sequence[ExecutionObservation]:
        return tuple(self._observations)

    def phase3_events(self) -> Sequence[Any]:
        """Phase 3 contract instances produced by the outbound translation."""
        return tuple(self._phase3_events)

    def state(self) -> ExecutionRuntimeState:
        return self._state

    def engine_version(self) -> str | None:
        return self._engine_version

    def stop(self) -> None:
        if self._engine is not None:
            try:
                self._engine.dispose()
            finally:
                self._engine = None
                self._strategy = None
        self._state = ExecutionRuntimeState.STOPPED

    # --- internals ---

    def _build_engine(self) -> BacktestEngine:
        spec = self._instrument
        engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("PHASE4-001")))
        venue_kwargs: dict[str, Any] = {
            "venue": Venue(spec.venue),
            "oms_type": OmsType.NETTING,
            "account_type": AccountType.MARGIN,
            "base_currency": None,
            "starting_balances": [Money.from_str(spec.starting_balance)],
            "trade_execution": True,
            "bar_execution": False,
        }
        if self._queue_aware:
            # Tested combination: queue_position + liquidity_consumption + L2_MBP.
            venue_kwargs.update(
                queue_position=self._queue_settings.queue_position,
                liquidity_consumption=self._queue_settings.liquidity_consumption,
                book_type=BookType[self._queue_settings.book_type],
            )
        if self._latency_model is not None:
            venue_kwargs["latency_model"] = self._latency_model
        if self._fill_model is not None:
            venue_kwargs["fill_model"] = self._fill_model
        # ``add_venue`` is the public venue-registration API (``BacktestVenueConfig`` uses
        # ``name`` rather than ``venue``); the tested queue-aware settings are passed here.
        engine.add_venue(**venue_kwargs)
        engine.add_instrument(self._build_instrument())
        return engine

    def _build_instrument(self) -> CurrencyPair:
        spec = self._instrument
        start_ns = (_BASE_TS_MS - 1000) * _NS_PER_MS
        return CurrencyPair(
            instrument_id=InstrumentId.from_str(f"{spec.symbol}.{spec.venue}"),
            raw_symbol=Symbol(spec.symbol),
            base_currency=Currency.from_str(spec.base_currency),
            quote_currency=Currency.from_str(spec.quote_currency),
            price_precision=spec.price_precision,
            size_precision=spec.size_precision,
            price_increment=Price.from_str("1").as_double() and Price.from_str(
                str(Decimal(1).scaleb(-spec.price_precision))
            ),
            size_increment=Quantity.from_str(str(Decimal(1).scaleb(-spec.size_precision))),
            lot_size=Quantity.from_str(str(Decimal(1).scaleb(-spec.size_precision))),
            ts_event=start_ns,
            ts_init=start_ns,
            maker_fee=Decimal("0.0002"),
            taker_fee=Decimal("0.0004"),
        )

    def _submit_queued(self, strategy: _TranslationStrategy, event_ns: int) -> None:
        while self._queued:
            request, decided_ns = self._queued[0]
            if decided_ns is not None and event_ns <= decided_ns:
                break
            self._queued.popleft()
            order = inbound.build_order(strategy.order_factory, self._instrument, request)
            self._pending[str(order.client_order_id)] = request
            strategy.submit_order(order)

    def _state_event_id(self, request: ExecutionRequest, reference: str, label: str) -> UUID:
        key = f"{request.correlation_id}|{reference}"
        ordinal = self._order_state_counts.get(key, 0)
        self._order_state_counts[key] = ordinal + 1
        return uuid5(_EVENT_NAMESPACE, f"state-v1|{key}|{ordinal}|{label}")

    def _request_for(self, event) -> ExecutionRequest | None:
        return self._pending.get(str(event.client_order_id))

    def _record_order_event(self, strategy: _TranslationStrategy, label: str, event) -> None:
        request = self._request_for(event)
        if request is None:
            return
        order = strategy.cache.order(event.client_order_id)
        if order is None:
            return
        observation = outbound.observation_from_order(
            order,
            intent_id=request.intent_id,
            correlation_id=request.correlation_id,
            symbol=request.symbol,
            producer=self._producer,
        )
        native_key = (
            request.intent_id, label, int(getattr(event, "ts_event", 0)),
            observation.order_reference,
        )
        if native_key in self._seen_native_state_keys:
            raise RuntimeStateError("duplicate native order lifecycle callback.")
        reason = None
        if observation.state is OrderLifecycleState.REJECTED:
            # The native OrderRejected carries an explicit rejection reason.
            native_reason = getattr(event, "reason", None)
            if not isinstance(native_reason, str) or not native_reason.strip():
                raise outbound.TranslationError("native rejection is missing its source reason.")
            reason = native_reason
        elif observation.state in {OrderLifecycleState.CANCELED, OrderLifecycleState.EXPIRED}:
            native_reason = getattr(event, "reason", None)
            if isinstance(native_reason, str) and native_reason.strip():
                reason = native_reason
            else:
                native_source = {
                    "order_canceled": "OrderCanceled",
                    "order_expired": "OrderExpired",
                }.get(label)
                if native_source is None:
                    raise outbound.TranslationError("terminal source callback is unrecognized.")
                reason = f"source_event={native_source}; venue_reason=not supplied"
        identity = self._state_event_id(request, observation.order_reference, label)
        translated = outbound.to_phase3_order_state_event(
            observation, event_id=identity, reason=reason
        )
        self._seen_native_state_keys.add(native_key)
        self._observations.append(observation)
        self._phase3_events.append(translated)

    def _record_fill_event(self, strategy: _TranslationStrategy, event) -> None:
        request = self._request_for(event)
        if request is None:
            return
        order = strategy.cache.order(event.client_order_id)
        if order is None:
            return
        status_name = getattr(getattr(order, "status", None), "name", None)
        state = outbound.state_from_engine_status(str(status_name))
        observation = outbound.observation_from_fill(
            event,
            intent_id=request.intent_id,
            correlation_id=request.correlation_id,
            symbol=request.symbol,
            filled_quantity=Decimal(str(order.filled_qty)),
            remaining_quantity=Decimal(str(order.leaves_qty)),
            state=state,
            producer=self._producer,
        )
        trade_id = getattr(event, "trade_id", None)
        if trade_id is None or not str(trade_id):
            raise RuntimeStateError("native trade identity missing; refusing random fill id.")
        identity = uuid5(
            _EVENT_NAMESPACE,
            f"fill-v1|{request.correlation_id}|{observation.order_reference}|{trade_id}",
        )
        if identity in self._seen_native_fill_ids:
            raise RuntimeStateError("duplicate native fill id; refusing duplicate ledger effect.")
        self._seen_native_fill_ids.add(identity)
        self._observations.append(observation)
        self._phase3_events.append(
            outbound.to_phase3_order_state_event(
                observation,
                event_id=self._state_event_id(request, observation.order_reference, "order_filled"),
            )
        )
        self._phase3_events.append(outbound.to_phase3_fill_event(observation, event_id=identity))
