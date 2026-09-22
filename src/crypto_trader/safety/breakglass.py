"""Break-glass foundation: interface + simulated implementation only (Phase 9).

Break-glass is the emergency path that acts during a ``DEGRADED`` / ``RECOVERY_REQUIRED``
posture. Phase 9 implements the architecture and the deterministic posture change ONLY:

* the port is engine-independent and contains no venue connectivity whatsoever;
* the shipped implementation is a simulation that records requests and performs no I/O;
* no Binance mainnet/testnet client, credential, or authenticated call exists anywhere here.

The live break-glass path remains a PRE-LIVE MANDATORY GAP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from .errors import SafetyStateError

__all__ = [
    "BreakGlassAction",
    "BreakGlassPort",
    "BreakGlassRequest",
    "SimulatedBreakGlass",
]


class BreakGlassAction(str, Enum):
    """Emergency actions the break-glass path may request."""

    HALT = "halt"
    CANCEL_ALL = "cancel_all"
    FLATTEN_ALL = "flatten_all"


@dataclass(frozen=True)
class BreakGlassRequest:
    """One recorded emergency-action request."""

    action: BreakGlassAction
    reason_code: str
    reason: str
    request_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.action, BreakGlassAction):
            raise SafetyStateError("a break-glass request requires a BreakGlassAction.")
        if not self.reason_code or not self.reason:
            raise SafetyStateError("a break-glass request requires a reason code and reason.")


@runtime_checkable
class BreakGlassPort(Protocol):
    """The only surface a break-glass implementation may expose."""

    def execute_emergency_action(self, request: BreakGlassRequest) -> str: ...


@dataclass
class SimulatedBreakGlass:
    """A deterministic, offline break-glass implementation used for tests and dry runs."""

    accepted: list[BreakGlassRequest] = field(default_factory=list)

    def execute_emergency_action(self, request: BreakGlassRequest) -> str:
        """Record the request and return a simulated acknowledgement (no venue I/O)."""
        if not isinstance(request, BreakGlassRequest):
            raise SafetyStateError("execute_emergency_action expects a BreakGlassRequest.")
        self.accepted.append(request)
        return f"simulated:{request.action.value}:{request.request_id}"
