"""Stage-2 descriptive dataset contracts; never authorizes causal use or trading.

No trust-verifier or production test attestation hook exists in this package.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from .canonical import canonical_bytes, content_hash, sha256_text, _nfc

POLICY_ID = "offline-darc-descriptive-v1"
SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 2_097_152
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 32
MAX_RECORDS = 2_048
MAX_DEPTH = 32
MAX_KEYS = 2048
MAX_STRING_CHARS = 2_097_152
MAX_NUMBER_DIGITS = 80
MAX_HEADER_BODY = MAX_FRAME_BYTES + 48 + 8
EXPECTED_PROVIDER = "binance_usdm_public"
EXPECTED_SYMBOL = "ETHUSDT"
METRICS = frozenset({
    "current_open_interest", "current_funding_context",
    "funding_interval", "global_long_short_account_ratio",
})
RECORD_TYPES = frozenset({
    "bootstrap", "collector_start", "before_transport", "after_outcome",
    "raw_observation", "failed_poll", "collection_gap", "heartbeat",
    "clean_stop", "noncausal_delivery",
})


class DatasetError(ValueError):
    """Invalid untrusted dataset or invalid closed-schema contract."""


class IntegrityStatus(str, Enum):
    REFUSED = "REFUSED"
    FRAME_VALID_UNANCHORED = "FRAME_VALID_UNANCHORED"
    ARCHIVE_CONSISTENT_UNANCHORED = "ARCHIVE_CONSISTENT_UNANCHORED"


class CausalStatus(str, Enum):
    NOT_CAUSALLY_ELIGIBLE = "NOT_CAUSALLY_ELIGIBLE"


def _positive_int(n, name: str) -> int:
    if type(n) is not int or n <= 0:
        raise DatasetError("invalid " + name)
    return n


def _field_text(text: str, name: str, *, max_len=256) -> str:
    try:
        _nfc(text)
        if not 0 < len(text) <= max_len or any(ord(c) < 32 for c in text):
            raise DatasetError("invalid " + name)
    except (TypeError, ValueError) as exc:
        raise DatasetError("invalid " + name) from exc
    return text


@dataclass(frozen=True)
class SegmentSpec:
    file_name: str
    size_bytes: int
    sha256_hex: str
    session_id: str
    record_count: int

    def __post_init__(self):
        _field_text(self.file_name, "segment name")
        if (
            self.file_name != self.file_name.split("/")[-1]
            or "\\" in self.file_name
            or not self.file_name.endswith(".darc")
            or self.file_name.startswith(".")
        ):
            raise DatasetError("segment must be an explicit plain .darc basename")
        _field_text(self.session_id, "session_id")
        _positive_int(self.size_bytes, "segment byte length")
        if self.size_bytes > MAX_ARCHIVE_BYTES:
            raise DatasetError("segment byte budget exceeded")
        _positive_int(self.record_count, "segment record count")
        if self.record_count > MAX_RECORDS:
            raise DatasetError("segment record budget exceeded")
        try:
            sha256_text(self.sha256_hex)
        except ValueError as exc:
            raise DatasetError("invalid segment SHA256") from exc


@dataclass(frozen=True)
class DatasetManifest:
    provider: str
    symbol: str
    metrics: tuple[str, ...]
    coverage_start_ms: int
    coverage_end_ms: int
    segments: tuple[SegmentSpec, ...]
    cadence_ms_by_metric: Mapping[str, int]
    max_gap_ms_by_metric: Mapping[str, int]
    clock_step_tolerance_ms: int = 2_000
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION or type(self.schema_version) is not int:
            raise DatasetError("unsupported manifest version")
        if self.provider != EXPECTED_PROVIDER or self.symbol != EXPECTED_SYMBOL:
            raise DatasetError("unknown provider or symbol")
        if (type(self.metrics) is not tuple or not self.metrics
                or len(set(self.metrics)) != len(self.metrics) or not set(self.metrics) <= METRICS):
            raise DatasetError("unsupported/duplicate metrics")
        for field_name, value in (("coverage_start_ms", self.coverage_start_ms),
                                  ("coverage_end_ms", self.coverage_end_ms)):
            _positive_int(value, field_name)
        if self.coverage_start_ms > self.coverage_end_ms:
            raise DatasetError("reversed coverage interval")
        if (type(self.segments) is not tuple or not self.segments
                or len(self.segments) > MAX_SEGMENTS or any(not isinstance(x, SegmentSpec) for x in self.segments)):
            raise DatasetError("invalid segment list")
        names = [x.file_name for x in self.segments]
        if len(set(names)) != len(names):
            raise DatasetError("duplicate segments")
        if sum(x.size_bytes for x in self.segments) > MAX_ARCHIVE_BYTES:
            raise DatasetError("archive byte budget exceeded")
        if sum(x.record_count for x in self.segments) > MAX_RECORDS:
            raise DatasetError("archive record budget exceeded")
        if type(self.clock_step_tolerance_ms) is not int or not 0 <= self.clock_step_tolerance_ms <= 60_000:
            raise DatasetError("invalid clock-step tolerance")
        for field_name, raw in (("cadence_ms_by_metric", self.cadence_ms_by_metric),
                                ("max_gap_ms_by_metric", self.max_gap_ms_by_metric)):
            if not isinstance(raw, Mapping) or set(raw) != set(self.metrics):
                raise DatasetError("exact metric cadence and max gap coverage required")
            for metric, ms in raw.items():
                if metric not in METRICS:
                    raise DatasetError("unknown metric")
                _positive_int(ms, field_name)
                if ms > 86_400_000:
                    raise DatasetError("unsupported cadence or gap limit")
            object.__setattr__(self, field_name, MappingProxyType(dict(raw)))

    def fingerprint(self) -> str:
        return content_hash("darc-dataset-manifest-v1", {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "symbol": self.symbol,
            "metrics": list(self.metrics),
            "coverage_start_ms": self.coverage_start_ms,
            "coverage_end_ms": self.coverage_end_ms,
            "clock_step_tolerance_ms": self.clock_step_tolerance_ms,
            "cadence_ms_by_metric": dict(self.cadence_ms_by_metric),
            "max_gap_ms_by_metric": dict(self.max_gap_ms_by_metric),
            "segments": [{
                "file_name": x.file_name, "size_bytes": x.size_bytes,
                "sha256_hex": x.sha256_hex, "session_id": x.session_id,
                "record_count": x.record_count,
            } for x in self.segments],
        })


@dataclass(frozen=True)
class AdmissionReport:
    integrity_status: IntegrityStatus
    reasons: tuple[str, ...]
    evidence_digest: str
    observation_count: int = 0
    causal_status: CausalStatus = CausalStatus.NOT_CAUSALLY_ELIGIBLE
    permits_causal_use: bool = False

    def __post_init__(self):
        if not isinstance(self.integrity_status, IntegrityStatus):
            raise DatasetError("unknown integrity status")
        if self.causal_status is not CausalStatus.NOT_CAUSALLY_ELIGIBLE or self.permits_causal_use is not False:
            raise DatasetError("Stage2 cannot authorize causal use")
        if "anchor_missing" not in self.reasons:
            raise DatasetError("external anchor absent")
        sha256_text(self.evidence_digest)


@dataclass(frozen=True)
class RowRevision:
    row_event_ms: int
    first_seen_ms: int
    row_value_sha256: str
    provider: str
    symbol: str
    metric: str
    period: str
    permits_causal_use: bool = False

    def __post_init__(self):
        if self.permits_causal_use is not False:
            raise DatasetError("Stage2 row revisions cannot authorize causal use")
