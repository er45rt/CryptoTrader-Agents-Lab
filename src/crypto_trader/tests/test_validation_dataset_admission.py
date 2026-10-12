"""Causal admission must remain unreachable: synthetic descriptive evidence only."""
from __future__ import annotations
import hashlib
import base64
import json
from pathlib import Path
import pytest
from crypto_trader.validation.dataset_contracts import (
    DatasetManifest, SegmentSpec, IntegrityStatus, CausalStatus, DatasetError,
)
from crypto_trader.validation.dataset_admission import inspect_dataset, snapshot_revisions
from crypto_trader.validation.darc_frames import DarcError
from test_validation_darc_frames import wire, record

SID="test-session"
BASE=1791600000000
METRIC="current_open_interest"

def sample(tmp_path, *, changes=(), with_stop=True, with_body=True,
           receipt_offset_ms=0, with_outcome=True, with_heartbeat=True,
           status_float=False, body_bytes=None, rtt_outcome_override=None,
           duplicate_outcome=False, cumulative_drift=False,
           monotonic_shift=0, null_second_outcome=False, rtt_all_ms=1):
    events=[record("bootstrap",wall_ms=BASE,monotonic_ns=1),
        record("collector_start",wall_ms=BASE,monotonic_ns=2,
               symbol_list=["ETHUSDT"], cadences={METRIC:45.0})]
    body = body_bytes if body_bytes is not None else json.dumps({
        "symbol":"ETHUSDT","openInterest":"10","time":BASE+100,
    },separators=(",",":")).encode("utf-8")
    response = {
        "response_id": "receipt-1", "http_status": 200.0 if status_float else 200,
        "outcome": "response_received",
        "fetched_at": BASE+100+receipt_offset_ms,
        "raw_body_base64": base64.b64encode(body).decode("ascii"), "raw_body_size": len(body),
        "raw_body_encoding": "base64_of_requests_decoded_content",
        "http_date": "Sat, 10 Oct 2026 02:40:00 GMT", "rtt_ms": rtt_all_ms,
        "clock_window_ms": [BASE-2000, BASE+3100],
        "clock_quality": "http_date_window_consistent", "capture_reason": None,
    }
    if not with_body:
        response.pop("raw_body_base64")
    outcome_response = dict(response)
    if rtt_outcome_override is not None:
        outcome_response["rtt_ms"] = rtt_outcome_override
    traces = [record("before_transport",wall_ms=BASE+20,monotonic_ns=20000003,
               attempt_id="attempt-1",attempt=1,metric=METRIC,
               transport_invoked=True,parameters={},phase="before_transport"),
        record("after_outcome",wall_ms=BASE+100,monotonic_ns=99000003,
               attempt_id="attempt-1",attempt=1,metric=METRIC,
               transport_invoked=True,parameters={},phase="after_outcome",
               response=outcome_response),
        record("raw_observation",wall_ms=BASE+100,monotonic_ns=100000003,
               provider="binance_usdm_public",symbol="ETHUSDT",metric=METRIC,
               delivery_source="network_owner",available_at=BASE+100+receipt_offset_ms,
               availability_provenance="collector_receipt",raw_snapshot_only=True,
               **response),
        record("heartbeat",wall_ms=BASE+110,monotonic_ns=110000003,
               health="observing",last_successful_fetched_at={METRIC:BASE+100},
               recent_failure=None,recent_gap=None),
    ]
    if not with_outcome:
        traces=[x for x in traces if x["record_type"] != "after_outcome"]
    if not with_heartbeat:
        traces=[x for x in traces if x["record_type"] != "heartbeat"]
    events.extend(traces)
    if duplicate_outcome:
        events.extend([
            record("before_transport",wall_ms=BASE+150,monotonic_ns=150000003,
                   attempt_id="attempt-2",attempt=2,metric=METRIC,
                   transport_invoked=True,parameters={},phase="before_transport"),
            record("after_outcome",wall_ms=BASE+160,monotonic_ns=160000003,
                   attempt_id="attempt-2",attempt=2,metric=METRIC,
                   transport_invoked=True,parameters={},phase="after_outcome",
                   response={**response,"raw_body_base64":"W10=","raw_body_size":2}),
        ])
    if null_second_outcome:
        events.extend([
            record("before_transport",wall_ms=BASE+150,monotonic_ns=150000003,
                   attempt_id="attempt-other",attempt=2,metric=METRIC,
                   transport_invoked=True,parameters={},phase="before_transport"),
            record("after_outcome",wall_ms=BASE+160,monotonic_ns=160000003,
                   attempt_id="attempt-other",attempt=2,metric=METRIC,
                   transport_invoked=True,parameters={},phase="after_outcome",response=None),
        ])
    if cumulative_drift:
        for i in range(1,4):
            events.append(record("heartbeat",wall_ms=BASE+110+i,
                monotonic_ns=110000003+i*1000000000,health="observing"))
    events+=list(changes)
    if with_stop:
        events.append(record("clean_stop",wall_ms=BASE+200,
            monotonic_ns=3200000000 if cumulative_drift else 200000004))
    if monotonic_shift:
        for item in events:
            item["monotonic_ns"] += monotonic_shift
    data=b"".join(wire(x) for x in events)
    f=tmp_path/"one.darc"
    f.write_bytes(data)
    manifest=DatasetManifest(
        provider="binance_usdm_public",symbol="ETHUSDT",metrics=(METRIC,),
        coverage_start_ms=BASE+100,coverage_end_ms=BASE+100,
        segments=(SegmentSpec(file_name=f.name,size_bytes=len(data),
            sha256_hex=hashlib.sha256(data).hexdigest(),session_id=SID,
            record_count=len(events)),),
        cadence_ms_by_metric={METRIC:45000}, max_gap_ms_by_metric={METRIC:45000},
    )
    return manifest

