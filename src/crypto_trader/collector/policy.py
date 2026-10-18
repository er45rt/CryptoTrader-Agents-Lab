"""Phase-1-only cadence and evidence policies; no trading or normalization."""
from dataclasses import dataclass
import math
from pathlib import Path

from crypto_trader.tools import derivatives_schema as ds

COLLECTOR_VERSION = "phase1-1"
RECORD_VERSION = 1
SYMBOL = "ETHUSDT"
NS_PER_SECOND = 1_000_000_000
NS_PER_MS = 1_000_000
# Strictly exceed TTL + 5s; defaults leave 15/30s actual margins. Completion-
# based monotonic deadlines cannot run early or catch up after runtime delays.
MIN_TTL_MARGIN_SECONDS = 5.0
DEFAULT_INTERVALS = (
    (ds.Metric.CURRENT_OI, 45.0),
    (ds.Metric.CURRENT_FUNDING, 45.0),
    (ds.Metric.FUNDING_INTERVAL, 90.0),
    (ds.Metric.GLOBAL_LONG_SHORT, 90.0),
)
HEARTBEAT_SECONDS = 60.0
# 1/8 of the approved 8s waiter budget: storage latency is an operational defect,
# not permission to weaken sync. This deliberately favors safety over uptime.
JOURNAL_LATENCY_LIMIT_NS = int(ds.IN_FLIGHT_WAIT_SECONDS * NS_PER_SECOND / 8)
# Conservative alarm, not proof of NTP: retain every residual for smaller-step
# analysis; whole-ms wall precision and scheduling can cause small discrepancies.
WALL_STEP_ALARM_MS = 2_000
SHUTDOWN_SLICE_SECONDS = 0.1
# 1MiB decoded bodies expand to about 1.4MiB in base64; 2MiB also permits metadata.
MAX_RECORD_BYTES = 2_097_152
EXIT_CONFIGURATION = 64
EXIT_LOCKED = 73
EXIT_GAP_FAILED = 74
EXIT_FATAL_WITH_GAP = 75


@dataclass(frozen=True)
class Config:
    archive: Path
    symbol: str = SYMBOL
    intervals: tuple = DEFAULT_INTERVALS
    host_time_sync_acknowledged: bool = False

    def validate(self, adapter_policy=ds.PollingPolicy()):
        if self.symbol != SYMBOL:
            raise ValueError("phase1_ethusdt_only")
        if not self.host_time_sync_acknowledged:
            raise ValueError("host_time_sync_acknowledgement_required")
        if (len(self.intervals) != len(DEFAULT_INTERVALS)
                or set(m for m, _ in self.intervals) != set(m for m, _ in DEFAULT_INTERVALS)):
            raise ValueError("phase1_metric_scope")
        for metric, interval in self.intervals:
            if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                    or not math.isfinite(interval)
                    or interval <= adapter_policy.ttl(metric) + MIN_TTL_MARGIN_SECONDS):
                raise ValueError("cadence_must_exceed_actual_ttl_and_margin")
        budget = request_budget(self.intervals)
        if budget["steady_rpm"] > ds.MAX_STEADY_STATE_RPM:
            raise ValueError("collector_request_budget_exceeded")
        return budget


def request_budget(intervals):
    rates = [ds.SECONDS_PER_MINUTE / interval for _, interval in intervals]
    steady = sum(rates)
    return {
        "steady_rpm": steady,
        "retry_average_rpm": ds.TOTAL_ATTEMPTS * steady,
        # Half-open 60-second window includes the initial polls; no separate
        # addition of startup would double-count them.
        "worst_case_window_attempts": ds.TOTAL_ATTEMPTS * sum(
            math.ceil(ds.SECONDS_PER_MINUTE / interval) for _, interval in intervals),
        "startup_calls": len(intervals),
        "startup_attempts": ds.TOTAL_ATTEMPTS * len(intervals),
        "headroom_percent": (1 - steady / ds.LOCAL_MAX_RPM) * 100,
    }


def external_destination(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith("\\\\"):
        raise ValueError("absolute_local_external_archive_required")
    resolved = path.resolve()
    if str(resolved).startswith("\\\\"):
        raise ValueError("absolute_local_external_archive_required")
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise ValueError("archive_must_be_outside_git")
    return resolved

