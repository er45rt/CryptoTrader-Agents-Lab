"""Offline DARC integrity/temporal description, NEVER market-data admission.

Independently anchored custody and source publication proof are absent.
Both physical integrity and internal consistency are UNANCHORED descriptions.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from decimal import Decimal, DecimalException
from datetime import datetime, timezone
import json

from .canonical import canonical_bytes, content_hash, CanonicalError
from .darc_frames import DarcError, read_segment, _parse_object
from .dataset_contracts import (
    AdmissionReport, CausalStatus, DatasetError, DatasetManifest, IntegrityStatus,
    POLICY_ID, RowRevision, METRICS, EXPECTED_PROVIDER, EXPECTED_SYMBOL,
)


def _typed_ms(value, description: str) -> int:
    if type(value) is not int or value <= 0:
        raise DatasetError("invalid " + description)
    return value


def _report(manifest, integrity, reasons, *, decision=None, observations=0, encountered=()):
    reasons = tuple(sorted(set(reasons) | {"anchor_missing"}))
    return AdmissionReport(
        integrity_status=integrity, reasons=reasons,
        evidence_digest=content_hash("dataset-stage2-evidence-v1", {
            "policy": POLICY_ID, "manifest_sha256": manifest.fingerprint(),
            "integrity": integrity.value, "causal": "NOT_CAUSALLY_ELIGIBLE",
            "reasons": list(reasons), "requested_decision_ms": decision,
            "observation_count": observations, "verified_segment_digests": list(encountered),
            "permits_causal_use": False,
        }), observation_count=observations,
    )


_B64 = {ch: i for i, ch in enumerate(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)}


def _strict_b64(value, size):
    """Bounded, canonical decoder; no new imports or dynamic import capability."""
    if (not isinstance(value, str) or type(size) is not int
            or size < 2 or size > 1_000_000
            or len(value) % 4 != 0 or len(value) > 1_333_336
            or len(value) != 4 * ((size + 2) // 3)):
        raise DatasetError("raw_observation_provenance_untrusted")
    out = bytearray()
    for idx in range(0, len(value), 4):
        chars = value[idx:idx+4]
        padding = len(chars) - len(chars.rstrip("="))
        if padding > 2 or (padding and idx != len(value)-4):
            raise DatasetError("raw_observation_provenance_untrusted")
        if any(x not in _B64 for x in chars[:4-padding]):
            raise DatasetError("raw_observation_provenance_untrusted")
        n = 0
        for ch in chars[:4-padding]:
            n = (n << 6) | _B64[ch]
        n <<= padding * 6
        if (padding == 1 and n & 0xFF != 0) or (padding == 2 and n & 0xFFFF != 0):
            # Noncanonical unused bits (after shift) require explicit zero.
            raise DatasetError("raw_observation_provenance_untrusted")
        out.append((n >> 16) & 255)
        if padding < 2: out.append((n >> 8) & 255)
        if padding < 1: out.append(n & 255)
        if len(out) > size:
            raise DatasetError("raw_observation_provenance_untrusted")
    if len(out) != size:
        raise DatasetError("raw_observation_provenance_untrusted")
    return bytes(out)


def _bounded_decimal(value, *, allow_negative=False):
    if type(value) is not str or not value or len(value)>80:
        raise DatasetError("raw_observation_provenance_untrusted")
    try:
        number=Decimal(value)
        if not number.is_finite() or abs(number.adjusted())>1000:
            raise DatasetError("raw_observation_provenance_untrusted")
        if not allow_negative and number < 0:
            raise DatasetError("raw_observation_provenance_untrusted")
        return number
    except DecimalException as exc:
        raise DatasetError("raw_observation_provenance_untrusted") from exc


def _validate_response(rec: dict, outcome: dict, *, metric: str):
    """Check internal collector evidence, NOT external provider authenticity."""
    for k in (
        "response_id", "http_status", "fetched_at", "raw_body_base64",
        "raw_body_size", "raw_body_encoding", "http_date",
        "clock_window_ms", "clock_quality", "capture_reason", "outcome", "rtt_ms",
    ):
        if type(rec.get(k)) is not type(outcome.get(k)) or rec.get(k) != outcome.get(k):
            raise DatasetError("raw_observation_provenance_untrusted")
    if (
        type(rec.get("http_status")) is not int or rec["http_status"] != 200
        or rec.get("capture_reason") is not None
        or rec.get("outcome") != "response_received"
        or rec.get("raw_body_encoding") != "base64_of_requests_decoded_content"
        or not isinstance(rec.get("http_date"), str)
        or not (8 <= len(rec["http_date"]) <= 128)
    ):
        raise DatasetError("raw_observation_provenance_untrusted")
    bounds = rec.get("clock_window_ms")
    fetched = _typed_ms(rec.get("fetched_at"),"fetched_at")
    if not (
        isinstance(bounds, list) and len(bounds)==2
        and all(type(v) is int for v in bounds)
        and 0 < bounds[0] <= fetched <= bounds[1]
        and bounds[1] - bounds[0] <= 60_000
    ):
        raise DatasetError("raw_observation_provenance_untrusted")
    rtt = rec.get("rtt_ms")
    if (type(rtt) not in (int, Decimal) or not Decimal(rtt).is_finite()
            or not 0 <= rtt <= 60_000):
        raise DatasetError("raw_observation_provenance_untrusted")
    try:
        date_ms = int(datetime.strptime(
            rec["http_date"], "%a, %d %b %Y %H:%M:%S GMT"
        ).replace(tzinfo=timezone.utc).timestamp() * 1000)
        if abs(date_ms - fetched) > 6_000:
            raise DatasetError("raw_observation_provenance_untrusted")
        body = _strict_b64(rec.get("raw_body_base64"), rec.get("raw_body_size"))
        payload = _parse_object(body, require_record=False)
        if metric == "current_open_interest":
            if not isinstance(payload,dict) or payload.get("symbol")!="ETHUSDT":
                raise DatasetError("raw_observation_provenance_untrusted")
            _bounded_decimal(payload.get("openInterest"))
        elif metric == "current_funding_context":
            if not isinstance(payload,dict) or payload.get("symbol")!="ETHUSDT":
                raise DatasetError("raw_observation_provenance_untrusted")
            _bounded_decimal(payload.get("lastFundingRate"),allow_negative=True)
            if _bounded_decimal(payload.get("markPrice")) <= 0:
                raise DatasetError("raw_observation_provenance_untrusted")
            if _bounded_decimal(payload.get("indexPrice")) <= 0:
                raise DatasetError("raw_observation_provenance_untrusted")
            if type(payload.get("nextFundingTime")) is not int or payload["nextFundingTime"]<=0:
                raise DatasetError("raw_observation_provenance_untrusted")
        elif metric in ("funding_interval","global_long_short_account_ratio"):
            if (not isinstance(payload,list) or not payload
                    or not all(isinstance(row,dict) and row.get("symbol")=="ETHUSDT" for row in payload)):
                raise DatasetError("raw_observation_provenance_untrusted")
            for row in payload:
                if metric=="funding_interval":
                    interval=row.get("fundingIntervalHours")
                    if type(interval) is not int or not 0 < interval <= 24:
                        raise DatasetError("raw_observation_provenance_untrusted")
                else:
                    if _bounded_decimal(row.get("longShortRatio")) <= 0:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    for name in ("longAccount","shortAccount"):
                        if _bounded_decimal(row.get(name)) > 1:
                            raise DatasetError("raw_observation_provenance_untrusted")
                    if type(row.get("timestamp")) is not int or row["timestamp"]<=0:
                        raise DatasetError("raw_observation_provenance_untrusted")
    except (ValueError, UnicodeError, RecursionError, OverflowError, DecimalException, DarcError) as exc:
        raise DatasetError("raw_observation_provenance_untrusted") from exc


def inspect_dataset(archive_root, manifest: DatasetManifest, *,
                    requested_decision_ms=None, unexpected_fields=None) -> AdmissionReport:
    """Reports only unanchored file integrity; NEVER real causal permission."""
    if not isinstance(manifest, DatasetManifest):
        raise DatasetError("typed closed-schema manifest required")
    if requested_decision_ms is not None:
        _typed_ms(requested_decision_ms,"decision timestamp")
    if unexpected_fields is not None and not isinstance(unexpected_fields, dict):
        raise DatasetError("bad unknown-field mapping")
    if unexpected_fields:
        return _report(manifest,IntegrityStatus.REFUSED,("unknown_manifest_field",),decision=requested_decision_ms)

    encountered = []
    by_session = {}
    try:
        root=Path(archive_root)
        if not root.is_absolute() or str(root).startswith(("\\\\","//")) or not root.is_dir() or root.is_symlink():
            raise DatasetError("invalid archive root")
        expected = [s.file_name for s in manifest.segments]
        listed = [p.name for p in root.iterdir() if p.suffix.lower()==".darc"]
        if len(set(listed)) != len(listed) or set(listed)!=set(expected):
            raise DatasetError("missing or extra DARC segments")
        for segment in manifest.segments:
            parsed=read_segment(root/segment.file_name,expected_sha256=segment.sha256_hex,
                                expected_size=segment.size_bytes,max_records=segment.record_count)
            if parsed.record_count!=segment.record_count:
                raise DatasetError("manifest record count mismatch")
            encountered.append(parsed.digest)
            session=by_session.setdefault(segment.session_id, [])
            for rec in parsed.records:
                if rec.get("session_id")!=segment.session_id:
                    raise DatasetError("segment session mismatch")
                session.append(rec)
    except (DarcError, DatasetError, OSError, TypeError, ValueError):
        return _report(manifest,IntegrityStatus.REFUSED,("physical_or_manifest_mismatch",),
                       decision=requested_decision_ms,encountered=encountered)

    observed={}
    responses=set()
    intervals=[]
    try:
        for sid,recs in by_session.items():
            if len(recs)<5 or recs[0]["record_type"]!="bootstrap" or recs[1]["record_type"]!="collector_start" or recs[-1]["record_type"]!="clean_stop":
                raise DatasetError("invalid lifecycle order")
            if (sum(x["record_type"]=="bootstrap" for x in recs)!=1
                    or sum(x["record_type"]=="collector_start" for x in recs)!=1
                    or sum(x["record_type"]=="clean_stop" for x in recs)!=1):
                raise DatasetError("invalid lifecycle order")
            if not any(x["record_type"]=="heartbeat" for x in recs):
                raise DatasetError("collection_gap")
            begin,finish = _typed_ms(recs[1].get("wall_ms"),"start"), _typed_ms(recs[-1].get("wall_ms"),"stop")
            attempts={}
            outcomes={}
            outcome_rids=set()
            successful_outcome_rids=set()
            session_receipts=set()
            baseline_monotonic=_typed_ms(recs[1].get("monotonic_ns"),"collector start monotonic_ns")
            prior_wall=prior_mono=None
            last_receipt=0
            for index, rec in enumerate(recs):
                wall=_typed_ms(rec.get("wall_ms"),"wall_ms")
                mono=_typed_ms(rec.get("monotonic_ns"),"monotonic_ns")
                if prior_mono is not None:
                    if mono <= prior_mono:
                        raise DatasetError("clock_discontinuity")
                    drift_ns=(wall-prior_wall)*1_000_000-(mono-prior_mono)
                    if abs(drift_ns)>manifest.clock_step_tolerance_ms*1_000_000:
                        raise DatasetError("clock_discontinuity")
                if index>=1 and abs((wall-begin)*1000000-(mono-baseline_monotonic))>manifest.clock_step_tolerance_ms*1000000:
                    raise DatasetError("clock_discontinuity")
                prior_wall,prior_mono=wall,mono
                if rec.get("wall_step_alarm",False) is not False:
                    raise DatasetError("clock_discontinuity")
                residual=rec.get("wall_step_residual_ms")
                if residual is not None:
                    if type(residual) not in (int,Decimal) or not Decimal(residual).is_finite():
                        raise DatasetError("clock_discontinuity")
                    if abs(residual)>manifest.clock_step_tolerance_ms:
                        raise DatasetError("clock_discontinuity")
                kind=rec["record_type"]
                if kind in ("failed_poll","collection_gap","noncausal_delivery"):
                    raise DatasetError("collection_gap")
                if kind=="heartbeat":
                    if rec.get("health")!="observing":
                        raise DatasetError("collection_gap")
                    if not begin <= wall <= finish:
                        raise DatasetError("clock_discontinuity")
                if kind=="before_transport":
                    attempt_id=rec.get("attempt_id")
                    if not begin <= wall <= finish or not isinstance(attempt_id,str) or not attempt_id or attempt_id in attempts:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    if rec.get("transport_invoked") is not True:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    attempts[attempt_id]=(rec.get("metric"), True, mono, wall)
                if kind=="after_outcome":
                    attempt_id=rec.get("attempt_id")
                    if attempt_id not in attempts or attempt_id in outcomes or not begin <= wall <= finish:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    if attempts[attempt_id][:2] != (rec.get("metric"),True) or rec.get("transport_invoked") is not True:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    evidence=rec.get("response")
                    if evidence is None:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    if evidence is not None:
                        if not isinstance(evidence,dict) or not isinstance(evidence.get("response_id"),str):
                            raise DatasetError("raw_observation_provenance_untrusted")
                        if (type(evidence.get("http_status")) is not int
                                or evidence["http_status"] != 200
                                or evidence.get("capture_reason") is not None):
                            raise DatasetError("collection_gap")
                        rid=evidence["response_id"]
                        if rid in outcome_rids:
                            raise DatasetError("raw_observation_provenance_untrusted")
                        fetched_evidence=evidence.get("fetched_at")
                        if type(fetched_evidence) is not int or not attempts[attempt_id][3] <= fetched_evidence <= wall:
                            raise DatasetError("raw_observation_provenance_untrusted")
                        rtt_evidence=evidence.get("rtt_ms")
                        if (type(rtt_evidence) not in (int,Decimal)
                                or not Decimal(rtt_evidence).is_finite()
                                or Decimal(rtt_evidence) < 0
                                or Decimal(rtt_evidence)*1000000 > mono-attempts[attempt_id][2]):
                            raise DatasetError("raw_observation_provenance_untrusted")
                        outcome_rids.add(rid)
                        if (type(evidence.get("http_status")) is not int
                                or evidence["http_status"] != 200
                                or evidence.get("capture_reason") is not None):
                            raise DatasetError("collection_gap")
                        successful_outcome_rids.add(rid)
                    outcomes[attempt_id]=evidence
                if kind=="raw_observation":
                    metric=rec.get("metric")
                    if (
                        not begin <= wall <= finish or metric not in manifest.metrics
                        or rec.get("provider")!=manifest.provider or rec.get("symbol")!=manifest.symbol
                        or rec.get("delivery_source")!="network_owner"
                        or rec.get("clock_quality")!="http_date_window_consistent"
                        or rec.get("raw_snapshot_only") is not True
                        or rec.get("availability_provenance")!="collector_receipt"
                    ):
                        raise DatasetError("raw_observation_provenance_untrusted")
                    fetched=_typed_ms(rec.get("fetched_at"),"fetched_at")
                    available=_typed_ms(rec.get("available_at"),"available_at")
                    rid=rec.get("response_id")
                    if (not isinstance(rid,str) or not rid or rid in responses
                            or available!=fetched or available>wall
                            or abs(wall-available)>manifest.clock_step_tolerance_ms
                            or not begin <= available <= finish or available <= last_receipt):
                        raise DatasetError("duplicate_or_invalid_receipt")
                    last_receipt=available
                    match=None
                    for attempt_id,evidence in outcomes.items():
                        if isinstance(evidence,dict) and evidence.get("response_id")==rid:
                            if attempts[attempt_id][:2]!=(metric,True):
                                raise DatasetError("raw_observation_provenance_untrusted")
                            match=evidence
                            break
                    if match is None:
                        raise DatasetError("raw_observation_provenance_untrusted")
                    _validate_response(rec,match,metric=metric)
                    responses.add(rid)
                    observed.setdefault(metric,[]).append(available)
            if set(attempts)!=set(outcomes) or not successful_outcome_rids.issubset(responses):
                raise DatasetError("raw_observation_provenance_untrusted")
            intervals.append((begin,finish))
        if len(intervals)>1:
            # Without independent UTC-clock reconciliation, session stitching
            # is descriptive only; do not claim continuous coverage.
            raise DatasetError("overlapping_session_utc_windows")
        count=0
        for metric in manifest.metrics:
            stamps=observed.get(metric,[])
            if not stamps:
                raise DatasetError("missing_metric_observation")
            stamps.sort()
            if stamps[0]>manifest.coverage_start_ms or stamps[-1]<manifest.coverage_end_ms:
                raise DatasetError("incomplete_coverage")
            if any(b<=a or b-a>manifest.max_gap_ms_by_metric[metric]
                   for a,b in zip(stamps,stamps[1:])):
                raise DatasetError("coverage_gap")
            count+=len(stamps)
    except (DatasetError,TypeError,ValueError,KeyError,DecimalException,ArithmeticError) as exc:
        reason=str(exc)
        if reason not in {
            "clock_discontinuity","collection_gap","raw_observation_provenance_untrusted",
            "duplicate_or_invalid_receipt","missing_metric_observation",
            "incomplete_coverage","coverage_gap","overlapping_session_utc_windows",
            "invalid lifecycle order",
        }:
            reason="invalid_archive_timeline"
        return _report(manifest,IntegrityStatus.REFUSED,(reason,),decision=requested_decision_ms,
                       observations=len(responses),encountered=encountered)
    return _report(manifest,IntegrityStatus.ARCHIVE_CONSISTENT_UNANCHORED,(),
                   decision=requested_decision_ms,observations=count,encountered=encountered)


def snapshot_revisions(snapshots, *, provider, symbol, metric, period):
    """Each value TRANSITION receives its own first-seen snapshot time, including A-B-A."""
    if provider!=EXPECTED_PROVIDER or symbol!=EXPECTED_SYMBOL or metric not in METRICS:
        raise DatasetError("unsupported revision source")
    if not isinstance(period,str) or not period or len(period)>64:
        raise DatasetError("invalid revision period")
    current={}
    out=[]
    prior=0
    for received,rows in snapshots:
        _typed_ms(received,"snapshot receipt")
        if received<=prior:
            raise DatasetError("nonmonotone snapshot receipt")
        prior=received
        if not isinstance(rows,(list,tuple)) or len(rows)>2048:
            raise DatasetError("invalid revision rows")
        for row in rows:
            if not isinstance(row,dict) or type(row.get("timestamp")) is not int:
                raise DatasetError("invalid historical row")
            timestamp=_typed_ms(row["timestamp"],"historical event")
            try:
                digest=sha256(canonical_bytes(row)).hexdigest()
            except (CanonicalError,TypeError,ValueError) as exc:
                raise DatasetError("invalid revision value") from exc
            if current.get(timestamp)!=digest:
                current[timestamp]=digest
                out.append(RowRevision(
                    row_event_ms=timestamp,first_seen_ms=received,row_value_sha256=digest,
                    provider=provider,symbol=symbol,metric=metric,period=period,
                ))
    return tuple(out)
