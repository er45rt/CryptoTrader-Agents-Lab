"""Phase 15: offline WebSocket/stream resilience coverage (clean close, silent socket).

Every test here is deterministic and offline: the transport double never touches the network and
the clock/sleep are injected, so backoff is asserted exactly instead of waited out.

Required checklist coverage:

* test_A_clean_close_recovers_through_a_bounded_deterministic_sequence
* test_B_silent_socket_recovers_through_a_separate_bounded_sequence
* test_C_pre_disconnect_decision_can_never_become_executable
* test_D_lifecycle_journal_is_deterministic_classified_and_secret_free
"""

from __future__ import annotations

import itertools
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.events import STREAM_LIFECYCLE_EVENT_TYPE
from crypto_trader.mainnet import (
    API_KEY_ENV_VAR,
    API_SECRET_ENV_VAR,
    BoundedReconnectPolicy,
    DryRunDisposition,
    OfflineStreamTransport,
    StreamLifecycleState,
    StreamMarketStateProvider,
    StreamReason,
    StreamSupervisor,
    StreamTransportError,
)
from crypto_trader.mainnet.errors import StreamRecoveryExhaustedError, StreamStateError
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.safety import SafetyMode
from crypto_trader.tests import mainnet_fixtures as fx

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"
PRICE = Decimal("27000.00")

#: Small, explicit test policy: 3 attempts, 2s -> 4s -> 8s, 30s heartbeat timeout.
POLICY = BoundedReconnectPolicy(
    max_attempts=3,
    base_delay_seconds=2.0,
    max_delay_seconds=8.0,
    heartbeat_timeout_seconds=30.0,
)

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


class FakeClock:
    """Deterministic injectable clock whose sleep ADVANCES time (no real waiting)."""

    def __init__(self, start: datetime = BASE) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@contextmanager
