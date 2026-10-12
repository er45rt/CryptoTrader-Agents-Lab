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


    # Temporal mode is private to the offline Paper/Nautilus bridge. The existing
    # simulate() batch entry point remains intact for Phase-4 callers.
    def start_temporal(self) -> None:
        if getattr(self, "_temporal_runtime", None) is not None:
            raise RuntimeError("temporal simulation is already started.")
        runtime = NautilusExecutionRuntime(
            self.instrument,
            queue_aware=self.queue_aware,
            queue_settings=self.queue_settings,
        )
        runtime.start()
        try:
            runtime.enable_temporal()
            if self.book_updates or self.trades:
                runtime.feed_market_data(book_updates=self.book_updates, trades=self.trades)
        except Exception:
            runtime.stop()
            raise
        self._temporal_runtime = runtime
        self._temporal_timeline = sorted(
            {item.ts_ns for item in self.book_updates}
            | {item.ts_ns for item in self.trades}
        )
        self._temporal_cursor = 0
        self._temporal_boundary = 0

    def _temporal_snapshot(self) -> SimulationOutcome:
        runtime = getattr(self, "_temporal_runtime", None)
        if runtime is None:
            raise RuntimeError("temporal simulation has not started.")
        return SimulationOutcome(
            observations=tuple(runtime.observations()),
            phase3_events=tuple(runtime.phase3_events()),
            engine_version=runtime.engine_version(),
            book_type=self.queue_settings.book_type if self.queue_aware else None,
            queue_aware=self.queue_aware,
        )

    def advance_until(self, timestamp_ns: int) -> SimulationOutcome:
        if type(timestamp_ns) is not int or timestamp_ns <= 0:
            raise ValueError("temporal boundary must be positive integer nanoseconds.")
        runtime = getattr(self, "_temporal_runtime", None)
        if runtime is None or timestamp_ns < self._temporal_boundary:
            raise RuntimeError("temporal replay cannot move backwards or start implicitly.")
        # Commit the requested logical time only after successful advancement.
        next_cursor = self._temporal_cursor
        while (
            next_cursor < len(self._temporal_timeline)
            and self._temporal_timeline[next_cursor] <= timestamp_ns
        ):
            next_cursor += 1
        if next_cursor > self._temporal_cursor:
            # All tied market events were loaded into the SAME engine ahead of time.
            # Integer nanosecond 'end' is exclusive; use the last whole tie group +1.
            runtime.run_until(end_ns=self._temporal_timeline[next_cursor - 1] + 1)
            self._temporal_cursor = next_cursor
        # Streaming cannot certify delayed timer processing past the last market tick.
        covered = next_cursor > 0 and self._temporal_timeline[next_cursor - 1] == timestamp_ns
        if not covered and runtime.has_unsettled_orders():
            raise RuntimeError(
                "unverified latency/timer state: no market event covers the decision boundary."
            )
        self._temporal_boundary = timestamp_ns
        return self._temporal_snapshot()

    def submit_temporal(self, request: ExecutionRequest, *, decided_ns: int) -> None:
        runtime = getattr(self, "_temporal_runtime", None)
        if runtime is None or type(decided_ns) is not int or decided_ns != self._temporal_boundary:
            raise RuntimeError("timed submission must follow an advance to decision completion.")
        runtime.submit_timed(request, decided_ns=decided_ns)

    def market_horizon_ns(self) -> int:
        """Last timestamp supported by the immutable simulated market-data tape."""
        return max(self._temporal_timeline, default=0)

    def finish_temporal(self) -> SimulationOutcome:
        runtime = getattr(self, "_temporal_runtime", None)
        if runtime is None:
            raise RuntimeError("temporal replay already completed or not started.")
        try:
            if self._temporal_cursor < len(self._temporal_timeline):
                runtime.run_until(end_ns=self._temporal_timeline[-1] + 1)
                self._temporal_cursor = len(self._temporal_timeline)
            if self._temporal_timeline:
                runtime.end_temporal()
            return self._temporal_snapshot()
        finally:
            runtime.stop()
            self._temporal_runtime = None

    def abort_temporal(self) -> None:
        """Dispose an aborted offline replay without processing pending market events."""
        runtime = getattr(self, "_temporal_runtime", None)
        if runtime is not None:
            try:
                runtime.stop()
            finally:
                self._temporal_runtime = None