def test_clean_archive_is_consistent_but_never_eligible(tmp_path):
    m=sample(tmp_path)
    report=inspect_dataset(tmp_path,m)
    assert report.integrity_status==IntegrityStatus.ARCHIVE_CONSISTENT_UNANCHORED
    assert report.causal_status==CausalStatus.NOT_CAUSALLY_ELIGIBLE
    assert report.permits_causal_use is False
    assert "anchor_missing" in report.reasons
    assert len(report.evidence_digest)==64
    assert report.observation_count==1

@pytest.mark.parametrize("kind",["failed_poll","collection_gap","noncausal_delivery","heartbeat"])
def test_gap_failure_or_degraded_heartbeat_blocks_archive(tmp_path,kind):
    x=record(kind,monotonic_ns=150000000,wall_ms=BASE+150,
        health="degraded" if kind=="heartbeat" else None,reason="synthetic_gap")
    manifest=sample(tmp_path,changes=(x,))
    report=inspect_dataset(tmp_path,manifest)
    assert report.integrity_status==IntegrityStatus.REFUSED
    assert report.permits_causal_use is False
    assert "anchor_missing" in report.reasons

def test_missing_clean_stop_and_duplicate_response_id(tmp_path):
    m=sample(tmp_path,with_stop=False)
    assert inspect_dataset(tmp_path,m).integrity_status==IntegrityStatus.REFUSED
    repeat=record("raw_observation",wall_ms=BASE+150,monotonic_ns=150000003,
        provider="binance_usdm_public",symbol="ETHUSDT",metric=METRIC,
        response_id="receipt-1",delivery_source="network_owner",http_status=200,
        clock_quality="http_date_window_consistent",fetched_at=BASE+150,
        available_at=BASE+150,availability_provenance="collector_receipt",raw_snapshot_only=True)
    m=sample(tmp_path,changes=(repeat,))
    assert inspect_dataset(tmp_path,m).integrity_status==IntegrityStatus.REFUSED

def test_wrong_or_missing_manifest_segment_refused(tmp_path):
    m=sample(tmp_path)
    (tmp_path/"one.darc").unlink()
    assert inspect_dataset(tmp_path,m).integrity_status==IntegrityStatus.REFUSED

def test_extra_segment_refused_without_autoload(tmp_path):
    m=sample(tmp_path)
    (tmp_path/"unlisted.darc").write_bytes(wire(record("heartbeat")))
    assert inspect_dataset(tmp_path,m).integrity_status==IntegrityStatus.REFUSED

