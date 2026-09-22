"""Phase 11: Spot Testnet client behaviour under realistic failure modes."""

from decimal import Decimal

import pytest

from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    ClockDriftError,
    RateLimitBanError,
    RateLimitError,
    ScriptedTransport,
    WithdrawalPolicy,
    TransportTimeout,
)
from crypto_trader.execution.binance_testnet import TestnetSymbolError as SymbolError
from crypto_trader.execution.binance_testnet import TestnetRejectedError as RejectedError
from crypto_trader.execution.binance_testnet import UnsafeTestnetAccountError as UnsafeAccountError
from crypto_trader.tests import testnet_fixtures as fx

CLIENT_ORDER_ID = "ct-0000000000000000abcd"


def test_verify_environment_accepts_a_least_privilege_testnet_account():
    transport = ScriptedTransport(responses=fx.environment_responses())
    client = fx.client(transport)

    environment = client.verify_environment()

    assert environment.venue_label == "binance-spot-testnet"
    assert environment.base_url == "https://testnet.binance.vision"
    assert environment.account.can_trade is True
    assert environment.account.withdrawals_available is False
    assert "withdrawals unavailable" in " ".join(environment.notes)
    assert fx.request_paths(transport) == [
        "/api/v3/time",
        "/api/v3/exchangeInfo",
        "/api/v3/account",
    ]


def test_verify_environment_fails_closed_when_withdrawals_are_enabled():
    transport = ScriptedTransport(responses=fx.environment_responses(can_withdraw=True))
    client = fx.client(transport)

    with pytest.raises(UnsafeAccountError):
        client.verify_environment()


def test_record_only_policy_accepts_a_venue_default_withdrawal_flag_and_records_it():
    """Binance Spot Testnet grants the withdrawal bit by default; it must be recorded, not used."""
    transport = ScriptedTransport(responses=fx.environment_responses(can_withdraw=True))
    client = fx.client(transport, withdrawal_policy=WithdrawalPolicy.RECORD_ONLY)

    environment = client.verify_environment()

    assert environment.account.withdrawals_available is True
    assert environment.to_record()["can_withdraw"] is True
    assert any("VENUE REPORTS WITHDRAWALS ENABLED" in note for note in environment.notes)
    assert any("no withdrawal capability is implemented" in note for note in environment.notes)


def test_verify_environment_fails_closed_when_trading_is_disabled():
    transport = ScriptedTransport(responses=fx.environment_responses(can_trade=False))

    with pytest.raises(UnsafeAccountError):
        fx.client(transport).verify_environment()


def test_environment_record_and_request_log_contain_no_credentials():
    transport = ScriptedTransport(responses=fx.environment_responses())
    client = fx.client(transport)
    environment = client.verify_environment()

    rendered = str(environment.to_record()) + " ".join(client.request_log())

    assert fx.FAKE_API_KEY not in rendered
    assert fx.FAKE_API_SECRET not in rendered
    assert "<redacted>" in " ".join(client.request_log())


# --- order placement -------------------------------------------------------

def test_place_order_sends_a_signed_limited_order():
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    order = client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert order.client_order_id == CLIENT_ORDER_ID
    assert order.state.value == "accepted"
    assert order.remaining_quantity == Decimal("0.00100000")
    request = transport.requests[-1]
    assert "signature=" in request.query
    assert "timestamp=" in request.query
    assert "recvWindow=5000" in request.query
    assert "newClientOrderId=" in request.query
    assert fx.FAKE_API_SECRET not in request.safe_repr()


def test_duplicate_client_order_id_never_places_a_second_order():
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),  # the idempotent status lookup
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    request = fx.limit_order_request(CLIENT_ORDER_ID)

    first = client.place_order(request)
    second = client.place_order(request)

    assert first.deduplicated is False
    assert second.deduplicated is True
    posts = [item for item in transport.requests if item.method == "POST"]
    assert len(posts) == 1, "the duplicate submission must not reach the venue"


def test_venue_side_duplicate_is_adopted_instead_of_failing():
    """After a restart the local dedup cache is empty; the venue's own dedup must be adopted."""
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.rejected(code=-2010, msg="Duplicate order sent."),
            fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=99),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    order = client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert order.deduplicated is True
    assert order.venue_order_id == "99"
    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_partial_fill_is_reported_with_exact_decimals():
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(
                CLIENT_ORDER_ID,
                status="PARTIALLY_FILLED",
                executed_qty="0.00040000",
                cummulative_quote="10.80000000",
                fills=(fx.fill(qty="0.00040000"),),
            ),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    order = client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert order.state.value == "partially_filled"
    assert order.executed_quantity == Decimal("0.00040000")
    assert order.remaining_quantity == Decimal("0.00060000")
    assert order.fills[0].price == Decimal("27000.00")
    assert order.fills[0].commission_asset == "BNB"


def test_cancel_order_returns_the_canceled_state_and_is_idempotent():
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
            fx.order_response(CLIENT_ORDER_ID, status="CANCELED"),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    canceled = client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)
    again = client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)

    assert canceled.state.value == "canceled"
    assert again.state.value == "canceled"
    deletes = [item for item in transport.requests if item.method == "DELETE"]
    assert len(deletes) == 1, "canceling an already-terminal order must not hit the venue again"


