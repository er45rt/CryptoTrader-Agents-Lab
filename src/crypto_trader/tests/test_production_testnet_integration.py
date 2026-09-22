"""Phase 14: the production runtime over the real Binance SPOT TESTNET adapter (scripted transport).

Every order in this module is offline: the Phase 11 adapter is driven by the scripted transport
double, so no credential value is used and no network request is made.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.events import ORDER_STATE_EVENT_TYPE
from crypto_trader.breakglass import BinanceSpotTestnetEmergencyControl
from crypto_trader.execution.binance_testnet import (
    SPOT_TESTNET_REST_BASE,
    AmbiguousOrderError,
    TransportTimeout,
    UnsafeTestnetAccountError,
)
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import reconstruct_order_states
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT,
    build_runtime,
    control_for,
    intent,
    operator_command,
)

START_CORRELATION_ID = UUID("2f0a1d84-6c37-4a19-9d52-7e3b8c04a1f6")


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _reference_for(intent_obj) -> str:
    """The adapter's deterministic idempotency key (never invented by the runtime)."""
    return f"ct-{intent_obj.intent_id.hex[:20]}"


def _wired(journal, state_dir, transport, *, control=None):
    client = fx.client(transport)
    port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    runtime = build_runtime(
        journal=journal, state_dir=state_dir, execution=port, control=control
    )
    return runtime, port, client


# --- 12: testnet execution still works through the production runtime -------------

def test_testnet_execution_flows_through_the_production_runtime(journal, state_dir):
    intent_obj = intent("testnet-flow-1")
    transport = fx.ScriptedTransport(
        [*fx.environment_responses(), fx.order_response(_reference_for(intent_obj))]
    )
    runtime, port, _client = _wired(journal, state_dir, transport)

    report = runtime.start()
    assert report.environment == "testnet"
    assert report.base_url == SPOT_TESTNET_REST_BASE
    assert report.safety_mode == "normal"

    outcome = runtime.execute_intent(intent_obj)

    assert outcome.submitted is True
    assert outcome.submission is not None
    assert outcome.submission.order_reference == _reference_for(intent_obj)
    assert outcome.submission.fence_term == 1
    assert outcome.submission.safety_mode == "normal"
    assert outcome.submission.environment == "testnet"
    assert port.observations()
    assert ORDER_STATE_EVENT_TYPE in [event.event_type for event in journal.replay()]
    writes = [request for request in transport.requests if request.is_write]
    assert len(writes) == 1


# --- 13: ambiguity is preserved, never retried -------------------------------------

def test_an_ambiguous_submit_stays_unknown_pending_reconciliation(journal, state_dir):
    transport = fx.ScriptedTransport(
        [*fx.environment_responses(), TransportTimeout("the write outcome is unknown")]
    )
    runtime, port, _client = _wired(journal, state_dir, transport)
    runtime.start()

    with pytest.raises(AmbiguousOrderError):
        runtime.execute_intent(intent("ambiguous-1"))

    assert runtime.status()["last_outcome"] == "unknown_pending_reconciliation"
    writes = [request for request in transport.requests if request.is_write]
    assert len(writes) == 1, "an ambiguous write must never be blindly retried"
    pending = [
        state for state in port.local_states() if state.submission_attempted and state.is_open
    ]
    assert pending, "the ambiguous submission must stay pending for reconciliation"


# --- 16: the withdrawal-disabled invariant still fails closed ----------------------

def test_a_withdrawal_capable_key_is_refused_before_any_order(journal, state_dir):
    transport = fx.ScriptedTransport(fx.environment_responses(can_withdraw=True))
    runtime, _port, _client = _wired(journal, state_dir, transport)

    with pytest.raises(UnsafeTestnetAccountError):
        runtime.start()

    assert runtime.state().value == "stopped"
    assert runtime.status()["last_refusal"].startswith("execution_boundary_start_failed")
    assert not [request for request in transport.requests if request.is_write]


# --- 17: no credential value can leak into the runtime's observable surface --------

