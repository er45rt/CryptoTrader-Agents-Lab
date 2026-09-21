"""Tests for the Binance official public market data tool (Phase 1 migration).

All tests are network-free: they patch requests.get on the tool module, so no
request ever leaves the test process.
"""

import json

import pytest
import requests
from unittest.mock import MagicMock, patch

from crypto_trader.tools.binance_public_tool import (
    DEFAULT_BASE_URL,
    BinancePublicTool,
)

MODULE = "crypto_trader.tools.binance_public_tool"

# A realistic api/v3/klines payload row (12 fields, Binance spot shape).
KLINE_ROW = [
    1672531200000, "40000.00000000", "40100.00000000", "39900.00000000",
    "40050.00000000", "1000.00000000", 1672534799999, "40000000.00000000",
    120, "500.00000000", "20000000.00000000", "0",
]


def make_response(status_code=200, payload=None, headers=None, text=""):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.text = text
    if payload is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = payload
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    return response


@pytest.fixture
def tool():
    """A tool instance with a deterministic timeout/attempt budget for tests."""
    return BinancePublicTool(timeout_seconds=7.0, max_attempts=3)


# --- Configuration ---

def test_defaults_to_official_binance_base_url(monkeypatch):
    monkeypatch.delenv("BINANCE_API_BASE_URL", raising=False)
    assert BinancePublicTool().base_url == DEFAULT_BASE_URL
    assert DEFAULT_BASE_URL == "https://api.binance.com"


def test_base_url_override_is_normalized(monkeypatch):
    monkeypatch.setenv("BINANCE_API_BASE_URL", "https://api.binance.us/")
    assert BinancePublicTool().base_url == "https://api.binance.us"