def test_cancel_all_open_orders_cancels_every_open_order():
    transport = ScriptedTransport(
        responses=[
            fx.open_orders_response("ct-a", "ct-b"),
            fx.order_response("ct-a", status="CANCELED"),
            fx.order_response("ct-b", status="CANCELED"),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    results = client.cancel_all_open_orders(fx.SYMBOL)

    assert [order.client_order_id for order in results] == ["ct-a", "ct-b"]
    assert all(order.state.value == "canceled" for order in results)


def test_order_validation_never_rounds():
    transport = ScriptedTransport(responses=[fx.exchange_info_response(step="0.001")])
    client = fx.client(transport)

    with pytest.raises(SymbolError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID, quantity="0.00150000"))


# --- ambiguity / retries / rate limits -------------------------------------

def test_write_timeout_is_ambiguous_and_never_retried():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), TransportTimeout("timed out")]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(AmbiguousOrderError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    posts = [item for item in transport.requests if item.method == "POST"]
    assert len(posts) == 1


def test_write_server_error_is_ambiguous_and_never_retried():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.server_error()]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(AmbiguousOrderError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_rejected_order_raises_a_deterministic_error_without_ambiguity():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.rejected(code=-2010)]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(RejectedError) as error:
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert "-2010" in str(error.value)


def test_reads_retry_with_backoff_on_server_errors():
    transport = ScriptedTransport(
        responses=[fx.server_error(), fx.server_error(), fx.price_response()]
    )
    client = fx.client(transport)

    price = client.price()

    assert price == Decimal("27000.00")
    assert len(transport.requests) == 3


def test_reads_retry_after_rate_limiting_and_honour_retry_after():
    slept: list[float] = []
    transport = ScriptedTransport(responses=[fx.rate_limited("2"), fx.price_response()])
    client = fx.client(transport)
    assert client.budget is not None
    client.budget.sleep = slept.append
    client.budget.monotonic = lambda: 0.0

    price = client.price()

    assert price == Decimal("27000.00")
    assert client.budget.violations == 1


def test_writes_are_not_retried_after_rate_limiting():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.rate_limited("1")]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(RateLimitError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_ip_ban_stops_the_run_immediately():
    transport = ScriptedTransport(responses=[fx.banned("120"), fx.price_response()])
    client = fx.client(transport)

    with pytest.raises(RateLimitBanError):
        client.price()

    assert len(transport.requests) == 1


# --- clock -----------------------------------------------------------------

def test_clock_must_be_synchronized_before_signing():
    transport = ScriptedTransport(responses=[fx.exchange_info_response()])
    client = fx.client(transport)

    with pytest.raises(ClockDriftError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))


def test_clock_synchronization_compensates_the_server_offset():
    transport = ScriptedTransport(responses=[fx.time_response(fx.SERVER_TIME_MS + 1_500)])
    client = fx.client(transport)

    offset = client.synchronize_clock()

    assert offset == 1_500
    assert client.clock.timestamp_ms() == fx.SERVER_TIME_MS + 1_500


def test_excessive_clock_drift_fails_closed():
    drift_clock = fx.clock(offset_ms=3_000, recv_window_ms=1_000)
    drift_clock._synchronized = True
    transport = ScriptedTransport(responses=[fx.exchange_info_response()])
    client = fx.client(transport, clock_instance=drift_clock)

    with pytest.raises(ClockDriftError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))


def test_a_large_but_compensated_offset_is_accepted():
    """A 1.4s skew is harmless: the timestamp sent already includes the measured offset."""
    skewed = fx.clock(offset_ms=1_400, recv_window_ms=5_000)
    skewed._synchronized = True
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.order_response(CLIENT_ORDER_ID, status="NEW")]
    )
    client = fx.client(transport, clock_instance=skewed)

    order = client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert order.client_order_id == CLIENT_ORDER_ID
    assert f"timestamp={fx.SERVER_TIME_MS + 1_400}" in transport.requests[-1].query


def test_an_implausible_offset_is_treated_as_a_broken_time_source():
    broken = fx.clock(offset_ms=900_000, recv_window_ms=60_000)
    broken._synchronized = True
    transport = ScriptedTransport(responses=[fx.exchange_info_response()])
    client = fx.client(transport, clock_instance=broken)

    with pytest.raises(ClockDriftError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))


def test_timestamp_rejection_resynchronizes_reads_once():
    transport = ScriptedTransport(
        responses=[
            fx.rejected(code=-1021, msg="Timestamp for this request is outside of the recvWindow"),
            fx.time_response(),
            fx.price_response(),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    price = client.price()

    assert price == Decimal("27000.00")
    assert [item.url.split("binance.vision")[-1].split("?")[0] for item in transport.requests] == [
        "/api/v3/ticker/price",
        "/api/v3/time",
        "/api/v3/ticker/price",
    ]


def test_timestamp_rejection_on_a_write_fails_closed_without_retry():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.rejected(code=-1021, msg="outside recvWindow")]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(ClockDriftError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_recv_window_is_clamped_to_the_documented_maximum():
    from crypto_trader.execution.binance_testnet import MAX_RECV_WINDOW_MS

    with pytest.raises(Exception):
        fx.clock(recv_window_ms=MAX_RECV_WINDOW_MS + 1)


# --- reconnect -------------------------------------------------------------

def test_reconnect_reverifies_endpoint_clock_and_permissions():
    # Instrument rules are cached, so a re-verification refreshes the clock and the account only.
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.time_response(), fx.account_response()]
    )
    client = fx.client(transport)
    client.verify_environment()

    environment = client.reconnect()

    assert environment.venue_label == "binance-spot-testnet"
    assert fx.request_paths(transport).count("/api/v3/time") == 2
    assert fx.request_paths(transport).count("/api/v3/account") == 2
    assert fx.request_paths(transport).count("/api/v3/exchangeInfo") == 1
