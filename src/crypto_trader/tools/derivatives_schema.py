"""Advisory derivatives contract; no execution-domain dependencies.

Times are integer Unix milliseconds. Source event time is not publication time.
Historical API rows are exploratory evidence, never causal replay evidence.
All numeric policies live here; no empirical magnitude/spike cutoff is invented.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from enum import Enum
import math
from typing import Any, Iterable

SCHEMA_VERSION = 1
PROVIDER = "binance_usdm_public"
MS_PER_SECOND = 1000
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
# Distinguishes contemporary provider milliseconds from seconds (provider data
# postdates 2001). The upper bound is the last millisecond of datetime year 9999.
MIN_UNIX_MS = 1_000_000_000_000
MAX_UNIX_MS = 253_402_300_799_999
HTTP_TIMEOUT_SECONDS = 3.0
HTTP_CONNECT_READ_PHASES = 2
TOTAL_ATTEMPTS = 2
RETRY_AFTER_MAX_SECONDS = 2.0
IN_FLIGHT_WAIT_SECONDS = HTTP_TIMEOUT_SECONDS * TOTAL_ATTEMPTS + RETRY_AFTER_MAX_SECONDS
LOCAL_RATE_WINDOW_SECONDS = 60.0
LOCAL_MAX_RPM = 30
MIN_HEADROOM_FRACTION = 0.20
MAX_STEADY_STATE_RPM = LOCAL_MAX_RPM * (1 - MIN_HEADROOM_FRACTION)
# An unknown server cooldown blocks one full local limiter window, without sleep.
UNKNOWN_RATE_LIMIT_COOLDOWN_SECONDS = LOCAL_RATE_WINDOW_SECONDS
CURRENT_OI_TTL_SECONDS = 30.0
CURRENT_FUNDING_TTL_SECONDS = 30.0
LONG_SHORT_TTL_SECONDS = 60.0
HISTORICAL_OI_TTL_SECONDS = 60.0
SETTLED_FUNDING_TTL_SECONDS = 300.0
CLOSED_HISTORY_TTL_SECONDS = 3600.0
FUNDING_INFO_TTL_SECONDS = 60.0
PUBLIC_ENDPOINT_COUNT = 6
# Connect and read each have their own 3s timeout. This is planning arithmetic,
# not a wall deadline: DNS and a trickled body can take longer.
NOMINAL_SNAPSHOT_SECONDS = (PUBLIC_ENDPOINT_COUNT * (
    TOTAL_ATTEMPTS * HTTP_TIMEOUT_SECONDS * HTTP_CONNECT_READ_PHASES
    + RETRY_AFTER_MAX_SECONDS) + IN_FLIGHT_WAIT_SECONDS)
# Negative responses refresh at the shortest default observation cadence.
# Admission includes this cadence so outage recovery cannot silently exceed24RPM.
FAILURE_TTL_SECONDS = CURRENT_OI_TTL_SECONDS
ADMISSION_LEASE_SECONDS = SETTLED_FUNDING_TTL_SECONDS
# Conservative advisory latency budgets from the approved patch specification.
CURRENT_OI_MAX_AGE_SECONDS = 120.0
PERIOD_MAX_AGE_MULTIPLIER = 2
PERIOD_MAX_AGE_GRACE_SECONDS = 60.0
SETTLED_FUNDING_GRACE_SECONDS = 900.0
# A current snapshot may age at most 1/60 of its resolved cycle and at most 120s.
CURRENT_FUNDING_AGE_CYCLE_DIVISOR = 60
CURRENT_FUNDING_MAX_AGE_SECONDS = 120.0
# Round up to complete metadata cycles beyond TTL + nominal snapshot latency.
FUNDING_INFO_MAX_AGE_SECONDS = FUNDING_INFO_TTL_SECONDS * (
    1 + math.ceil(NOMINAL_SNAPSHOT_SECONDS / FUNDING_INFO_TTL_SECONDS))
# Soft invariant: next settlement should be within one current cycle. A schedule
# transition may explain a larger gap; retain it only as suspect advisory evidence.
NEXT_FUNDING_MAX_INTERVAL_MULTIPLIER = 1
# Latest settlement and next settlement normally span one cycle. Metadata changes
# or delayed historical rows can explain a different gap: suspect, never invented.
SETTLED_TO_NEXT_EXPECTED_INTERVALS = 1
# Mathematical rounding bounds, not anomaly thresholds: half a displayed unit.
ROUNDING_HALF_UNIT = Decimal("0.5")
DOCUMENTED_DEFAULT_FUNDING_HOURS = 8
DOCUMENTED_DEFAULT_VERSION = "binance-faq-360033525031-retrieved-2026-10-04"
DOCUMENTED_DEFAULT_URL = "https://www.binance.com/en/support/faq/detail/360033525031"
PERIOD_SECONDS = {
    "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400,
}
DEFAULT_PERIOD = "5m"
DEFAULT_HISTORY_LIMIT = 1
MAX_FUNDING_HISTORY_LIMIT = 1000
MAX_STATISTICS_HISTORY_LIMIT = 500


class Metric(str, Enum):
    CURRENT_FUNDING = "current_funding_context"
    SETTLED_FUNDING = "settled_funding_history"
    FUNDING_INTERVAL = "funding_interval"
    CURRENT_OI = "current_open_interest"
    HISTORICAL_OI = "historical_open_interest"
    GLOBAL_LONG_SHORT = "global_long_short_account_ratio"


class Status(str, Enum):
    AVAILABLE = "available"
    SUSPECT = "suspect"
    UNAVAILABLE = "unavailable"


LIVE_PROVENANCE = "live_fetch"
HISTORICAL_PROVENANCE = "unknown_historical_api"
HISTORICAL_METRICS = frozenset({Metric.SETTLED_FUNDING, Metric.HISTORICAL_OI, Metric.GLOBAL_LONG_SHORT})
# This patch has no verified causal archive or A/B evaluation evidence. Row-level
# replay admissibility never establishes feature adoption or sufficient coverage.
STRICT_AB_ADOPTION_STATUS = "NOT YET ELIGIBLE"
STRICT_AB_ADOPTION_REASON = "causal_archive_and_evaluation_evidence_not_verified"


@dataclass(frozen=True)
class PollingPolicy:
    current_oi: float = CURRENT_OI_TTL_SECONDS
    current_funding: float = CURRENT_FUNDING_TTL_SECONDS
    long_short: float = LONG_SHORT_TTL_SECONDS
    historical_oi: float = HISTORICAL_OI_TTL_SECONDS
    settled_funding: float = SETTLED_FUNDING_TTL_SECONDS
    funding_info: float = FUNDING_INFO_TTL_SECONDS

    def ttl(self, metric: Metric) -> float:
        return {
            Metric.CURRENT_OI: self.current_oi, Metric.CURRENT_FUNDING: self.current_funding,
            Metric.GLOBAL_LONG_SHORT: self.long_short, Metric.HISTORICAL_OI: self.historical_oi,
            Metric.SETTLED_FUNDING: self.settled_funding, Metric.FUNDING_INTERVAL: self.funding_info,
        }[metric]


@dataclass(frozen=True)
class RequestBudget:
    symbols: int
    steady_state_rpm: float
    failure_state_rpm: float
    admission_state_rpm: float
    retry_steady_state_rpm: float
    startup_requests: int
    startup_attempts: int
    worst_case_bounded_rpm: int
    healthy_worst_case_bounded_rpm: int
    headroom_percent: float
    admitted: bool


def request_budget(symbols: int, policy: PollingPolicy = PollingPolicy()) -> RequestBudget:
    if type(symbols) is not int or symbols < 0:
        raise ValueError("invalid_symbol_count")
    ttls = [policy.ttl(metric) for metric in Metric]
    if any(isinstance(ttl, bool) or not math.isfinite(ttl) or ttl <= 0 for ttl in ttls):
        raise ValueError("invalid_polling_policy")
    per_symbol = [policy.ttl(metric) for metric in Metric if metric != Metric.FUNDING_INTERVAL]
    steady = SECONDS_PER_MINUTE * (symbols * sum(1 / ttl for ttl in per_symbol) + 1 / policy.funding_info)
    negative_per_symbol = [min(ttl, FAILURE_TTL_SECONDS) for ttl in per_symbol]
    negative_info = min(policy.funding_info, FAILURE_TTL_SECONDS)
    failure = SECONDS_PER_MINUTE * (symbols * sum(1 / ttl for ttl in negative_per_symbol) + 1 / negative_info)
    admission = max(steady, failure)
    startup = len(per_symbol) * symbols + 1
    windows = symbols * sum(math.ceil(SECONDS_PER_MINUTE / ttl) for ttl in per_symbol)
    windows += math.ceil(SECONDS_PER_MINUTE / policy.funding_info)
    failure_windows = symbols * sum(math.ceil(SECONDS_PER_MINUTE / ttl) for ttl in negative_per_symbol)
    failure_windows += math.ceil(SECONDS_PER_MINUTE / negative_info)
    return RequestBudget(symbols, steady, failure, admission, min(LOCAL_MAX_RPM, TOTAL_ATTEMPTS * steady),
                         startup, TOTAL_ATTEMPTS * startup,
                         min(LOCAL_MAX_RPM, TOTAL_ATTEMPTS * max(windows, failure_windows)),
                         min(LOCAL_MAX_RPM, TOTAL_ATTEMPTS * windows),
                         (1 - steady / LOCAL_MAX_RPM) * 100, admission <= MAX_STEADY_STATE_RPM)


def valid_timestamp(value: Any) -> bool:
    return type(value) is int and MIN_UNIX_MS <= value <= MAX_UNIX_MS


@dataclass(frozen=True)
class FundingInterval:
    hours: int
    interval_source: str
    fetched_at: int
    available_at: int
    default_version: str | None = None

    def valid(self) -> bool:
        return (type(self.hours) is int and self.hours > 0 and valid_timestamp(self.fetched_at)
                and self.available_at == self.fetched_at and valid_timestamp(self.available_at)
                and ((self.interval_source == "fundingInfo" and self.default_version is None)
                     or (self.interval_source == "documented_default"
                         and self.hours == DOCUMENTED_DEFAULT_FUNDING_HOURS
                         and self.default_version == DOCUMENTED_DEFAULT_VERSION)))


@dataclass(frozen=True)
class CurrentFundingContext:
    last_funding_rate: Decimal
    next_funding_time: int
    mark_price: Decimal
    index_price: Decimal
    time: int
    interval: FundingInterval


@dataclass(frozen=True)
class SettledFunding:
    funding_rate: Decimal
    mark_price: Decimal | None = None


@dataclass(frozen=True)
class OpenInterest:
    amount: Decimal
    quote_value: Decimal | None = None


@dataclass(frozen=True)
class LongShortAccounts:
    long_short_ratio: Decimal
    long_account: Decimal
    short_account: Decimal


Value = FundingInterval | CurrentFundingContext | SettledFunding | OpenInterest | LongShortAccounts


def _finite(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _half_unit(value: Decimal) -> Decimal:
    return Decimal(10) ** value.as_tuple().exponent * ROUNDING_HALF_UNIT


def value_invalid_reason(metric: Metric, value: Value | None, event_time: int | None) -> str | None:
    try:
        if metric == Metric.FUNDING_INTERVAL:
            return None if isinstance(value, FundingInterval) and value.valid() else "invalid_funding_interval"
        if metric == Metric.CURRENT_FUNDING:
            if not isinstance(value, CurrentFundingContext) or not value.interval.valid():
                return "invalid_funding_context"
            if not all(_finite(v) for v in (value.last_funding_rate, value.mark_price, value.index_price)):
                return "non_finite_value"
            if value.mark_price <= 0 or value.index_price <= 0:
                return "impossible_price"
            if not valid_timestamp(value.next_funding_time) or value.time != event_time:
                return "invalid_funding_timestamp"
            if value.next_funding_time <= value.time:
                return "funding_next_time_not_after_event"
            return None
        if metric == Metric.SETTLED_FUNDING:
            if not isinstance(value, SettledFunding) or not _finite(value.funding_rate):
                return "invalid_settled_funding"
            if value.mark_price is not None and (not _finite(value.mark_price) or value.mark_price <= 0):
                return "impossible_price"
            return None
        if metric in (Metric.CURRENT_OI, Metric.HISTORICAL_OI):
            if not isinstance(value, OpenInterest) or not _finite(value.amount) or value.amount < 0:
                return "impossible_open_interest"
            if metric == Metric.HISTORICAL_OI and value.quote_value is None:
                return "missing_open_interest_value"
            if value.quote_value is not None and (not _finite(value.quote_value) or value.quote_value < 0):
                return "impossible_open_interest_value"
            return None
        if metric == Metric.GLOBAL_LONG_SHORT:
            if not isinstance(value, LongShortAccounts):
                return "invalid_long_short_accounts"
            ratio, long, short = value.long_short_ratio, value.long_account, value.short_account
            if not all(_finite(v) for v in (ratio, long, short)):
                return "non_finite_value"
            if ratio < 0 or not 0 <= long <= 1 or not 0 < short <= 1:
                return "impossible_long_short_accounts"
            long_error, short_error, ratio_error = map(_half_unit, (long, short, ratio))
            if abs(long + short - 1) > long_error + short_error:
                return "inconsistent_account_shares"
            # Interval arithmetic honors each field's displayed precision; a small
            # rounded short share legitimately permits a large ratio range.
            low = max(Decimal(0), long - long_error) / min(Decimal(1), short + short_error)
            denominator = short - short_error
            high = min(Decimal(1), long + long_error) / denominator if denominator > 0 else Decimal("Infinity")
            if ratio + ratio_error < low or ratio - ratio_error > high:
                return "inconsistent_long_short_ratio"
            return None
    except (ArithmeticError, ValueError, TypeError, OverflowError):
        return "malformed_required_fields"
    return "unknown_metric"


@dataclass(frozen=True)
class Observation:
    symbol: str
    metric: Metric
    event_time: int | None
    available_at: int | None
    fetched_at: int
    availability_provenance: str
    status: Status
    value: Value | None
    quality_reason: str | None
    max_age_seconds: float
    period: str | None = None
    provider: str = PROVIDER
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        reason = self._contract_reason()
        if reason:
            raise ValueError(reason)

    def _contract_reason(self) -> str | None:
        if self.metric not in Metric or self.status not in Status or self.provider != PROVIDER:
            return "invalid_observation_contract"
        if self.metric in HISTORICAL_METRICS:
            if self.available_at is not None or self.availability_provenance != HISTORICAL_PROVENANCE:
                return "historical_availability_must_be_unknown"
        elif (self.status == Status.UNAVAILABLE and self.available_at is None
              and self.availability_provenance == HISTORICAL_PROVENANCE):
            pass  # Explicit unknown availability rejected by the replay boundary.
        elif self.availability_provenance != LIVE_PROVENANCE or self.available_at != self.fetched_at:
            return "live_availability_must_equal_fetch"
        if self.status == Status.UNAVAILABLE and (self.value is not None or not self.quality_reason):
            return "unavailable_must_have_no_value_and_reason"
        return None

    def invalid_reason(self, now: int, *, check_freshness: bool = True) -> str | None:
        reason = self._contract_reason()
        if reason:
            return reason
        if self.status == Status.UNAVAILABLE:
            return self.quality_reason
        if not valid_timestamp(now) or not valid_timestamp(self.fetched_at):
            return "invalid_timestamp"
        if self.metric not in HISTORICAL_METRICS and self.fetched_at > now:
            return "observation_after_decision_time"
        if not math.isfinite(self.max_age_seconds) or self.max_age_seconds <= 0:
            return "invalid_freshness_policy"
        if self.event_time is None:
            if self.metric != Metric.FUNDING_INTERVAL:
                return "missing_event_time"
        elif not valid_timestamp(self.event_time) or self.event_time > self.fetched_at:
            return "invalid_event_time"
        reason = value_invalid_reason(self.metric, self.value, self.event_time)
        if reason:
            return reason
        reference = self.event_time if self.event_time is not None else self.fetched_at
        if reference > now:
            return "observation_after_decision_time"
        # A pre-settlement view is no longer current at its advertised transition.
        # This time invariant has no grace, even when generic age checks are off.
        if self.metric == Metric.CURRENT_FUNDING and self.value.next_funding_time <= now:
            return "stale_funding_context_after_scheduled_time"
        if check_freshness and now - reference > self.max_age_seconds * MS_PER_SECOND:
            return "stale_observation"
        interval = self.value.interval if isinstance(self.value, CurrentFundingContext) else None
        if interval and interval.fetched_at > now:
            return "future_funding_interval"
        if interval and check_freshness and now - interval.fetched_at > FUNDING_INFO_MAX_AGE_SECONDS * MS_PER_SECOND:
            return "stale_funding_interval"
        return None

    @property
    def replay_eligible(self) -> bool:
        # Derived from the contract and its input provenance, never caller supplied.
        return (self.status in (Status.AVAILABLE, Status.SUSPECT)
                and self.metric not in HISTORICAL_METRICS
                and self.availability_provenance == LIVE_PROVENANCE
                and valid_timestamp(self.available_at) and self.available_at == self.fetched_at
                and self._contract_reason() is None
                and value_invalid_reason(self.metric, self.value, self.event_time) is None)

    def assess(self, now: int, *, check_freshness: bool = True) -> Observation:
        reason = self.invalid_reason(now, check_freshness=check_freshness)
        if reason:
            return replace(self, status=Status.UNAVAILABLE, value=None, quality_reason=reason)
        return self

    def to_record(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            return value
        record = encode(asdict(self))
        record["replay_eligible"] = self.replay_eligible
        record["usage"] = "exploratory_only" if self.metric in HISTORICAL_METRICS else "advisory_only"
        record["confidence_adjustment"] = "reduce" if self.status != Status.AVAILABLE else "none"
        record["may_support_primary_trade_thesis"] = False
        record["strict_ab_adoption_status"] = STRICT_AB_ADOPTION_STATUS
        return record


def for_strict_replay(observation: Observation, decision_time: int) -> Observation:
    # Unknown is never coerced to zero or accepted because the event happened early.
    if (observation.available_at is None or observation.availability_provenance != LIVE_PROVENANCE
            or observation.metric in HISTORICAL_METRICS):
        return replace(observation, available_at=None, availability_provenance=HISTORICAL_PROVENANCE,
                       status=Status.UNAVAILABLE, value=None, quality_reason="unknown_replay_availability")
    if (not valid_timestamp(decision_time) or not observation.replay_eligible
            or observation.available_at > decision_time or observation.fetched_at > decision_time):
        return replace(observation, status=Status.UNAVAILABLE, value=None, quality_reason="replay_availability_after_decision_or_invalid")
    if isinstance(observation.value, CurrentFundingContext):
        interval = observation.value.interval
        if interval.available_at > decision_time or interval.fetched_at > decision_time:
            return replace(observation, status=Status.UNAVAILABLE, value=None, quality_reason="replay_dependency_after_decision")
    return observation.assess(decision_time)


def strict_replay(observations: Iterable[Observation], decision_time: int) -> tuple[Observation, ...]:
    checked = (for_strict_replay(item, decision_time) for item in observations)
    return tuple(item for item in checked if item.status in (Status.AVAILABLE, Status.SUSPECT))


def strict_ab_adoption_gate(observations: Iterable[Observation] = ()) -> dict[str, Any]:
    """Always closed in this patch; counts describe rows, not adoption progress.

    Counts are nested: historical_exploratory is a subset of replay-ineligible.
    No duration, caller assertion, or number of live rows verifies an archive or
    meaningful causal A/B evidence. A later separately reviewed patch must do so.
    """
    rows = tuple(observations)
    if any(not isinstance(row, Observation) for row in rows):
        raise TypeError("adoption_gate_requires_observations")
    replay_count = sum(row.replay_eligible for row in rows)
    return {"status": STRICT_AB_ADOPTION_STATUS, "eligible": False,
            "reason": STRICT_AB_ADOPTION_REASON,
            "counts_are_not_adoption_evidence": True,
            "total_observation_count": len(rows),
            "replay_eligible_observation_count": replay_count,
            "replay_ineligible_observation_count": len(rows) - replay_count,
            "historical_exploratory_observation_count": sum(row.metric in HISTORICAL_METRICS for row in rows)}