def test_cross_session_monotonic_never_trusted_and_bad_order(tmp_path):
    bad=record("heartbeat",wall_ms=BASE+150,monotonic_ns=1,health="observing")
    m=sample(tmp_path,changes=(bad,))
    r=inspect_dataset(tmp_path,m)
    assert r.integrity_status==IntegrityStatus.REFUSED
    assert "clock_discontinuity" in r.reasons

def test_attestation_fields_unknown_manifest_and_no_eligibility(tmp_path):
    m=sample(tmp_path)
    assert inspect_dataset(tmp_path,m,unexpected_fields={"attestor":"trusted"}).integrity_status==IntegrityStatus.REFUSED
    assert inspect_dataset(tmp_path,m,unexpected_fields={"anything":"x"}).integrity_status==IntegrityStatus.REFUSED
    for report in (
        inspect_dataset(tmp_path,m),
        inspect_dataset(tmp_path,m,unexpected_fields={"expiry":"tomorrow"}),
    ):
        assert report.causal_status==CausalStatus.NOT_CAUSALLY_ELIGIBLE
        assert report.permits_causal_use is False

def test_report_identity_binds_decision_policy(tmp_path):
    m=sample(tmp_path)
    x=inspect_dataset(tmp_path,m,requested_decision_ms=BASE+201)
    y=inspect_dataset(tmp_path,m,requested_decision_ms=BASE+202)
    assert x.evidence_digest!=y.evidence_digest

def test_snapshot_revisions_never_backdated(tmp_path):
    rows=[
        {"timestamp": BASE-100000,"longShortRatio":"1.1"},
        {"timestamp": BASE-100000,"longShortRatio":"1.2"},
    ]
    revisions=snapshot_revisions((
        (BASE+100, [rows[0]]),
        (BASE+200, [rows[1]]),
    ),provider="binance_usdm_public",symbol="ETHUSDT",metric="global_long_short_account_ratio",period="5m")
    assert len(revisions)==2
    assert revisions[0].first_seen_ms==BASE+100
    assert revisions[1].first_seen_ms==BASE+200
    assert revisions[0].row_event_ms==BASE-100000
    assert all(x.permits_causal_use is False for x in revisions)


def test_invalid_residual_backdated_receipt_and_missing_body_are_refused(tmp_path):
    for bad_residual in ("bad", [], {"a":1}):
        r=record("heartbeat",wall_ms=BASE+150,monotonic_ns=150000003,
                 health="observing",wall_step_residual_ms=bad_residual)
        m=sample(tmp_path,changes=(r,))
        assert inspect_dataset(tmp_path,m).integrity_status==IntegrityStatus.REFUSED
    m=sample(tmp_path,with_body=False)
    assert inspect_dataset(tmp_path,m).integrity_status==IntegrityStatus.REFUSED

def test_production_row_revision_flag_must_remain_false():
    from crypto_trader.validation.dataset_contracts import RowRevision
    with pytest.raises(DatasetError):
        RowRevision(row_event_ms=BASE,first_seen_ms=BASE+1,
            row_value_sha256="a"*64,provider="binance_usdm_public",
            symbol="ETHUSDT",metric="global_long_short_account_ratio",
            period="5m", permits_causal_use=True)

def test_aba_revision_appearing_again_gets_new_receipt():
    a={"timestamp":BASE-300,"longShortRatio":"1.1"}
    b={"timestamp":BASE-300,"longShortRatio":"1.2"}
    r=snapshot_revisions(((BASE+100,[a]),(BASE+200,[b]),(BASE+300,[a])),
        provider="binance_usdm_public",symbol="ETHUSDT",
        metric="global_long_short_account_ratio",period="5m")
    assert [x.first_seen_ms for x in r]==[BASE+100,BASE+200,BASE+300]


@pytest.mark.parametrize("options", [
    {"receipt_offset_ms":-5_000_000},
    {"with_outcome":False},
    {"with_heartbeat":False},
    {"status_float":True},
])
def test_untrusted_receipt_trace_and_clock_fail_closed(tmp_path,options):
    manifest=sample(tmp_path,**options)
    report=inspect_dataset(tmp_path,manifest)
    assert report.integrity_status==IntegrityStatus.REFUSED,options
    assert report.permits_causal_use is False


