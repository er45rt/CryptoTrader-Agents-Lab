"""Phase 15 Stage 3: explicit execution capability and the production fail-closed gate.

The gate must never grant venue-write authority from a capability flag, a bare enum value, or a
durable-looking Protocol shape. Only the approved durable submission boundary (runtime-owned
marker -> authorization -> one send, wired to the runtime's own durable journal) may write.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

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
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import INSTRUMENT as PRODUCTION_INSTRUMENT, startup_responses
from crypto_trader.tests.production_fixtures import (
    RecordingExecutionPort,
    build_runtime,
    intent as production_intent,
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


# --- 8/9: the approved durable path is accepted and behaves durably ---------------

def test_approved_durable_path_is_accepted_and_keeps_marker_authorization_send(tmp_path):
    intent_obj = production_intent("stage3-durable")
    reference = f"ct-{intent_obj.intent_id.hex[:20]}"
    transport = ScriptedTransport(
        responses=[
            *startup_responses(),
            fx.order_response(reference, status="NEW"),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        port = OrderRuntime(client=client, instrument=PRODUCTION_INSTRUMENT, journal=journal)
        runtime = build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port)

        assert port.capability is ExecutionCapability.DURABLE_VENUE_WRITING
        assert isinstance(port, DurableVenueWritePort)
        assert runtime is not None

        runtime.start()
        outcome = runtime.execute_intent(intent_obj)

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
