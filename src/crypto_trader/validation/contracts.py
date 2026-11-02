"""Immutable Stage-1 synthetic research schemas; no execution permissions."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .canonical import CanonicalError, canonical_uuid, sha256_text, utc_nanoseconds, _nfc, SERIALIZER_ID

class RegistryError(RuntimeError):
    """Fail closed; callers must never start research after rejection."""

class TrialStatus(str, Enum):
    STARTED = "STARTED"
    REJECTED_PRECHECK = "REJECTED_PRECHECK"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    COMPLETED = "COMPLETED"
    INTERRUPTED_UNRESOLVED = "INTERRUPTED_UNRESOLVED"

class IntegrityStatus(str, Enum):
    UNANCHORED = "UNANCHORED"
    INTEGRITY_FAILED = "INTEGRITY_FAILED"
    VERIFIED_ANCHORED = "VERIFIED_ANCHORED"  # reserved for future external custodian

class ResearchLabel(str, Enum):
    EXPLORATORY = "EXPLORATORY"
    CUTOFF_DECLARED = "CUTOFF_DECLARED"  # not proof of model independence

class HoldoutAuditType(str, Enum):
    ACCESS_REQUESTED = "ACCESS_REQUESTED"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    DISCLOSED = "DISCLOSED"
    RETIRED = "RETIRED"

@dataclass(frozen=True)
class StudySpec:
    study_id: str
    preregistration_hash: str
    n_max: int
    allowed_paths: tuple[str, ...]
    cutoff: str = "UNKNOWN"
    version: int = 1
    serializer_id: str = SERIALIZER_ID

    def __post_init__(self):
        try:
            canonical_uuid(self.study_id)
            sha256_text(self.preregistration_hash)
            if type(self.n_max) is not int or not 1 <= self.n_max <= 1000000:
                raise CanonicalError("invalid study budget")
            if type(self.version) is not int or self.version != 1 or self.serializer_id != SERIALIZER_ID:
                raise CanonicalError("unknown schema or serializer")
            if type(self.allowed_paths) is not tuple or not self.allowed_paths or len(set(self.allowed_paths)) != len(self.allowed_paths):
                raise CanonicalError("invalid allowlist")
            for path in self.allowed_paths:
                _nfc(path)
                if (
                    not path or path.startswith("/") or "\\" in path or ":" in path
                    or any(x in ("", ".", "..", ".env", ".git", "secrets") for x in path.split("/"))
                    or path.lower().endswith((".pem", ".key", ".env"))
                ):
                    raise CanonicalError("unsafe allowlist path")
            _nfc(self.cutoff)
            if self.cutoff != "UNKNOWN":
                utc_nanoseconds(self.cutoff)
        except (CanonicalError, TypeError) as exc:
            raise RegistryError("invalid study specification") from exc

@dataclass(frozen=True)
class TrialReceipt:
    trial_id: str
    study_id: str
    status: str
    seed_hex: str
    research_label: str
    binding_hash: str
    counted_attempt: bool = True

@dataclass(frozen=True)
class Verification:
    status: str
    head_hash: str
    event_count: int
    anchored: bool = False
