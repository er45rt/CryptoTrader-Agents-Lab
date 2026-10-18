"""Phase 13: venue-side emergency control (Spot Testnet only, cancel-open only, never retried)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from crypto_trader.breakglass import BreakGlassControl, OwnerKind, parse_operator_command
from crypto_trader.breakglass.venue import (
    BinanceSpotTestnetEmergencyControl,
    PolicyOnlyVenueControl,
)
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.binance_testnet import (
    MAINNET_HOST_MARKERS,
    MainnetForbiddenError,
    TransportError,
    TransportTimeout,
    UnsafeTestnetAccountError,
    WithdrawalPolicy,
    require_testnet_base_url,
)
# Aliased so pytest never mistakes these classes for test classes in any collecting context.
from crypto_trader.execution.binance_testnet import (
    TestnetEndpointError as EndpointError,
    TestnetOrderRuntime as OrderRuntime,
)
from crypto_trader.events import (
    OPERATOR_COMMAND_EVENT_TYPE,
    RECONCILIATION_INTAKE_EVENT_TYPE,
    RECONCILIATION_OBSERVED_EVENT_TYPE,
    RECONCILIATION_SETTLED_EVENT_TYPE,
    OperatorCommandRecordPayload,
    OperatorCommandRecorded,
)
from crypto_trader.events.contracts import OperatorCommandRecordPayloadV3
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import LocalOrderState, OrderStateName
from crypto_trader.recovery import load_policy as load_recovery_policy
from crypto_trader.safety import SafetyController
from crypto_trader.tests import testnet_fixtures as txf

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def venue(journal, responses, *, withdrawal_policy=WithdrawalPolicy.RECORD_ONLY, retries=1):
    transport = txf.ScriptedTransport(responses=list(responses))
    client = txf.client(transport, retries=retries, withdrawal_policy=withdrawal_policy)
    runtime = OrderRuntime(
        client=client,
        instrument=InstrumentSpec(symbol=txf.SYMBOL, base_currency="BTC"),
        journal=journal,
    )
    return (
        BinanceSpotTestnetEmergencyControl(
            client=client, runtime=runtime, withdrawal_policy=withdrawal_policy
        ),
        client,
        runtime,
        transport,
    )


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


def test_preflight_verifies_testnet_clock_permissions_and_safety_state(journal):
    control, _, _, _ = venue(
        journal,
        [txf.time_response(), txf.exchange_info_response(), txf.account_response()],
    )
    preflight = control.preflight(safety=SafetyController(correlation_id=uuid4()))

    assert preflight.passed
    assert preflight.endpoint_is_testnet is True
    assert preflight.mainnet_hosts_blocked is True
    assert preflight.clock_synchronized is True
    assert preflight.withdrawals_available is False
    assert preflight.safety_allows_operation is True
    assert preflight.safety_mode == "normal"
    assert preflight.to_record()["venue_label"] == "binance-spot-testnet"


def test_preflight_fails_closed_on_a_withdrawal_capable_key(journal):
    control, _, _, _ = venue(
        journal,
        [txf.time_response(), txf.exchange_info_response(), txf.account_response(can_withdraw=True)],
        withdrawal_policy=WithdrawalPolicy.REQUIRE_DISABLED,
    )

    with pytest.raises(UnsafeTestnetAccountError):
        control.preflight(safety=SafetyController(correlation_id=uuid4()))


def test_the_testnet_default_flag_is_recorded_but_never_used(journal):
    control, _, _, _ = venue(
        journal,
        [txf.time_response(), txf.exchange_info_response(), txf.account_response(can_withdraw=True)],
    )
    preflight = control.preflight(safety=SafetyController(correlation_id=uuid4()))

    assert preflight.withdrawals_available is True
    assert preflight.withdrawal_policy_ok is True
    assert any("MUST be withdrawal-disabled" in note for note in preflight.notes)


def test_cancel_open_cancels_through_the_adapter_and_confirms_once(journal):
    control, client, _, transport = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-1"),
            txf.cancel_response("ct-1", order_id=100),
        ],
    )

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=SafetyController(correlation_id=uuid4())
    )
    deletes = [request for request in transport.requests if request.method == "DELETE"]

    assert outcome.confirmed
    assert outcome.cancelled_order_references == ("ct-1",)
    assert len(deletes) == 1
    assert outcome.to_record()["preflight"] is not None
    assert client.request_log()


def test_a_cancel_answering_for_another_venue_order_is_ambiguous(journal):
    # Review FZ-10: the strict listing is adopted as the lenient one was, so the cancel still
    # cross-checks the venue order it listed; an answer for another venue order id is not a
    # confirmed cancel of the listed one.
    control, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-1"),
            txf.cancel_response("ct-1", order_id=999),
        ],
    )

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=SafetyController(correlation_id=uuid4())
    )

    assert outcome.disposition == "ambiguous"
    assert outcome.cancelled_order_references == ()
    assert outcome.unconfirmed_order_references == ("ct-1",)


def test_an_ambiguous_cancel_is_never_retried(journal):
    control, _, _, transport = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-2"),
            TransportTimeout("lost"),
        ],
    )

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=SafetyController(correlation_id=uuid4())
    )
    deletes = [request for request in transport.requests if request.method == "DELETE"]

    assert outcome.ambiguous
    assert outcome.reason_code == "UNKNOWN_PENDING_RECONCILIATION"
    assert len(deletes) == 1


@pytest.mark.parametrize(
    ("disposition", "second_cancel"),
    [
        ("ambiguous", TransportTimeout("lost")),
        ("rejected", txf.rejected(-2011, "Unknown order sent.")),
    ],
)
def test_a_cancel_all_failing_part_way_journals_only_confirmed_cancels(
    journal, disposition, second_cancel
):
    # Review F-03/N-04: ct-1's cancel was confirmed, ct-2's failed. ct-1 stays on the record;
    # ct-2 is tracked for venue evidence but never recorded as cancelled.
    control, _, _, transport = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-1", "ct-2"),
            txf.cancel_response("ct-1", order_id=100),
            second_cancel,
        ],
    )

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=SafetyController(correlation_id=uuid4())
    )
    deletes = [request for request in transport.requests if request.method == "DELETE"]

    assert outcome.disposition == disposition
    assert outcome.cancelled_order_references == ("ct-1",)
    assert "ct-2" in control._order_references
    assert len(deletes) == 2


def test_an_unreachable_venue_is_reported_as_unavailable(journal):
    control, _, _, _ = venue(journal, [TransportError("down")])

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=SafetyController(correlation_id=uuid4())
    )

    assert outcome.disposition == "unavailable"
    assert outcome.reason_code == "venue_environment_unverified"


def test_the_request_log_never_exposes_the_api_key_or_signature(journal):
    control, client, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-3"),
            txf.cancel_response("ct-3", order_id=100),
        ],
    )
    control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=SafetyController(correlation_id=uuid4())
    )
    log = "\n".join(client.request_log())

    assert txf.FAKE_API_KEY not in log
    assert txf.FAKE_API_SECRET not in log
    assert "signature=<redacted>" in log


def test_endpoint_guard_accepts_only_the_documented_testnet_host():
    assert require_testnet_base_url("https://testnet.binance.vision") == (
        "https://testnet.binance.vision"
    )
    for marker in MAINNET_HOST_MARKERS:
        with pytest.raises(EndpointError):
            require_testnet_base_url(f"https://{marker}")
    with pytest.raises(EndpointError):
        require_testnet_base_url("https://example.invalid")
    with pytest.raises(EndpointError):
        require_testnet_base_url("http://testnet.binance.vision")


def test_a_mainnet_base_url_can_never_be_constructed(journal):
    with pytest.raises(MainnetForbiddenError):
        txf.ScriptedTransport(responses=[])
        from crypto_trader.execution.binance_testnet import BinanceSpotTestnetClient

        BinanceSpotTestnetClient(
            credentials=txf.credentials(),
            transport=txf.ScriptedTransport(responses=[]),
            clock=txf.clock(),
            base_url=f"https://{MAINNET_HOST_MARKERS[0]}",
            symbol=txf.SYMBOL,
        )


def test_break_glass_cancel_records_lifecycle_events_for_the_journal(journal, tmp_path):
    control, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-4"),
            txf.cancel_response("ct-4", order_id=100),
        ],
    )
    breakglass = BreakGlassControl(
        journal=journal, state_dir=tmp_path / "state", venue=control, clock=lambda: BASE
    )
    breakglass.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    outcome = breakglass.handle(
        parse_operator_command(
            {
                "command_type": "cancel_open",
                "command_id": str(uuid4()),
                "operator_label": "jamal",
                "reason": "venue emergency",
                "source": "operator-cli",
            },
            delivered_at=BASE,
        )
    )
    event_types = [event.event_type for event in journal.read_range()]

    assert outcome.venue_control == "confirmed"
    assert "order.state.changed" in event_types
    assert breakglass.status().new_risk_permitted is False
    assert breakglass.ownership_store.require_fence().owner_kind is OwnerKind.EMERGENCY


def _operator_command(command_type):
    return parse_operator_command(
        {
            "command_type": command_type,
            "command_id": str(uuid4()),
            "operator_label": "jamal",
            "reason": "venue emergency",
            "source": "operator-cli",
        },
        delivered_at=BASE,
    )


def _cancel_in_a_first_process(journal, state_dir, *later_responses):
    """Process A: cancel ct-4 through the safety path; its venue memory dies with it."""
    first, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-4"),
            txf.cancel_response("ct-4", order_id=100),
            *later_responses,
        ],
    )
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=first,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    process.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    assert process.handle(_operator_command("cancel_open")).venue_control == "confirmed"
    return process


def test_cancel_open_journals_the_cancelled_references_in_a_v2_record(journal, tmp_path):
    process = _cancel_in_a_first_process(journal, tmp_path / "state")
    records = [event for event in journal.read_range() if event.event_type == OPERATOR_COMMAND_EVENT_TYPE]

    assert [event.schema_version for event in records] == [2]
    assert records[0].payload.model_dump().get("cancelled_order_references") == ["ct-4"]
    ledger_record = process.load_ledger().records[0]
    assert getattr(ledger_record, "cancelled_order_references", None) == ("ct-4",)


def _reconcile_after_cancel(root, *, new_process):
    root.mkdir(parents=True)
    with SQLiteEventJournal(root / "journal.db") as journal:
        canceled = txf.order_response("ct-4", status="CANCELED")
        process = _cancel_in_a_first_process(journal, root / "state", canceled)
        transport = None
        if new_process:
            second, client, _, transport = venue(journal, [canceled])
            client.clock.synchronize(txf.SERVER_TIME_MS)
            process = BreakGlassControl(
                journal=journal,
                state_dir=root / "state",
                venue=second,
                clock=lambda: BASE,
                reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
            )
        return process.handle(_operator_command("reconcile")), transport


def test_reconcile_in_a_new_process_matches_the_cancelling_process(tmp_path):
    same, _ = _reconcile_after_cancel(tmp_path / "same", new_process=False)
    new, transport = _reconcile_after_cancel(tmp_path / "new", new_process=True)

    # ct-4 has no durable submission attempt here, so the venue evidence escalates it as an
    # unknown external order: both processes must stay pending (never "confirmed by evidence").
    assert (new.reconciliation_status, new.reason_code, new.reconciliation_result) == (
        "pending",
        "UNKNOWN_PENDING_RECONCILIATION",
        "reconciliation_left_orders_unresolved",
    )
    assert (same.reconciliation_status, same.reason_code, same.reconciliation_result) == (
        "pending",
        "UNKNOWN_PENDING_RECONCILIATION",
        "reconciliation_left_orders_unresolved",
    )
    assert [request.method for request in transport.requests] == ["GET"]


class _SilentVenue:
    """Tracks the journaled references but never returns venue evidence for them."""

    def __init__(self):
        self.tracked = []

    def cancel_open_orders(self, *, correlation_id, occurred_at, safety):
        raise AssertionError("reconcile must never cancel")

    def venue_reports(self):
        return ()

    def track_order_references(self, references):
        self.tracked.extend(references)


def _reconcile_in_a_new_process_with(journal, state_dir, venue_control):
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=venue_control,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    return process.handle(_operator_command("reconcile"))


def test_reconcile_stays_pending_when_the_venue_cannot_query_journaled_cancels(journal, tmp_path):
    state_dir = tmp_path / "state"
    _cancel_in_a_first_process(journal, state_dir)

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, PolicyOnlyVenueControl())

    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_missing_venue_evidence_for_cancelled_orders",
    )


def test_reconcile_stays_pending_when_the_venue_has_no_evidence_for_journaled_cancels(
    journal, tmp_path
):
    state_dir = tmp_path / "state"
    _cancel_in_a_first_process(journal, state_dir)
    silent = _SilentVenue()

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, silent)

    assert silent.tracked == ["ct-4"]
    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_missing_venue_evidence_for_cancelled_orders",
    )


def test_a_part_way_cancel_failure_never_journals_the_failed_cancel(journal, tmp_path):
    # Review F-03/N-04: the journal is a new process's only memory: the confirmed cancel (ct-1)
    # must reach it, and the cancel that timed out (ct-2) must never be recorded as cancelled.
    control, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-1", "ct-2"),
            txf.cancel_response("ct-1", order_id=100),
            TransportTimeout("lost"),
        ],
    )
    process = BreakGlassControl(
        journal=journal,
        state_dir=tmp_path / "state",
        venue=control,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    process.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )

    outcome = process.handle(_operator_command("cancel_open"))

    assert outcome.venue_control == "ambiguous"
    assert process.load_ledger().records[0].cancelled_order_references == ("ct-1",)
    assert process.load_ledger().records[0].unconfirmed_order_references == ("ct-2",)
    records = [e for e in journal.read_range() if e.event_type == OPERATOR_COMMAND_EVENT_TYPE]
    assert [event.schema_version for event in records] == [3]


def test_reconcile_in_a_new_process_still_asks_about_an_unconfirmed_cancel(journal, tmp_path):
    # Review N-05: ct-1's cancel was confirmed and ct-2's was rejected. A new process knows both
    # only from the journal; it must ask the venue about ct-2 too and stay pending without it.
    state_dir = tmp_path / "state"
    control, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-1", "ct-2"),
            txf.cancel_response("ct-1", order_id=100),
            txf.rejected(-2011, "Unknown order sent."),
        ],
    )
    first = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=control,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    first.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    assert first.handle(_operator_command("cancel_open")).venue_control == "rejected"
    silent = _SilentVenue()

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, silent)

    assert silent.tracked == ["ct-1", "ct-2"]
    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_missing_venue_evidence_for_cancelled_orders",
    )


_NEVER_LEARNED_THE_OPEN_ORDERS = {
    # Listing the open orders failed, so not a single order reference was ever known.
    "listing timed out": (
        [txf.time_response(), txf.exchange_info_response(), txf.account_response(), TransportTimeout("lost")],
        "rejected",
    ),
    "listing rejected": (
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.rejected(-1003, "Too many requests."),
        ],
        "rejected",
    ),
    # The venue could not even be verified, so the cancel was never attempted.
    "venue unreachable": ([TransportError("down")], "unavailable"),
    # Review FZ-10: a 200 whose body is empty or not JSON reaches the client as None, and a body
    # that is not a list names no orders either; neither is a listing of zero open orders.
    "listing body empty or not JSON": (
        [txf.time_response(), txf.exchange_info_response(), txf.account_response(), txf.response(200, None)],
        "rejected",
    ),
    "listing body not a list": (
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.response(200, {"orders": []}),
        ],
        "rejected",
    ),
}


def _cancel_that_never_learned_the_open_orders(journal, state_dir, case):
    responses, disposition = _NEVER_LEARNED_THE_OPEN_ORDERS[case]
    control, _, _, transport = venue(journal, responses)
    first = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=control,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    first.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    assert first.handle(_operator_command("cancel_open")).venue_control == disposition
    record = first.load_ledger().records[0]
    assert (record.cancelled_order_references, record.unconfirmed_order_references) == ((), ())
    assert not [request for request in transport.requests if request.method == "DELETE"]


@pytest.mark.parametrize("case", sorted(_NEVER_LEARNED_THE_OPEN_ORDERS))
def test_reconcile_never_resolves_a_cancel_that_never_learned_the_open_orders(
    journal, tmp_path, case
):
    # Review F-03 (remainder): with no order reference journaled there was nothing to verify, so a
    # new process resolved the epoch "confirmed by evidence" while orders could still be open.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, case)

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, _SilentVenue())

    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_missing_open_orders_listing",
    )


@pytest.mark.parametrize(
    ("listing", "status", "result"),
    [
        ([txf.open_orders_response()], "resolved", "reconciliation_confirmed_by_evidence"),
        (
            [txf.open_orders_response("ct-9"), txf.order_response("ct-9")],
            "pending",
            "reconciliation_left_orders_unresolved",
        ),
        ([TransportTimeout("lost")], "pending", "reconciliation_missing_open_orders_listing"),
        # Review F03-R1: a listed order the status read reports nothing for is not settled.
        (
            [txf.open_orders_response("ct-9"), txf.rejected(-2013, "Order does not exist.")],
            "pending",
            "reconciliation_missing_venue_evidence_for_listed_orders",
        ),
        (
            [txf.open_orders_response("ct-9"), txf.rejected(-1003, "Too many requests.")],
            "pending",
            "reconciliation_missing_venue_evidence_for_listed_orders",
        ),
        # Review FZ-10: an empty, non-JSON or non-list 200 body is no listing at all.
        ([txf.response(200, None)], "pending", "reconciliation_missing_open_orders_listing"),
        ([txf.response(200, {"orders": []})], "pending", "reconciliation_missing_open_orders_listing"),
    ],
    ids=[
        "nothing open",
        "an order still open",
        "listing fails again",
        "listed order status -2013",
        "listed order status -1003",
        "listing body empty or not JSON",
        "listing body not a list",
    ],
)
def test_reconcile_lists_the_open_orders_a_failed_cancel_never_learned(
    journal, tmp_path, listing, status, result
):
    # Review F-03 (remainder): only a complete listing taken now can settle what is open. An empty
    # listing is evidence; every listed order is reconciled like any other; a failed one is not.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    second, client, _, transport = venue(journal, listing)
    client.clock.synchronize(txf.SERVER_TIME_MS)

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, second)

    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (status, result)
    assert transport.requests and "openOrders" in transport.requests[0].url, transport.requests
    assert not [request for request in transport.requests if request.method == "DELETE"]


class _ListsButNeverReports(_SilentVenue):
    """Lists one open order but never returns venue evidence for it."""

    def track_open_orders(self):
        self.tracked.append("ct-9")
        return ("ct-9",)


def test_reconcile_never_resolves_a_listed_order_without_venue_evidence(journal, tmp_path):
    # Review F03-R1: a successful listing only names the open orders; each listed order still
    # needs its own venue report before the epoch may resolve, like every journaled reference.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    venue_control = _ListsButNeverReports()

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, venue_control)

    assert venue_control.tracked == ["ct-9"]
    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_missing_venue_evidence_for_listed_orders",
    )


# --- Review F-03 historical (reduced scope): durable observations, the intake and the ack ------


class _Crash(BaseException):
    """A hard stop (a killed process) that no reconcile code may turn into an outcome."""


class _CrashesOnReports(_SilentVenue):
    """The process dies while it reads the venue evidence, after the run has started."""

    def venue_reports(self):
        raise _Crash()


class _FailsOnReports(_SilentVenue):
    """The venue evidence read fails the way a transport failure does."""

    def venue_reports(self):
        raise TransportError("down")


def _halted_process(journal, state_dir):
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    assert process.handle(_operator_command("halt_new_risk")).accepted
    return process


def test_a_listed_external_order_stays_outstanding_after_it_leaves_the_listing(journal, tmp_path):
    # Review F-03 (Argon): the listing found ct-9, which the journal does not know; it then filled
    # or was cancelled and left the listing, and a new process saw an empty listing and resolved the
    # epoch, dropping the only evidence of ct-9. Its observation is journaled now and stays
    # outstanding.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    seen, client, _, _ = venue(journal, [txf.open_orders_response("ct-9"), txf.order_response("ct-9")])
    client.clock.synchronize(txf.SERVER_TIME_MS)
    first = _reconcile_in_a_new_process_with(journal, state_dir, seen)
    gone, client, _, _ = venue(journal, [txf.open_orders_response()])
    client.clock.synchronize(txf.SERVER_TIME_MS)

    second = _reconcile_in_a_new_process_with(journal, state_dir, gone)

    assert first.reconciliation_status == "pending"
    assert (second.reconciliation_status, second.reconciliation_result) == (
        "pending",
        "reconciliation_external_orders_outstanding",
    )


def test_a_later_reconcile_run_requires_a_fresh_acknowledgement(journal, tmp_path):
    # Review F-03 (ADV-02/FZ-13): an acknowledgement covers the evidence it saw; any later reconcile
    # run starts from new evidence, so normal is restored only after a fresh acknowledgement.
    process = _halted_process(journal, tmp_path / "state")
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"
    assert process.handle(_operator_command("acknowledge")).accepted
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"

    stale = process.handle(_operator_command("restore_normal"))

    assert (stale.accepted, stale.reason_code) == (False, "restore_denied_acknowledgement_missing")
    assert process.handle(_operator_command("acknowledge")).accepted
    assert process.handle(_operator_command("restore_normal")).accepted


def test_a_clock_stepped_back_never_replays_a_later_run_before_the_acknowledgement(
    journal, tmp_path
):
    # Replay orders by occurred_at first. With the wall clock stepped back after the acknowledgement
    # (an NTP correction), the later run was journaled before it, so the stale acknowledgement
    # survived and normal was restored on evidence nobody acknowledged.
    now = [BASE]
    process = BreakGlassControl(
        journal=journal,
        state_dir=tmp_path / "state",
        clock=lambda: now[0],
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    assert process.handle(_operator_command("halt_new_risk")).accepted
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"
    now[0] = BASE + timedelta(seconds=10)
    assert process.handle(_operator_command("acknowledge")).accepted
    now[0] = BASE + timedelta(seconds=5)
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"

    stale = process.handle(_operator_command("restore_normal"))

    assert (stale.accepted, stale.reason_code) == (False, "restore_denied_acknowledgement_missing")
    assert stale.delivered_at == BASE + timedelta(seconds=10)


def test_a_run_that_died_before_recording_what_it_saw_keeps_the_epoch_unsettled(journal, tmp_path):
    # Review F-03 (FZ-06/ADV-01): the run journals its intake before the first venue read. A run that
    # died before journaling its observation may have seen orders nobody recorded, so no later run,
    # acknowledgement or restore may treat the epoch as settled.
    state_dir = tmp_path / "state"
    process = _halted_process(journal, state_dir)
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"
    assert process.handle(_operator_command("acknowledge")).accepted
    crashing = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=_CrashesOnReports(),
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    with pytest.raises(_Crash):
        crashing.handle(_operator_command("reconcile"))

    later = _reconcile_in_a_new_process_with(journal, state_dir, PolicyOnlyVenueControl())
    acknowledge = process.handle(_operator_command("acknowledge"))
    restore = process.handle(_operator_command("restore_normal"))

    assert (later.reconciliation_status, later.reason_code) == ("pending", "observation_possibly_lost")
    assert (acknowledge.accepted, acknowledge.reason_code) == (
        False,
        "acknowledgement_denied_observation_possibly_lost",
    )
    assert (restore.accepted, restore.reason_code) == (
        False,
        "restore_denied_observation_possibly_lost",
    )


def test_status_names_every_reconcile_run_gate_that_blocks_restore(journal, tmp_path):
    # Review F-03: after a run died before journaling what it saw, status listed only the missing
    # acknowledgement, so an operator was sent to acknowledge and then refused for another reason.
    state_dir = tmp_path / "state"
    process = _halted_process(journal, state_dir)
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"
    assert process.handle(_operator_command("acknowledge")).accepted
    crashing = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=_CrashesOnReports(),
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    with pytest.raises(_Crash):
        crashing.handle(_operator_command("reconcile"))

    assert process.status().restore_blockers == (
        "observation_possibly_lost",
        "reconciliation_run_incomplete",
        "operator_acknowledgement_missing",
    )


def test_a_failed_venue_read_inside_a_run_is_recorded_and_the_next_run_settles(journal, tmp_path):
    # Review F-03: an ordinary venue failure inside a run is journaled with the run's observation
    # (evidence unavailable, so pending), never left as a lost observation; the next run settles.
    state_dir = tmp_path / "state"
    _halted_process(journal, state_dir)

    failed = _reconcile_in_a_new_process_with(journal, state_dir, _FailsOnReports())
    later = _reconcile_in_a_new_process_with(journal, state_dir, PolicyOnlyVenueControl())

    assert (failed.reconciliation_status, failed.reconciliation_result) == (
        "pending",
        "reconciliation_venue_evidence_unavailable",
    )
    assert later.reconciliation_status == "resolved"


def test_a_run_that_died_after_recording_what_it_saw_is_superseded_by_the_next(
    journal, tmp_path, monkeypatch
):
    # Review F-03: everything such a run saw is journaled, so the next complete run may settle the
    # epoch; until it does, neither the acknowledgement nor restore treats the epoch as complete.
    from crypto_trader.breakglass import control as control_module

    process = _halted_process(journal, tmp_path / "state")
    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"
    assert process.handle(_operator_command("acknowledge")).accepted

    def crash(self, **kwargs):
        raise _Crash()

    with monkeypatch.context() as patch:
        patch.setattr(control_module.RestartRecovery, "run", crash)
        with pytest.raises(_Crash):
            process.handle(_operator_command("reconcile"))
    acknowledge = process.handle(_operator_command("acknowledge"))
    restore = process.handle(_operator_command("restore_normal"))
    later = process.handle(_operator_command("reconcile"))

    assert (acknowledge.accepted, acknowledge.reason_code) == (
        False,
        "acknowledgement_denied_reconciliation_run_incomplete",
    )
    assert (restore.accepted, restore.reason_code) == (
        False,
        "restore_denied_reconciliation_run_incomplete",
    )
    assert later.reconciliation_status == "resolved"
    assert process.handle(_operator_command("acknowledge")).accepted
    assert process.handle(_operator_command("restore_normal")).accepted


def test_a_run_journals_each_listed_order_the_journal_does_not_know(journal, tmp_path):
    # Review F-03: the observation names every listed order the journal does not know, with the
    # venue order id the strict listing reported; an order the journal knows is never external.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    listing, client, _, _ = venue(
        journal,
        [
            txf.open_orders_response("ct-1", "ct-9"),
            txf.order_response("ct-1", order_id=100),
            txf.order_response("ct-9", order_id=101),
        ],
    )
    client.clock.synchronize(txf.SERVER_TIME_MS)
    known = LocalOrderState(
        order_id=uuid4(),
        order_reference="ct-1",
        intent_id=None,
        correlation_id=None,
        symbol=txf.SYMBOL,
        state=OrderStateName.ACCEPTED,
        remaining_quantity=Decimal("0.001"),
        submission_attempted=True,
    )
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=listing,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
        local_state_provider=lambda: (known,),
    )

    outcome = process.handle(_operator_command("reconcile"))
    intake, observed = [
        event
        for event in journal.read_range()
        if event.event_type in (RECONCILIATION_INTAKE_EVENT_TYPE, RECONCILIATION_OBSERVED_EVENT_TYPE)
    ]

    assert (intake.event_type, intake.payload.command_id) == (
        RECONCILIATION_INTAKE_EVENT_TYPE,
        outcome.command_id,
    )
    assert (observed.causation_id, observed.payload.listing) == (intake.event_id, "complete")
    assert observed.payload.venue_evidence_complete is True
    assert [
        (order.client_order_id, order.venue_order_id) for order in observed.payload.external_orders
    ] == [("ct-9", "101")]
    assert outcome.reconciliation_status == "pending"


def test_a_run_for_a_synthesised_epoch_keeps_the_ledger_derivable(journal, tmp_path):
    # Review F-03: a fail-closed safety state with no journaled epoch synthesises one; its run is
    # journaled too, settles nothing, and never breaks the ledger for the commands after it.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "safety_state.json").write_text("{not json", encoding="utf-8")
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )

    first = process.handle(_operator_command("reconcile"))
    second = process.handle(_operator_command("reconcile"))

    assert first.accepted and second.accepted
    assert process.status().epoch_provenance == "provenance_missing"
    assert process.load_ledger().epoch is None


# --- Review F-03 settlement: an external order the venue proves never executed ---------------


def _an_outstanding_external_order(journal, state_dir):
    """The listing found ct-9 (venue order 100), which the journal does not know: outstanding."""
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    seen, client, _, _ = venue(
        journal, [txf.open_orders_response("ct-9"), txf.order_response("ct-9", order_id=100)]
    )
    client.clock.synchronize(txf.SERVER_TIME_MS)
    first = _reconcile_in_a_new_process_with(journal, state_dir, seen)
    assert first.reconciliation_status == "pending"


def _settled_events(journal):
    return [e for e in journal.read_range() if e.event_type == RECONCILIATION_SETTLED_EVENT_TYPE]


def test_an_external_order_the_venue_shows_cancelled_unexecuted_is_settled(journal, tmp_path):
    # Review F-03 settlement: the strict status read shows the venue order first observed, a
    # terminal status and no execution, so the order is journaled as settled and the epoch can
    # resolve, be acknowledged and return to normal.
    state_dir = tmp_path / "state"
    _an_outstanding_external_order(journal, state_dir)
    later, client, _, transport = venue(
        journal,
        [txf.open_orders_response(), txf.order_response("ct-9", status="CANCELED", order_id=100)],
    )
    client.clock.synchronize(txf.SERVER_TIME_MS)

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, later)
    settled = _settled_events(journal)
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )

    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "resolved",
        "reconciliation_confirmed_by_evidence",
    )
    assert [
        (order.client_order_id, order.venue_order_id, order.status, order.executed_quantity)
        for order in settled[0].payload.settled_orders
    ] == [("ct-9", "100", "CANCELED", Decimal("0"))]
    assert not [request for request in transport.requests if request.method == "DELETE"]
    assert process.handle(_operator_command("acknowledge")).accepted
    assert process.handle(_operator_command("restore_normal")).accepted


def _without(response, field):
    body = dict(response.body)
    del body[field]
    return txf.response(200, body)


@pytest.mark.parametrize(
    "evidence",
    [
        txf.order_response("ct-9", status="NEW", order_id=100),
        txf.order_response("ct-9", status="FILLED", order_id=100, executed_qty="0.00100000"),
        txf.order_response("ct-9", status="CANCELED", order_id=100, executed_qty="0.00050000"),
        txf.order_response("ct-9", status="EXPIRED_IN_MATCH", order_id=100),
        txf.order_response("ct-9", status="CANCELED", order_id=101),
        txf.rejected(-2013, "Order does not exist."),
        TransportTimeout("lost"),
        # A lenient read would assume no execution, or accept another order's answer.
        _without(txf.order_response("ct-9", status="CANCELED", order_id=100), "executedQty"),
        txf.order_response("ct-8", status="CANCELED", order_id=100),
    ],
    ids=[
        "still open",
        "filled",
        "partly executed then cancelled",
        "outside the approved terminal statuses",
        "another venue order",
        "unknown to the venue",
        "status read timed out",
        "no executed quantity reported",
        "answer for another client order id",
    ],
)
def test_an_external_order_stays_outstanding_without_proof_it_never_executed(
    journal, tmp_path, evidence
):
    # Review F-03 settlement: anything short of the exact venue order, an approved terminal status
    # and zero execution leaves the order outstanding; executed orders need trade history.
    state_dir = tmp_path / "state"
    _an_outstanding_external_order(journal, state_dir)
    later, client, _, _ = venue(journal, [txf.open_orders_response(), evidence])
    client.clock.synchronize(txf.SERVER_TIME_MS)

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, later)

    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_external_orders_outstanding",
    )
    assert _settled_events(journal) == []


class _ListsWithIdentities(_SilentVenue):
    """Lists the given orders with their venue order ids and answers the given status evidence."""

    def __init__(self, listed=(), evidence=()):
        super().__init__()
        self.listed = dict(listed)
        self.evidence = dict(evidence)

    def track_open_orders(self):
        return tuple(self.listed)

    def listed_venue_order_ids(self):
        return dict(self.listed)

    def order_settlement_evidence(self, reference):
        return self.evidence.get(reference)


def test_settling_a_reused_client_order_id_settles_only_that_venue_order(journal, tmp_path):
    # Review F-03 settlement: the venue may reuse a client order id once an order ends. ct-9 was
    # listed as venue order 100 and later as 205; settling 205 must leave 100 outstanding.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    for listed in ({"ct-9": "100"}, {"ct-9": "205"}):
        _reconcile_in_a_new_process_with(journal, state_dir, _ListsWithIdentities(listed=listed))
    settles = _ListsWithIdentities(evidence={"ct-9": ("205", "CANCELED", Decimal("0"))})

    outcome = _reconcile_in_a_new_process_with(journal, state_dir, settles)
    settled = _settled_events(journal)[0].payload.settled_orders
    ledger = BreakGlassControl(journal=journal, state_dir=state_dir).load_ledger()

    assert (outcome.reconciliation_status, outcome.reconciliation_result) == (
        "pending",
        "reconciliation_external_orders_outstanding",
    )
    assert [order.venue_order_id for order in settled] == ["205"]
    assert ledger.observations.external_orders == (("ct-9", "100"),)


# --- Review ADV-04: a listing run journaled before reconcile runs recorded anything ------------


def _as_code_before_run_events(patch):
    """Behave as the code before review F-03: no intake, observation or settlement, no run gate."""
    from crypto_trader.breakglass import control as control_module

    patch.setattr(
        control_module.BreakGlassControl, "_append_run_event", lambda self, *a, **k: uuid4()
    )
    patch.setattr(control_module.BreakGlassControl, "_reconcile_run_blocker", lambda self: None)


def test_a_listing_run_by_older_code_keeps_the_epoch_unsettled(journal, tmp_path, monkeypatch):
    # Review ADV-04: older code listed the open orders a failed cancel never learned, found ct-9,
    # failed to read its status and journaled nothing about it. ct-9 then left the listing, and the
    # new code resolved the epoch on an empty listing: the only evidence of ct-9 was gone.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    older, client, _, _ = venue(
        journal, [txf.open_orders_response("ct-9"), txf.rejected(-1003, "Too many requests.")]
    )
    client.clock.synchronize(txf.SERVER_TIME_MS)
    with monkeypatch.context() as patch:
        _as_code_before_run_events(patch)
        legacy = _reconcile_in_a_new_process_with(journal, state_dir, older)
    gone, client, _, _ = venue(journal, [txf.open_orders_response()])
    client.clock.synchronize(txf.SERVER_TIME_MS)

    later = _reconcile_in_a_new_process_with(journal, state_dir, gone)
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    acknowledge = process.handle(_operator_command("acknowledge"))
    restore = process.handle(_operator_command("restore_normal"))

    assert legacy.reconciliation_result == "reconciliation_missing_venue_evidence_for_listed_orders"
    assert (later.reconciliation_status, later.reason_code) == (
        "pending",
        "legacy_external_evidence_unverifiable",
    )
    assert (acknowledge.accepted, acknowledge.reason_code) == (
        False,
        "acknowledgement_denied_legacy_external_evidence_unverifiable",
    )
    assert (restore.accepted, restore.reason_code) == (
        False,
        "restore_denied_legacy_external_evidence_unverifiable",
    )
    assert process.status().restore_blockers[0] == "legacy_external_evidence_unverifiable"


def test_a_run_by_older_code_before_the_open_orders_were_unknown_never_listed(
    journal, tmp_path, monkeypatch
):
    # Review ADV-04: the older run came before any cancel left the open orders unknown, so it never
    # listed them and hid nothing; the epoch settles as usual.
    state_dir = tmp_path / "state"
    control, _, _, _ = venue(
        journal,
        [txf.time_response(), txf.exchange_info_response(), txf.account_response(), TransportTimeout("lost")],
    )
    first = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=control,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    first.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    assert first.handle(_operator_command("halt_new_risk")).accepted
    with monkeypatch.context() as patch:
        _as_code_before_run_events(patch)
        assert first.handle(_operator_command("reconcile")).accepted
    assert first.handle(_operator_command("cancel_open")).venue_control == "rejected"
    listing, client, _, _ = venue(journal, [txf.open_orders_response()])
    client.clock.synchronize(txf.SERVER_TIME_MS)

    later = _reconcile_in_a_new_process_with(journal, state_dir, listing)

    assert (later.reconciliation_status, later.reconciliation_result) == (
        "resolved",
        "reconciliation_confirmed_by_evidence",
    )


def test_a_listing_run_by_older_code_in_a_closed_epoch_never_blocks_a_later_one(
    journal, tmp_path, monkeypatch
):
    # Review ADV-04: an epoch the older code already closed keeps its history; only the open
    # epoch is judged, so a new epoch settles as usual.
    state_dir = tmp_path / "state"
    _cancel_that_never_learned_the_open_orders(journal, state_dir, "listing timed out")
    process = BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=_ListsWithIdentities(),
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    with monkeypatch.context() as patch:
        _as_code_before_run_events(patch)
        assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"
        assert process.handle(_operator_command("acknowledge")).accepted
        assert process.handle(_operator_command("restore_normal")).accepted
    assert process.handle(_operator_command("halt_new_risk")).accepted

    assert process.handle(_operator_command("reconcile")).reconciliation_status == "resolved"


def _journal_a_v3_cancel(journal, tmp_path):
    # ct-1's cancel is confirmed and ct-2's times out, so the command is journaled as V3.
    control, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-1", "ct-2"),
            txf.cancel_response("ct-1", order_id=100),
            TransportTimeout("lost"),
        ],
    )
    process = BreakGlassControl(
        journal=journal,
        state_dir=tmp_path / "state",
        venue=control,
        clock=lambda: BASE,
        reconciliation_policy=load_recovery_policy({"policy_id": "test"}),
    )
    process.ownership_store.takeover(
        owner_id="normal:runtime",
        owner_kind=OwnerKind.NORMAL,
        acquired_at=BASE,
        reason_code="runtime_started",
    )
    assert process.handle(_operator_command("cancel_open")).venue_control == "ambiguous"
    return process


def test_the_audit_record_of_a_v3_command_shows_both_reference_lists(journal, tmp_path):
    # Review N-06: the audit record rebuilt from the journal shows the cancel the venue never
    # confirmed (ct-2) apart from the confirmed one (ct-1).
    process = _journal_a_v3_cancel(journal, tmp_path)

    record = process.load_ledger().records[0].to_record()

    assert record["cancelled_order_references"] == ["ct-1"]
    assert record["unconfirmed_order_references"] == ["ct-2"]


def test_the_v3_contract_refuses_a_reference_both_cancelled_and_unconfirmed(journal, tmp_path):
    # Review N-07: the writer keeps the two lists apart; the contract must refuse an overlap too,
    # for any V3 payload it validates on read or replay.
    _journal_a_v3_cancel(journal, tmp_path)
    (event,) = [e for e in journal.read_range() if e.event_type == OPERATOR_COMMAND_EVENT_TYPE]
    data = event.payload.model_dump()
    valid = OperatorCommandRecordPayloadV3.model_validate(data)
    assert (valid.cancelled_order_references, valid.unconfirmed_order_references) == (
        ["ct-1"],
        ["ct-2"],
    )
    overlap = {**data, "cancelled_order_references": ["ct-1", "ct-2"]}

    with pytest.raises(ValidationError, match="both cancelled and unconfirmed"):
        OperatorCommandRecordPayloadV3.model_validate(overlap)


def test_a_legacy_v1_command_record_replays_with_no_cancelled_references(journal, tmp_path):
    legacy = _operator_command("cancel_open")
    journal.append(
        OperatorCommandRecorded(
            event_id=uuid4(),
            occurred_at=BASE,
            correlation_id=legacy.command_id,
            causation_id=legacy.causation_id,
            producer="application.breakglass",
            sequence_number=100,
            payload=OperatorCommandRecordPayload(
                command_id=legacy.command_id,
                command_type="cancel_open",
                requested_at=legacy.requested_at,
                operator_label=legacy.operator_label,
                operator_identity=legacy.operator_identity,
                reason=legacy.reason,
                source=legacy.source,
                previous_safety_state="normal",
                resulting_safety_state="reduce_only",
                disposition="accepted",
                reason_code="break_glass_engaged_cancel_open",
                command_digest=legacy.digest,
                mode_path=("normal", "reduce_only"),
                venue_control="confirmed",
                reconciliation_status="pending",
                epoch_opened=True,
                epoch_closed=False,
                fence_term=2,
            ),
        )
    )
    control = BreakGlassControl(journal=journal, state_dir=tmp_path / "state", clock=lambda: BASE)

    assert type(list(journal.read_range())[0]) is OperatorCommandRecorded
    record = control.load_ledger().records[0]
    assert getattr(record, "cancelled_order_references", None) == ()


def test_venue_reports_expose_only_reference_backed_evidence(journal):
    control, client, _, _ = venue(
        journal,
        [txf.order_response("ct-5", status="CANCELED")],
    )
    client.clock.synchronize(txf.SERVER_TIME_MS)
    control.track_order_references(["ct-5"])
    reports = control.venue_reports()

    assert len(reports) == 1
    assert reports[0].order_reference == "ct-5"
    assert reports[0].state.value == "canceled"
    assert reports[0].remaining_quantity == Decimal("0.00100000")


def test_cancel_preflight_is_current_and_attached_to_outcome(journal):
    control, _, _, transport = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response("ct-red-1"),
            txf.cancel_response("ct-red-1", order_id=100),
        ],
    )
    safety = SafetyController(correlation_id=uuid4())

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=safety
    )

    assert outcome.confirmed
    assert outcome.preflight is not None
    assert outcome.preflight.safety_mode == safety.mode.value
    assert len([r for r in transport.requests if r.method == "DELETE"]) == 1


def test_cancel_preflight_refuses_non_trading_account_before_delete(journal):
    control, _, _, transport = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(can_trade=False),
        ],
    )

    outcome = control.cancel_open_orders(
        correlation_id=uuid4(),
        occurred_at=BASE,
        safety=SafetyController(correlation_id=uuid4()),
    )

    assert outcome.disposition == "unavailable"
    assert outcome.reason_code == "venue_environment_unverified"
    assert [request for request in transport.requests if request.method == "DELETE"] == []
    assert txf.request_paths(transport).count("/api/v3/exchangeInfo") == 1


def test_testnet_acceptance_rejects_undocumented_binance_vision_hosts():
    for undocumented in (
        "https://binance.vision",
        "https://other.binance.vision",
        "https://child.testnet.binance.vision",
    ):
        with pytest.raises(EndpointError):
            require_testnet_base_url(undocumented)


def test_preflight_forces_symbol_rule_refresh_even_when_cache_is_warm(journal):
    control, client, _, transport = venue(
        journal,
        [
            txf.exchange_info_response(),
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
        ],
    )
    client.symbol_rules()
    assert txf.request_paths(transport).count("/api/v3/exchangeInfo") == 1

    preflight = control.preflight(safety=SafetyController(correlation_id=uuid4()))

    assert preflight.passed
    assert txf.request_paths(transport).count("/api/v3/exchangeInfo") == 2


def test_failed_second_cancel_never_reuses_stale_preflight_evidence(journal):
    control, _, _, _ = venue(
        journal,
        [
            txf.time_response(),
            txf.exchange_info_response(),
            txf.account_response(),
            txf.open_orders_response(),
            TransportError("second preflight unavailable"),
        ],
    )
    safety = SafetyController(correlation_id=uuid4())

    first = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=safety
    )
    second = control.cancel_open_orders(
        correlation_id=uuid4(), occurred_at=BASE, safety=safety
    )

    assert first.preflight is not None
    assert second.disposition == "unavailable"
    assert second.preflight is None
