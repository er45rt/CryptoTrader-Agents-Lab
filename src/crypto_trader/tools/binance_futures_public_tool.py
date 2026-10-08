"""Unauthenticated USD-M public GET data for analysis agents only.

One process-shared cache, admission policy, cooldown and rolling attempt limiter.
No background polling or archival. HTTP timeout is a connect/read timeout; it is
not a total wall-clock deadline. All failures suppress values and reduce confidence.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
import math
import re
import threading
import time
from typing import Any, Literal, Type

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from . import derivatives_schema as ds

PUBLIC_BASE_URL = "https://fapi.binance.com"
ENDPOINTS = {
    ds.Metric.SETTLED_FUNDING: "/fapi/v1/fundingRate",
    ds.Metric.CURRENT_FUNDING: "/fapi/v1/premiumIndex",
    ds.Metric.FUNDING_INTERVAL: "/fapi/v1/fundingInfo",
    ds.Metric.CURRENT_OI: "/fapi/v1/openInterest",
    ds.Metric.HISTORICAL_OI: "/futures/data/openInterestHist",
    ds.Metric.GLOBAL_LONG_SHORT: "/futures/data/globalLongShortAccountRatio",
}
QUERY_KEYS = {
    ENDPOINTS[ds.Metric.FUNDING_INTERVAL]: frozenset(),
    ENDPOINTS[ds.Metric.CURRENT_FUNDING]: frozenset({"symbol"}),
    ENDPOINTS[ds.Metric.CURRENT_OI]: frozenset({"symbol"}),
    ENDPOINTS[ds.Metric.SETTLED_FUNDING]: frozenset({"symbol", "limit", "startTime", "endTime"}),
    ENDPOINTS[ds.Metric.HISTORICAL_OI]: frozenset({"symbol", "period", "limit", "startTime", "endTime"}),
    ENDPOINTS[ds.Metric.GLOBAL_LONG_SHORT]: frozenset({"symbol", "period", "limit", "startTime", "endTime"}),
}
RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+(?:USDT|USDC)$")


@dataclass(frozen=True)
class RawResult:
    payload: Any
    fetched_at: int
    reason: str | None = None
    attempts: int = 0


@dataclass(frozen=True)
class CacheEntry:
    result: RawResult
    expires_at: float


class SharedPublicState:
    """Process scope only. Map locks never cover transport, sleep or event waits."""

    def __init__(self, policy: ds.PollingPolicy = ds.PollingPolicy(), *,
                 monotonic=time.monotonic, wall=time.time, sleep=time.sleep):
        if not ds.request_budget(1, policy).admitted:
            raise ValueError("steady_state_request_budget_exceeded")
        self.policy = policy
        self.monotonic, self.wall, self.sleep = monotonic, wall, sleep
        self.lock = threading.RLock()
        self.cache: dict[tuple, CacheEntry] = {}
        self.in_flight: dict[tuple, threading.Event] = {}
        self.attempts: deque[float] = deque()
        self.leases: dict[str, float] = {}
        self.cooldown_until = 0.0

    def wall_ms(self) -> int:
        return int(self.wall() * ds.MS_PER_SECOND)

    def admit(self, symbol: str) -> bool:
        with self.lock:
            now = self.monotonic()
            self.leases = {key: expires for key, expires in self.leases.items() if expires > now}
            projected = len(set(self.leases) | {symbol})
            if not ds.request_budget(projected, self.policy).admitted:
                return False
            self.leases[symbol] = now + ds.ADMISSION_LEASE_SECONDS
            return True

    def reserve_attempt(self) -> str | None:
        with self.lock:
            now = self.monotonic()
            if now < self.cooldown_until:
                return "provider_rate_limit_cooldown"
            while self.attempts and self.attempts[0] <= now - ds.LOCAL_RATE_WINDOW_SECONDS:
                self.attempts.popleft()
            if len(self.attempts) >= ds.LOCAL_MAX_RPM:
                return "local_rate_limit_exhausted"
            self.attempts.append(now)
            return None

    def cooldown(self, seconds: float | None) -> None:
        with self.lock:
            delay = ds.UNKNOWN_RATE_LIMIT_COOLDOWN_SECONDS if seconds is None else seconds
            # Python float overflow cannot create an unbounded sleep; infinity
            # means a process that stays fail-closed until restart.
            self.cooldown_until = max(self.cooldown_until, self.monotonic() + delay)


SHARED_STATE = SharedPublicState()


def _public_get(path: str, parameters: dict[str, Any]):
    if path not in ENDPOINTS.values():
        raise ValueError("endpoint_not_allowed")
    if set(parameters) - QUERY_KEYS[path]:
        raise ValueError("query_parameter_not_allowed")
    # A fresh session per attempt cannot retain cookies. Ignoring netrc, proxy
    # environment and environment CA overrides is intentional and fail-closed.
    with requests.Session() as session:
        session.trust_env = False
        return session.get(PUBLIC_BASE_URL + path, params=parameters,
                           timeout=ds.HTTP_TIMEOUT_SECONDS, allow_redirects=False, verify=True)


def _retry_after(header: Any, fetched_at: int) -> float | None:
    if not isinstance(header, str) or not header.strip():
        return None
    try:
        seconds = float(header)
    except (ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - fetched_at / ds.MS_PER_SECOND
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("malformed_numeric_field")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non_finite_value")
    return number


class BinanceFuturesPublicClient:
    def __init__(self, *, state: SharedPublicState | None = None, transport=None):
        self.state = SHARED_STATE if state is None else state
        self.transport = transport or _public_get

    def _http(self, metric: ds.Metric, parameters: dict[str, Any]) -> RawResult:
        for attempt in range(1, ds.TOTAL_ATTEMPTS + 1):
            reason = self.state.reserve_attempt()
            if reason:
                return RawResult(None, self.state.wall_ms(), reason, attempt - 1)
            try:
                response = self.transport(ENDPOINTS[metric], parameters)
                status = response.status_code
                if status in (429, 418):
                    fetched = self.state.wall_ms()
                    delay = _retry_after(response.headers.get("Retry-After"), fetched)
                    self.state.cooldown(delay)
                    if status == 418:
                        return RawResult(None, fetched, "provider_ip_banned", attempt)
                    if delay is None:
                        return RawResult(None, fetched, "rate_limited_retry_after_unknown", attempt)
                    if delay > ds.RETRY_AFTER_MAX_SECONDS:
                        return RawResult(None, fetched, "rate_limited_retry_after_exceeds_budget", attempt)
                    if attempt == ds.TOTAL_ATTEMPTS:
                        return RawResult(None, fetched, "http_retry_budget_exhausted", attempt)
                    self.state.sleep(delay)
                    continue
                if status != 200:
                    if status in RETRYABLE_HTTP and attempt < ds.TOTAL_ATTEMPTS:
                        continue
                    reason = "http_retry_budget_exhausted" if status in RETRYABLE_HTTP else "provider_http_error"
                    return RawResult(None, self.state.wall_ms(), reason, attempt)
                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    return RawResult(None, self.state.wall_ms(), "malformed_json", attempt)
                # Availability is after body receipt/decoding, never request send.
                return RawResult(payload, self.state.wall_ms(), attempts=attempt)
            except requests.exceptions.Timeout:
                reason = "timeout_retry_budget_exhausted"
            except requests.exceptions.ConnectionError:
                reason = "provider_down_retry_budget_exhausted"
            except requests.exceptions.RequestException:
                return RawResult(None, self.state.wall_ms(), "provider_transport_error", attempt)
            except Exception:
                return RawResult(None, self.state.wall_ms(), "internal_error", attempt)
            if attempt == ds.TOTAL_ATTEMPTS:
                return RawResult(None, self.state.wall_ms(), reason, attempt)
        raise AssertionError("bounded_request_loop")

    def _raw(self, metric: ds.Metric, symbol: str, parameters: dict[str, Any]) -> RawResult:
        key = (ds.PROVIDER, symbol, metric.value, parameters.get("period"), tuple(sorted(parameters.items())))
        with self.state.lock:
            now = self.state.monotonic()
            self.state.cache = {item: cached for item, cached in self.state.cache.items() if now < cached.expires_at}
            entry = self.state.cache.get(key)
            if entry and self.state.monotonic() < entry.expires_at:
                return entry.result
            event = self.state.in_flight.get(key)
            owner = event is None
            if owner:
                event = threading.Event()
                self.state.in_flight[key] = event
        if not owner:
            if not event.wait(timeout=ds.IN_FLIGHT_WAIT_SECONDS):
                return RawResult(None, self.state.wall_ms(), "in_flight_wait_budget_exhausted")
            with self.state.lock:
                entry = self.state.cache.get(key)
                return entry.result if entry else RawResult(None, self.state.wall_ms(), "missing_shared_result")
        result = RawResult(None, self.state.wall_ms(), "provider_transport_error")
        try:
            result = self._http(metric, parameters)
            return result
        except Exception:
            result = RawResult(None, self.state.wall_ms(), "internal_error")
            return result
        finally:
            ttl = self.state.policy.ttl(metric)
            if parameters.get("endTime") is not None:
                ttl = max(ttl, ds.CLOSED_HISTORY_TTL_SECONDS)
            with self.state.lock:
                now = self.state.monotonic()
                if result.reason:
                    ttl = min(ttl, max(ds.FAILURE_TTL_SECONDS, self.state.cooldown_until - now))
                # A no-send local denial is not a provider observation to cache.
                if result.attempts:
                    self.state.cache[key] = CacheEntry(result, now + ttl)
                self.state.in_flight.pop(key, None)
                event.set()

    def _unavailable(self, symbol: str, metric: ds.Metric, reason: str,
                     fetched: int | None = None, period: str | None = None) -> ds.Observation:
        fetched = self.state.wall_ms() if fetched is None else fetched
        historical = metric in ds.HISTORICAL_METRICS
        return ds.Observation(symbol, metric, None, None if historical else fetched, fetched,
                              ds.HISTORICAL_PROVENANCE if historical else ds.LIVE_PROVENANCE,
                              ds.Status.UNAVAILABLE, None, reason, ds.CURRENT_OI_MAX_AGE_SECONDS, period)

    def funding_interval(self, symbol: str) -> ds.Observation:
        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
            return self._unavailable(symbol, ds.Metric.FUNDING_INTERVAL, "invalid_symbol")
        if not self.state.admit(symbol):
            return self._unavailable(symbol, ds.Metric.FUNDING_INTERVAL, "steady_state_request_budget_exceeded")
        raw = self._raw(ds.Metric.FUNDING_INTERVAL, "*", {})
        if raw.reason:
            return self._unavailable(symbol, ds.Metric.FUNDING_INTERVAL, raw.reason, raw.fetched_at)
        try:
            if not isinstance(raw.payload, list):
                raise ValueError("malformed_funding_info")
            by_symbol = {}
            for row in raw.payload:
                if (not isinstance(row, dict) or not isinstance(row.get("symbol"), str)
                        or not row["symbol"] or row["symbol"] != row["symbol"].strip()
                        or row["symbol"] in by_symbol or type(row.get("fundingIntervalHours")) is not int
                        or row["fundingIntervalHours"] <= 0):
                    raise ValueError("malformed_funding_info")
                by_symbol[row["symbol"]] = row["fundingIntervalHours"]
            found = symbol in by_symbol
            interval = ds.FundingInterval(by_symbol[symbol] if found else ds.DOCUMENTED_DEFAULT_FUNDING_HOURS,
                                          "fundingInfo" if found else "documented_default",
                                          raw.fetched_at, raw.fetched_at,
                                          None if found else ds.DOCUMENTED_DEFAULT_VERSION)
            obs = ds.Observation(symbol, ds.Metric.FUNDING_INTERVAL, None, raw.fetched_at, raw.fetched_at,
                                 ds.LIVE_PROVENANCE, ds.Status.AVAILABLE, interval, None, ds.FUNDING_INFO_MAX_AGE_SECONDS)
            return obs.assess(self.state.wall_ms())
        except (ValueError, TypeError, KeyError):
            return self._unavailable(symbol, ds.Metric.FUNDING_INTERVAL, "malformed_funding_info", raw.fetched_at)

    def observations(self, metric: ds.Metric, symbol: str, *, period: str = ds.DEFAULT_PERIOD,
                     limit: int = ds.DEFAULT_HISTORY_LIMIT, start_time: int | None = None,
                     end_time: int | None = None, exploratory: bool = False) -> tuple[ds.Observation, ...]:
        if exploratory and metric not in ds.HISTORICAL_METRICS:
            return (self._unavailable(symbol, metric, "invalid_query", period=period),)
        if metric == ds.Metric.FUNDING_INTERVAL:
            return (self.funding_interval(symbol),)
        if (not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol)
                or period not in ds.PERIOD_SECONDS):
            return (self._unavailable(symbol, metric, "invalid_query", period=period),)
        maximum = ds.MAX_FUNDING_HISTORY_LIMIT if metric == ds.Metric.SETTLED_FUNDING else ds.MAX_STATISTICS_HISTORY_LIMIT
        if (type(limit) is not int or not 1 <= limit <= maximum
                or any(item is not None and not ds.valid_timestamp(item) for item in (start_time, end_time))
                or (end_time is not None and end_time > self.state.wall_ms())
                or (start_time is not None and end_time is not None and start_time > end_time)
                or (metric not in ds.HISTORICAL_METRICS and (start_time is not None or end_time is not None))):
            return (self._unavailable(symbol, metric, "invalid_query", period=period),)
        if not self.state.admit(symbol):
            return (self._unavailable(symbol, metric, "steady_state_request_budget_exceeded", period=period),)
        interval = None
        if metric in (ds.Metric.CURRENT_FUNDING, ds.Metric.SETTLED_FUNDING):
            interval_observation = self.funding_interval(symbol)
            if interval_observation.status == ds.Status.UNAVAILABLE:
                return (self._unavailable(symbol, metric, "funding_interval_unavailable", period=period),)
            interval = interval_observation.value
        parameters: dict[str, Any] = {"symbol": symbol}
        if metric in ds.HISTORICAL_METRICS:
            parameters["limit"] = limit
            if metric != ds.Metric.SETTLED_FUNDING:
                parameters["period"] = period
            if start_time is not None:
                parameters["startTime"] = start_time
            if end_time is not None:
                parameters["endTime"] = end_time
        raw = self._raw(metric, symbol, parameters)
        if raw.reason:
            return (self._unavailable(symbol, metric, raw.reason, raw.fetched_at, period),)
        rows = raw.payload if metric in ds.HISTORICAL_METRICS else [raw.payload]
        if not isinstance(rows, list) or not rows:
            return (self._unavailable(symbol, metric, "missing_observation", raw.fetched_at, period),)
        # Explicit history queries assess age relative to their requested historical
        # end; they remain exploratory-only and cannot be promoted to current data.
        reference_time = end_time if end_time is not None else self.state.wall_ms()
        observations = tuple(self._normalize(row, metric, symbol, raw.fetched_at, period, interval).assess(
            reference_time, check_freshness=not exploratory)
                             for row in rows)
        event_times = [obs.event_time for obs in observations if obs.event_time is not None]
        if event_times != sorted(set(event_times)) or any(
            (start_time is not None and event < start_time) or (end_time is not None and event > end_time)
            for event in event_times
        ):
            return (self._unavailable(symbol, metric, "inconsistent_history_timestamps", raw.fetched_at, period),)
        return observations

    def _normalize(self, row: Any, metric: ds.Metric, symbol: str, fetched: int,
                   period: str, interval: ds.FundingInterval | None) -> ds.Observation:
        try:
            if not isinstance(row, dict) or row.get("symbol") != symbol:
                raise ValueError("missing_or_mismatched_symbol")
            reason = None
            if metric == ds.Metric.CURRENT_FUNDING:
                event = row["time"]
                value = ds.CurrentFundingContext(_number(row["lastFundingRate"]), row["nextFundingTime"],
                                                 _number(row["markPrice"]), _number(row["indexPrice"]), event, interval)
                cycle_seconds = interval.hours * ds.SECONDS_PER_HOUR
                age = min(ds.CURRENT_FUNDING_MAX_AGE_SECONDS, cycle_seconds / ds.CURRENT_FUNDING_AGE_CYCLE_DIVISOR)
                if (ds.valid_timestamp(event) and ds.valid_timestamp(value.next_funding_time)
                        and value.next_funding_time - event > cycle_seconds * ds.MS_PER_SECOND * ds.NEXT_FUNDING_MAX_INTERVAL_MULTIPLIER):
                    reason = "funding_schedule_interval_mismatch"
            elif metric == ds.Metric.SETTLED_FUNDING:
                event = row["fundingTime"]
                value = ds.SettledFunding(_number(row["fundingRate"]), _number(row["markPrice"]) if "markPrice" in row else None)
                age = interval.hours * ds.SECONDS_PER_HOUR + ds.SETTLED_FUNDING_GRACE_SECONDS
            elif metric == ds.Metric.CURRENT_OI:
                event, value, age = row["time"], ds.OpenInterest(_number(row["openInterest"])), ds.CURRENT_OI_MAX_AGE_SECONDS
            elif metric == ds.Metric.HISTORICAL_OI:
                event = row["timestamp"]  # Provider documents end of period, not availability.
                value = ds.OpenInterest(_number(row["sumOpenInterest"]), _number(row["sumOpenInterestValue"]))
                age = ds.PERIOD_SECONDS[period] * ds.PERIOD_MAX_AGE_MULTIPLIER + ds.PERIOD_MAX_AGE_GRACE_SECONDS
            elif metric == ds.Metric.GLOBAL_LONG_SHORT:
                event = row["timestamp"]  # Provider documents end of period, not availability.
                value = ds.LongShortAccounts(_number(row["longShortRatio"]), _number(row["longAccount"]), _number(row["shortAccount"]))
                age = ds.PERIOD_SECONDS[period] * ds.PERIOD_MAX_AGE_MULTIPLIER + ds.PERIOD_MAX_AGE_GRACE_SECONDS
            else:
                raise ValueError("unknown_metric")
            if not ds.valid_timestamp(event):
                return self._unavailable(symbol, metric, "invalid_timestamp", fetched, period)
            historical = metric in ds.HISTORICAL_METRICS
            obs = ds.Observation(symbol, metric, event, None if historical else fetched, fetched,
                                 ds.HISTORICAL_PROVENANCE if historical else ds.LIVE_PROVENANCE,
                                 ds.Status.SUSPECT if reason else ds.Status.AVAILABLE, value, reason, age,
                                 period if metric in (ds.Metric.HISTORICAL_OI, ds.Metric.GLOBAL_LONG_SHORT) else None)
            return obs
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            return self._unavailable(symbol, metric, "malformed_required_fields", fetched, period)

    def snapshot(self, symbol: str) -> dict[str, Any]:
        observations = {metric.value: self.observations(metric, symbol)[0] for metric in ds.Metric}
        # Later requests may consume the earlier metric's freshness budget.
        delivered_at = self.state.wall_ms()
        observations = {name: obs.assess(delivered_at) for name, obs in observations.items()}
        current = observations[ds.Metric.CURRENT_FUNDING.value]
        settled = observations[ds.Metric.SETTLED_FUNDING.value]
        quality_checks = []
        def cross_check(status: ds.Status, reason: str) -> None:
            quality_checks.append({"status": status.value, "quality_reason": reason,
                "assessed_at": delivered_at, "available_at": None,
                "availability_provenance": ds.HISTORICAL_PROVENANCE, "replay_eligible": False,
                "dependencies": [{"metric": obs.metric.value, "event_time": obs.event_time,
                    "fetched_at": obs.fetched_at, "available_at": obs.available_at,
                    "availability_provenance": obs.availability_provenance} for obs in (current, settled)]})
        if (current.status != ds.Status.UNAVAILABLE and settled.status != ds.Status.UNAVAILABLE
                and settled.event_time > current.event_time):
            observations[ds.Metric.CURRENT_FUNDING.value] = self._unavailable(
                symbol, ds.Metric.CURRENT_FUNDING, "settled_funding_after_current_context", current.fetched_at)
            cross_check(ds.Status.UNAVAILABLE, "settled_funding_after_current_context")
        elif current.status != ds.Status.UNAVAILABLE and settled.status != ds.Status.UNAVAILABLE:
            expected_gap = (current.value.interval.hours * ds.SECONDS_PER_HOUR * ds.MS_PER_SECOND
                            * ds.SETTLED_TO_NEXT_EXPECTED_INTERVALS)
            if current.value.next_funding_time - settled.event_time != expected_gap:
                cross_check(ds.Status.SUSPECT, "funding_settlement_schedule_mismatch")
        statuses = [obs.status for obs in observations.values()] + [ds.Status(item["status"]) for item in quality_checks]
        status = ds.Status.UNAVAILABLE if all(item == ds.Status.UNAVAILABLE for item in statuses) else (
            ds.Status.AVAILABLE if all(item == ds.Status.AVAILABLE for item in statuses) else ds.Status.SUSPECT)
        return {"status": status.value, "provider": ds.PROVIDER, "schema_version": ds.SCHEMA_VERSION,
                "assessed_at": delivered_at, "replay_eligible": False, "quality_checks": quality_checks,
                "confidence_adjustment": "none" if status == ds.Status.AVAILABLE else "reduce",
                "may_support_primary_trade_thesis": False,
                "strict_ab_adoption_gate": ds.strict_ab_adoption_gate(observations.values()),
                "observations": {name: obs.to_record() for name, obs in observations.items()}}


class BinanceFuturesPublicInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["snapshot", "history"] = "snapshot"
    symbol: str = Field(..., description="One USD-M symbol, e.g. ETHUSDT.")
    metric: ds.Metric | None = None
    period: str = ds.DEFAULT_PERIOD
    limit: int = ds.DEFAULT_HISTORY_LIMIT
    start_time: int | None = None
    end_time: int | None = None


class BinanceFuturesPublicTool(BaseTool):
    name: str = "Binance Futures Public Advisory Data"
    description: str = (
        "Public GET-only USD-M funding context, settled funding, interval metadata, OI and global L/S. "
        "Use snapshot for one symbol; history requires a historical metric. Values are advisory only. "
        "Unavailable means no value: reduce confidence, never invent or base a primary trade thesis on it. "
        "Suspect is limited advisory evidence with reduced confidence. Historical API data is exploratory "
        "only and cannot enter strict replay or feature-adoption evidence. "
        f"Strict A/B adoption gate: {ds.STRICT_AB_ADOPTION_STATUS}."
    )
    args_schema: Type[BaseModel] = BinanceFuturesPublicInput

    def _run(self, symbol: str, action: str = "snapshot", metric: ds.Metric | str | None = None,
             period: str = ds.DEFAULT_PERIOD, limit: int = ds.DEFAULT_HISTORY_LIMIT,
             start_time: int | None = None, end_time: int | None = None) -> str:
        try:
            client = BinanceFuturesPublicClient()
            if action == "snapshot":
                return json.dumps(client.snapshot(symbol), allow_nan=False)
            metric = ds.Metric(metric)
            if action != "history" or metric not in ds.HISTORICAL_METRICS:
                raise ValueError("invalid_action")
            observations = client.observations(metric, symbol, period=period, limit=limit,
                                               start_time=start_time, end_time=end_time, exploratory=True)
            return json.dumps({"status": "unavailable" if all(obs.status == ds.Status.UNAVAILABLE for obs in observations) else "available",
                               "usage": "exploratory_only", "query_reference_time": end_time,
                               "freshness_context": "historical_range_no_live_freshness",
                               "confidence_adjustment": "reduce" if any(obs.status != ds.Status.AVAILABLE for obs in observations) else "none",
                               "may_support_primary_trade_thesis": False, "replay_eligible": False,
                               "strict_ab_adoption_gate": ds.strict_ab_adoption_gate(observations),
                               "observations": [obs.to_record() for obs in observations]}, allow_nan=False)
        except (ValueError, TypeError):
            return json.dumps({"status": "unavailable", "value": None, "quality_reason": "invalid_query",
                               "confidence_adjustment": "reduce", "replay_eligible": False,
                               "strict_ab_adoption_gate": ds.strict_ab_adoption_gate()})
        except Exception:
            return json.dumps({"status": "unavailable", "value": None, "quality_reason": "internal_error",
                               "confidence_adjustment": "reduce", "replay_eligible": False,
                               "strict_ab_adoption_gate": ds.strict_ab_adoption_gate()})
