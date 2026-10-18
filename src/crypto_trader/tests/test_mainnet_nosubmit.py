"""Phase 15: the internal hard no-submit guarantee and the read-only endpoint allowlist."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    ExecutionTimeInForce,
)
from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.execution.binance_testnet.client import PATH_ORDER
from crypto_trader.mainnet import (
    DRY_RUN_MODE,
    READ_ONLY_PATHS,
    DryRunMode,
    MainnetConfigError,
    MainnetEndpointError,
    MainnetMutationForbiddenError,
    assert_no_mutation_surface,
    parse_dry_run_mode,
    require_mainnet_base_url,
    require_read_only_path,
)
from crypto_trader.tests import mainnet_fixtures as fx


class _HostileAdapter:
    """A stand-in that wrongly exposes a mutating method."""

    def place_order(self, request):  # pragma: no cover - must be detected, never called
        return request


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        intent_id=uuid4(),
        correlation_id=uuid4(),
        symbol=fx.SYMBOL,
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("0.01000"),
        limit_price=Decimal("27000.00"),
        time_in_force=ExecutionTimeInForce.FOK,
    )


# --- the gate --------------------------------------------------------------

def test_the_gate_refuses_a_venue_submission_and_counts_the_attempt():
    gate = fx.gate()
    gate.start()

    with pytest.raises(MainnetMutationForbiddenError):
        gate.submit(_request())

    assert gate.mutation_attempts == 1
    assert gate.mutation_attempt_log == ("order_submit",)
    with pytest.raises(MainnetMutationForbiddenError):
        gate.assert_mutation_free()


@pytest.mark.parametrize(
    "operation", ["order_cancel", "order_replace", "account_transfer", "capital_withdrawal"]
)
def test_other_mutating_operations_are_refused(operation: str):
    gate = fx.gate()

    with pytest.raises(MainnetMutationForbiddenError):
        gate.refuse(operation)
    assert gate.mutation_attempts == 1


def test_the_gate_records_a_would_submit_without_any_venue_call():
    gate = fx.gate()

    assert gate.guard_would_submit("dry-order-1") == "WOULD_SUBMIT"
    assert gate.would_submit_references == ("dry-order-1",)
    assert gate.mutation_attempts == 0
    gate.assert_mutation_free()
    assert gate.observations() == ()


# --- mode cannot be downgraded --------------------------------------------

def test_the_only_supported_mode_is_the_readonly_dry_run():
    assert parse_dry_run_mode(None) is DRY_RUN_MODE
    assert parse_dry_run_mode("mainnet_readonly_dry_run") is DryRunMode.MAINNET_READONLY_DRY_RUN
    assert list(DryRunMode) == [DryRunMode.MAINNET_READONLY_DRY_RUN]


@pytest.mark.parametrize(
    "alias", ["live", "mainnet", "trade", "trading", "canary", "canary_live", "production", "real"]
)
def test_a_live_looking_mode_is_refused(alias: str):
    with pytest.raises(MainnetMutationForbiddenError):
        parse_dry_run_mode(alias)


def test_an_unknown_mode_fails_closed():
    with pytest.raises(MainnetConfigError):
        parse_dry_run_mode("banana")
    with pytest.raises(MainnetConfigError):
        parse_dry_run_mode("")


def test_the_gate_cannot_be_constructed_in_a_live_mode():
    from crypto_trader.mainnet import HardNoSubmitGate

    with pytest.raises(MainnetMutationForbiddenError):
        HardNoSubmitGate(instrument=fx.INSTRUMENT, mode="live")


# --- mutation surface ------------------------------------------------------

def test_the_read_only_surfaces_have_no_mutating_method():
    assert_no_mutation_surface(fx.gate())
    assert_no_mutation_surface(fx.client(ScriptedTransport(responses=[])))


def test_a_mutating_surface_is_detected():
    with pytest.raises(MainnetMutationForbiddenError):
        assert_no_mutation_surface(_HostileAdapter())


# --- endpoint separation ---------------------------------------------------

def test_the_mainnet_host_is_accepted_and_a_testnet_host_is_refused():
    assert require_mainnet_base_url("https://api.binance.com") == "https://api.binance.com"
    for bad in (
        "https://testnet.binance.vision",
        "http://api.binance.com",
        "https://example.com",
        "",
    ):
        with pytest.raises(MainnetEndpointError):
            require_mainnet_base_url(bad)


def test_the_read_only_allowlist_accepts_only_read_paths():
    for path in sorted(READ_ONLY_PATHS):
        assert require_read_only_path(path) == path
    with pytest.raises(MainnetMutationForbiddenError):
        require_read_only_path(PATH_ORDER)
    with pytest.raises(MainnetEndpointError):
        require_read_only_path("/api/v3/someUnknownEndpoint")
    with pytest.raises(MainnetEndpointError):
        require_read_only_path("api/v3/account")


def test_the_client_only_ever_sends_get_and_never_reaches_the_order_path():
    transport = ScriptedTransport(responses=[fx.response(200, fx.restrictions_body())])
    client = fx.client(transport)

    client.api_restrictions()
    assert [request.method for request in transport.requests] == ["GET"]

    before = len(transport.requests)
    with pytest.raises(MainnetMutationForbiddenError):
        client._get(PATH_ORDER, {}, signed=True)
    assert len(transport.requests) == before, "a refused path must not reach the transport"

    with pytest.raises(MainnetEndpointError):
        client._get("/api/v3/someUnknownEndpoint", {}, signed=False)
    assert len(transport.requests) == before


def test_a_mutation_attempt_on_the_client_surface_is_refused_before_transport():
    transport = ScriptedTransport(responses=[])
    client = fx.client(transport)

    with pytest.raises(MainnetMutationForbiddenError):
        client._get("/api/v3/order/../../anything", {}, signed=True)
    assert transport.requests == []


def test_mainnet_transport_retry_uses_injected_backoff_sleep():
    from crypto_trader.execution.binance_testnet import TransportTimeout
    from crypto_trader.mainnet import MainnetReadOnlyClient

    sleeps: list[float] = []
    transport = ScriptedTransport(
        responses=[
            TransportTimeout("transient read timeout"),
            fx.response(200, fx.restrictions_body()),
        ]
    )
    client = MainnetReadOnlyClient(
        credentials=fx.credentials(),
        transport=transport,
        clock=fx.clock(),
        sleep=sleeps.append,
    )

    client.api_restrictions()

    assert sleeps == [client.retry.delay_for_attempt(1)]
    assert [request.method for request in transport.requests] == ["GET", "GET"]


def test_mainnet_final_transport_failure_does_not_sleep_after_exhaustion():
    from crypto_trader.execution.binance_testnet import TransportTimeout
    from crypto_trader.mainnet import MainnetReadOnlyClient

    sleeps: list[float] = []
    transport = ScriptedTransport(
        responses=[TransportTimeout("timeout")] * 3
    )
    client = MainnetReadOnlyClient(
        credentials=fx.credentials(),
        transport=transport,
        clock=fx.clock(),
        sleep=sleeps.append,
    )

    with pytest.raises(TransportTimeout):
        client.api_restrictions()

    assert sleeps == [
        client.retry.delay_for_attempt(1),
        client.retry.delay_for_attempt(2),
    ]
    assert len(transport.requests) == 3


def test_mainnet_non_200_response_neither_retries_nor_sleeps():
    from crypto_trader.mainnet import MainnetReadOnlyClient

    sleeps: list[float] = []
    transport = ScriptedTransport(
        responses=[fx.response(429, {"code": -1003, "msg": "Too many requests"})]
    )
    client = MainnetReadOnlyClient(
        credentials=fx.credentials(),
        transport=transport,
        clock=fx.clock(),
        sleep=sleeps.append,
    )

    with pytest.raises(MainnetEndpointError):
        client.api_restrictions()

    assert sleeps == []
    assert len(transport.requests) == 1


def test_mainnet_signed_retry_rebuilds_timestamp_each_attempt():
    from urllib.parse import parse_qs
    from crypto_trader.execution.binance_testnet import TestnetClock, TransportTimeout
    from crypto_trader.mainnet import MainnetReadOnlyClient

    ticks = iter([fx.SERVER_TIME_MS / 1000, (fx.SERVER_TIME_MS + 1000) / 1000])
    clock = TestnetClock(time_source=lambda: next(ticks), recv_window_ms=5_000)
    clock.synchronize(fx.SERVER_TIME_MS, local_time_ms=fx.SERVER_TIME_MS)
    transport = ScriptedTransport(
        responses=[
            TransportTimeout("first signed read timed out"),
            fx.response(200, fx.restrictions_body()),
        ]
    )
    client = MainnetReadOnlyClient(
        credentials=fx.credentials(),
        transport=transport,
        clock=clock,
        sleep=lambda _seconds: None,
    )

    client.api_restrictions()

    timestamps = [
        int(parse_qs(request.query)["timestamp"][0])
        for request in transport.requests
    ]
    assert timestamps == [fx.SERVER_TIME_MS, fx.SERVER_TIME_MS + 1000]
