"""Deterministic market-data stream lifecycle and offline transport double (Phase 15).

The live canary will depend on a persistent Binance market-data stream, and Binance recycles
connections (a documented ~24h disconnect) while a silent socket can stay logically open and stop
producing data. Neither may ever be able to feed a new-risk decision with stale state, so this
module makes the lifecycle explicit and deterministic:

    CONNECTED -> RESUBSCRIBING -> RESYNCING -> READY

and on a clean close or a silent timeout::

    (any state) -> STALE -> RECONNECTING -> CONNECTED -> RESUBSCRIBING -> RESYNCING -> READY

Guarantees enforced here:

* new risk is permitted only in ``ready``, and ``ready`` requires a fresh snapshot bound to the
  CURRENT session (a pre-disconnect snapshot is invalidated by construction);
* a clean close and a silent socket stay distinct reason codes (``CLEAN_CLOSE`` /
  ``SILENT_TIMEOUT``); the timeout is measured from the last VALID message, so recovery never
  depends solely on TCP close detection;
* reconnect is bounded by :class:`BoundedReconnectPolicy` with deterministic, jitter-free backoff
  and injected clock/sleep, so there is no infinite tight retry loop;
* every transition is journaled as the additive, secret-free ``stream.lifecycle.recorded``
  contract;
* no LLM, strategy or configuration path can drive a transition: the only inputs are typed
  lifecycle methods and the injected transport.

This module performs no networking of its own. The production adapter would supply a real
transport; the tests use :class:`OfflineStreamTransport`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from crypto_trader.events import StreamLifecyclePayload, StreamLifecycleRecorded
from crypto_trader.persistence import EventJournal
from crypto_trader.safety import SafetyMode
from crypto_trader.validity import MarketObservation

from .errors import (
    MainnetConfigError,
    StreamLifecycleError,
    StreamRecoveryExhaustedError,
    StreamSessionMismatchError,
    StreamStateError,
    StreamTransportError,
)

__all__ = [
    "BoundedReconnectPolicy",
    "OfflineStreamTransport",
    "StreamLifecycleRecord",
    "StreamLifecycleState",
    "StreamMarketStateProvider",
    "StreamReason",
    "StreamSnapshot",
    "StreamSupervisor",
]

#: Fragments that must never appear in a lifecycle record. They mirror the break-glass guard so a
#: credential pasted into any future free-text field is refused before it reaches the journal.
_CREDENTIAL_MARKERS = (
    "api_key",
    "api-key",
    "apikey",
    "api_secret",
    "apisecret",
    "secret_key",
    "secretkey",
    "x-mbx",
    "signature=",
    "bearer ",
    "authorization",
)


class StreamLifecycleState(str, Enum):
    """The complete stream lifecycle vocabulary. New risk is permitted only in ``READY``."""

    CONNECTED = "connected"
    STALE = "stale"
    RECONNECTING = "reconnecting"
    RESUBSCRIBING = "resubscribing"
    RESYNCING = "resyncing"
    READY = "ready"

    @property
    def permits_new_risk(self) -> bool:
        """True only in ``ready``: every other state keeps new risk blocked."""
        return self is StreamLifecycleState.READY

    @property
    def market_state_is_stale(self) -> bool:
        """True whenever a fresh synchronized snapshot is not available."""
        return self is not StreamLifecycleState.READY


class StreamReason(str, Enum):
    """Deterministic stream reasons. The two disconnect reasons are deliberately distinct."""

    CLEAN_CLOSE = "CLEAN_CLOSE"
    SILENT_TIMEOUT = "SILENT_TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    SUBSCRIPTION_FAILED = "SUBSCRIPTION_FAILED"
    RESYNC_FAILED = "RESYNC_FAILED"
    RECONNECT_BUDGET_EXHAUSTED = "RECONNECT_BUDGET_EXHAUSTED"

    @property
    def is_disconnect(self) -> bool:
        return self in (StreamReason.CLEAN_CLOSE, StreamReason.SILENT_TIMEOUT)


#: The permitted lifecycle transitions. Anything else fails closed.
_ALLOWED_TRANSITIONS: Mapping[StreamLifecycleState, frozenset[StreamLifecycleState]] = {
    StreamLifecycleState.STALE: frozenset(
        {
            StreamLifecycleState.CONNECTED,
            StreamLifecycleState.RECONNECTING,
            StreamLifecycleState.STALE,
        }
    ),
    StreamLifecycleState.CONNECTED: frozenset(
        {
            StreamLifecycleState.RESUBSCRIBING,
            StreamLifecycleState.RECONNECTING,
            StreamLifecycleState.STALE,
        }
    ),
    StreamLifecycleState.RESUBSCRIBING: frozenset(
        {
            StreamLifecycleState.RESYNCING,
            StreamLifecycleState.RECONNECTING,
            StreamLifecycleState.STALE,
        }
    ),
    StreamLifecycleState.RESYNCING: frozenset(
        {
            StreamLifecycleState.READY,
            StreamLifecycleState.RECONNECTING,
            StreamLifecycleState.STALE,
        }
    ),
    StreamLifecycleState.READY: frozenset(
        {StreamLifecycleState.STALE, StreamLifecycleState.RECONNECTING}
    ),
    StreamLifecycleState.RECONNECTING: frozenset(
        {StreamLifecycleState.CONNECTED, StreamLifecycleState.STALE}
    ),
}


def _utc(now: datetime, *, name: str = "now") -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise MainnetConfigError(f"{name} must be timezone-aware.")
    return now.astimezone(timezone.utc)


@dataclass(frozen=True)
class BoundedReconnectPolicy:
    """Deterministic bounded reconnect parameters. There is no unbounded retry path."""

    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    heartbeat_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool):
            raise MainnetConfigError("max_attempts must be an integer.")
        if self.max_attempts < 1:
            raise MainnetConfigError("max_attempts must be at least 1.")
        for name in ("base_delay_seconds", "max_delay_seconds", "heartbeat_timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise MainnetConfigError(f"{name} must be a positive number.")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise MainnetConfigError("max_delay_seconds must be >= base_delay_seconds.")

    def delay_for_attempt(self, attempt: int) -> float:
        """The deterministic, jitter-free delay before a 1-based reconnect attempt."""
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise MainnetConfigError("a reconnect attempt number must be an integer >= 1.")
        if attempt > self.max_attempts:
            raise StreamRecoveryExhaustedError(
                f"reconnect attempt {attempt} exceeds the bounded budget of {self.max_attempts}."
            )
        current = self.base_delay_seconds
        for _ in range(attempt - 1):
            current *= 2
        return float(min(self.max_delay_seconds, current))


@dataclass(frozen=True)
class StreamSnapshot:
    """One fresh, session-bound market snapshot. It is the ONLY input to a new-risk decision."""

    session_id: str
    snapshot_id: str
    symbol: str
    price: Decimal
    market_event_ts: datetime
    local_ingest_ts: datetime
    generation: int

    def to_observation(self) -> MarketObservation:
        """The engine-independent observation the validity/risk layers consume."""
        return MarketObservation(
            symbol=self.symbol,
            price=self.price,
            market_event_ts=self.market_event_ts,
            local_ingest_ts=self.local_ingest_ts,
            market_reference=self.snapshot_id,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "price": str(self.price),
            "market_event_ts": self.market_event_ts.astimezone(timezone.utc).isoformat(),
            "local_ingest_ts": self.local_ingest_ts.astimezone(timezone.utc).isoformat(),
            "generation": self.generation,
        }


@dataclass(frozen=True)
class StreamLifecycleRecord:
    """One immutable lifecycle transition (the in-memory mirror of the journaled payload)."""

    state: StreamLifecycleState
    previous_state: StreamLifecycleState
    reason_code: StreamReason | None
    session_id: str | None
    previous_session_id: str | None
    disconnect_at: datetime | None
    reconnect_at: datetime | None
    resubscription_result: str | None
    resync_result: str | None
    attempt: int
    delay_seconds: float
    new_risk_permitted: bool
    recorded_at: datetime

    def to_record(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "previous_state": self.previous_state.value,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "session_id": self.session_id,
            "previous_session_id": self.previous_session_id,
            "disconnect_at": (
                None if self.disconnect_at is None else self.disconnect_at.isoformat()
            ),
            "reconnect_at": None if self.reconnect_at is None else self.reconnect_at.isoformat(),
            "resubscription_result": self.resubscription_result,
            "resync_result": self.resync_result,
            "attempt": self.attempt,
            "delay_seconds": self.delay_seconds,
            "new_risk_permitted": self.new_risk_permitted,
            "recorded_at": self.recorded_at.isoformat(),
        }


class OfflineStreamTransport:
    """A deterministic offline double for a market-data socket.

    The two required failure modes stay separate:

    * :meth:`close_cleanly` - the socket explicitly closes (``CLEAN_CLOSE``);
    * :meth:`go_silent` - the socket stays logically open but emits no data/heartbeat/pong, so the
      supervisor's own data timeout fires (``SILENT_TIMEOUT``).

    The double never touches the network and never carries credential material.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        session_id_factory: Callable[[], str] | None = None,
        prices: Mapping[str, Decimal] | None = None,
    ) -> None:
        if not callable(clock):
            raise MainnetConfigError("the offline stream transport requires a clock.")
        self._clock = clock
        self._new_session_id = session_id_factory or (lambda: uuid4().hex)
        self._prices: dict[str, Decimal] = {
            symbol: Decimal(str(price)) for symbol, price in (prices or {}).items()
        }
        self._open = False
        self._session_id: str | None = None
        self._last_valid_message_at: datetime | None = None
        self._clean_close = False
        self._hard_drop = False
        self._silent = False
        self._subscriptions: tuple[str, ...] = ()
        self._snapshot_counter = 0
        self._connect_failures = 0
        self._subscribe_failures = 0
        self._resync_failures = 0

    # --- test injection ------------------------------------------------------

    def inject_connect_failures(self, count: int) -> None:
        self._connect_failures = self._require_count(count)

    def inject_subscribe_failures(self, count: int) -> None:
        self._subscribe_failures = self._require_count(count)

    def inject_resync_failures(self, count: int) -> None:
        self._resync_failures = self._require_count(count)

    def set_price(self, symbol: str, price: Decimal | str) -> None:
        self._prices[symbol] = Decimal(str(price))

    @staticmethod
    def _require_count(count: int) -> int:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise MainnetConfigError("injected failure counts must be non-negative integers.")
        return count

    # --- socket surface ------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def closed_cleanly(self) -> bool:
        return self._clean_close

    @property
    def last_valid_message_at(self) -> datetime | None:
        return self._last_valid_message_at

    @property
    def subscriptions(self) -> tuple[str, ...]:
        return self._subscriptions

    def connect(self) -> str:
        """Open the socket and return the NEW session identity."""
        if self._open:
            raise StreamTransportError("the offline stream transport is already open.")
        if self._connect_failures:
            self._connect_failures -= 1
            raise StreamTransportError("the stream connect attempt was refused (injected).")
        session_id = self._new_session_id()
        if not isinstance(session_id, str) or not session_id.strip():
            raise StreamTransportError("the session id factory returned an unusable identity.")
        self._session_id = session_id
        self._open = True
        self._clean_close = False
        self._hard_drop = False
        self._silent = False
        self._subscriptions = ()
        self._last_valid_message_at = _utc(self._clock(), name="stream clock")
        return session_id

    def close_cleanly(self) -> None:
        """Model an explicit socket close. The supervisor must classify it CLEAN_CLOSE."""
        self._open = False
        self._clean_close = True
        self._subscriptions = ()

    def drop_connection(self) -> None:
        """Model an unexpected transport drop (never classified as a clean close)."""
        self._open = False
        self._hard_drop = True
        self._subscriptions = ()

    def reset_for_reconnect(self) -> None:
        """Tear down the current socket so a reconnect can open a NEW session.

        This is how a silent (logically open) socket is recovered: the supervisor deliberately
        drops the unusable socket itself instead of waiting for a TCP close that may never come.
        """
        self._open = False
        self._clean_close = False
        self._hard_drop = False
        self._silent = False
        self._subscriptions = ()

    def go_silent(self) -> None:
        """Keep the socket logically open but suppress every future message/heartbeat."""
        if not self._open:
            raise StreamTransportError("cannot go silent on a closed socket.")
        self._silent = True

    def resume_emitting(self) -> None:
        self._silent = False

    def heartbeat(self) -> None:
        """Emit one valid heartbeat/pong (a silent socket cannot)."""
        self._require_usable()
        self._last_valid_message_at = _utc(self._clock(), name="stream clock")

    def subscribe(self, channels: Sequence[str]) -> tuple[str, ...]:
        """(Re)subscribe on the current session."""
        self._require_usable()
        if self._subscribe_failures:
            self._subscribe_failures -= 1
            raise StreamTransportError("the resubscription was refused (injected).")
        if not channels:
            raise StreamTransportError("at least one stream channel is required.")
        self._subscriptions = tuple(str(channel) for channel in channels)
        self._last_valid_message_at = _utc(self._clock(), name="stream clock")
        return self._subscriptions

    def snapshot(self, symbol: str) -> StreamSnapshot:
        """Produce a fresh snapshot bound to the CURRENT session."""
        self._require_usable()
        if not self._subscriptions:
            raise StreamTransportError("a snapshot requires an active subscription.")
        if self._resync_failures:
            self._resync_failures -= 1
            raise StreamTransportError("the state resynchronization was refused (injected).")
        price = self._prices.get(symbol, Decimal("27000.00"))
        now = _utc(self._clock(), name="stream clock")
        self._snapshot_counter += 1
        assert self._session_id is not None
        self._last_valid_message_at = now
        return StreamSnapshot(
            session_id=self._session_id,
            snapshot_id=f"{self._session_id}:{symbol}:{self._snapshot_counter}",
            symbol=symbol,
            price=price,
            market_event_ts=now,
            local_ingest_ts=now,
            generation=self._snapshot_counter,
        )

    def disconnect_reason(
        self, now: datetime, *, heartbeat_timeout_seconds: float
    ) -> StreamReason | None:
        """Classify the CURRENT transport condition, or None when the socket looks healthy."""
        if self._hard_drop:
            return StreamReason.TRANSPORT_ERROR
        if self._clean_close:
            return StreamReason.CLEAN_CLOSE
        if not self._open:
            return StreamReason.TRANSPORT_ERROR
        last = self._last_valid_message_at
        if last is None:
            return StreamReason.SILENT_TIMEOUT
        idle = (_utc(now) - last).total_seconds()
        if idle > heartbeat_timeout_seconds:
            return StreamReason.SILENT_TIMEOUT
        return None

    def _require_usable(self) -> None:
        if not self._open:
            raise StreamTransportError("the stream socket is closed.")
        if self._silent:
            raise StreamTransportError("the stream socket is silent; no message is available.")


