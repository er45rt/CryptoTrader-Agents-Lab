"""Phase 1 raw evidence only. Called solely by the dedicated collector process."""
from dataclasses import asdict
import base64
import os
import time
from uuid import uuid4

from crypto_trader.tools import binance_futures_public_tool as adapter
from crypto_trader.tools import derivatives_schema as ds
from .journal import JournalError
from .policy import (
    COLLECTOR_VERSION, RECORD_VERSION, SYMBOL, NS_PER_SECOND, NS_PER_MS,
    HEARTBEAT_SECONDS, WALL_STEP_ALARM_MS, JOURNAL_LATENCY_LIMIT_NS,
    SHUTDOWN_SLICE_SECONDS, EXIT_GAP_FAILED, EXIT_FATAL_WITH_GAP,
)


TRUSTED_CLOCK_QUALITY = "http_date_window_consistent"


class Clock:
    def wall_ms(self):
        return time.time_ns() // NS_PER_MS

    def mono_ns(self):
        return time.monotonic_ns()

    def sleep(self, seconds):
        time.sleep(seconds)


class FatalCollection(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("collection_stopped_without_clean_stop")


def response_evidence(response):
    if response is None:
        return None
    fields = asdict(response)
    body = fields.pop("raw_body")
    fields["raw_body_base64"] = base64.b64encode(body).decode("ascii") if body is not None else None
    fields["raw_body_encoding"] = "base64_of_requests_decoded_content"
    return fields


class Collector:
    def __init__(self, config, journal, *, bootstrap, clock=None,
                 client_factory=None):
        self.config, self.journal = config, journal
        self.budget = config.validate()
        self.bootstrap = bootstrap
        if not bootstrap.get("startup_validated"):
            raise ValueError("startup_not_validated")
        self.clock = clock or Clock()
        self.session_id = journal.session_id
        factory = client_factory or adapter.BinanceFuturesPublicClient
        self.client = factory(state=adapter.SharedPublicState(), observer=self.observe)
        self.stop_requested = False
        self.stop_reason = "requested"
        self.previous_clock = None
        self.before_ids = set()
        self.response_ids = set()
        self.latencies = []
        self.last_success = {}
        self.last_failure = None
        self.last_gap = None
        self.poll_metric = None

    def request_stop(self, reason="signal"):
        self.stop_requested, self.stop_reason = True, reason

    def persist(self, kind, *, check_latency=True, **fields):
        started = self.clock.mono_ns()
        wall, mono = self.clock.wall_ms(), self.clock.mono_ns()
        residual = None
        if self.previous_clock is not None:
            old_wall, old_mono = self.previous_clock
            residual = wall - old_wall - (mono - old_mono) / NS_PER_MS
        record = {
            "record_type": kind, "record_version": RECORD_VERSION,
            "collector_version": COLLECTOR_VERSION, "session_id": self.session_id,
            "process_id": os.getpid(), "wall_ms": wall, "monotonic_ns": mono,
            "monotonic_source": "time.monotonic_ns", "monotonic_unit": "ns",
            "wall_step_residual_ms": residual,
            "wall_step_alarm": residual is not None and abs(residual) > WALL_STEP_ALARM_MS,
            **fields,
        }
        self.journal.append(record)
        self.previous_clock = (wall, mono)
        elapsed = self.clock.mono_ns() - started
        if check_latency and elapsed >= JOURNAL_LATENCY_LIMIT_NS:
            raise JournalError("journal_latency_exceeds_operational_budget")
        return elapsed

    def gap(self, reason, **fields):
        self.persist("collection_gap", reason=reason, metric=self.poll_metric,
                     symbol=SYMBOL, **fields)
        self.last_gap = reason

    def fatal(self, reason, *, inside_observer=False):
        code = EXIT_FATAL_WITH_GAP
        try:
            failed_segment = str(self.journal.path)
            if self.journal.poisoned:
                self.journal.new_segment()
            self.persist("collection_gap", check_latency=False, failed_segment=failed_segment, reason=reason,
                         metric=self.poll_metric, symbol=SYMBOL, fatal=True)
            self.last_gap = reason
        except BaseException:
            code = EXIT_GAP_FAILED
        # Adapter isolates BaseException: an observer must not return after
        # failed/slow durability, which could allow an unjournaled send/retry.
        if inside_observer:
            # Stdio may be closed/reused; never write diagnostics into an
            # unknown fd or block on a pipe. Durable gap and exit are evidence.
            os._exit(code)
        raise FatalCollection(code)

    def observe(self, event):
        try:
            started = self.clock.mono_ns()
            fields = asdict(event)
            fields["response"] = response_evidence(event.response)
            self.persist(event.phase, **fields)
            elapsed = self.clock.mono_ns() - started
            if elapsed >= JOURNAL_LATENCY_LIMIT_NS:
                raise JournalError("observer_latency_exceeds_operational_budget")
            self.latencies.append({"phase": event.phase, "attempt_id": event.attempt_id,
                                   "duration_ns": elapsed})
            if event.phase == "before_transport":
                self.before_ids.add(event.attempt_id)
            elif event.response is not None:
                if event.transport_invoked and event.attempt_id not in self.before_ids:
                    raise JournalError("outcome_without_durable_before_transport")
                self.response_ids.add(event.response.response_id)
        except BaseException:
            self.fatal("observer_journal_failure_or_latency", inside_observer=True)

    def start(self):
        self.persist("bootstrap", bootstrap=self.bootstrap)
        self.persist("collector_start", symbol_list=[SYMBOL],
                     archive_destination=str(self.config.archive.resolve()),
                     cadences={m.value: seconds for m, seconds in self.config.intervals},
                     request_budget=self.budget,
                     host_time_sync_acknowledged=self.config.host_time_sync_acknowledged,
                     prior_segments=[asdict(item) for item in self.journal.previous])
        for item in self.journal.previous:
            if item.status != "complete_valid":
                self.gap("prior_segment_" + item.status, segment=asdict(item))

    def poll(self, metric):
        self.poll_metric = metric.value
        self.before_ids.clear()
        self.response_ids.clear()
        self.latencies.clear()
        delivery = None
        try:
            delivery = self.client.raw_delivery(metric, SYMBOL)
        except Exception:
            self.persist("failed_poll", symbol=SYMBOL, metric=metric.value,
                         reason="adapter_call_exception", decided_at=self.clock.wall_ms())
            self.last_failure = "adapter_call_exception"
        diagnostics = list(self.client.observer_diagnostics)
        self.client.observer_diagnostics.clear()
        if diagnostics:
            self.gap("observer_diagnostics", diagnostics=[asdict(d) for d in diagnostics])
            return
        if delivery is None:
            return
        response = delivery.response
        if delivery.delivery_source in ("cache", "shared_waiter"):
            self.persist("noncausal_delivery", symbol=SYMBOL, metric=metric.value,
                         delivery_source=delivery.delivery_source,
                         original_response_id=delivery.response_id,
                         original_provenance_fetched_at=response.fetched_at if response else None)
            self.gap("scheduled_poll_not_network_owner")
            return
        if delivery.delivery_source == "network_owner":
            events = delivery.provenance.events
            unbound = any(e.transport_invoked and e.attempt_id not in self.before_ids for e in events)
            if unbound or (response is not None and response.response_id not in self.response_ids):
                self.gap("unbound_network_provenance")
                return
        received = (
            delivery.delivery_source == "network_owner" and response is not None
            and response.capture_reason is None and response.http_status == 200
            and ds.valid_timestamp(response.fetched_at) and response.raw_body is not None
            and delivery.result.reason is None and bool(response.response_id)
        )
        # Fail closed: only the exact trusted quality may become a causal observation.
        untrusted_clock = received and response.clock_quality != TRUSTED_CLOCK_QUALITY
        eligible = received and not untrusted_clock
        common = {
            "provider": ds.PROVIDER, "symbol": SYMBOL, "metric": metric.value,
            "request_parameters": dict(delivery.provenance.events[-1].parameters)
                                  if delivery.provenance.events else {},
            "delivery_source": delivery.delivery_source,
            "observer_latencies": list(self.latencies),
        }
        if eligible:
            self.persist("raw_observation", **common, **response_evidence(response),
                         available_at=response.fetched_at,
                         availability_provenance="collector_receipt",
                         raw_snapshot_only=True)
            self.last_success[metric.value] = response.fetched_at
        else:
            reason = "untrusted_clock_quality" if untrusted_clock else (
                delivery.result.reason or delivery.outcome)
            self.persist("failed_poll", **common, reason=reason,
                         decided_at=delivery.decided_at or self.clock.wall_ms(),
                         response=response_evidence(response))
            self.last_failure = reason
            if untrusted_clock:
                self.gap("untrusted_clock_quality", clock_quality=response.clock_quality,
                         response_id=response.response_id)
            elif response is not None and response.capture_reason is not None:
                self.gap("provenance_capture_failed", capture_reason=response.capture_reason)

    def heartbeat(self):
        self.persist("heartbeat", health="gap" if self.last_gap else
                     ("degraded" if self.last_failure else "observing"),
                     last_successful_fetched_at=dict(self.last_success),
                     recent_failure=self.last_failure, recent_gap=self.last_gap)

    def run(self):
        try:
            self.start()
            due = {metric: self.clock.mono_ns() for metric, _ in self.config.intervals}
            heartbeat_due = self.clock.mono_ns() + int(HEARTBEAT_SECONDS * NS_PER_SECOND)
            while not self.stop_requested:
                for metric, seconds in self.config.intervals:
                    if self.stop_requested:
                        break
                    if self.clock.mono_ns() >= due[metric]:
                        self.poll(metric)
                        due[metric] = self.clock.mono_ns() + int(seconds * NS_PER_SECOND)
                if not self.stop_requested and self.clock.mono_ns() >= heartbeat_due:
                    self.heartbeat()
                    heartbeat_due = self.clock.mono_ns() + int(HEARTBEAT_SECONDS * NS_PER_SECOND)
                if not self.stop_requested:
                    self.clock.sleep(SHUTDOWN_SLICE_SECONDS)
            self.persist("clean_stop", shutdown_reason=self.stop_reason)
            return 0
        except FatalCollection as error:
            return error.code
        except Exception:
            try:
                self.fatal("collector_journal_or_lifecycle_failure")
            except FatalCollection as error:
                return error.code
        finally:
            self.journal.close()
