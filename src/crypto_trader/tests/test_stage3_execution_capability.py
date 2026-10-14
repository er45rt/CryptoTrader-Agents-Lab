"""Phase 15 Stage 3: explicit execution capability and the production fail-closed gate.

The gate must never grant venue-write authority from a capability flag, a bare enum value, or a
durable-looking Protocol shape. Only the approved durable submission boundary (runtime-owned
marker -> authorization -> one send, wired to the runtime's own durable journal) may write.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from crypto_trader.events import (
    ExecutionFillRecorded,
    FillRecordPayload,
    OrderSubmissionAttemptPayload,
    OrderSubmissionAttempted,
)
from crypto_trader.execution import (
    DurableVenueWritePort,
    ExecutionCapability,
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    ExecutionTimeInForce,
    InMemoryExecutionPort,
    InstrumentSpec,
    NonDurableExecutionError,
    PreparedVenueWrite,
)
from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStartupError, ProductionStateError
from crypto_trader.production.runtime import JournalView
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    BASE,
    NAMESPACE,
    PRICE,
    INSTRUMENT as PRODUCTION_INSTRUMENT,
    startup_responses,
)
from crypto_trader.tests.production_fixtures import (
    RecordingExecutionPort,
    admitted_execute,
    admitted_prepare,
    admitted_submit,
    build_runtime,
    canonical_risk_refresh_responses,
    canonical_test_risk_source,
    control_for,
    intent as production_intent,
    operator_command,
    seed_canonical_equity_root,
)

INSTRUMENT = InstrumentSpec(symbol="BTCUSDT", base_currency="BTC", starting_balance="100000 USDT")
INTENT_ID = UUID("10101010-1010-4010-8010-101010101010")
CORRELATION_ID = UUID("20202020-2020-4020-8020-202020202020")


def _request() -> ExecutionRequest:
    return ExecutionRequest.create(
        correlation_id=CORRELATION_ID,
        symbol="BTCUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("0.00100000"),
        limit_price=Decimal("27000.00"),
        time_in_force=ExecutionTimeInForce.GTC,
        intent_id=INTENT_ID,
    )


def _seed_owned_long_position(journal: SQLiteEventJournal) -> None:
    seed_canonical_equity_root(
        journal,
        occurred_at=BASE - timedelta(minutes=1),
        base_balance=Decimal("0"),
        quote_balance=Decimal("100000"),
        mark_price=PRICE,
    )
    order_id = uuid5(NAMESPACE, "reduce-only-owned-long-order")
    intent_id = uuid5(NAMESPACE, "reduce-only-owned-long-intent")
    correlation_id = uuid5(NAMESPACE, "reduce-only-owned-long-correlation")
    journal.append(
        OrderSubmissionAttempted(
            event_id=uuid5(NAMESPACE, "reduce-only-owned-long-attempt"),
            occurred_at=BASE - timedelta(seconds=2),
            correlation_id=correlation_id,
            producer="application.production",
            strategy_id="strategy.production-test",
            order_id=order_id,
            symbol=PRODUCTION_INSTRUMENT.symbol,
            payload=OrderSubmissionAttemptPayload(
                venue_reference=f"ct-{order_id.hex[:20]}",
                intent_id=intent_id,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                limit_price=PRICE,
                time_in_force="gtc",
                venue_producer="infrastructure.binance_testnet",
            ),
        )
    )
    journal.append(
        ExecutionFillRecorded(
            event_id=uuid5(NAMESPACE, "reduce-only-owned-long-fill"),
            occurred_at=BASE - timedelta(seconds=1),
            correlation_id=correlation_id,
            producer="infrastructure.binance_testnet",
            strategy_id="strategy.production-test",
            order_id=order_id,
            symbol=PRODUCTION_INSTRUMENT.symbol,
            sequence_number=1,
            payload=FillRecordPayload(
                quantity=Decimal("1"),
                price=PRICE,
                fee=Decimal("0"),
                fee_asset=None,
                remaining_quantity=Decimal("0"),
                liquidity="taker",
            ),
        )
    )


def _reduce_only_risk_refresh_responses():
    account = fx.account_response(free="73000")
    return [
        fx.time_response(fx.SERVER_TIME_MS + 1),
        fx.open_orders_response(),
        account,
        fx.price_response(str(PRICE)),
        fx.open_orders_response(),
    ]


def _sell_order_response(client_order_id: str):
    response = fx.order_response(
        client_order_id,
        status="NEW",
        orig_qty="0.10000",
        executed_qty="0",
        price=str(PRICE),
    )
    response.body["side"] = "SELL"
    return response


class _ShapeOnlyPort:
    """A legacy execution object with no capability declaration at all."""

    def __init__(self, **attrs: object) -> None:
        self.submit_calls = 0
        for name, value in attrs.items():
            setattr(self, name, value)

    def start(self) -> None:  # pragma: no cover - never reached
        pass

    def stop(self) -> None:  # pragma: no cover - never reached
        pass

    def state(self) -> ExecutionRuntimeState:  # pragma: no cover - never reached
        return ExecutionRuntimeState.STOPPED

    def observations(self):  # pragma: no cover - never reached
        return ()

    def submit(self, request: ExecutionRequest) -> str:
        self.submit_calls += 1
        return "LEGACY-1"


class _FakeJournal:
    """A structurally valid but non-durable journal (in-memory only)."""

    def __init__(self) -> None:
        self.events: dict[object, object] = {}

    def append(self, event: object) -> bool:
        self.events[getattr(event, "event_id", None)] = event
        return True

    def read_by_id(self, event_id: object) -> object | None:
        return self.events.get(event_id)


class _Marker:
    def __init__(self) -> None:
        self.event_id = uuid4()


class _DurableShapedImpostor:
    """Declares durable venue writing and LOOKS durable, but bypasses the approved boundary."""

    capability = ExecutionCapability.DURABLE_VENUE_WRITING

    def __init__(self) -> None:
        self.journal = _FakeJournal()
        self.venue_writes = 0

    def durable_attempt_journal(self) -> _FakeJournal:
        return self.journal

    def durable_attempt_marker(self) -> _Marker:  # shape only, never used by the runtime
        return _Marker()

    def start(self) -> None:  # pragma: no cover - never reached
        pass

    def stop(self) -> None:  # pragma: no cover - never reached
        pass

    def state(self) -> ExecutionRuntimeState:  # pragma: no cover - never reached
        return ExecutionRuntimeState.STOPPED

    def observations(self):  # pragma: no cover - never reached
        return ()

    def submit(self, request: ExecutionRequest) -> str:
        # The bypass: a venue write with no marker, no authorization and no send boundary.
        self.venue_writes += 1
        return "IMPOSTOR-1"


class _NominalImpostor(DurableVenueWritePort):
    """Inherits the approved contract but wires it to its own non-authoritative journal."""

    def __init__(self) -> None:
        self._journal = _FakeJournal()
        self.venue_writes = 0

    def durable_attempt_journal(self):
        return self._journal

    def _prepare_durable_write(self, request: ExecutionRequest) -> PreparedVenueWrite:
        return PreparedVenueWrite(handle="prepared")

    def _durable_attempt_event(self, prepared, request) -> _Marker:
        return _Marker()

    def _bind_durable_authorization(self, prepared, marker) -> PreparedVenueWrite:
        return prepared

    def _send_authorized_write(self, prepared, request) -> str:
        self.venue_writes += 1
        return "NOMINAL-IMPOSTOR-1"

    def start(self) -> None:  # pragma: no cover - never reached
        pass

    def stop(self) -> None:  # pragma: no cover - never reached
        pass

    def state(self) -> ExecutionRuntimeState:  # pragma: no cover - never reached
        return ExecutionRuntimeState.STOPPED

    def observations(self):  # pragma: no cover - never reached
        return ()

    def submit(self, request: ExecutionRequest) -> str:  # pragma: no cover - never reached
        self.venue_writes += 1
        return "NOMINAL-IMPOSTOR-1"


# --- 1: the capability model is explicit ------------------------------------------

def test_execution_capability_is_explicit_and_closed():
    assert {c.name for c in ExecutionCapability} == {
        "NON_WRITING",
        "DURABLE_VENUE_WRITING",
        "VENUE_WRITING_UNPROVEN",
    }
    assert ExecutionCapability.NON_WRITING.writes_to_venue is False
    assert ExecutionCapability.DURABLE_VENUE_WRITING.writes_to_venue is True
    assert ExecutionCapability.VENUE_WRITING_UNPROVEN.writes_to_venue is True


def test_durable_submission_sequence_cannot_be_overridden():
    with pytest.raises(TypeError):

        class _Override(DurableVenueWritePort):  # noqa: D401 - negative control
            capability = ExecutionCapability.DURABLE_VENUE_WRITING

            def durable_attempt_journal(self):  # pragma: no cover - never reached
                return None

            def _prepare_durable_write(self, request):  # pragma: no cover
                raise AssertionError

            def _durable_attempt_event(self, prepared, request):  # pragma: no cover
                raise AssertionError

            def _bind_durable_authorization(self, prepared, marker):  # pragma: no cover
                raise AssertionError

            def _send_authorized_write(self, prepared, request):  # pragma: no cover
                raise AssertionError

            def submit_durably(self, request):  # pragma: no cover - forbidden override
                return "BYPASS"


# --- 2-5: unknown / malformed capability fails closed -----------------------------

@pytest.mark.parametrize(
    "attrs",
    [
        {},  # A: no declaration at all
        {"writes_to_venue": None},  # B: legacy None
        {"writes_to_venue": "yes"},  # C: legacy invalid string
        {"capability": "durable_venue_writing"},  # D: capability of the wrong type
        {"capability": ExecutionCapability.VENUE_WRITING_UNPROVEN},  # E: unproven venue writing
    ],
    ids=["missing", "none", "invalid_string", "malformed_enum", "unproven_writing"],
)
def test_unknown_or_malformed_capability_fails_closed_without_side_effects(tmp_path, attrs):
    port = _ShapeOnlyPort(**attrs)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        with pytest.raises(NonDurableExecutionError):
            build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port)

    assert port.submit_calls == 0, "no venue-write side effect may occur"


# --- 6/7: durable-SHAPED but non-durable execution is refused ---------------------

def test_durable_shaped_object_without_the_contract_is_refused(tmp_path):
    port = _DurableShapedImpostor()
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        with pytest.raises(NonDurableExecutionError):
            build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port)

    assert port.venue_writes == 0, "shape alone must never grant venue-write authority"


def test_durable_contract_subclass_with_a_foreign_journal_is_refused(tmp_path):
    port = _NominalImpostor()
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        with pytest.raises(NonDurableExecutionError):
            build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port)

    assert port.venue_writes == 0


def test_contradictory_capability_on_the_durable_contract_is_refused(tmp_path):
    class _Contradiction(_NominalImpostor):
        capability = ExecutionCapability.NON_WRITING

    port = _Contradiction()
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        with pytest.raises(NonDurableExecutionError):
            build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port)

    assert port.venue_writes == 0


# --- 8/9: venue-writing risk binding is canonical and exact ------------------------

def test_venue_writing_runtime_refuses_noncanonical_risk_provider(tmp_path):
    client = fx.client(ScriptedTransport([]))
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        port = OrderRuntime(client=client, instrument=PRODUCTION_INSTRUMENT, journal=journal)
        with pytest.raises(ProductionStateError, match="canonical ProductionRiskStateSource"):
            build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port)


@pytest.mark.parametrize("mismatch", ["client", "journal", "control"])
def test_venue_writing_runtime_refuses_mismatched_canonical_risk_binding(tmp_path, mismatch):
    client = fx.client(ScriptedTransport([]))
    other_client = fx.client(ScriptedTransport([]))
    with (
        SQLiteEventJournal(tmp_path / "journal.db") as journal,
        SQLiteEventJournal(tmp_path / "other.db") as other_journal,
    ):
        state_dir = tmp_path / "state"
        control = control_for(journal, state_dir)
        port = OrderRuntime(client=client, instrument=PRODUCTION_INSTRUMENT, journal=journal)
        source_client = other_client if mismatch == "client" else client
        source_journal = other_journal if mismatch == "journal" else journal
        source_control = (
            control_for(journal, tmp_path / "other-state")
            if mismatch == "control"
            else control
        )
        risk_source = canonical_test_risk_source(
            journal=source_journal,
            control=source_control,
            client=source_client,
        )

        with pytest.raises(ProductionStateError, match="not bound"):
            build_runtime(
                journal=journal,
                state_dir=state_dir,
                execution=port,
                control=control,
                risk_state_source=risk_source,
            )


# --- 10/11: the approved durable path is accepted and behaves durably --------------

def test_writing_startup_refuses_safety_round_trip_before_journal_baseline(
    tmp_path, monkeypatch
):
    transport = ScriptedTransport(responses=startup_responses())
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        port = OrderRuntime(client=client, instrument=PRODUCTION_INSTRUMENT, journal=journal)
        state_dir = tmp_path / "state"
        seed_canonical_equity_root(journal)
        control = control_for(journal, state_dir)
        risk_source = canonical_test_risk_source(
            journal=journal, control=control, client=client
        )
        runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=port,
            control=control,
            risk_state_source=risk_source,
            bind_admission=True,
        )
        original_capture = JournalView.capture

        def capture_after_round_trip(active_journal, symbol):
            assert control.handle(
                operator_command("reduce_only", seed="startup-roundtrip-reduce")
            ).accepted
            assert control.handle(
                operator_command("restore_normal", seed="startup-roundtrip-restore")
            ).accepted
            return original_capture(active_journal, symbol)

        monkeypatch.setattr(JournalView, "capture", capture_after_round_trip)

        with pytest.raises(
            ProductionStartupError, match="safety state changed during startup"
        ):
            runtime.start()

        assert control.load_safety_state().controller.mode.value == "normal"
        assert not [request for request in transport.requests if request.method == "POST"]


def test_approved_durable_path_is_accepted_and_keeps_marker_authorization_send(tmp_path):
    intent_obj = production_intent("stage3-durable")
    reference = f"ct-{intent_obj.intent_id.hex[:20]}"
    transport = ScriptedTransport(
        responses=[
            *startup_responses(),
            *canonical_risk_refresh_responses(),
            fx.order_response(reference, status="NEW"),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        port = OrderRuntime(client=client, instrument=PRODUCTION_INSTRUMENT, journal=journal)
        state_dir = tmp_path / "state"
        seed_canonical_equity_root(journal)
        control = control_for(journal, state_dir)
        risk_source = canonical_test_risk_source(
            journal=journal, control=control, client=client
        )
        runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=port,
            control=control,
            risk_state_source=risk_source,
            bind_admission=True,
        )

        assert port.capability is ExecutionCapability.DURABLE_VENUE_WRITING
        assert isinstance(port, DurableVenueWritePort)
        assert runtime is not None

        runtime.start()
        outcome = admitted_execute(runtime, intent_obj)

        assert outcome.submitted is True
        posts = [item for item in transport.requests if item.method == "POST"]
        assert len(posts) == 1, "exactly one send through the approved boundary"
        markers = [
            event
            for event in journal.read_range()
            if event.event_type == "order.submission.attempted"
        ]
        assert len(markers) == 1
        assert markers[0].payload.venue_reference == reference



def test_durable_runtime_restarts_in_reduce_only_without_restoring_normal(tmp_path):
    first_transport = ScriptedTransport(responses=startup_responses())
    first_client = fx.client(first_transport)
    first_client.clock.synchronize(fx.SERVER_TIME_MS)

    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        state_dir = tmp_path / "state"
        control = control_for(journal, state_dir)
        first_port = OrderRuntime(
            client=first_client,
            instrument=PRODUCTION_INSTRUMENT,
            journal=journal,
        )
        first_source = canonical_test_risk_source(
            journal=journal,
            control=control,
            client=first_client,
        )
        first_runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=first_port,
            control=control,
            risk_state_source=first_source,
            bind_admission=True,
        )

        first_report = first_runtime.start()
        reduced = control.handle(
            operator_command("reduce_only", seed="stage3-restart-reduce-only")
        )
        first_runtime.stop()

        second_transport = ScriptedTransport(responses=startup_responses())
        second_client = fx.client(second_transport)
        second_client.clock.synchronize(fx.SERVER_TIME_MS)
        second_port = OrderRuntime(
            client=second_client,
            instrument=PRODUCTION_INSTRUMENT,
            journal=journal,
        )
        second_source = canonical_test_risk_source(
            journal=journal,
            control=control,
            client=second_client,
        )
        second_runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=second_port,
            control=control,
            risk_state_source=second_source,
            bind_admission=True,
        )

        second_report = second_runtime.start()

        assert reduced.accepted
        assert second_report.safety_mode == "reduce_only"
        assert second_report.fence_term == first_report.fence_term + 1
        assert control.status().safety_mode == "reduce_only"
        assert control.status().new_risk_permitted is False
        assert not [request for request in first_transport.requests if request.is_write]
        assert not [request for request in second_transport.requests if request.is_write]


def test_durable_reduce_only_reduction_keeps_marker_then_single_send(tmp_path):
    target = production_intent(
        "stage3-reduce-only-sell",
        side=ExecutionSide.SELL,
        quantity=Decimal("0.10000"),
    )
    reference = f"ct-{target.intent_id.hex[:20]}"
    transport = ScriptedTransport(
        responses=[
            *startup_responses(),
            *_reduce_only_risk_refresh_responses(),
            _sell_order_response(reference),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        state_dir = tmp_path / "state"
        _seed_owned_long_position(journal)
        control = control_for(journal, state_dir)
        port = OrderRuntime(
            client=client,
            instrument=PRODUCTION_INSTRUMENT,
            journal=journal,
        )
        risk_source = canonical_test_risk_source(
            journal=journal,
            control=control,
            client=client,
        )
        runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=port,
            control=control,
            risk_state_source=risk_source,
            bind_admission=True,
        )

        report = runtime.start()
        reduced = control.handle(
            operator_command("reduce_only", seed="stage3-durable-reduce-only")
        )
        outcome = admitted_execute(runtime, target)

        assert reduced.accepted
        assert reduced.reconciliation_required is False
        assert control.ownership_store.require_fence().term == report.fence_term
        assert outcome.submitted is True
        assert outcome.preparation.safety_mode == "reduce_only"
        assert outcome.submission is not None
        assert outcome.submission.fence.safety_mode == "reduce_only"

        posts = [item for item in transport.requests if item.method == "POST"]
        assert len(posts) == 1
        markers = [
            event
            for event in journal.read_range()
            if event.event_type == "order.submission.attempted"
        ]
        assert len(markers) == 2
        assert markers[-1].payload.venue_reference == reference


@pytest.mark.parametrize(
    ("side", "quantity", "reason_fragment"),
    [
        (ExecutionSide.BUY, "0.10000", "reduce_only_violation"),
        (ExecutionSide.SELL, "1.10000", "reduce_only_quantity_exceeded"),
    ],
)
def test_durable_reduce_only_blocks_increase_and_overclose_before_post(
    tmp_path, side, quantity, reason_fragment
):
    target = production_intent(
        f"stage3-reduce-only-reject-{side.value}-{quantity}",
        side=side,
        quantity=Decimal(quantity),
    )
    transport = ScriptedTransport(
        responses=[
            *startup_responses(),
            *_reduce_only_risk_refresh_responses(),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        state_dir = tmp_path / "state"
        _seed_owned_long_position(journal)
        control = control_for(journal, state_dir)
        port = OrderRuntime(
            client=client,
            instrument=PRODUCTION_INSTRUMENT,
            journal=journal,
        )
        risk_source = canonical_test_risk_source(
            journal=journal,
            control=control,
            client=client,
        )
        runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=port,
            control=control,
            risk_state_source=risk_source,
            bind_admission=True,
        )

        runtime.start()
        control.handle(
            operator_command(
                "reduce_only",
                seed=f"stage3-reduce-only-reject-mode-{side.value}-{quantity}",
            )
        )
        outcome = admitted_execute(runtime, target)

        assert outcome.submitted is False
        assert reason_fragment in (outcome.blocked_reason_code or "")
        assert not [item for item in transport.requests if item.method == "POST"]


def test_submit_rechecks_canonical_risk_client_binding(tmp_path):
    target = production_intent("stage3-risk-binding-recheck")
    reference = f"ct-{target.intent_id.hex[:20]}"
    transport = ScriptedTransport(
        responses=[
            *startup_responses(),
            *canonical_risk_refresh_responses(),
            fx.order_response(reference, status="NEW"),
        ]
    )
    client = fx.client(transport)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        state_dir = tmp_path / "state"
        seed_canonical_equity_root(journal)
        control = control_for(journal, state_dir)
        port = OrderRuntime(client=client, instrument=PRODUCTION_INSTRUMENT, journal=journal)
        risk_source = canonical_test_risk_source(
            journal=journal, control=control, client=client
        )
        runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=port,
            control=control,
            risk_state_source=risk_source,
            bind_admission=True,
        )
        runtime.start()
        preparation = admitted_prepare(runtime, target)
        assert preparation.approved

        risk_source._client = fx.client(ScriptedTransport([]))

        with pytest.raises(ProductionStateError, match="not bound"):
            admitted_submit(runtime, preparation)

        assert not [request for request in transport.requests if request.method == "POST"]


def test_non_writing_implementations_are_explicitly_classified(tmp_path):
    assert InMemoryExecutionPort.capability is ExecutionCapability.NON_WRITING
    assert RecordingExecutionPort.capability is ExecutionCapability.NON_WRITING
    from crypto_trader.execution.nautilus.runtime import NautilusExecutionRuntime

    assert NautilusExecutionRuntime.capability is ExecutionCapability.NON_WRITING

    # A legitimate non-venue port still constructs and runs.
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        runtime = build_runtime(
            journal=journal,
            state_dir=tmp_path / "state",
            execution=RecordingExecutionPort(INSTRUMENT),
        )
    assert runtime is not None


# --- 10: the synthetic fault machinery is test-only -------------------------------

def test_synthetic_mainnet_fault_machinery_has_no_production_importers():
    package = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("mainnet.faults"):
                offenders.append(f"{path.relative_to(package)}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "crypto_trader.mainnet":
                for alias in node.names:
                    if alias.name in {
                        "inject_post_send_ambiguity",
                        "classify_send_fault",
                        "require_post_send_ambiguity",
                        "AmbiguityInjectionResult",
                        "SendFault",
                        "FaultClass",
                        "IMMEDIATE_REJECTION_FAULTS",
                        "POST_SEND_AMBIGUITY_FAULTS",
                    }:
                        offenders.append(f"{path.relative_to(package)}:{node.lineno}:{alias.name}")

    assert offenders == [], offenders
