"""Phase 11: Spot Testnet client behaviour under realistic failure modes."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    ClockDriftError,
    RateLimitAdmissionDeferredError,
    RateLimitBanError,
    RateLimitBudget,
    RateLimitError,
    TokenBucket,
    WriteIpBannedOutcomeUnknownError,
    WriteRateLimitedOutcomeUnknownError,
    ScriptedTransport,
    UnsupportedOrderTypeError,
    WithdrawalPolicy,
    TransportTimeout,
)
from crypto_trader.execution.binance_testnet import TestnetOrderRequest as OrderRequest
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

    order = fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

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

    first = fx.place_order(client, request)
    second = fx.place_order(client, request)

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

    order = fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

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

    order = fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

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
            fx.cancel_response(CLIENT_ORDER_ID),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    canceled = client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)
    again = client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)

    assert canceled.state.value == "canceled"
    assert canceled.client_order_id == CLIENT_ORDER_ID
    assert canceled.venue_order_id == "42"
    assert client._orders[CLIENT_ORDER_ID] is canceled
    assert "venue-generated-cancel-id" not in client._orders
    assert again.state.value == "canceled"
    deletes = [item for item in transport.requests if item.method == "DELETE"]
    assert len(deletes) == 1, "canceling an already-terminal order must not hit the venue again"


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("field,value", [
    ("origClientOrderId", None), ("origClientOrderId", "other-order"),
    ("origClientOrderId", 123), ("origClientOrderId", ""),
    ("clientOrderId", None), ("clientOrderId", 123), ("clientOrderId", " "),
    ("orderId", None), ("orderId", True), ("orderId", "42"),
    ("orderId", 0), ("orderId", -1),
    ("symbol", "ETHUSDT"), ("symbol", None),
    ("status", "NEW"), ("status", "FILLED"), ("status", None),
    ("origQty", None), ("executedQty", "NaN"), ("executedQty", "2"),
])
def test_cancel_rejects_malformed_or_contradictory_response_without_retry(cached, field, value):
    body = dict(fx.cancel_response(CLIENT_ORDER_ID).body)
    if value is None:
        body.pop(field)
    else:
        body[field] = value
    responses = [fx.order_response(CLIENT_ORDER_ID)] if cached else []
    transport = ScriptedTransport(responses=[*responses, fx.response(200, body)])
    client = fx.client(transport, retries=3)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    if cached:
        client.order_status(fx.SYMBOL, CLIENT_ORDER_ID)
    before = dict(client._orders)
    with pytest.raises(AmbiguousOrderError):
        client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)
    assert client._orders == before
    assert len([request for request in transport.requests if request.method == "DELETE"]) == 1


@pytest.mark.parametrize("body", [None, [], "invalid"])
def test_cancel_rejects_non_object_payload_without_retry(body):
    transport = ScriptedTransport(responses=[fx.response(200, body)])
    client = fx.client(transport, retries=3)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(AmbiguousOrderError):
        client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)
    assert len(transport.requests) == 1
    assert client._orders == {}


def test_cancel_rejects_a_different_cached_venue_identity():
    transport = ScriptedTransport(responses=[
        fx.order_response(CLIENT_ORDER_ID), fx.cancel_response(CLIENT_ORDER_ID, order_id=99),
    ])
    client = fx.client(transport, retries=3)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    original = client.order_status(fx.SYMBOL, CLIENT_ORDER_ID)
    with pytest.raises(AmbiguousOrderError):
        client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)
    assert client._orders == {CLIENT_ORDER_ID: original}
    assert len([request for request in transport.requests if request.method == "DELETE"]) == 1


def test_cancel_without_a_local_cache_preserves_original_identity():
    transport = ScriptedTransport(responses=[fx.cancel_response(CLIENT_ORDER_ID)])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    canceled = client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID)
    assert canceled.client_order_id == CLIENT_ORDER_ID
    assert canceled.venue_order_id == "42"
    assert client.cancel_order(fx.SYMBOL, CLIENT_ORDER_ID) is canceled
    assert len(transport.requests) == 1


def test_cancel_all_open_orders_cancels_every_open_order():
    transport = ScriptedTransport(
        responses=[
            fx.open_orders_response("ct-a", "ct-b"),
            fx.cancel_response("ct-a", order_id=100),
            fx.cancel_response("ct-b", order_id=101),
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
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID, quantity="0.00150000"))


def test_valid_limit_fok_prepares_through_shared_filters():
    transport = ScriptedTransport(responses=[fx.exchange_info_response()])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    request = OrderRequest(
        client_order_id=CLIENT_ORDER_ID,
        symbol=fx.SYMBOL,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.00100000"),
        price=Decimal("27000.00"),
        time_in_force="FOK",
    )

    prepared = client.prepare_order(request)

    assert prepared.requires_write is True
    assert "timeInForce=FOK" in prepared.request.query
    assert not [item for item in transport.requests if item.method == "POST"]


def test_market_venue_write_is_rejected_before_any_request():
    client = fx.client(ScriptedTransport(responses=[]))
    request = OrderRequest(
        client_order_id=CLIENT_ORDER_ID,
        symbol=fx.SYMBOL,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.00100000"),
    )

    with pytest.raises(UnsupportedOrderTypeError):
        client.prepare_order(request)

    assert client.transport.requests == []


def test_verify_environment_fails_closed_on_incomplete_exchange_info():
    malformed = fx.exchange_info_response()
    malformed.body["symbols"][0]["filters"][1].pop("maxQty")
    transport = ScriptedTransport(
        responses=[fx.time_response(), malformed, fx.account_response()]
    )
    client = fx.client(transport)

    with pytest.raises(SymbolError):
        client.verify_environment()

    assert fx.request_paths(transport) == ["/api/v3/time", "/api/v3/exchangeInfo"]


# --- ambiguity / retries / rate limits -------------------------------------

def test_write_timeout_is_ambiguous_and_never_retried():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), TransportTimeout("timed out")]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(AmbiguousOrderError):
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    posts = [item for item in transport.requests if item.method == "POST"]
    assert len(posts) == 1


def test_write_server_error_is_ambiguous_and_never_retried():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.server_error()]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(AmbiguousOrderError):
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_rejected_order_raises_a_deterministic_error_without_ambiguity():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.rejected(code=-2010)]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(RejectedError) as error:
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    assert "-2010" in str(error.value)


def test_reads_retry_with_backoff_on_server_errors():
    transport = ScriptedTransport(
        responses=[fx.server_error(), fx.server_error(), fx.price_response()]
    )
    client = fx.client(transport)

    price = client.price()

    assert price == Decimal("27000.00")
    assert len(transport.requests) == 3


def test_admit_now_on_exhausted_bucket_never_calls_sleep():
    bucket_sleeps: list[float] = []
    budget_sleeps: list[float] = []
    bucket = TokenBucket(
        capacity=1,
        refill_per_second=1.0,
        monotonic=lambda: 0.0,
        sleep=bucket_sleeps.append,
    )
    budget = RateLimitBudget(
        bucket=bucket,
        sleep=budget_sleeps.append,
        monotonic=lambda: 0.0,
    )

    budget.admit_now(1)
    with pytest.raises(RateLimitAdmissionDeferredError):
        budget.admit_now(1)

    assert bucket_sleeps == []
    assert budget_sleeps == []


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


def test_post_send_429_is_ambiguous_never_retryable_and_never_retried():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.rate_limited("1")]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(WriteRateLimitedOutcomeUnknownError) as caught:
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    assert isinstance(caught.value, AmbiguousOrderError)
    assert not isinstance(caught.value, RateLimitError)
    assert caught.value.reason_code == "venue_rate_limited_after_send"
    assert caught.value.http_status == 429
    assert caught.value.retry_after_seconds == 1.0
    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_post_send_418_is_ambiguous_but_leaves_the_budget_banned():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.banned("120")]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(WriteIpBannedOutcomeUnknownError) as caught:
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    assert isinstance(caught.value, AmbiguousOrderError)
    assert not isinstance(caught.value, RateLimitBanError)
    assert caught.value.reason_code == "venue_ip_banned_after_send"
    assert caught.value.http_status == 418
    assert client.budget is not None and client.budget.banned is True
    request_count = len(transport.requests)
    with pytest.raises(RateLimitBanError):
        client.price()
    assert len(transport.requests) == request_count


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
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))


def test_a_large_but_compensated_offset_is_accepted():
    """A 1.4s skew is harmless: the timestamp sent already includes the measured offset."""
    skewed = fx.clock(offset_ms=1_400, recv_window_ms=5_000)
    skewed._synchronized = True
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.order_response(CLIENT_ORDER_ID, status="NEW")]
    )
    client = fx.client(transport, clock_instance=skewed)

    order = fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    assert order.client_order_id == CLIENT_ORDER_ID
    assert f"timestamp={fx.SERVER_TIME_MS + 1_400}" in transport.requests[-1].query


def test_an_implausible_offset_is_treated_as_a_broken_time_source():
    broken = fx.clock(offset_ms=900_000, recv_window_ms=60_000)
    broken._synchronized = True
    transport = ScriptedTransport(responses=[fx.exchange_info_response()])
    client = fx.client(transport, clock_instance=broken)

    with pytest.raises(ClockDriftError):
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))


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
        fx.place_order(client, fx.limit_order_request(CLIENT_ORDER_ID))

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_recv_window_is_clamped_to_the_documented_maximum():
    from crypto_trader.execution.binance_testnet import MAX_RECV_WINDOW_MS

    with pytest.raises(Exception):
        fx.clock(recv_window_ms=MAX_RECV_WINDOW_MS + 1)


# --- reconnect -------------------------------------------------------------

def test_reconnect_reverifies_endpoint_clock_permissions_and_symbol_rules():
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.time_response(),
            fx.exchange_info_response(),
            fx.account_response(),
        ]
    )
    client = fx.client(transport)
    client.verify_environment()

    environment = client.reconnect()

    assert environment.venue_label == "binance-spot-testnet"
    assert fx.request_paths(transport).count("/api/v3/time") == 2
    assert fx.request_paths(transport).count("/api/v3/account") == 2
    assert fx.request_paths(transport).count("/api/v3/exchangeInfo") == 2


def test_prepare_order_reuses_fresh_symbol_rules_without_an_extra_read():
    now = [0.0]
    transport = ScriptedTransport(responses=[fx.exchange_info_response()])
    client = fx.client(transport, rules_monotonic=lambda: now[0])
    client.clock.synchronize(fx.SERVER_TIME_MS)

    client.symbol_rules()
    client.prepare_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert fx.request_paths(transport).count("/api/v3/exchangeInfo") == 1


def test_prepare_order_refreshes_expired_symbol_rules_before_any_write():
    now = [0.0]
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(min_notional="5"),
            fx.exchange_info_response(min_notional="999999"),
        ]
    )
    client = fx.client(transport, rules_ttl_seconds=60.0, rules_monotonic=lambda: now[0])
    client.clock.synchronize(fx.SERVER_TIME_MS)

    assert client.symbol_rules().min_notional == Decimal("5")
    now[0] = 60.0
    with pytest.raises(SymbolError):
        client.prepare_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert fx.request_paths(transport).count("/api/v3/exchangeInfo") == 2
    assert [item for item in transport.requests if item.method == "POST"] == []


def test_symbol_rule_cache_ttl_boundary_and_backwards_clock_both_refresh():
    now = [10.0]
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(min_notional="5"),
            fx.exchange_info_response(min_notional="6"),
            fx.exchange_info_response(min_notional="7"),
        ]
    )
    client = fx.client(transport, rules_ttl_seconds=60.0, rules_monotonic=lambda: now[0])

    assert client.symbol_rules().min_notional == Decimal("5")
    now[0] = 69.999
    assert client.symbol_rules().min_notional == Decimal("5")
    now[0] = 70.0
    assert client.symbol_rules().min_notional == Decimal("6")
    now[0] = 69.0
    assert client.symbol_rules().min_notional == Decimal("7")
    assert fx.request_paths(transport).count("/api/v3/exchangeInfo") == 3


def test_failed_forced_refresh_invalidates_previous_symbol_rule_cache():
    now = [0.0]
    bad_one = fx.exchange_info_response()
    bad_one.body["symbols"][0]["status"] = "BREAK"
    bad_two = fx.exchange_info_response()
    bad_two.body["symbols"][0]["status"] = "BREAK"
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.time_response(), bad_one, bad_two]
    )
    client = fx.client(transport, rules_monotonic=lambda: now[0])

    client.verify_environment()
    with pytest.raises(SymbolError):
        client.reconnect()
    with pytest.raises(SymbolError):
        client.prepare_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert fx.request_paths(transport).count("/api/v3/exchangeInfo") == 3
    assert [item for item in transport.requests if item.method == "POST"] == []


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_symbol_rule_ttl_is_rejected(bad):
    from crypto_trader.execution.binance_testnet.errors import TestnetEndpointError

    with pytest.raises(TestnetEndpointError):
        fx.client(ScriptedTransport([]), rules_ttl_seconds=bad)


# G6: strict startup reads preserve the raw evidence boundary and never adopt a cache.
@pytest.mark.parametrize("body", [None, {}, "", "[]", 0, False, {"orders": []}])
def test_startup_open_orders_rejects_non_list_empty_bodies(body):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    client = fx.client(ScriptedTransport([fx.response(200, body)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_open_orders(fx.SYMBOL)
    assert client._orders == {}


@pytest.mark.parametrize("field,value", [
    ("symbol", "ETHUSDT"), ("clientOrderId", "other"), ("clientOrderId", None),
    ("orderId", None), ("orderId", True), ("status", None), ("status", "UNKNOWN"),
    ("origQty", None), ("executedQty", None), ("origQty", "NaN"),
    ("executedQty", "Infinity"), ("origQty", "-1"), ("executedQty", "-1"),
    ("executedQty", "2"), ("executedQty", "0.0001"),
])
def test_startup_status_rejects_invalid_identity_status_and_quantity(field, value):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    body = dict(fx.order_response(CLIENT_ORDER_ID).body)
    body[field] = value
    client = fx.client(ScriptedTransport([fx.response(200, body)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_order_status(fx.SYMBOL, CLIENT_ORDER_ID)
    assert client._orders == {}


@pytest.mark.parametrize("status,original,executed", [
    ("FILLED", "1", "0.9"), ("PARTIALLY_FILLED", "1", "0"),
    ("PARTIALLY_FILLED", "1", "1"), ("NEW", "0", "0"),
])
def test_startup_status_rejects_inconsistent_quantities(status, original, executed):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    client = fx.client(ScriptedTransport([fx.order_response(
        CLIENT_ORDER_ID, status=status, orig_qty=original, executed_qty=executed,
    )]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_order_status(fx.SYMBOL, CLIENT_ORDER_ID)


@pytest.mark.parametrize("http,code,absent", [
    (400, -2013, True), (401, -2013, False), (403, -2013, False),
    (400, "-2013", False), (400, -2013.0, False), (400, -2010, False),
    (302, -2013, False), (400, True, False),
])
def test_startup_exact_order_absence_is_typed_only_for_the_approved_read(http, code, absent):
    from crypto_trader.execution.binance_testnet.errors import OrderNotFoundError
    transport = ScriptedTransport([fx.response(http, {"code": code, "msg": "missing"})])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(RejectedError) as caught:
        client.startup_order_status(fx.SYMBOL, CLIENT_ORDER_ID)
    assert isinstance(caught.value, OrderNotFoundError) is absent
    assert not transport.requests[0].is_write
    assert client._orders == {}


def test_startup_valid_reads_are_signed_and_leave_client_cache_untouched():
    transport = ScriptedTransport([
        fx.open_orders_response(CLIENT_ORDER_ID),
        fx.order_response("another", status="FILLED", executed_qty="0.00100000"),
    ])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    assert client.startup_open_orders(fx.SYMBOL)[0].client_order_id == CLIENT_ORDER_ID
    assert client.startup_order_status(fx.SYMBOL, "another").status == "FILLED"
    assert client._orders == {}
    assert all(r.method == "GET" and "signature=" in r.query for r in transport.requests)


def test_startup_my_trades_is_signed_strict_and_cache_free():
    transport = ScriptedTransport([fx.my_trades_response(
        fx.trade(trade_id=7), fx.trade(trade_id=8, qty="0.00025000", quote_qty="6.75000000"),
    )])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    trades = client.startup_my_trades(fx.SYMBOL, "42")

    assert [trade.trade_id for trade in trades] == ["7", "8"]
    assert trades[0].price == Decimal("27000.00")
    assert trades[0].commission_asset == "BTC"
    assert trades[0].is_buyer is True and trades[0].is_maker is False
    assert client._orders == {}
    request = transport.requests[0]
    assert request.method == "GET" and "/api/v3/myTrades" in request.url
    assert "orderId=42" in request.query and "limit=1000" in request.query
    assert "signature=" in request.query


@pytest.mark.parametrize("field,value", [
    ("symbol", "ETHUSDT"), ("orderId", 99), ("id", True), ("id", 0), ("id", -1),
    ("price", "0"), ("qty", "0"), ("quoteQty", "NaN"), ("commission", "-1"),
    ("commissionAsset", ""), ("time", 0), ("isBuyer", 1), ("isMaker", "false"),
])
def test_startup_my_trades_rejects_malformed_trade_fields(field, value):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    item = fx.trade()
    item[field] = value
    client = fx.client(ScriptedTransport([fx.my_trades_response(item)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_my_trades(fx.SYMBOL, "42")


@pytest.mark.parametrize("ids", [(8, 7), (7, 7)])
def test_startup_my_trades_rejects_non_monotonic_or_duplicate_trade_ids(ids):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    body = fx.my_trades_response(*(fx.trade(trade_id=value) for value in ids))
    client = fx.client(ScriptedTransport([body]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_my_trades(fx.SYMBOL, "42")


def test_startup_my_trades_refuses_a_potentially_truncated_max_page():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    page = [fx.trade(trade_id=index) for index in range(1000)]
    client = fx.client(ScriptedTransport([fx.response(200, page)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError, match="completeness is not provable"):
        client.startup_my_trades(fx.SYMBOL, "42")


@pytest.mark.parametrize("venue_order_id", ["0", "abc", " 42", "+42", "042", True, 42])
def test_startup_my_trades_rejects_noncanonical_requested_order_id(venue_order_id):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    client = fx.client(ScriptedTransport([]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_my_trades(fx.SYMBOL, venue_order_id)


def test_startup_my_trades_rejects_non_list_body_and_accepts_empty_list():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    bad = fx.client(ScriptedTransport([fx.response(200, {})]))
    bad.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        bad.startup_my_trades(fx.SYMBOL, "42")
    empty = fx.client(ScriptedTransport([fx.my_trades_response()]))
    empty.clock.synchronize(fx.SERVER_TIME_MS)
    assert empty.startup_my_trades(fx.SYMBOL, "42") == ()


def test_startup_my_trades_accepts_999_row_complete_page():
    page = [fx.trade(trade_id=index) for index in range(1, 1000)]
    client = fx.client(ScriptedTransport([fx.response(200, page)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    trades = client.startup_my_trades(fx.SYMBOL, "42")
    assert len(trades) == 999 and trades[-1].trade_id == "999"


def test_startup_my_trades_wraps_timestamp_overflow_as_typed_error():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    item = fx.trade(time_ms=10**30)
    client = fx.client(ScriptedTransport([fx.my_trades_response(item)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError, match="outside the supported range"):
        client.startup_my_trades(fx.SYMBOL, "42")


def test_startup_my_trades_rejects_binary_float_economics():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    item = fx.trade()
    item["price"] = 27000.0
    client = fx.client(ScriptedTransport([fx.my_trades_response(item)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_my_trades(fx.SYMBOL, "42")


def test_startup_account_snapshot_requires_explicit_base_and_quote_evidence():
    transport = ScriptedTransport([fx.account_response()])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    snapshot = client.startup_account_snapshot("BTC", "USDT")

    assert snapshot.venue_account_id == "123456789"
    assert snapshot.venue_update_time_ms == fx.SERVER_TIME_MS
    assert [item.asset for item in snapshot.balances] == ["BTC", "USDT"]
    assert snapshot.balance("BTC").total == Decimal("1.0")
    assert snapshot.balance("USDT").total == Decimal("100000")
    request = transport.requests[0]
    assert request.method == "GET" and "/api/v3/account" in request.url
    assert "signature=" in request.query


def test_startup_account_snapshot_accepts_explicit_zero_balance():
    response = fx.account_response(free="0")
    response.body["balances"][0]["locked"] = "0"
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    snapshot = client.startup_account_snapshot("USDT")
    assert snapshot.balance("USDT").total == Decimal("0")


def test_startup_account_snapshot_rejects_missing_required_asset():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body["balances"] = [response.body["balances"][0]]
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError, match="missing required asset"):
        client.startup_account_snapshot("BTC", "USDT")


@pytest.mark.parametrize("field,value", [("uid", None), ("uid", 0), ("uid", True), ("updateTime", None), ("updateTime", 0), ("updateTime", True)])
def test_startup_account_snapshot_rejects_missing_or_malformed_identity(field, value):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body[field] = value
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot("BTC", "USDT")


@pytest.mark.parametrize("field,value", [("free", "abc"), ("free", "NaN"), ("free", "-1"), ("locked", "Infinity"), ("locked", -1)])
def test_startup_account_snapshot_rejects_malformed_balances(field, value):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body["balances"][0][field] = value
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot("USDT")


def test_startup_account_snapshot_rejects_duplicate_required_asset_entries():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body["balances"].append(dict(response.body["balances"][0]))
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError, match="duplicate"):
        client.startup_account_snapshot("USDT")



def test_startup_account_snapshot_rejects_malformed_foreign_balance():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body["balances"].append({"asset": "BNB", "free": "NaN", "locked": "0"})
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(BinanceTestnetError, match="BNB free"):
        client.startup_account_snapshot("BTC", "USDT")


def test_startup_account_snapshot_rejects_duplicate_foreign_asset_entries():
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body["balances"].extend([
        {"asset": "BNB", "free": "0", "locked": "0"},
        {"asset": "BNB", "free": "1", "locked": "0"},
    ])
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(BinanceTestnetError, match="duplicate"):
        client.startup_account_snapshot("BTC", "USDT")


def test_startup_account_snapshot_strictly_validates_but_does_not_value_foreign_assets():
    response = fx.account_response()
    response.body["balances"].append({"asset": "BNB", "free": "2.5", "locked": "0.1"})
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)

    snapshot = client.startup_account_snapshot("BTC", "USDT")

    assert [balance.asset for balance in snapshot.balances] == ["BTC", "USDT"]


@pytest.mark.parametrize("assets", [(), ("btc",), (" BTC",), ("BTC", "BTC")])
def test_startup_account_snapshot_rejects_noncanonical_asset_requests(assets):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    client = fx.client(ScriptedTransport([]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot(*assets)


def test_startup_account_snapshot_pins_zero_balance_semantics_and_weight():
    transport = ScriptedTransport([fx.account_response()])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    client.startup_account_snapshot("BTC", "USDT")
    request = transport.requests[0]
    assert request.weight == 20
    assert "omitZeroBalances=false" in request.query


@pytest.mark.parametrize("body", [None, {"uid": 1, "updateTime": 1, "balances": {}}, {"uid": 1, "updateTime": 1, "balances": ["bad"]}])
def test_startup_account_snapshot_rejects_malformed_containers(body):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    client = fx.client(ScriptedTransport([fx.response(200, body)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot("USDT")


@pytest.mark.parametrize("missing", ["free", "locked"])
def test_startup_account_snapshot_rejects_missing_balance_fields(missing):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response()
    response.body["balances"][0].pop(missing)
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot("USDT")


@pytest.mark.parametrize("raw", [" 1 ", "1_000", "1e5", "-0", "+1", "١"])
def test_startup_account_snapshot_rejects_noncanonical_decimal_strings(raw):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    response = fx.account_response(free=raw)
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot("USDT")


@pytest.mark.parametrize("asset", ["BTC/", "BT C", "BTC-USD", "BṪC"])
def test_startup_account_snapshot_rejects_non_alphanumeric_asset_codes(asset):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError
    client = fx.client(ScriptedTransport([]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(BinanceTestnetError):
        client.startup_account_snapshot(asset)


def test_legacy_account_snapshot_does_not_require_new_account_identity_fields():
    response = fx.account_response()
    response.body.pop("uid")
    response.body.pop("updateTime")
    client = fx.client(ScriptedTransport([response]))
    client.clock.synchronize(fx.SERVER_TIME_MS)
    legacy = client.account_snapshot(asset="USDT")
    assert legacy.free_balance == Decimal("100000")


def test_strict_account_models_reject_invalid_direct_construction():
    from datetime import datetime, timezone
    from crypto_trader.execution.binance_testnet import VenueAccountSnapshot, VenueAssetBalance
    with pytest.raises(ValueError):
        VenueAssetBalance(asset="BTC", free=Decimal("-1"), locked=Decimal("0"))
    good = VenueAssetBalance(asset="BTC", free=Decimal("1"), locked=Decimal("0"))
    with pytest.raises(ValueError):
        VenueAccountSnapshot(
            venue_account_id="123", venue_update_time_ms=1,
            fetched_at=datetime(2026, 1, 1), balances=(good,)
        )
    with pytest.raises(ValueError):
        VenueAccountSnapshot(
            venue_account_id="123", venue_update_time_ms=1,
            fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), balances=(good, good)
        )
    with pytest.raises(ValueError):
        VenueAccountSnapshot(
            venue_account_id="123", venue_update_time_ms=1,
            fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), balances=[good]
        )


def test_fill_event_identity_is_single_canonical_pinned_vector():
    from uuid import UUID
    from crypto_trader.execution.binance_testnet.translate import (
        canonical_fill_event_id,
        deterministic_fill_event_uuid,
        venue_trade_event_id,
    )

    expected_fill = UUID("517535ff-b400-53a4-9733-5dff33160d69")
    expected_trade = UUID("f6eb7f37-045d-5f90-b67e-5f7bef78150f")

    assert canonical_fill_event_id("ct-test", "7") == expected_fill
    assert deterministic_fill_event_uuid("ct-test", "7") == expected_fill
    assert venue_trade_event_id("BTCUSDT", "42", "7") == expected_trade


def test_startup_mark_evidence_is_strict_timestamped_and_cache_free():
    from crypto_trader.execution.binance_testnet import MarkEvidence

    transport = ScriptedTransport([fx.price_response("27000.00"), fx.price_response("27001.00")])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    first = client.startup_mark_evidence(fx.SYMBOL)
    second = client.startup_mark_evidence(fx.SYMBOL)

    assert isinstance(first, MarkEvidence)
    assert first.symbol == fx.SYMBOL
    assert first.price == Decimal("27000.00")
    assert second.price == Decimal("27001.00")
    assert first.observed_at == datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc)
    assert first.reference == f"binance.ticker.price:{fx.SYMBOL}"
    assert len(transport.requests) == 2
    assert all(request.method == "GET" for request in transport.requests)
    assert all("/api/v3/ticker/price" in request.url for request in transport.requests)


@pytest.mark.parametrize("body", [
    None,
    {"symbol": "ETHUSDT", "price": "27000.00"},
    {"symbol": fx.SYMBOL},
    {"symbol": fx.SYMBOL, "price": 27000},
    {"symbol": fx.SYMBOL, "price": " 27000 "},
    {"symbol": fx.SYMBOL, "price": "1e4"},
    {"symbol": fx.SYMBOL, "price": "NaN"},
    {"symbol": fx.SYMBOL, "price": "0"},
])
def test_startup_mark_evidence_rejects_malformed_or_mismatched_payload(body):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError

    client = fx.client(ScriptedTransport([fx.response(200, body)]))
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(BinanceTestnetError):
        client.startup_mark_evidence(fx.SYMBOL)


@pytest.mark.parametrize("symbol", ["btcUSDT", " BTCUSDT", "BTC-USDT", "BT CUSDT", ""])
def test_startup_mark_evidence_rejects_noncanonical_symbol_without_network(symbol):
    from crypto_trader.execution.binance_testnet.errors import BinanceTestnetError

    transport = ScriptedTransport([])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)

    with pytest.raises(BinanceTestnetError):
        client.startup_mark_evidence(symbol)

    assert transport.requests == []


def test_venue_account_snapshot_rejects_update_time_after_fetch_time():
    from crypto_trader.execution.binance_testnet import VenueAccountSnapshot, VenueAssetBalance

    with pytest.raises(ValueError, match="updateTime cannot be later"):
        VenueAccountSnapshot(
            venue_account_id="123",
            venue_update_time_ms=fx.SERVER_TIME_MS + 1,
            fetched_at=datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc),
            balances=(
                VenueAssetBalance(asset="BTC", free=Decimal("1"), locked=Decimal("0")),
            ),
        )


def test_cached_adoption_rejects_economic_mismatch_without_cache_mutation():
    bad = dict(fx.order_response(CLIENT_ORDER_ID, status="NEW").body)
    bad["side"] = "SELL"
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
            fx.response(200, bad),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    request = fx.limit_order_request(CLIENT_ORDER_ID)

    fx.place_order(client, request)
    before = client.known_orders()

    with pytest.raises(AmbiguousOrderError):
        client.prepare_order(request)

    assert client.known_orders() == before
    assert len([item for item in transport.requests if item.method == "POST"]) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: {k: v for k, v in body.items() if k != "status"}, id="missing_status"),
        pytest.param(lambda body: {k: v for k, v in body.items() if k != "type"}, id="missing_type"),
        pytest.param(lambda body: {k: v for k, v in body.items() if k != "timeInForce"}, id="missing_tif"),
        pytest.param(lambda body: {**body, "origQty": 0.001}, id="float_orig_qty"),
    ],
)
def test_cached_adoption_rejects_missing_or_noncanonical_required_fields(mutate):
    reread = dict(fx.order_response(CLIENT_ORDER_ID, status="NEW").body)
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
            fx.response(200, mutate(reread)),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    request = fx.limit_order_request(CLIENT_ORDER_ID)
    fx.place_order(client, request)
    before = client.known_orders()

    with pytest.raises(AmbiguousOrderError):
        client.prepare_order(request)

    assert client.known_orders() == before
    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_cached_adoption_malformed_side_is_typed_ambiguous_and_preserves_cache():
    bad = dict(fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=42).body)
    bad["side"] = []
    transport = ScriptedTransport(
        responses=[
            fx.exchange_info_response(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=42),
            fx.response(200, bad),
        ]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    request = fx.limit_order_request(CLIENT_ORDER_ID)

    fx.place_order(client, request)
    before = client.known_orders()

    with pytest.raises(AmbiguousOrderError):
        client.prepare_order(request)

    assert client.known_orders() == before
    assert len([item for item in transport.requests if item.method == "POST"]) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: {**body, "side": []}, id="side_list"),
        pytest.param(lambda body: {**body, "side": {}}, id="side_mapping"),
        pytest.param(lambda body: {**body, "fills": [1]}, id="fills_scalar"),
        pytest.param(lambda body: {**body, "transactTime": 10**30}, id="timestamp_overflow"),
        pytest.param(lambda body: {**body, "updateTime": -(10**18), "transactTime": None}, id="timestamp_oserror"),
    ],
)
def test_cached_adoption_malformed_payloads_are_typed_ambiguous(mutate):
    good = fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=42)
    bad = mutate(dict(good.body))
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), good, fx.response(200, bad)]
    )
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    request = fx.limit_order_request(CLIENT_ORDER_ID)
    fx.place_order(client, request)
    before = client.known_orders()

    with pytest.raises(AmbiguousOrderError):
        client.prepare_order(request)

    assert client.known_orders() == before
    assert len([item for item in transport.requests if item.method == "POST"]) == 1