def test_receipt_revised_after_snapshot_never_backdated(tmp_path):
    revisions=snapshot_revisions((
        (BASE+100,[{"timestamp":BASE-600_000,"longShortRatio":"1.0"}]),
        (BASE+200,[{"timestamp":BASE-600_000,"longShortRatio":"1.1"}]),
        (BASE+300,[{"timestamp":BASE-600_000,"longShortRatio":"1.0"}]),
    ),provider="binance_usdm_public",symbol="ETHUSDT",
      metric="global_long_short_account_ratio",period="5m")
    assert [rev.first_seen_ms for rev in revisions]==[BASE+100,BASE+200,BASE+300]
    assert all(not rev.permits_causal_use for rev in revisions)


def test_unsupported_manifest_has_no_causal_status_escalation(tmp_path):
    report=inspect_dataset(tmp_path,sample(tmp_path),unexpected_fields={"signature":"fake"})
    assert report.integrity_status==IntegrityStatus.REFUSED
    assert report.causal_status==CausalStatus.NOT_CAUSALLY_ELIGIBLE
    assert report.permits_causal_use is False


@pytest.mark.parametrize("case, expected", [
    ("duplicate_outcome","raw_observation_provenance_untrusted"),
    ("rtt_mismatch","raw_observation_provenance_untrusted"),
    ("rtt_invalid","raw_observation_provenance_untrusted"),
    ("btc_body","raw_observation_provenance_untrusted"),
    ("nonfinite_body","raw_observation_provenance_untrusted"),
    ("cumulative_drift","clock_discontinuity"),
])
def test_reviewer_consistency_witnesses(tmp_path,case,expected):
    options={
        "duplicate_outcome":{"duplicate_outcome":True},
        "rtt_mismatch":{"rtt_outcome_override":60000},
        "rtt_invalid":{"rtt_outcome_override":"invalid"},
        "btc_body":{"body_bytes":b'{"symbol":"BTCUSDT","openInterest":"10"}'},
        "nonfinite_body":{"body_bytes":b'{"symbol":"ETHUSDT","openInterest":"10","bad":1e9999}'},
        "cumulative_drift":{"cumulative_drift":True},
    }[case]
    report=inspect_dataset(tmp_path,sample(tmp_path,**options))
    assert report.integrity_status==IntegrityStatus.REFUSED,case
    assert expected in report.reasons,(case,report.reasons)
    assert report.permits_causal_use is False


def test_large_embedded_json_array_refused_without_causal_admission(tmp_path):
    # Input is under DARC frame/aggregate wire caps but exceeds node budget.
    body=b'{"symbol":"ETHUSDT","openInterest":"10","values":['+b"0,"*12000+b"0]}"
    report=inspect_dataset(tmp_path,sample(tmp_path,body_bytes=body))
    assert report.integrity_status==IntegrityStatus.REFUSED
    assert "raw_observation_provenance_untrusted" in report.reasons


def test_large_monotonic_origin_cannot_hide_cumulative_clock_drift(tmp_path):
    report=inspect_dataset(
        tmp_path,sample(tmp_path,cumulative_drift=True,monotonic_shift=10**30)
    )
    assert report.integrity_status==IntegrityStatus.REFUSED
    assert "clock_discontinuity" in report.reasons


def test_missing_second_transport_outcome_evidence_is_not_consistent(tmp_path):
    report=inspect_dataset(tmp_path,sample(tmp_path,null_second_outcome=True))
    assert report.integrity_status==IntegrityStatus.REFUSED
    assert "raw_observation_provenance_untrusted" in report.reasons