class StreamSupervisor:
    """The deterministic stream lifecycle state machine (no LLM, no configuration bypass)."""

    def __init__(
        self,
        *,
        transport: OfflineStreamTransport,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] | None = None,
        policy: BoundedReconnectPolicy | None = None,
        journal: EventJournal | None = None,
        channels: Sequence[str] = ("bookTicker",),
        symbols: Sequence[str] = ("BTCUSDT",),
        producer: str = "application.mainnet.stream",
        correlation_id: UUID | None = None,
    ) -> None:
        if not isinstance(transport, OfflineStreamTransport):
            raise MainnetConfigError("a stream transport is required.")
        if not callable(clock):
            raise MainnetConfigError("the stream supervisor requires a clock.")
        if not symbols:
            raise MainnetConfigError("the stream supervisor requires at least one symbol.")
        self._transport = transport
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._policy = policy or BoundedReconnectPolicy()
        self._journal = journal
        self._channels = tuple(str(channel) for channel in channels)
        self._symbols = tuple(str(symbol) for symbol in symbols)
        self._producer = producer
        self._correlation_id = correlation_id or uuid4()
        self._state = StreamLifecycleState.STALE
        self._session_id: str | None = None
        self._previous_session_id: str | None = None
        self._generation = 0
        self._attempt = 0
        self._delay_seconds = 0.0
        self._last_reason: StreamReason | None = None
        self._disconnect_at: datetime | None = None
        self._reconnect_at: datetime | None = None
        self._resubscription_result: str | None = None
        self._resync_result: str | None = None
        self._snapshots: dict[str, StreamSnapshot] = {}
        self._records: list[StreamLifecycleRecord] = []
        self._sequence = 0
        self._started = False

    # --- reads ---------------------------------------------------------------

    @property
    def policy(self) -> BoundedReconnectPolicy:
        return self._policy

    @property
    def state(self) -> StreamLifecycleState:
        return self._state

    @property
    def new_risk_permitted(self) -> bool:
        """New risk is permitted ONLY in ``ready`` with a fresh current-session snapshot."""
        return self._state.permits_new_risk and self._has_fresh_snapshot()

    @property
    def market_state_is_stale(self) -> bool:
        return not self.new_risk_permitted

    @property
    def required_safety_mode(self) -> SafetyMode:
        """The existing safety vocabulary implied by the lifecycle state."""
        return SafetyMode.NORMAL if self.new_risk_permitted else SafetyMode.HALT_NEW_RISK

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def previous_session_id(self) -> str | None:
        return self._previous_session_id

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def last_reason(self) -> StreamReason | None:
        return self._last_reason

    @property
    def disconnect_at(self) -> datetime | None:
        return self._disconnect_at

    @property
    def reconnect_at(self) -> datetime | None:
        return self._reconnect_at

    @property
    def resubscription_result(self) -> str | None:
        return self._resubscription_result

    @property
    def resync_result(self) -> str | None:
        return self._resync_result

    @property
    def records(self) -> tuple[StreamLifecycleRecord, ...]:
        return tuple(self._records)

    def lifecycle_states(self) -> tuple[StreamLifecycleState, ...]:
        return tuple(record.state for record in self._records)

    def current_snapshot(self, symbol: str) -> StreamSnapshot | None:
        """The snapshot usable for NEW decisions, or None when no fresh state exists."""
        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            return None
        if self._state is not StreamLifecycleState.READY:
            return None
        if snapshot.session_id != self._session_id:
            return None
        return snapshot

    def is_current_snapshot(self, snapshot_id: str) -> bool:
        """True only for a snapshot issued in the CURRENT session/generation."""
        for snapshot in self._snapshots.values():
            if snapshot.snapshot_id == snapshot_id:
                return self.current_snapshot(snapshot.symbol) is not None
        return False

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> StreamLifecycleState:
        """Connect, subscribe and resynchronize. READY requires a fresh snapshot."""
        if self._started:
            raise StreamStateError("the stream supervisor has already been started.")
        self._started = True
        if not self._recover_attempt(attempt=0, delay_seconds=0.0):
            raise StreamTransportError(
                "the initial stream connection could not reach READY; the stream stays stale and "
                "new risk stays blocked until recover() succeeds."
            )
        return self._state

    def detect(self) -> StreamReason | None:
        """Probe the transport. Returns the disconnect reason when one is detected."""
        if self._state is StreamLifecycleState.STALE:
            return self._last_reason
        now = _utc(self._clock(), name="stream clock")
        reason = self._transport.disconnect_reason(
            now, heartbeat_timeout_seconds=self._policy.heartbeat_timeout_seconds
        )
        if reason is None:
            return None
        self._mark_stale(reason)
        return reason

    def recover(self) -> StreamLifecycleState:
        """Run the bounded reconnect -> resubscribe -> resync -> READY sequence."""
        if self._state is StreamLifecycleState.READY:
            raise StreamStateError("the stream is already READY; no recovery is required.")
        # A silent socket stays logically open: drop it ourselves before reconnecting.
        self._transport.reset_for_reconnect()
        self._transition(StreamLifecycleState.RECONNECTING, reason=None)
        for attempt in range(1, self._policy.max_attempts + 1):
            delay = self._policy.delay_for_attempt(attempt)
            self._sleep(delay)
            self._attempt = attempt
            if self._state is not StreamLifecycleState.RECONNECTING:
                self._transition(StreamLifecycleState.RECONNECTING, reason=None)
            if self._recover_attempt(attempt=attempt, delay_seconds=delay):
                return self._state
        self._last_reason = StreamReason.RECONNECT_BUDGET_EXHAUSTED
        self._transition(
            StreamLifecycleState.STALE,
            reason=StreamReason.RECONNECT_BUDGET_EXHAUSTED,
            attempt=self._policy.max_attempts,
        )
        raise StreamRecoveryExhaustedError(
            f"the bounded reconnect budget of {self._policy.max_attempts} attempts was exhausted; "
            "the stream stays stale and new risk stays blocked."
        )

    # --- internals -----------------------------------------------------------

    def _has_fresh_snapshot(self) -> bool:
        return all(self.current_snapshot(symbol) is not None for symbol in self._symbols)

    def _recover_attempt(self, *, attempt: int, delay_seconds: float) -> bool:
        # A previous attempt may have left a half-open socket behind; never reuse it.
        self._transport.reset_for_reconnect()
        try:
            session_id = self._transport.connect()
        except StreamTransportError:
            self._transition(
                StreamLifecycleState.STALE,
                reason=StreamReason.TRANSPORT_ERROR,
                attempt=attempt,
                delay_seconds=delay_seconds,
            )
            return False
        self._previous_session_id = self._session_id
        self._session_id = session_id
        self._generation += 1
        self._snapshots.clear()
        self._reconnect_at = _utc(self._clock(), name="stream clock")
        self._transition(
            StreamLifecycleState.CONNECTED,
            reason=None,
            attempt=attempt,
            delay_seconds=delay_seconds,
        )

        self._resubscription_result = "pending"
        self._transition(
            StreamLifecycleState.RESUBSCRIBING,
            reason=None,
            attempt=attempt,
            delay_seconds=delay_seconds,
        )
        try:
            self._transport.subscribe(self._channels)
        except StreamTransportError:
            self._resubscription_result = "failed"
            self._transition(
                StreamLifecycleState.RECONNECTING,
                reason=StreamReason.SUBSCRIPTION_FAILED,
                attempt=attempt,
                delay_seconds=delay_seconds,
            )
            return False
        self._resubscription_result = "succeeded"

        self._resync_result = "pending"
        self._transition(
            StreamLifecycleState.RESYNCING,
            reason=None,
            attempt=attempt,
            delay_seconds=delay_seconds,
        )
        fresh: dict[str, StreamSnapshot] = {}
        try:
            for symbol in self._symbols:
                fresh[symbol] = self._transport.snapshot(symbol)
        except StreamTransportError:
            self._resync_result = "failed"
            self._transition(
                StreamLifecycleState.RECONNECTING,
                reason=StreamReason.RESYNC_FAILED,
                attempt=attempt,
                delay_seconds=delay_seconds,
            )
            return False
        self._resync_result = "succeeded"
        for snapshot in fresh.values():
            if snapshot.session_id != self._session_id:
                raise StreamSessionMismatchError(
                    "a resynchronized snapshot did not belong to the current stream session."
                )
        self._snapshots = fresh
        self._transition(
            StreamLifecycleState.READY,
            reason=None,
            attempt=attempt,
            delay_seconds=delay_seconds,
        )
        return True

    def _mark_stale(self, reason: StreamReason) -> None:
        if self._state is StreamLifecycleState.STALE:
            self._last_reason = reason
            return
        self._disconnect_at = _utc(self._clock(), name="stream clock")
        # A reconnect timestamp belongs to the reconnect that follows, never to the disconnect.
        self._reconnect_at = None
        self._last_reason = reason
        self._snapshots.clear()
        self._transition(StreamLifecycleState.STALE, reason=reason)

    def _transition(
        self,
        target: StreamLifecycleState,
        *,
        reason: StreamReason | None,
        attempt: int | None = None,
        delay_seconds: float | None = None,
    ) -> StreamLifecycleRecord:
        if not isinstance(target, StreamLifecycleState):
            raise StreamStateError("a StreamLifecycleState is required for a transition.")
        if target not in _ALLOWED_TRANSITIONS[self._state]:
            raise StreamStateError(
                f"an illegal stream transition {self._state.value!r} -> {target.value!r} was "
                "refused; the lifecycle is deterministic and fail-closed."
            )
        previous = self._state
        self._state = target
        if attempt is not None:
            self._attempt = attempt
        if delay_seconds is not None:
            self._delay_seconds = delay_seconds
        record = StreamLifecycleRecord(
            state=target,
            previous_state=previous,
            reason_code=reason,
            session_id=self._session_id,
            previous_session_id=self._previous_session_id,
            disconnect_at=self._disconnect_at,
            reconnect_at=self._reconnect_at,
            resubscription_result=self._resubscription_result,
            resync_result=self._resync_result,
            attempt=self._attempt,
            delay_seconds=self._delay_seconds,
            new_risk_permitted=self.new_risk_permitted,
            recorded_at=_utc(self._clock(), name="stream clock"),
        )
        self._records.append(record)
        self._journal_record(record)
        return record

    def _journal_record(self, record: StreamLifecycleRecord) -> None:
        payload = StreamLifecyclePayload(
            state=record.state.value,
            previous_state=record.previous_state.value,
            reason_code=None if record.reason_code is None else record.reason_code.value,
            session_id=record.session_id,
            previous_session_id=record.previous_session_id,
            disconnect_at=record.disconnect_at,
            reconnect_at=record.reconnect_at,
            resubscription_result=record.resubscription_result,
            resync_result=record.resync_result,
            attempt=record.attempt,
            delay_seconds=record.delay_seconds,
            new_risk_permitted=record.new_risk_permitted,
        )
        self._assert_secret_free(payload)
        if self._journal is None:
            return
        self._sequence += 1
        self._journal.append(
            StreamLifecycleRecorded(
                occurred_at=record.recorded_at,
                correlation_id=self._correlation_id,
                producer=self._producer,
                sequence_number=self._sequence,
                payload=payload,
            )
        )

    @staticmethod
    def _assert_secret_free(payload: StreamLifecyclePayload) -> None:
        for value in payload.model_dump().values():
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            for marker in _CREDENTIAL_MARKERS:
                if marker in lowered:
                    raise StreamLifecycleError(
                        "a stream lifecycle record looked like it contained credential material; "
                        "nothing secret is ever journaled."
                    )


class StreamMarketStateProvider:
    """The engine-independent market-state provider bound to the stream lifecycle.

    It returns an observation ONLY when the stream is READY and the snapshot belongs to the
    current session. Any other condition yields ``None``, which the existing Phase 6 validity gate
    already fails closed as ``stale`` / ``market_state_unavailable``.
    """

    def __init__(self, supervisor: StreamSupervisor) -> None:
        if not isinstance(supervisor, StreamSupervisor):
            raise MainnetConfigError("a StreamSupervisor is required.")
        self._supervisor = supervisor

    @property
    def supervisor(self) -> StreamSupervisor:
        return self._supervisor

    def latest(self, symbol: str) -> MarketObservation | None:
        snapshot = self._supervisor.current_snapshot(symbol)
        return None if snapshot is None else snapshot.to_observation()
