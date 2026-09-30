"""Deterministic fixtures for the Phase 11 Binance Spot Testnet tests.

Not a test module. Every value here is synthetic and contains no credential material: the fake key
pair below is used only with a scripted transport that never reaches the network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from crypto_trader.execution.binance_testnet import (
    HttpResponse,
    RateLimitBudget,
    RetryPolicy,
    ScriptedTransport,
    TokenBucket,
    WithdrawalPolicy,
    deterministic_order_uuid,
)
from crypto_trader.execution.binance_testnet import TestnetOrderRequest
from uuid import uuid4
# Aliased so pytest never mistakes these classes for test classes in any collecting context.
from crypto_trader.execution.binance_testnet import (
    BinanceSpotTestnetClient as SpotClient,
    TestnetClock as SpotClock,
    TestnetCredentials as SpotCredentials,
)

#: Synthetic, non-secret placeholder values used only against a scripted transport.
FAKE_API_KEY = "testnet-key-placeholder"
FAKE_API_SECRET = "testnet-secret-placeholder"

SYMBOL = "BTCUSDT"
SERVER_TIME_MS = 1_767_225_600_000


def credentials() -> TestnetCredentials:
    return SpotCredentials(api_key=FAKE_API_KEY, api_secret=FAKE_API_SECRET)


def clock(*, offset_ms: int = 0, recv_window_ms: int = 5_000) -> SpotClock:
    return SpotClock(
        time_source=lambda: SERVER_TIME_MS / 1000,
        offset_ms=offset_ms,
        recv_window_ms=recv_window_ms,
    )


def budget(*, capacity: int = 1200) -> RateLimitBudget:
    return RateLimitBudget(
        bucket=TokenBucket(capacity=capacity, refill_per_second=1000.0),
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )


def client(
    transport: ScriptedTransport,
    *,
    clock_instance: SpotClock | None = None,
    retries: int = 3,
    withdrawal_policy: WithdrawalPolicy = WithdrawalPolicy.REQUIRE_DISABLED,
) -> SpotClient:
    return SpotClient(
        credentials=credentials(),
        transport=transport,
        clock=clock_instance or clock(),
        symbol=SYMBOL,
        retry=RetryPolicy(max_attempts=retries, base_delay_seconds=0.001, max_delay_seconds=0.01),
        withdrawal_policy=withdrawal_policy,
        budget=budget(),
    )


def place_order(
    spot_client: SpotClient,
    request: TestnetOrderRequest,
    *,
    marker_event_id=None,
    intent_id=None,
    correlation_id=None,
):
    """TEST-ONLY helper: drive a fresh client write below the runtime boundary.

    * ``marker_event_id`` / ``intent_id`` / ``correlation_id`` are generated synthetically here and
      are only used to satisfy the client's authorization *shape* check.
    * These synthetic ids do NOT prove durable journal authorization. In production the
      authorization is created only by ``TestnetOrderRuntime`` after a REAL committed
      ``order.submission.attempted`` marker has been appended to the durable journal.
    * This helper exists solely so client-level tests can exercise behaviour below the runtime
      boundary; no production code may use it.
    * Already-known orders take the read-only dedup path and need no authorization.
    """
    prepared = spot_client.prepare_order(request)
    if not prepared.requires_write:
        return spot_client.submit_prepared(prepared)
    prepared = spot_client._authorize_attempt(
        prepared,
        marker_event_id=marker_event_id or uuid4(),
        venue_reference=prepared.client_order_id,
        order_id=deterministic_order_uuid(prepared.client_order_id),
        intent_id=intent_id or uuid4(),
        correlation_id=correlation_id or uuid4(),
        symbol=prepared.symbol,
        side=prepared.side,
        order_type=prepared.order_type,
        quantity=prepared.quantity,
        limit_price=prepared.limit_price,
        time_in_force=prepared.time_in_force,
    )
    return spot_client.submit_prepared(prepared)


# --- responses -------------------------------------------------------------

def response(status: int, body: Any, **headers: str) -> HttpResponse:
    return HttpResponse(status_code=status, body=body, headers=dict(headers))


def time_response(server_time_ms: int = SERVER_TIME_MS) -> HttpResponse:
    return response(200, {"serverTime": server_time_ms})


def price_response(price: str = "27000.00") -> HttpResponse:
    return response(200, {"symbol": SYMBOL, "price": price})


def exchange_info_response(
    *,
    symbol: str = SYMBOL,
    tick: str = "0.01",
    step: str = "0.00001",
    min_qty: str = "0.00001",
    max_qty: str = "9000.00000000",
    min_notional: str = "5",
) -> HttpResponse:
    return response(
        200,
        {
            "symbols": [
                {
                    "symbol": symbol,
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": tick},
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": step,
                            "minQty": min_qty,
                            "maxQty": max_qty,
                        },
                        {"filterType": "NOTIONAL", "minNotional": min_notional},
                    ],
                }
            ]
        },
    )


def account_response(
    *,
    can_trade: bool = True,
    can_withdraw: bool = False,
    asset: str = "USDT",
    free: str = "100000",
    permissions: tuple[str, ...] = ("SPOT",),
) -> HttpResponse:
    return response(
        200,
        {
            "canTrade": can_trade,
            "canWithdraw": can_withdraw,
            "accountType": "SPOT",
            "permissions": list(permissions),
            "makerCommission": 10,
            "takerCommission": 10,
            "balances": [
                {"asset": asset, "free": free, "locked": "0"},
                {"asset": "BTC", "free": "1.0", "locked": "0"},
            ],
        },
    )


def order_response(
    client_order_id: str,
    *,
    status: str = "NEW",
    order_id: int = 42,
    orig_qty: str = "0.00100000",
    executed_qty: str = "0",
    price: str = "27000.00",
    cummulative_quote: str = "0",
    fills: tuple[dict[str, str], ...] = (),
    transact_time: int = SERVER_TIME_MS,
) -> HttpResponse:
    return response(
        200,
        {
            "symbol": SYMBOL,
            "orderId": order_id,
            "clientOrderId": client_order_id,
            "transactTime": transact_time,
            "price": price,
            "origQty": orig_qty,
            "executedQty": executed_qty,
            "cummulativeQuoteQty": cummulative_quote,
            "status": status,
            "timeInForce": "GTC",
            "type": "LIMIT",
            "side": "BUY",
            "fills": list(fills),
        },
    )


def fill(
    price: str = "27000.00",
    qty: str = "0.00050000",
    commission: str = "0.0000005",
    asset: str = "BNB",
    trade_id: int = 7,
) -> dict[str, str]:
    return {
        "price": price,
        "qty": qty,
        "commission": commission,
        "commissionAsset": asset,
        "tradeId": str(trade_id),
    }


def rejected(code: int = -2010, msg: str = "Account has insufficient balance") -> HttpResponse:
    return response(400, {"code": code, "msg": msg})


def rate_limited(retry_after: str = "1") -> HttpResponse:
    return response(429, {"code": -1003, "msg": "Too many requests"}, **{"Retry-After": retry_after})


def banned(retry_after: str = "120") -> HttpResponse:
    return response(418, {"code": -1003, "msg": "IP banned"}, **{"Retry-After": retry_after})


def server_error() -> HttpResponse:
    return response(503, {"code": -1001, "msg": "Internal error"})


def open_orders_response(
    *order_ids: str, status: str = "NEW", executed_qty: str = "0"
) -> HttpResponse:
    return response(
        200,
        [
            {
                "symbol": SYMBOL,
                "orderId": 100 + index,
                "clientOrderId": client_order_id,
                "price": "27000.00",
                "origQty": "0.00100000",
                "executedQty": executed_qty,
                "cummulativeQuoteQty": "0",
                "status": status,
                "timeInForce": "GTC",
                "type": "LIMIT",
                "side": "BUY",
                "time": SERVER_TIME_MS,
            }
            for index, client_order_id in enumerate(order_ids)
        ],
    )


def limit_order_request(
    client_order_id: str, *, quantity: str = "0.00100000", price: str = "27000.00"
):
    from crypto_trader.execution.binance_testnet import TestnetOrderRequest

    return TestnetOrderRequest(
        client_order_id=client_order_id,
        symbol=SYMBOL,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal(quantity),
        price=Decimal(price),
        time_in_force="GTC",
    )


def environment_responses(*, can_withdraw: bool = False, can_trade: bool = True):
    """The responses `verify_environment` consumes, in call order."""
    return [
        time_response(),
        exchange_info_response(),
        account_response(can_trade=can_trade, can_withdraw=can_withdraw),
    ]


def request_paths(transport: ScriptedTransport) -> list[str]:
    return [
        request.url.split("binance.vision", 1)[-1].split("?", 1)[0]
        for request in transport.requests
    ]