def test_no_rapidapi_configuration_is_required(monkeypatch):
    """The tool must construct with no environment variables set at all."""
    for key in ("RAPIDAPI_KEY", "RAPIDAPI_BINANCE_HOST", "BINANCE_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    instance = BinancePublicTool()
    assert instance.base_url == DEFAULT_BASE_URL
    assert not hasattr(instance, "rapidapi_key")
    assert not hasattr(instance, "rapidapi_host")


def test_endpoints_use_official_api_v3_paths(tool):
    assert tool.kline_endpoint_path == "/api/v3/klines"
    assert tool.price_ticker_endpoint_path == "/api/v3/ticker/price"
    assert tool.ticker_24hr_endpoint_path == "/api/v3/ticker/24hr"


# --- Success paths ---

@patch(f"{MODULE}.requests.get")
def test_get_kline_data_success(mock_get, tool):
    mock_get.return_value = make_response(payload=[KLINE_ROW])

    result = json.loads(tool._run(
        action="get_kline_data",
        parameters={"symbol": "BTCUSDT", "interval": "1h", "limit": 1},
    ))

    assert result["status"] == "ok"
    assert result["provider"] == "binance_public"
    assert result["data"] == [KLINE_ROW]
    assert result["validation"]["candles_received"] == 1
    assert result["validation"]["range_start_open_time_ms"] == 1672531200000

    mock_get.assert_called_once()
    call = mock_get.call_args
    assert call.args[0] == f"{DEFAULT_BASE_URL}/api/v3/klines"
    assert call.kwargs["params"] == {"symbol": "BTCUSDT", "interval": "1h", "limit": 1}
    assert call.kwargs["timeout"] == tool.timeout_seconds
    assert "x-rapidapi-key" not in {k.lower() for k in call.kwargs["headers"]}


@patch(f"{MODULE}.requests.get")
def test_get_symbol_price_ticker_success(mock_get, tool):
    mock_get.return_value = make_response(payload={"symbol": "BTCUSDT", "price": "40000.00000000"})

    result = json.loads(tool._run(
        action="get_symbol_price_ticker",
        parameters={"symbol": "BTCUSDT"},
    ))

    assert result["status"] == "ok"
    assert result["data"]["price"] == "40000.00000000"
    assert result["validation"]["symbol"] == "BTCUSDT"
    assert mock_get.call_args.args[0] == f"{DEFAULT_BASE_URL}/api/v3/ticker/price"


@patch(f"{MODULE}.requests.get")
def test_get_24hr_ticker_success(mock_get, tool):
    mock_get.return_value = make_response(payload={
        "symbol": "ETHUSDT",
        "openPrice": "2400.00",
        "highPrice": "2500.00",
        "lowPrice": "2350.00",
        "lastPrice": "2450.00",
        "volume": "100000.00",
        "priceChangePercent": "2.08",
    })

    result = json.loads(tool._run(action="get_24hr_ticker", parameters={"symbol": "ETHUSDT"}))

    assert result["status"] == "ok"
    assert result["validation"]["lastPrice"] == "2450.00"
    assert mock_get.call_args.args[0] == f"{DEFAULT_BASE_URL}/api/v3/ticker/24hr"


# --- Error handling: structured errors, never treated as data ---

@pytest.mark.parametrize("status_code,expected_type", [
    (400, "http_error"),
    (403, "http_error"),
    (404, "http_error"),
    (418, "ip_banned"),
])
@patch(f"{MODULE}.requests.get")
def test_non_retryable_http_errors_are_structured(mock_get, status_code, expected_type, tool):
    mock_get.return_value = make_response(status_code=status_code, payload={"code": -1121, "msg": "Invalid symbol."})

    result = json.loads(tool._run(action="get_24hr_ticker", parameters={"symbol": "ETHUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == expected_type
    assert result["http_status"] == status_code
    assert result["data"] is None
    assert "hint" in result
    assert mock_get.call_count == 1  # no retry for non-transient failures


@patch(f"{MODULE}.requests.get")
def test_timeout_returns_structured_error_after_bounded_retries(mock_get, tool):
    mock_get.side_effect = requests.exceptions.Timeout("timed out")

    with patch(f"{MODULE}.time.sleep") as mock_sleep:
        result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == "timeout"
    assert result["attempts"] == tool.max_attempts == 3
    assert result["data"] is None
    assert mock_get.call_count == 3
    assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0]  # bounded exponential backoff


@patch(f"{MODULE}.requests.get")
def test_connection_error_returns_structured_error(mock_get, tool):
    mock_get.side_effect = requests.exceptions.ConnectionError("dns failure")

    with patch(f"{MODULE}.time.sleep"):
        result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == "network_error"
    assert result["attempts"] == 3


# --- Retry behavior ---

@patch(f"{MODULE}.requests.get")
def test_transient_server_error_is_retried_then_succeeds(mock_get, tool):
    mock_get.side_effect = [
        make_response(status_code=503, payload={"msg": "unavailable"}),
        make_response(payload={"symbol": "BTCUSDT", "price": "40000.00"}),
    ]

    with patch(f"{MODULE}.time.sleep") as mock_sleep:
        result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "ok"
    assert mock_get.call_count == 2
    assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0]


@patch(f"{MODULE}.requests.get")
def test_rate_limit_honors_retry_after_then_succeeds(mock_get, tool):
    mock_get.side_effect = [
        make_response(status_code=429, payload={"msg": "too many requests"}, headers={"Retry-After": "3"}),
        make_response(payload={"symbol": "BTCUSDT", "price": "40000.00"}),
    ]

    with patch(f"{MODULE}.time.sleep") as mock_sleep:
        result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "ok"
    assert mock_get.call_count == 2
    assert [call.args[0] for call in mock_sleep.call_args_list] == [3.0]


@patch(f"{MODULE}.requests.get")
def test_rate_limit_returns_structured_error_after_retry_budget(mock_get, tool):
    mock_get.return_value = make_response(status_code=429, payload={"msg": "too many requests"})

    with patch(f"{MODULE}.time.sleep"):
        result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == "rate_limited"
    assert result["attempts"] == 3
    assert mock_get.call_count == 3


# --- Response validation ---

@patch(f"{MODULE}.requests.get")
def test_empty_kline_response_is_rejected(mock_get, tool):
    mock_get.return_value = make_response(payload=[])

    result = json.loads(tool._run(
        action="get_kline_data",
        parameters={"symbol": "FAKEUSDT", "interval": "1h", "limit": 5},
    ))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_response"
    assert "empty candle list" in result["message"]


@patch(f"{MODULE}.requests.get")
def test_malformed_kline_row_is_rejected(mock_get, tool):
    mock_get.return_value = make_response(payload=[[1672531200000, "not-a-number"]])

    result = json.loads(tool._run(
        action="get_kline_data",
        parameters={"symbol": "BTCUSDT", "interval": "1h"},
    ))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_response"


@patch(f"{MODULE}.requests.get")
def test_out_of_order_kline_timestamps_are_rejected(mock_get, tool):
    row_later = [row for row in KLINE_ROW]
    row_earlier = [row for row in KLINE_ROW]
    row_earlier[0] = 1672527600000
    mock_get.return_value = make_response(payload=[row_later, row_earlier])

    result = json.loads(tool._run(
        action="get_kline_data",
        parameters={"symbol": "BTCUSDT", "interval": "1h"},
    ))

    assert result["status"] == "error"
    assert "ascending order" in result["message"]


@patch(f"{MODULE}.requests.get")
def test_missing_price_field_is_rejected(mock_get, tool):
    mock_get.return_value = make_response(payload={"symbol": "BTCUSDT"})

    result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_response"
    assert "price" in result["message"]


@patch(f"{MODULE}.requests.get")
def test_incomplete_24hr_payload_is_rejected(mock_get, tool):
    mock_get.return_value = make_response(payload={"symbol": "ETHUSDT", "lastPrice": "2450.00"})

    result = json.loads(tool._run(action="get_24hr_ticker", parameters={"symbol": "ETHUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_response"
    assert "openPrice" in result["message"]


@patch(f"{MODULE}.requests.get")
def test_non_json_response_is_rejected(mock_get, tool):
    mock_get.return_value = make_response(status_code=200, payload=None, text="<html>blocked</html>")

    result = json.loads(tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_json"


# --- Input validation and unknown actions ---

def test_invalid_action_returns_structured_error(tool):
    result = json.loads(tool._run(action="non_existent_action", parameters={}))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_response"
    assert "Unsupported action" in result["message"]


def test_missing_parameters_return_structured_error(tool):
    result = json.loads(tool._run(action="get_kline_data", parameters={"interval": "1h"}))

    assert result["status"] == "error"
    assert result["error_type"] == "invalid_response"
    assert "symbol" in result["message"]