def _stream(tmp_path: Path, *, policy: BoundedReconnectPolicy = POLICY):
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = FakeClock()
    counter = itertools.count(1)
    transport = OfflineStreamTransport(
        clock=clock, session_id_factory=lambda: f"session-{next(counter)}", prices={SYMBOL: PRICE}
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        supervisor = StreamSupervisor(
            transport=transport,
            clock=clock,
            sleep=clock.sleep,
            policy=policy,
            journal=journal,
            symbols=(SYMBOL,),
        )
        yield clock, transport, supervisor, journal


def _lifecycle_events(journal) -> list:
    return [
        event
        for event in journal.read_range()
        if event.event_type == STREAM_LIFECYCLE_EVENT_TYPE
    ]


def _ready_with_snapshot(supervisor: StreamSupervisor) -> bool:
    return supervisor.new_risk_permitted and supervisor.current_snapshot(SYMBOL) is not None


# --- A: clean close ---------------------------------------------------------

def test_A_clean_close_recovers_through_a_bounded_deterministic_sequence(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()

        assert supervisor.state is StreamLifecycleState.READY
        assert supervisor.new_risk_permitted is True
        first_session = supervisor.session_id
        first_snapshot = supervisor.current_snapshot(SYMBOL)
        assert first_snapshot is not None

        # 1. an explicit clean close.
        transport.close_cleanly()
        assert transport.is_open is False

        # 2. detected, classified CLEAN_CLOSE (distinct from a silent socket).
        reason = supervisor.detect()
        assert reason is StreamReason.CLEAN_CLOSE
        assert supervisor.last_reason is StreamReason.CLEAN_CLOSE

        # 3. market state becomes stale.
        assert supervisor.state is StreamLifecycleState.STALE
        assert supervisor.market_state_is_stale is True
        assert supervisor.current_snapshot(SYMBOL) is None

        # 4. NO_NEW_RISK, using the existing safety vocabulary.
        assert supervisor.new_risk_permitted is False
        assert supervisor.required_safety_mode is SafetyMode.HALT_NEW_RISK

        # 5/6. bounded reconnect with deterministic backoff, resubscription, resync.
        supervisor.recover()
        assert clock.sleeps == [POLICY.delay_for_attempt(1)] == [2.0]
        assert supervisor.state is StreamLifecycleState.READY
        assert supervisor.session_id is not None
        assert supervisor.session_id != first_session
        assert supervisor.previous_session_id == first_session
        assert transport.subscriptions == ("bookTicker",)
        assert supervisor.resubscription_result == "succeeded"
        assert supervisor.resync_result == "succeeded"

        # 7. only a FRESH post-reconnect snapshot can feed new decisions.
        second_snapshot = supervisor.current_snapshot(SYMBOL)
        assert second_snapshot is not None
        assert second_snapshot.snapshot_id != first_snapshot.snapshot_id
        assert supervisor.is_current_snapshot(first_snapshot.snapshot_id) is False
        assert supervisor.is_current_snapshot(second_snapshot.snapshot_id) is True
        assert supervisor.new_risk_permitted is True

        # The full deterministic lifecycle sequence, in order.
        assert supervisor.lifecycle_states() == (
            StreamLifecycleState.CONNECTED,
            StreamLifecycleState.RESUBSCRIBING,
            StreamLifecycleState.RESYNCING,
            StreamLifecycleState.READY,
            StreamLifecycleState.STALE,
            StreamLifecycleState.RECONNECTING,
            StreamLifecycleState.CONNECTED,
            StreamLifecycleState.RESUBSCRIBING,
            StreamLifecycleState.RESYNCING,
            StreamLifecycleState.READY,
        )
        assert _lifecycle_events(journal)[4].payload.reason_code == "CLEAN_CLOSE"


# --- B: silent socket (NO valid message / heartbeat / pong) -----------------

def test_B_silent_socket_recovers_through_a_separate_bounded_sequence(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()
        first_session = supervisor.session_id
        first_snapshot = supervisor.current_snapshot(SYMBOL)
        assert first_snapshot is not None

        # The socket stays LOGICALLY OPEN but produces no valid message/heartbeat/pong.
        transport.go_silent()
        assert transport.is_open is True

        # Before the heartbeat/data timeout nothing is stale yet.
        clock.advance(POLICY.heartbeat_timeout_seconds - 1)
        assert supervisor.detect() is None
        assert supervisor.state is StreamLifecycleState.READY

        # Deterministic timeout, measured from the last VALID message (not TCP close).
        clock.advance(2)
        reason = supervisor.detect()
        assert reason is StreamReason.SILENT_TIMEOUT
        assert transport.is_open is True  # never conflated with a clean close
        assert supervisor.last_reason is StreamReason.SILENT_TIMEOUT
        assert supervisor.state is StreamLifecycleState.STALE
        assert supervisor.market_state_is_stale is True
        assert supervisor.new_risk_permitted is False
        assert supervisor.required_safety_mode is SafetyMode.HALT_NEW_RISK
        assert supervisor.current_snapshot(SYMBOL) is None

        supervisor.recover()
        assert clock.sleeps == [POLICY.delay_for_attempt(1)] == [2.0]
        assert supervisor.state is StreamLifecycleState.READY
        assert supervisor.session_id != first_session
        assert supervisor.previous_session_id == first_session
        assert supervisor.resubscription_result == "succeeded"
        assert supervisor.resync_result == "succeeded"
        second_snapshot = supervisor.current_snapshot(SYMBOL)
        assert second_snapshot is not None
        assert second_snapshot.snapshot_id != first_snapshot.snapshot_id
        assert supervisor.new_risk_permitted is True

        reasons = [
            event.payload.reason_code
            for event in _lifecycle_events(journal)
            if event.payload.reason_code is not None
        ]
        assert reasons == ["SILENT_TIMEOUT"]


# --- C: pre-disconnect decisions can never become executable ----------------

def test_C_pre_disconnect_decision_can_never_become_executable(tmp_path):
    state_dir = tmp_path / "state"
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()
        provider = StreamMarketStateProvider(supervisor)
        first_snapshot = supervisor.current_snapshot(SYMBOL)
        assert first_snapshot is not None

        chosen: dict[str, str | None] = {"reference": None}

        def decision_source(snapshot):
            stamp = snapshot.market_event_ts
            return fx.intent(
                f"ws-{uuid4().hex[:8]}",
                market_reference=chosen["reference"] or snapshot.market_reference,
                market_event_ts=stamp,
                decision_started_at=stamp,
                decision_completed_at=stamp,
            )

        pipeline = fx.pipeline(
            journal=journal,
            state_dir=state_dir,
            tmp_path=tmp_path,
            market=provider,
            decision_source=decision_source,
        )
        pipeline.start()

        # 1. READY: a fresh snapshot + fresh decision may proceed (dry-run WOULD_SUBMIT).
        first = pipeline.run_cycle()
        assert first.disposition is DryRunDisposition.WOULD_SUBMIT

        # 2. disconnect: state and decisions become unusable immediately.
        transport.close_cleanly()
        assert supervisor.detect() is StreamReason.CLEAN_CLOSE
        assert supervisor.new_risk_permitted is False

        second = pipeline.run_cycle()
        assert second.disposition is DryRunDisposition.WOULD_REJECT
        assert second.reason_code == "market_state_unavailable"

        # 3. reconnect + resync produces a NEW session and a NEW snapshot.
        supervisor.recover()
        second_snapshot = supervisor.current_snapshot(SYMBOL)
        assert second_snapshot is not None
        assert second_snapshot.snapshot_id != first_snapshot.snapshot_id
        assert supervisor.is_current_snapshot(first_snapshot.snapshot_id) is False

        # 4. the OLD decision reference still cannot become executable after reconnect.
        chosen["reference"] = first_snapshot.snapshot_id
        third = pipeline.run_cycle()
        assert third.disposition is DryRunDisposition.WOULD_REJECT
        assert third.reason_code == "decision_snapshot_mismatch"

        # 5. a NEW post-reconnect snapshot + fresh decision/validity/risk may proceed again.
        chosen["reference"] = None
        fourth = pipeline.run_cycle()
        assert fourth.disposition is DryRunDisposition.WOULD_SUBMIT
        assert fourth.snapshot_reference == second_snapshot.snapshot_id

        # Nothing was ever submitted or constructed against the venue.
        assert pipeline.gate.mutation_attempts == 0
        assert pipeline.status()["mainnet_mutation_attempts"] == 0
        assert all("oms:dry_run_representation" not in result.stages for result in (second, third))


# --- D: auditable, deterministic, secret-free classification ----------------

def _journal_fingerprint(events) -> list[tuple]:
    return [
        (
            event.payload.state,
            event.payload.previous_state,
            event.payload.reason_code,
            event.payload.disconnect_at.isoformat() if event.payload.disconnect_at else None,
            event.payload.reconnect_at.isoformat() if event.payload.reconnect_at else None,
            event.payload.resubscription_result,
            event.payload.resync_result,
            event.payload.attempt,
            event.payload.delay_seconds,
            event.payload.new_risk_permitted,
            event.payload.session_id,
            event.payload.previous_session_id,
        )
        for event in events
    ]


def test_D_lifecycle_journal_is_deterministic_classified_and_secret_free(tmp_path):
    runs: list[tuple[list[tuple], list[dict]]] = []
    for run in range(2):
        with _stream(tmp_path / f"run-{run}") as (clock, transport, supervisor, journal):
            supervisor.start()
            transport.close_cleanly()
            assert supervisor.detect() is StreamReason.CLEAN_CLOSE
            supervisor.recover()
            transport.go_silent()
            clock.advance(POLICY.heartbeat_timeout_seconds + 1)
            assert supervisor.detect() is StreamReason.SILENT_TIMEOUT
            supervisor.recover()
            events = _lifecycle_events(journal)
            runs.append((_journal_fingerprint(events), [e.model_dump(mode="json") for e in events]))

    first, second = runs
    assert first[0] == second[0]  # deterministic classification, timestamps and delays

    fingerprint = first[0]
    reasons = {row[2] for row in fingerprint if row[2] is not None}
    assert reasons == {"CLEAN_CLOSE", "SILENT_TIMEOUT"}  # distinct reasons, both recorded

    clean_rows = [row for row in fingerprint if row[2] == "CLEAN_CLOSE"]
    silent_rows = [row for row in fingerprint if row[2] == "SILENT_TIMEOUT"]
    assert clean_rows and silent_rows
    for row in (*clean_rows, *silent_rows):
        assert row[3] is not None  # disconnect timestamp
        assert row[4] is None  # a reconnect timestamp is not applicable to a disconnect record
        assert row[0] == "stale"
        assert row[9] is False  # new_risk_permitted
    # Each disconnect is followed by a reconnect whose timestamp is strictly later.
    for row in (*clean_rows, *silent_rows):
        index = fingerprint.index(row)
        later_reconnects = [
            later[4]
            for later in fingerprint[index + 1 :]
            if later[0] == "ready" and later[4] is not None
        ]
        assert later_reconnects
        assert later_reconnects[0] > row[3]
    assert clean_rows[0][0] == "stale"
    assert silent_rows[0][0] == "stale"

    # Prior/new session identity, resubscription/state-resync outcomes, final lifecycle state.
    ready_rows = [row for row in fingerprint if row[0] == "ready"]
    assert len(ready_rows) == 3  # initial READY + one after each recovery
    reconnect_ready_rows = [row for row in ready_rows if row[11] is not None]
    assert len(reconnect_ready_rows) == 2
    for row in reconnect_ready_rows:
        assert row[5] == "succeeded" and row[6] == "succeeded"  # resubscribe + resync outcomes
        assert row[10] != row[11]  # new session identity differs from the prior one
    for step in ("connected", "resubscribing", "resyncing", "ready"):
        assert step in {row[0] for row in fingerprint}

    rendered = json.dumps(first[1])
    lowered = rendered.lower()
    for marker in _CREDENTIAL_MARKERS:
        assert marker not in lowered
    for name in (API_KEY_ENV_VAR, API_SECRET_ENV_VAR):
        value = os.environ.get(name)
        if value:
            assert value not in rendered


# --- supporting coverage: bounded retry, fresh-snapshot READY gating --------

def test_reconnect_is_bounded_with_deterministic_backoff_and_no_tight_loop(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()
        transport.close_cleanly()
        assert supervisor.detect() is StreamReason.CLEAN_CLOSE

        transport.inject_connect_failures(99)  # every bounded attempt fails
        with pytest.raises(StreamRecoveryExhaustedError):
            supervisor.recover()

        assert clock.sleeps == [
            POLICY.delay_for_attempt(attempt) for attempt in range(1, POLICY.max_attempts + 1)
        ]
        assert len(clock.sleeps) == POLICY.max_attempts  # bounded, never an infinite tight loop
        assert supervisor.state is StreamLifecycleState.STALE
        assert supervisor.new_risk_permitted is False
        assert supervisor.last_reason is StreamReason.RECONNECT_BUDGET_EXHAUSTED
        final = _lifecycle_events(journal)[-1].payload
        assert final.reason_code == "RECONNECT_BUDGET_EXHAUSTED"
        assert final.state == "stale"
        assert final.new_risk_permitted is False


def test_ready_requires_a_successful_resubscription_and_fresh_snapshot(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()
        transport.close_cleanly()
        assert supervisor.detect() is StreamReason.CLEAN_CLOSE

        transport.inject_subscribe_failures(1)  # first recovery attempt cannot resubscribe
        supervisor.recover()

        assert clock.sleeps == [POLICY.delay_for_attempt(1), POLICY.delay_for_attempt(2)] == [2.0, 4.0]
        assert supervisor.state is StreamLifecycleState.READY
        assert supervisor.resubscription_result == "succeeded"
        assert supervisor.resync_result == "succeeded"
        assert _ready_with_snapshot(supervisor)
        assert "SUBSCRIPTION_FAILED" in {
            event.payload.reason_code for event in _lifecycle_events(journal)
        }


def test_stream_market_state_provider_fails_closed_unless_ready(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        provider = StreamMarketStateProvider(supervisor)
        assert provider.latest(SYMBOL) is None  # not started yet: stale by construction

        supervisor.start()
        observation = provider.latest(SYMBOL)
        assert observation is not None
        assert observation.market_reference == supervisor.current_snapshot(SYMBOL).snapshot_id

        transport.close_cleanly()
        assert supervisor.detect() is StreamReason.CLEAN_CLOSE
        assert provider.latest(SYMBOL) is None


def test_recovery_is_refused_when_already_ready(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()
        with pytest.raises(StreamStateError):
            supervisor.recover()


def test_a_closed_socket_cannot_deliver_data(tmp_path):
    with _stream(tmp_path) as (clock, transport, supervisor, journal):
        supervisor.start()
        transport.close_cleanly()
        with pytest.raises(StreamTransportError):
            transport.snapshot(SYMBOL)
