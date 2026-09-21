# src/crypto_trader/tools/binance_public_tool.py
"""
Binance market data tool backed by the official Binance public REST API.

This module replaces the previous RapidAPI proxy integration. It only talks to
public, unauthenticated market-data endpoints:

    GET {BINANCE_API_BASE_URL}/api/v3/klines
    GET {BINANCE_API_BASE_URL}/api/v3/ticker/price
    GET {BINANCE_API_BASE_URL}/api/v3/ticker/24hr

No API key, secret, or trading/account endpoint is used anywhere in this module.
"""

import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Literal, Optional, Type

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger("crypto_trader.tools.binance")

# --- Configuration (all overridable, all optional) ---

DEFAULT_BASE_URL = "https://api.binance.com"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 8.0

# Binance uses 429 (rate limit) and 418 (IP ban) for throttling; 5xx are transient.
RETRYABLE_STATUS_CODES = {408, 418, 425, 429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 451}


class BinanceRequestError(Exception):
    """A request failure that has already been classified for reporting."""

    def __init__(
        self,
        error_type: str,
        message: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


# --- Input Schemas for different Binance public API actions ---

class KlineDataInput(BaseModel):
    """Input for fetching Kline/Candlestick data."""
    symbol: str = Field(..., description="Trading symbol (e.g., 'BTCUSDT', 'ETHBTC').")
    interval: str = Field(..., description="Kline interval (e.g., '1m', '1h', '1d').")
    startTime: Optional[int] = Field(None, description="Start time in milliseconds (Unix timestamp). Optional.")
    endTime: Optional[int] = Field(None, description="End time in milliseconds (Unix timestamp). Optional.")
    limit: Optional[int] = Field(None, description="Number of klines to retrieve. Default 500, max 1000. Optional.")


class SymbolPriceTickerInput(BaseModel):
    """Input for fetching symbol price ticker."""
    symbol: Optional[str] = Field(None, description="Trading symbol (e.g., 'BTCUSDT'). If omitted, prices for all symbols are returned.")


class Ticker24hrInput(BaseModel):
    """Input for fetching 24hr ticker price change statistics."""
    symbol: Optional[str] = Field(None, description="Trading symbol (e.g., 'BTCUSDT'). If omitted, stats for all symbols are returned.")


class BinancePublicToolInput(BaseModel):
    """
    Defines the action and corresponding parameters for the Binance public market data tool.
    """
    action: Literal['get_kline_data', 'get_symbol_price_ticker', 'get_24hr_ticker'] = Field(..., description="The Binance API action to perform.")
    parameters: Dict[str, Any] = Field(..., description="A dictionary of parameters specific to the chosen action.")


# --- Helpers ---

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using default %s.", name, raw, default)
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r; using default %s.", name, raw, default)
        return default
    return value if value >= 1 else default


def _as_finite_float(value: Any) -> Optional[float]:
    """Return a finite float for a Binance numeric field, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _compute_backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff: base * 2^(attempt-1), clamped to cap."""
    return min(base * (2 ** (attempt - 1)), cap)


class BinancePublicTool(BaseTool):
    name: str = "Binance Market Data Tool (Official Public API)"
    description: str = (
        "A tool to interact with the official Binance public market data REST endpoints. "
        "Supports actions: 'get_kline_data' for historical OHLCV, "
        "'get_symbol_price_ticker' for current prices, and "
        "'get_24hr_ticker' for 24-hour price change statistics. "
        "Read-only public market data only; no authentication and no order placement. "
        "Input to this tool should be a JSON object with 'action' and 'parameters' keys. "
        "Responses are returned as a JSON string; failures are returned as a structured "
        "JSON object with a top-level 'status' of 'error'."
    )
    args_schema: Type[BaseModel] = BinancePublicToolInput

    base_url: Optional[str] = None
    timeout_seconds: Optional[float] = None
    max_attempts: Optional[int] = None

    # Official Binance public endpoint paths (api/v3 - spot market data).
    kline_endpoint_path: str = "/api/v3/klines"
    price_ticker_endpoint_path: str = "/api/v3/ticker/price"
    ticker_24hr_endpoint_path: str = "/api/v3/ticker/24hr"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        if not self.base_url:
            self.base_url = os.getenv("BINANCE_API_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        self.base_url = self.base_url.rstrip("/")

        if self.timeout_seconds is None:
            self.timeout_seconds = _env_float("BINANCE_HTTP_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        if self.max_attempts is None:
            self.max_attempts = _env_int("BINANCE_HTTP_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)

        self._backoff_base = _env_float("BINANCE_HTTP_BACKOFF_BASE", DEFAULT_BACKOFF_BASE_SECONDS)
        self._backoff_cap = _env_float("BINANCE_HTTP_BACKOFF_MAX", DEFAULT_BACKOFF_MAX_SECONDS)

    # --- HTTP layer ---

    def _build_headers(self) -> Dict[str, str]:
        try:
            from crypto_trader import __version__ as pkg_version
        except Exception:
            pkg_version = "0.1.0"
        return {
            "Accept": "application/json",
            "User-Agent": f"crypto-trader/{pkg_version} (+read-only market data)",
        }

    def _request_once(self, endpoint_path: str, params: Optional[Dict[str, Any]]) -> Any:
        """Perform a single GET request, returning parsed JSON or raising BinanceRequestError."""
        url = f"{self.base_url}{endpoint_path}"
        query_params = {k: v for k, v in params.items() if v is not None} if params else None

        try:
            response = requests.get(
                url,
                headers=self._build_headers(),
                params=query_params,
                timeout=self.timeout_seconds,
            )
        except requests.exceptions.Timeout as exc:
            raise BinanceRequestError(
                "timeout",
                f"Request to {url} timed out after {self.timeout_seconds}s: {exc}",
                retryable=True,
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise BinanceRequestError(
                "network_error",
                f"Network error contacting {url}: {exc}",
                retryable=True,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise BinanceRequestError(
                "request_error",
                f"Request to {url} failed: {exc}",
                retryable=True,
            ) from exc

        status_code = response.status_code
        if status_code >= 400:
            retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
            detail = ""
            try:
                body = response.json()
                detail = f" Binance response: {json.dumps(body)[:500]}"
            except ValueError:
                detail = f" Response body: {response.text[:300]}"

            if status_code == 429 or status_code == 418:
                raise BinanceRequestError(
                    "rate_limited" if status_code == 429 else "ip_banned",
                    f"Binance throttled the request (HTTP {status_code}).{detail}",
                    status_code=status_code,
                    # 429 is a transient throttle: retry within the bounded budget,
                    # honoring Retry-After when Binance sends one. 418 (IP ban) is not retried.
                    retryable=status_code == 429,
                    retry_after=retry_after,
                )
            if status_code in NON_RETRYABLE_STATUS_CODES:
                raise BinanceRequestError(
                    "http_error",
                    f"Binance returned HTTP {status_code} for {url}.{detail}",
                    status_code=status_code,
                    retryable=False,
                )
            raise BinanceRequestError(
                "http_error",
                f"Binance returned HTTP {status_code} for {url}.{detail}",
                status_code=status_code,
                retryable=status_code in RETRYABLE_STATUS_CODES,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise BinanceRequestError(
                "invalid_json",
                f"Binance returned a non-JSON response for {url}: {response.text[:200]}",
                status_code=status_code,
                retryable=False,
            ) from exc

    @staticmethod
    def _parse_retry_after(raw_value: Optional[str]) -> Optional[float]:
        if not raw_value:
            return None
        try:
            return max(0.0, float(raw_value))
        except (TypeError, ValueError):
            return None

    def _make_request(self, endpoint_path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """
        Perform a GET request with a bounded retry loop and exponential backoff.

        Errors are never returned as if they were market data: every failure path
        returns a structured JSON error payload with 'status': 'error'.
        """
        attempts = 0
        last_error: Optional[BinanceRequestError] = None

        while attempts < self.max_attempts:
            attempts += 1
            try:
                payload = self._request_once(endpoint_path, params)
                return json.dumps(payload)
            except BinanceRequestError as exc:
                last_error = exc
                if not exc.retryable or attempts >= self.max_attempts:
                    break
                if exc.retry_after is not None:
                    delay = min(exc.retry_after, self._backoff_cap)
                else:
                    delay = _compute_backoff_delay(attempts, self._backoff_base, self._backoff_cap)
                logger.warning(
                    "Binance request to %s failed (attempt %s/%s, type=%s); retrying in %.2fs.",
                    endpoint_path, attempts, self.max_attempts, exc.error_type, delay,
                )
                time.sleep(delay)

        error = last_error or BinanceRequestError("unknown_error", "Request failed for an unknown reason.")
        return json.dumps(self._error_payload(error, endpoint_path, attempts))

    def _error_payload(self, error: BinanceRequestError, endpoint_path: str, attempts: int) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": "error",
            "error_type": error.error_type,
            "message": error.message,
            "provider": "binance_public",
            "endpoint": endpoint_path,
            "attempts": attempts,
            "max_attempts": self.max_attempts,
            "data": None,
        }
        if error.status_code is not None:
            payload["http_status"] = error.status_code
        if error.retry_after is not None:
            payload["retry_after_seconds"] = error.retry_after
        payload["hint"] = (
            "No market data was returned. Do not fabricate or infer values from this response; "
            "report the failure and its cause in your output."
        )
        return payload

    @staticmethod
    def _validation_error(message: str) -> str:
        return json.dumps({
            "status": "error",
            "error_type": "invalid_response",
            "message": message,
            "provider": "binance_public",
            "data": None,
            "hint": (
                "No market data was returned. Do not fabricate or infer values from this response; "
                "report the validation failure in your output."
            ),
        })

    # --- Response validation (shape/consistency only; no derived analysis) ---

    def _validate_klines(self, payload: Any, symbol: Optional[str]) -> Dict[str, Any]:
        if not isinstance(payload, list):
            return {"ok": False, "error": f"Expected a JSON array of candles, got {type(payload).__name__}."}
        if not payload:
            return {"ok": False, "error": "Binance returned an empty candle list (unknown symbol or no data in range)."}

        open_times: List[int] = []
        for index, candle in enumerate(payload):
            if not isinstance(candle, list) or len(candle) < 11:
                return {
                    "ok": False,
                    "error": f"Candle at index {index} is not a Binance kline array with at least 11 fields.",
                }
            open_time = _as_finite_float(candle[0])
            open_price = _as_finite_float(candle[1])
            high_price = _as_finite_float(candle[2])
            low_price = _as_finite_float(candle[3])
            close_price = _as_finite_float(candle[4])
            volume = _as_finite_float(candle[5])
            close_time = _as_finite_float(candle[6])

            if None in (open_time, close_time):
                return {"ok": False, "error": f"Candle at index {index} has a non-numeric open/close timestamp."}
            if None in (open_price, high_price, low_price, close_price, volume):
                return {"ok": False, "error": f"Candle at index {index} has non-numeric OHLCV fields."}
            if min(open_price, high_price, low_price, close_price) <= 0:
                return {"ok": False, "error": f"Candle at index {index} has a non-positive price."}
            if volume < 0:
                return {"ok": False, "error": f"Candle at index {index} has negative volume."}
            if high_price < low_price:
                return {"ok": False, "error": f"Candle at index {index} has high < low."}
            if close_time < open_time:
                return {"ok": False, "error": f"Candle at index {index} has close time before open time."}
            open_times.append(int(open_time))

        if any(later < earlier for earlier, later in zip(open_times, open_times[1:])):
            return {"ok": False, "error": "Candle open times are not in ascending order."}

        summary = {
            "candles_received": len(payload),
            "range_start_open_time_ms": open_times[0],
            "range_end_open_time_ms": open_times[-1],
            "symbol": symbol,
        }
        return {"ok": True, "summary": summary}

    def _validate_price_ticker(self, payload: Any) -> Dict[str, Any]:
        entries = payload if isinstance(payload, list) else [payload]
        if not entries:
            return {"ok": False, "error": "Binance returned an empty price ticker payload."}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return {"ok": False, "error": f"Ticker entry at index {index} is not a JSON object."}
            symbol = entry.get("symbol")
            price = _as_finite_float(entry.get("price"))
            if not isinstance(symbol, str) or not symbol:
                return {"ok": False, "error": f"Ticker entry at index {index} is missing a 'symbol' field."}
            if price is None or price <= 0:
                return {"ok": False, "error": f"Ticker entry for {symbol} has a missing, non-numeric, or non-positive 'price'."}
        summary = {"symbols_returned": len(entries), "sample_symbol": entries[0].get("symbol")}
        if len(entries) == 1:
            summary["symbol"] = entries[0]["symbol"]
            summary["price"] = entries[0]["price"]
        return {"ok": True, "summary": summary}

    def _validate_24hr_ticker(self, payload: Any, symbol: Optional[str]) -> Dict[str, Any]:
        if isinstance(payload, dict) and payload.get("code") is not None and payload.get("msg") is not None:
            return {"ok": False, "error": f"Binance error payload: {payload.get('code')} - {payload.get('msg')}"}

        entries = payload if isinstance(payload, list) else [payload]
        if not entries:
            return {"ok": False, "error": "Binance returned an empty 24hr ticker payload."}

        required_fields = ("symbol", "openPrice", "highPrice", "lowPrice", "lastPrice", "volume")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return {"ok": False, "error": f"24hr ticker entry at index {index} is not a JSON object."}
            for field in required_fields:
                if field not in entry:
                    return {"ok": False, "error": f"24hr ticker entry at index {index} is missing '{field}'."}
            open_price = _as_finite_float(entry.get("openPrice"))
            high_price = _as_finite_float(entry.get("highPrice"))
            low_price = _as_finite_float(entry.get("lowPrice"))
            last_price = _as_finite_float(entry.get("lastPrice"))
            volume = _as_finite_float(entry.get("volume"))
            if None in (open_price, high_price, low_price, last_price):
                return {"ok": False, "error": f"24hr ticker for {entry.get('symbol')} has non-numeric price fields."}
            if min(open_price, high_price, low_price, last_price) < 0:
                return {"ok": False, "error": f"24hr ticker for {entry.get('symbol')} has a negative price."}
            if volume is None or volume < 0:
                return {"ok": False, "error": f"24hr ticker for {entry.get('symbol')} has a missing or negative volume."}
            if high_price < low_price:
                return {"ok": False, "error": f"24hr ticker for {entry.get('symbol')} has highPrice < lowPrice."}

        summary = {"symbols_returned": len(entries), "symbol": symbol or entries[0].get("symbol")}
        if len(entries) == 1:
            entry = entries[0]
            summary["lastPrice"] = entry.get("lastPrice")
            summary["priceChangePercent"] = entry.get("priceChangePercent")
        return {"ok": True, "summary": summary}

    # --- Tool entry point ---

    def _run(
        self,
        action: Literal['get_kline_data', 'get_symbol_price_ticker', 'get_24hr_ticker'],
        parameters: Dict[str, Any],
    ) -> str:
        try:
            if action == "get_kline_data":
                validated = KlineDataInput(**(parameters or {}))
                params = validated.model_dump(exclude_none=True)
                raw = self._make_request(self.kline_endpoint_path, params)
                return self._validated_response(raw, self._validate_klines, params.get("symbol"))

            if action == "get_symbol_price_ticker":
                validated = SymbolPriceTickerInput(**(parameters or {}))
                params = validated.model_dump(exclude_none=True)
                raw = self._make_request(self.price_ticker_endpoint_path, params)
                return self._validated_response(raw, self._validate_price_ticker)

            if action == "get_24hr_ticker":
                validated = Ticker24hrInput(**(parameters or {}))
                params = validated.model_dump(exclude_none=True)
                raw = self._make_request(self.ticker_24hr_endpoint_path, params)
                return self._validated_response(raw, self._validate_24hr_ticker, params.get("symbol"))

            return self._validation_error(f"Unsupported action '{action}'.")
        except BinanceRequestError as exc:
            return json.dumps(self._error_payload(exc, "", 0))
        except Exception as exc:  # pydantic validation errors, unexpected bugs
            return self._validation_error(
                f"Could not process action '{action}' with parameters {parameters!r}: {exc}"
            )

    def _validated_response(self, raw: str, validator, *validator_args) -> str:
        """Validate a raw JSON response string; return the raw payload on success or a structured error."""
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            return self._validation_error(f"Provider returned non-JSON content: {exc}")

        if isinstance(payload, dict) and payload.get("status") == "error":
            return raw

        result = validator(payload, *validator_args)
        if not result.get("ok"):
            return self._validation_error(result.get("error", "Response validation failed."))

        wrapped = {
            "status": "ok",
            "provider": "binance_public",
            "validation": result.get("summary", {}),
            "data": payload,
        }
        return json.dumps(wrapped)


# --- Example Usage (safe, read-only; for testing the tool directly) ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    binance_tool = BinancePublicTool()
    print(f"BinancePublicTool initialized for base URL: {binance_tool.base_url}")

    print("\n--- Testing Get Kline Data (ETHBTC, 1m, limit 3) ---")
    print(binance_tool._run(action="get_kline_data", parameters={"symbol": "ETHBTC", "interval": "1m", "limit": 3}))

    print("\n--- Testing Get Symbol Price Ticker (BTCUSDT) ---")
    print(binance_tool._run(action="get_symbol_price_ticker", parameters={"symbol": "BTCUSDT"}))

    print("\n--- Testing Get 24hr Ticker (ETHUSDT) ---")
    print(binance_tool._run(action="get_24hr_ticker", parameters={"symbol": "ETHUSDT"}))