@pytest.mark.parametrize("metric,payload", [
    ("current_funding_context",{
        "symbol":"ETHUSDT","lastFundingRate":None,"markPrice":None,
        "indexPrice":None,"nextFundingTime":None,
    }),
    ("current_funding_context",{
        "symbol":"ETHUSDT","lastFundingRate":"0.01","markPrice":"0",
        "indexPrice":"1","nextFundingTime":BASE+1000,
    }),
    ("global_long_short_account_ratio",[{
        "symbol":"ETHUSDT","longShortRatio":None,
        "longAccount":"0.4","shortAccount":"0.6","timestamp":BASE,
    }]),
    ("funding_interval",[{"symbol":"ETHUSDT","fundingIntervalHours":"8"}]),
])
def test_invalid_metric_values_fail_closed(metric,payload):
    from crypto_trader.validation.dataset_admission import _validate_response
    body=json.dumps(payload,separators=(",",":")).encode()
    response={
        "response_id":"receipt-synthetic","http_status":200,
        "fetched_at":BASE+100,"raw_body_base64":base64.b64encode(body).decode("ascii"),
        "raw_body_size":len(body),"raw_body_encoding":"base64_of_requests_decoded_content",
        "http_date":"Sat, 10 Oct 2026 02:40:00 GMT",
        "clock_window_ms":[BASE-2000,BASE+3100],
        "clock_quality":"http_date_window_consistent",
        "capture_reason":None,"outcome":"response_received","rtt_ms":1,
    }
    with pytest.raises(DatasetError,match="raw_observation_provenance_untrusted"):
        _validate_response(response,dict(response),metric=metric)


@pytest.mark.parametrize("metric,payload",[
    ("current_funding_context",{
        "symbol":"ETHUSDT","lastFundingRate":"-0.0001",
        "markPrice":"100","indexPrice":"100","nextFundingTime":BASE+1000,
    }),
    ("funding_interval",[{"symbol":"ETHUSDT","fundingIntervalHours":8}]),
    ("global_long_short_account_ratio",[{
        "symbol":"ETHUSDT","longShortRatio":"1.0",
        "longAccount":"0.5","shortAccount":"0.5","timestamp":BASE-1000,
    }]),
])
def test_well_formed_metric_body_only_passes_descriptive_shape(metric,payload):
    from crypto_trader.validation.dataset_admission import _validate_response
    body=json.dumps(payload,separators=(",",":")).encode()
    response={
        "response_id":"receipt-synthetic","http_status":200,
        "fetched_at":BASE+100,"raw_body_base64":base64.b64encode(body).decode("ascii"),
        "raw_body_size":len(body),"raw_body_encoding":"base64_of_requests_decoded_content",
        "http_date":"Sat, 10 Oct 2026 02:40:00 GMT",
        "clock_window_ms":[BASE-2000,BASE+3100],
        "clock_quality":"http_date_window_consistent",
        "capture_reason":None,"outcome":"response_received","rtt_ms":1,
    }
    _validate_response(response,dict(response),metric=metric)


def test_unrecorded_http_failure_cannot_be_hidden_by_success(tmp_path):
    # A second attempted transport with failure evidence but no failed_poll
    # must never be described as a clean collection interval.
    failed=record("after_outcome",wall_ms=BASE+160,monotonic_ns=160000003,
        attempt_id="attempt-fail",attempt=2,metric=METRIC,
        transport_invoked=True,parameters={},phase="after_outcome",
        response={
            "response_id":"receipt-fail","http_status":429,
            "capture_reason":None,
        })
    begun=record("before_transport",wall_ms=BASE+150,monotonic_ns=150000003,
        attempt_id="attempt-fail",attempt=2,metric=METRIC,
        transport_invoked=True,parameters={},phase="before_transport")
    report=inspect_dataset(tmp_path,sample(tmp_path,changes=(begun,failed)))
    assert report.integrity_status==IntegrityStatus.REFUSED
    assert "collection_gap" in report.reasons


def test_second_bootstrap_is_not_a_clean_session(tmp_path):
    repeated=record("bootstrap",wall_ms=BASE+150,monotonic_ns=150000003)
    report=inspect_dataset(tmp_path,sample(tmp_path,changes=(repeated,)))
    assert report.integrity_status==IntegrityStatus.REFUSED


@pytest.mark.parametrize("options",[
    {"receipt_offset_ms":-90},
    {"receipt_offset_ms":5},
    {"rtt_all_ms":60000},
])
def test_transport_interval_ambiguity_fail_closed(tmp_path,options):
    result=inspect_dataset(tmp_path,sample(tmp_path,**options))
    assert result.integrity_status==IntegrityStatus.REFUSED
    assert "raw_observation_provenance_untrusted" in result.reasons
    assert result.permits_causal_use is False
