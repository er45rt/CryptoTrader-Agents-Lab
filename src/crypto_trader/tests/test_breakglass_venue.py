"""Phase 13: venue-side emergency control (Spot Testnet only, cancel-open only, never retried)."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from crypto_trader.breakglass import BreakGlassControl, OwnerKind, parse_operator_command
from crypto_trader.breakglass.venue import BinanceSpotTestnetEmergencyControl
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
from crypto_trader.persistence import SQLiteEventJournal
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
            txf.order_response("ct-1", status="CANCELED"),
        ],
    )

    outcome = control.cancel_open_orders(correlation_id=uuid4(), occurred_at=BASE)
    deletes = [request for request in transport.requests if request.method == "DELETE"]

    assert outcome.confirmed
    assert outcome.cancelled_order_references == ("ct-1",)
    assert len(deletes) == 1
    assert outcome.to_record()["preflight"] is None
    assert client.request_log()


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

    outcome = control.cancel_open_orders(correlation_id=uuid4(), occurred_at=BASE)
    deletes = [request for request in transport.requests if request.method == "DELETE"]

    assert outcome.ambiguous
    assert outcome.reason_code == "UNKNOWN_PENDING_RECONCILIATION"
    assert len(deletes) == 1


def test_an_unreachable_venue_is_reported_as_unavailable(journal):
    control, _, _, _ = venue(journal, [TransportError("down")])

    outcome = control.cancel_open_orders(correlation_id=uuid4(), occurred_at=BASE)

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
            txf.order_response("ct-3", status="CANCELED"),
        ],
    )
    control.cancel_open_orders(correlation_id=uuid4(), occurred_at=BASE)
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
            txf.order_response("ct-4", status="CANCELED"),
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
