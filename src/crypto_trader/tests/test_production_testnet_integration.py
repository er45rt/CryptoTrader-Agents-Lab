"""Phase 14: the production runtime over the real Binance SPOT TESTNET adapter (scripted transport).

Every order in this module is offline: the Phase 11 adapter is driven by the scripted transport
double, so no credential value is used and no network request is made.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from types import SimpleNamespace

import pytest

from crypto_trader.events import ORDER_STATE_EVENT_TYPE, ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
from crypto_trader.breakglass import BinanceSpotTestnetEmergencyControl
from crypto_trader.breakglass.ownership import OwnerKind, OwnershipStore
from crypto_trader.breakglass.errors import StaleWriterFenceError
from crypto_trader.execution import ExecutionSide
from crypto_trader.execution.binance_testnet import (
    SPOT_TESTNET_REST_BASE,
    AmbiguousOrderError,
    RateLimitAdmissionDeferredError,
    WriteIpBannedOutcomeUnknownError,
    WriteRateLimitedOutcomeUnknownError,
    TransportTimeout,
    UnsafeTestnetAccountError,
)
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionRiskStateSource, ProductionStateError
from crypto_trader.production.equity import AccountEquityBaselineWriter
from crypto_trader.recovery import reconstruct_order_states
from crypto_trader.validity import MarketObservation, ReplayMarketStateProvider
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.test_production_live_testnet import _require_reconciliation_closure
from crypto_trader.tests.production_fixtures import (
    BASE,
    INSTRUMENT,
    startup_responses,
    admitted_execute,
    admitted_prepare,
    admitted_submit,
    bind_test_admission,
    build_runtime,
    clock,
    control_for,
    intent,
    limits as production_limits,
    market_state,
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


def _flat_account_response():
    response = fx.account_response()
    for balance in response.body["balances"]:
        if balance["asset"] == "BTC":
            balance["free"] = "0"
    return response


def _risk_refresh_responses():
    return [
        fx.time_response(),
        fx.open_orders_response(),
        _flat_account_response(),
        fx.price_response("27000.00"),
        fx.open_orders_response(),
    ]


def _wired(
    journal,
    state_dir,
    transport,
    *,
    control=None,
    port=None,
    client=None,
    limits_value=None,
    bind_admission=True,
):
    client = client or fx.client(transport)
    port = port or OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    active_control = control or control_for(journal, state_dir)
    evidence_now = datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc)

    # G5b production writing requires one durable account-equity root and a canonical source.
    baseline_transport = fx.ScriptedTransport([
        fx.time_response(), fx.open_orders_response(), _flat_account_response(), fx.open_orders_response(),
    ])
    baseline_client = fx.client(baseline_transport)
    AccountEquityBaselineWriter(
        journal=journal,
        control=active_control,
        client=baseline_client,
        market_state=ReplayMarketStateProvider([
            MarketObservation(
                symbol=INSTRUMENT.symbol,
                price=Decimal("27000"),
                market_event_ts=evidence_now,
                local_ingest_ts=evidence_now,
                market_reference="integration-baseline-mark",
                sequence=1,
            )
        ]),
        instrument=INSTRUMENT,
        environment="binance-spot-testnet",
        venue_account_id="123456789",
        max_account_age_seconds=5,
        max_mark_age_seconds=5,
        clock=clock(evidence_now),
    ).create()

    # Venue-writing G5b risk evidence must come from the exact same authenticated client
    # that can submit the order. Insert the read-only refresh immediately after startup evidence.
    transport.responses[5:5] = _risk_refresh_responses()
    risk_source = ProductionRiskStateSource(
        journal=journal,
        control=active_control,
        client=client,
        instrument=INSTRUMENT,
        environment="binance-spot-testnet",
        venue_account_id="123456789",
        max_account_age_seconds=13 * 60 * 60,
        max_mark_age_seconds=13 * 60 * 60,
    )
    runtime = build_runtime(
        journal=journal,
        state_dir=state_dir,
        execution=port,
        control=active_control,
        risk_state_source=risk_source,
        limits_value=limits_value,
        bind_admission=bind_admission,
    )
    return runtime, port, client


# --- 12: testnet execution still works through the production runtime -------------

def test_venue_writing_runtime_refuses_start_without_admission_binding(
    journal, state_dir
):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, port, _client = _wired(
        journal,
        state_dir,
        transport,
        bind_admission=False,
    )

    with pytest.raises(ProductionStateError, match="requires a bound"):
        runtime.start()

    status = runtime.status()
    assert status["runtime_state"] == "stopped"
    assert status["last_refusal"] == "admission_unbound"
    assert port.state().value == "stopped"
    assert transport.requests == []


def test_unbound_writing_runtime_refuses_direct_decision_paths_before_io(
    journal, state_dir
):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, _port, _client = _wired(
        journal,
        state_dir,
        transport,
        bind_admission=False,
    )
    candidate = intent("unbound-direct-path")

    with pytest.raises(ProductionStateError, match="requires a bound"):
        runtime.execute_intent(candidate)
    with pytest.raises(ProductionStateError, match="requires a bound"):
        runtime.prepare_intent(candidate)
    with pytest.raises(ProductionStateError, match="requires a bound"):
        runtime.submit_prepared(object())  # token gate must win before payload validation

    assert transport.requests == []
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}


def test_bound_admission_is_the_only_writing_decision_ingress(journal, state_dir):
    intent_obj = intent("bound-admission-ingress")
    transport = fx.ScriptedTransport(
        [*startup_responses(), fx.order_response(_reference_for(intent_obj))]
    )
    runtime, _port, _client = _wired(
        journal,
        state_dir,
        transport,
        bind_admission=False,
    )
    admission = bind_test_admission(runtime)
    runtime.start()

    assert admission.offer(intent_obj) == (True, "accepted")
    outcome = admission.run_once()
    assert outcome is not None and outcome.submitted is True
    assert len([request for request in transport.requests if request.method == "POST"]) == 1

    with pytest.raises(ProductionStateError, match="admission-bound"):
        runtime.execute_intent(intent("bound-direct-bypass"))
    assert len([request for request in transport.requests if request.method == "POST"]) == 1


def test_invalid_strategy_id_is_rejected_before_any_attempt_or_post(journal, state_dir):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, _port, _client = _wired(journal, state_dir, transport)
    runtime.start()

    with pytest.raises(ProductionStateError):
        intent("invalid-strategy", strategy_id="my strategy")

    assert not any(
        event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE for event in journal.replay()
    )
    assert [request for request in transport.requests if request.is_write] == []


def test_unattributed_sell_is_blocked_before_any_venue_write(journal, state_dir):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, _port, _client = _wired(journal, state_dir, transport)
    runtime.start()

    outcome = admitted_execute(
        runtime,
        intent(
            "unattributed-sell",
            side=ExecutionSide.SELL,
            quantity=Decimal("0.001"),
        ),
    )

    assert outcome.submitted is False
    assert "insufficient_attributed_inventory" in (outcome.blocked_reason_code or "")
    assert not [request for request in transport.requests if request.method == "POST"]
    assert len(runtime._pending) == 0


@pytest.mark.parametrize(
    "limit_field,reason_code",
    [
        ("max_symbol_exposure_quote", "max_symbol_exposure_exceeded"),
        ("max_strategy_exposure_quote", "max_strategy_exposure_exceeded"),
        ("max_gross_exposure_quote", "max_gross_exposure_exceeded"),
        ("max_net_exposure_quote", "max_net_exposure_exceeded"),
    ],
)
def test_second_prepare_accounts_for_first_pending_reservation(
    journal, state_dir, limit_field, reason_code
):
    transport = fx.ScriptedTransport([
        *startup_responses(),
        *_risk_refresh_responses(),
    ])
    resolved_limits = replace(
        production_limits(),
        **{limit_field: Decimal("400")},
    )
    runtime, _port, _client = _wired(
        journal, state_dir, transport, limits_value=resolved_limits
    )
    runtime.start()

    first = admitted_prepare(
        runtime, intent("reservation-first", quantity=Decimal("0.01"))
    )
    second = admitted_prepare(
        runtime, intent("reservation-second", quantity=Decimal("0.01"))
    )

    assert first.approved is True
    assert second.approved is False
    assert reason_code in (second.blocked_reason_code or "")
    assert len(runtime._pending) == 1
    assert not [request for request in transport.requests if request.method == "POST"]


def test_testnet_execution_flows_through_the_production_runtime(journal, state_dir):
    intent_obj = intent("testnet-flow-1")
    transport = fx.ScriptedTransport(
        [*startup_responses(), fx.order_response(_reference_for(intent_obj))]
    )
    runtime, port, _client = _wired(journal, state_dir, transport)

    report = runtime.start()
    assert report.environment == "testnet"
    assert report.base_url == SPOT_TESTNET_REST_BASE
    assert report.safety_mode == "normal"

    outcome = admitted_execute(runtime, intent_obj)

    assert outcome.submitted is True
    assert outcome.submission is not None
    assert outcome.submission.order_reference == _reference_for(intent_obj)
    assert outcome.submission.fence_term == report.fence_term
    assert outcome.submission.safety_mode == "normal"
    assert outcome.submission.environment == "testnet"
    assert port.observations()
    events = list(journal.replay())
    assert ORDER_STATE_EVENT_TYPE in [event.event_type for event in events]
    attempts = [event for event in events if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE]
    assert len(attempts) == 1
    assert attempts[0].strategy_id == intent_obj.strategy_id
    writes = [request for request in transport.requests if request.is_write]
    assert len(writes) == 1
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}


# --- 13: ambiguity is preserved, never retried -------------------------------------

def test_an_ambiguous_submit_stays_unknown_pending_reconciliation(journal, state_dir):
    transport = fx.ScriptedTransport(
        [*startup_responses(), TransportTimeout("the write outcome is unknown")]
    )
    runtime, port, _client = _wired(journal, state_dir, transport)
    runtime.start()

    with pytest.raises(AmbiguousOrderError):
        admitted_execute(runtime, intent("ambiguous-1"))

    assert runtime.status()["last_outcome"] == "unknown_pending_reconciliation"
    writes = [request for request in transport.requests if request.is_write]
    assert len(writes) == 1, "an ambiguous write must never be blindly retried"
    pending = [
        state for state in port.local_states() if state.submission_attempted and state.is_open
    ]
    assert pending, "the ambiguous submission must stay pending for reconciliation"
    assert len(runtime._pending) == 1
    assert runtime._issued_preparations == {}


@pytest.mark.parametrize(
    "response,expected,reason_code,seed",
    [
        (
            fx.rate_limited("1"),
            WriteRateLimitedOutcomeUnknownError,
            "venue_rate_limited_after_send",
            "rate-ambiguous-429",
        ),
        (
            fx.banned("120"),
            WriteIpBannedOutcomeUnknownError,
            "venue_ip_banned_after_send",
            "rate-ambiguous-418",
        ),
    ],
    ids=["429", "418"],
)
def test_post_send_rate_limit_keeps_production_reservation_unknown(
    journal, state_dir, response, expected, reason_code, seed
):
    transport = fx.ScriptedTransport([*startup_responses(), response])
    runtime, port, client = _wired(journal, state_dir, transport)
    runtime.start()

    with pytest.raises(expected) as caught:
        admitted_execute(runtime, intent(seed))

    assert isinstance(caught.value, AmbiguousOrderError)
    assert runtime.status()["last_outcome"] == f"unknown_pending_reconciliation:{reason_code}"
    assert len([request for request in transport.requests if request.method == "POST"]) == 1
    assert len(runtime._pending) == 1
    assert runtime._issued_preparations == {}
    assert any(state.submission_attempted and state.is_open for state in port.local_states())
    attempts = [
        event for event in journal.replay()
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    ]
    terminals = [event for event in journal.replay() if event.event_type == ORDER_STATE_EVENT_TYPE]
    assert len(attempts) == 1
    assert terminals == []
    if expected is WriteIpBannedOutcomeUnknownError:
        assert client.budget is not None and client.budget.banned is True


def test_ambiguous_preparation_is_one_shot_and_cannot_drop_its_reservation(
    journal, state_dir
):
    intent_obj = intent("ambiguous-one-shot")
    transport = fx.ScriptedTransport(
        [*startup_responses(), fx.rate_limited("1")]
    )
    runtime, _port, _client = _wired(journal, state_dir, transport)
    runtime.start()
    preparation = admitted_prepare(runtime, intent_obj)

    with pytest.raises(WriteRateLimitedOutcomeUnknownError):
        admitted_submit(runtime, preparation)

    posts_before = len([request for request in transport.requests if request.method == "POST"])
    assert posts_before == 1
    assert len(runtime._pending) == 1
    assert runtime._issued_preparations == {}

    with pytest.raises(ProductionStateError, match="exact unused preparation"):
        admitted_submit(runtime, preparation)

    assert len([request for request in transport.requests if request.method == "POST"]) == posts_before
    assert len(runtime._pending) == 1
    assert runtime._issued_preparations == {}


def test_expired_prelock_validation_preserves_reservation_until_locked_cleanup(
    journal, state_dir
):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, _port, _client = _wired(journal, state_dir, transport)
    runtime.start()
    preparation = admitted_prepare(runtime, intent("expired-prelock-preserve"))

    runtime._clock = clock(BASE + timedelta(hours=1))
    with pytest.raises(ProductionStateError, match="expired before submission"):
        runtime._validate_submission_preparation(
            preparation, release_on_failure=False
        )

    assert len(runtime._pending) == 1
    assert runtime._issued_preparations.get(preparation.intent_id, (None,))[0] is preparation


def test_foreign_submit_is_refused_before_admission_or_reservation_cleanup(
    journal, state_dir
):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, port, _client = _wired(journal, state_dir, transport)
    runtime.start()
    preparation = admitted_prepare(runtime, intent("foreign-submit-refused"))

    admission_called = threading.Event()
    failures: list[BaseException] = []

    def fail_if_called() -> None:
        admission_called.set()
        raise AssertionError("foreign thread reached venue admission")

    port.await_write_admission = fail_if_called  # type: ignore[method-assign]

    def submit() -> None:
        try:
            admitted_submit(runtime, preparation)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=submit, name="foreign-submit", daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ProductionStateError)
    assert "thread-affine" in str(failures[0])
    assert admission_called.is_set() is False
    assert len(runtime._pending) == 1
    assert runtime._issued_preparations[preparation.intent_id][0] is preparation
    assert not [request for request in transport.requests if request.method == "POST"]
    assert not [
        event for event in journal.replay()
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    ]


def test_pre_send_deferred_admission_releases_reservation_without_marker_or_post(
    journal, state_dir
):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, _port, client = _wired(journal, state_dir, transport)
    runtime.start()
    preparation = admitted_prepare(runtime, intent("pre-send-deferred"))
    assert preparation.approved is True
    assert client.budget is not None
    client.budget.backoff_until = client.budget.monotonic() + 5.0

    with pytest.raises(RateLimitAdmissionDeferredError):
        admitted_submit(runtime, preparation)

    assert not [request for request in transport.requests if request.method == "POST"]
    assert not [
        event for event in journal.replay()
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    ]
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}


def test_rate_limit_race_after_preflight_fails_fast_inside_lock(
    journal, state_dir
):
    transport = fx.ScriptedTransport(startup_responses())
    runtime, port, client = _wired(journal, state_dir, transport)
    runtime.start()
    preparation = admitted_prepare(runtime, intent("preflight-race"))
    assert client.budget is not None

    def race_after_preflight() -> None:
        client.budget.backoff_until = client.budget.monotonic() + 5.0

    def forbidden_sleep(_seconds: float) -> None:
        pytest.fail("submission lock path attempted to sleep")

    port.await_write_admission = race_after_preflight
    client.budget.sleep = forbidden_sleep
    client.budget.bucket.sleep = forbidden_sleep

    with pytest.raises(RateLimitAdmissionDeferredError):
        admitted_submit(runtime, preparation)

    assert not [request for request in transport.requests if request.method == "POST"]
    assert not [
        event for event in journal.replay()
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    ]
    assert len(runtime._pending) == 0
    assert runtime._issued_preparations == {}


def test_rate_limit_wait_happens_before_lock_so_emergency_takeover_wins(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    journal_path = tmp_path / "journal.db"
    transport = fx.ScriptedTransport(startup_responses())

    ready = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    final_state: dict[str, object] = {}

    def owner_thread() -> None:
        with SQLiteEventJournal(journal_path) as thread_journal:
            runtime, _port, client = _wired(thread_journal, state_dir, transport)
            runtime.start()
            preparation = admitted_prepare(runtime, intent("pre-lock-wait-takeover"))
            assert client.budget is not None

            def blocking_sleep(_seconds: float) -> None:
                entered.set()
                assert release.wait(timeout=5.0)

            client.budget.backoff_until = client.budget.monotonic() + 60.0
            client.budget.sleep = blocking_sleep
            ready.set()
            try:
                admitted_submit(runtime, preparation)
            except BaseException as exc:
                failures.append(exc)
            final_state["pending"] = len(runtime._pending)
            final_state["issued"] = dict(runtime._issued_preparations)

    worker = threading.Thread(target=owner_thread, name="runtime-owner", daemon=True)
    worker.start()
    assert ready.wait(timeout=5.0)
    assert entered.wait(timeout=5.0), "submission never reached the pre-lock admission wait"

    takeover = OwnershipStore(state_dir / "ownership.json").takeover(
        owner_id="emergency:test",
        owner_kind=OwnerKind.EMERGENCY,
        acquired_at=BASE,
        reason_code="test_rate_limit_prelock_takeover",
    )
    assert takeover.owner_kind is OwnerKind.EMERGENCY

    release.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert any(isinstance(exc, StaleWriterFenceError) for exc in failures)
    assert not [request for request in transport.requests if request.method == "POST"]
    assert final_state == {"pending": 0, "issued": {}}


# --- 16: the withdrawal-disabled invariant still fails closed ----------------------

def test_a_withdrawal_capable_key_is_refused_before_any_order(journal, state_dir):
    transport = fx.ScriptedTransport(startup_responses(can_withdraw=True))
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
        [*startup_responses(), fx.order_response(_reference_for(intent_obj))]
    )
    runtime, port, client = _wired(journal, state_dir, transport)
    runtime.start()
    outcome = admitted_execute(runtime, intent_obj)

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
            [*startup_responses(), fx.order_response(_reference_for(intent_obj))]
        )
        runtime, port, client = _wired(journal, state_dir, transport)
        runtime.start(correlation_id=START_CORRELATION_ID)
        admitted_execute(runtime, intent_obj)

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
            *startup_responses(),
            fx.order_response(reference, status="NEW"),
            fx.time_response(),
            fx.exchange_info_response(),
            fx.account_response(),
            fx.response(200, [fx.order_response(reference, status="NEW").body]),
            fx.cancel_response(reference),
            fx.order_response(reference, status="CANCELED"),  # explicit terminal lookup
            fx.order_response(reference, status="CANCELED"),  # reconciliation evidence
        ]
    )
    client = fx.client(transport)
    port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    venue = BinanceSpotTestnetEmergencyControl(client=client, runtime=port)
    control = control_for(journal, state_dir, venue=venue)
    runtime, port, client = _wired(
        journal, state_dir, transport, control=control, port=port, client=client
    )
    runtime.start()
    submitted = admitted_execute(runtime, intent_obj)
    assert submitted.submitted is True

    outcome = control.handle(operator_command("cancel_open", seed="cancel-open-emergency"))

    assert outcome.disposition == "accepted"
    assert outcome.venue_control == "confirmed"
    assert outcome.resulting_safety_state == "cancel_open"
    assert runtime.status()["new_risk_permitted"] is False
    assert any(request.method == "DELETE" for request in transport.requests)

    terminal = client.order_status(fx.SYMBOL, reference)
    assert terminal.client_order_id == reference
    assert terminal.status == "CANCELED"
    assert terminal.venue_order_id == "42"
    reconstructed, _ = reconstruct_order_states(journal)
    states = reconstructed.states()
    assert len(states) == 1
    assert states[0].order_reference == reference
    assert states[0].state.value == "canceled"
    _require_reconciliation_closure(control)
    assert runtime.status()["new_risk_permitted"] is True
    assert len([request for request in transport.requests if request.method == "DELETE"]) == 1
    assert len([request for request in transport.requests if request.method == "POST"]) == 1

    # A fresh halt preserves the existing refusal check below, without a new order.
    control.handle(operator_command("halt_new_risk", seed="closure-rehalt"))

    blocked = admitted_execute(runtime, intent("cancel-open-2"))
    assert blocked.submitted is False
    assert "trading_halted" in (blocked.blocked_reason_code or "")


@pytest.mark.parametrize("status,reason", [
    ("pending", "UNKNOWN_PENDING_RECONCILIATION"),
    ("resolved", "UNKNOWN_PENDING_RECONCILIATION"),
])
def test_live_closure_assertions_fail_before_acknowledgement_on_unknown(status, reason):
    commands = []

    class UnresolvedControl:
        def handle(self, command):
            commands.append(command.command_type.value)
            return SimpleNamespace(
                accepted=True, reconciliation_status=status, reason_code=reason,
            )

    with pytest.raises(AssertionError):
        _require_reconciliation_closure(UnresolvedControl())
    assert commands == ["reconcile"]
