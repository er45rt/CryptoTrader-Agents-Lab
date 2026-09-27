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
from uuid import UUID

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
        self._runtime._submit_queued(self)

    def on_trade_tick(self, tick: TradeTick) -> None:
        self._runtime._submit_queued(self)

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
        self._queued: deque[ExecutionRequest] = deque()
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
        request.validate_for(self._instrument)
        reference = f"REQ-{request.intent_id.hex[:12]}"
        self._queued.append(request)
        return reference

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

    def _submit_queued(self, strategy: _TranslationStrategy) -> None:
        while self._queued:
            request = self._queued.popleft()
            order = inbound.build_order(strategy.order_factory, self._instrument, request)
            self._pending[str(order.client_order_id)] = request
            strategy.submit_order(order)

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
        self._observations.append(observation)
        self._phase3_events.append(outbound.to_phase3_order_state_event(observation))

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
        self._observations.append(observation)
        self._phase3_events.append(outbound.to_phase3_order_state_event(observation))
        self._phase3_events.append(outbound.to_phase3_fill_event(observation))
