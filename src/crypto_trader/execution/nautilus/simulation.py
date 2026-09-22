"""Nautilus-backed simulated execution for paper sessions (Phase 10 infrastructure).

This is the ONLY bridge between the engine-independent paper session and the engine. It:

* re-validates the supported engine version and the tested queue-aware configuration through the
  Phase 4 guards (defence in depth on top of the paper-mode config check);
* runs the approved requests through the offline simulated venue only;
* returns OUR types (``ExecutionObservation`` + Phase 3 contracts) and never leaks an engine object.

No exchange connectivity, no credential, no authenticated endpoint.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from crypto_trader.paper.simulation import ExecutionSimulator, SimulationOutcome

from ..ports import ExecutionRequest, InstrumentSpec, MarketBookUpdate, MarketTrade
from .guard import QueueAwareSettings, tested_queue_aware_settings
from .runtime import NautilusExecutionRuntime

__all__ = ["NautilusSimulationRunner"]


@dataclass
class NautilusSimulationRunner:
    """Runs one batch of approved requests against the offline queue-aware simulator."""

    instrument: InstrumentSpec
    book_updates: Sequence[MarketBookUpdate] = ()
    trades: Sequence[MarketTrade] = ()
    queue_aware: bool = True
    queue_settings: QueueAwareSettings = field(default_factory=tested_queue_aware_settings)

    def __post_init__(self) -> None:
        if isinstance(self, ExecutionSimulator):  # structural conformance, checked eagerly
            return
        raise TypeError("NautilusSimulationRunner must satisfy the ExecutionSimulator protocol.")

    def simulate(self, requests: Sequence[ExecutionRequest]) -> SimulationOutcome:
        runtime = NautilusExecutionRuntime(
            self.instrument,
            queue_aware=self.queue_aware,
            queue_settings=self.queue_settings,
        )
        runtime.start()
        try:
            for request in requests:
                runtime.submit(request)
            runtime.feed_market_data(book_updates=self.book_updates, trades=self.trades)
            runtime.run()
            observations = tuple(runtime.observations())
            events = tuple(runtime.phase3_events())
            engine_version = runtime.engine_version()
        finally:
            runtime.stop()
        return SimulationOutcome(
            observations=observations,
            phase3_events=events,
            engine_version=engine_version,
            book_type=self.queue_settings.book_type if self.queue_aware else None,
            queue_aware=self.queue_aware,
        )