def test_no_credential_value_leaks_into_records_or_the_journal(journal, state_dir):
    intent_obj = intent("secret-leak-1")
    transport = fx.ScriptedTransport(
        [*fx.environment_responses(), fx.order_response(_reference_for(intent_obj))]
    )
    runtime, port, client = _wired(journal, state_dir, transport)
    runtime.start()
    outcome = runtime.execute_intent(intent_obj)

    observable = json.dumps(
        {
            "status": runtime.status(),
            "startup": runtime.startup_report().to_record(),
            "submission": outcome.submission.to_record(),
            "requests": list(client.request_log()),
            "queries": [request.query for request in transport.requests],
            "journal": [str(event.payload) for event in journal.replay()],
        }
    )

    for secret in (fx.FAKE_API_KEY, fx.FAKE_API_SECRET):
        assert secret not in observable, "a credential value reached an observable surface"
    # The credential object, the log-safe request form and the client's own request log are all
    # value-free. (The scripted transport double is a test object, not a production surface.)
    assert fx.FAKE_API_KEY not in repr(client.credentials)
    assert fx.FAKE_API_SECRET not in repr(client.credentials)
    assert all(secret not in "".join(client.request_log()) for secret in (fx.FAKE_API_KEY, fx.FAKE_API_SECRET))
    assert all(
        secret not in request.safe_repr() for request in transport.requests
        for secret in (fx.FAKE_API_KEY, fx.FAKE_API_SECRET)
    )
    assert port.phase3_events()


# --- 18: deterministic journal and replay -----------------------------------------

def _deterministic_run(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    journal_path = root / "journal.db"
    state_dir = root / "state"
    intent_obj = intent("determinism-1")
    with SQLiteEventJournal(journal_path) as journal:
        transport = fx.ScriptedTransport(
            [*fx.environment_responses(), fx.order_response(_reference_for(intent_obj))]
        )
        client = fx.client(transport)
        control = control_for(journal, state_dir)
        port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        runtime = build_runtime(
            journal=journal, state_dir=state_dir, execution=port, control=control
        )
        runtime.start(correlation_id=START_CORRELATION_ID)
        runtime.execute_intent(intent_obj)

        def states():
            reconstructed, _ = reconstruct_order_states(journal)
            return sorted(
                (
                    state.order_reference,
                    str(state.order_id),
                    state.state.value,
                    str(state.filled_quantity),
                )
                for state in reconstructed.states()
            )

        first = states()
        second = states()
        event_types = sorted(event.event_type for event in journal.replay())
    return first, second, event_types


def test_the_journal_and_its_replay_are_deterministic(tmp_path):
    first, replay, events = _deterministic_run(tmp_path / "run-a")
    other, other_replay, other_events = _deterministic_run(tmp_path / "run-b")

    assert first == replay, "replaying the same journal must rebuild the same order state"
    assert first == other, "identical inputs must produce identical order state"
    assert replay == other_replay
    assert events == other_events
    assert first, "the deterministic run must have reconstructed the submitted order"


# --- 19: the normal CANCEL_OPEN emergency behavior remains available ---------------

def test_cancel_open_emergency_behavior_remains_available(journal, state_dir):
    intent_obj = intent("cancel-open-1")
    reference = _reference_for(intent_obj)
    transport = fx.ScriptedTransport(
        [
            *fx.environment_responses(),
            fx.order_response(reference, status="NEW"),
            fx.time_response(),
            fx.account_response(),
            fx.open_orders_response(reference),
            fx.order_response(reference, status="CANCELED"),
        ]
    )
    client = fx.client(transport)
    port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    venue = BinanceSpotTestnetEmergencyControl(client=client, runtime=port)
    control = control_for(journal, state_dir, venue=venue)
    runtime = build_runtime(
        journal=journal, state_dir=state_dir, execution=port, control=control
    )
    runtime.start()
    assert runtime.execute_intent(intent_obj).submitted is True

    outcome = control.handle(operator_command("cancel_open", seed="cancel-open-emergency"))

    assert outcome.disposition == "accepted"
    assert outcome.venue_control == "confirmed"
    assert outcome.resulting_safety_state == "cancel_open"
    assert runtime.status()["new_risk_permitted"] is False
    assert any(request.method == "DELETE" for request in transport.requests)

    blocked = runtime.execute_intent(intent("cancel-open-2"))
    assert blocked.submitted is False
    assert "trading_halted" in (blocked.blocked_reason_code or "")
