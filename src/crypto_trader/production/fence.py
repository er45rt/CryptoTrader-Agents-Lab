"""The final safety + writer-fence gate for new risk (Phase 14).

This is the last check before a venue submission, and it is deliberately un-cacheable:
every call re-reads the authoritative journal, the persisted safety state and the current
ownership record. ProductionRuntime holds the ownership operation lock from this check through
submission; a check on its own is only a snapshot, never a concurrency guarantee.

Order of the checks (fail closed at the first failure):

1. the HELD writer token must still be current - an emergency takeover or a competing normal
   writer mints a higher fencing term and raises ``StaleWriterFenceError``;
2. no break-glass epoch may be open;
3. the persisted safety state must be ``normal`` with the kill switch released;
4. the authoritative lease must still be held by this runtime's normal owner.

The gate never submits anything and never mutates state: it only answers "may this writer create
new risk right now?".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from crypto_trader.breakglass import BreakGlassControl, OwnerKind, StaleWriterFenceError, WriterGuard
from crypto_trader.safety import SafetyController, SafetyMode

from .environment import TradingEnvironment
from .errors import ProductionFenceError, ProductionSafetyBlockedError

__all__ = ["FenceCheck", "ProductionFence"]


@dataclass(frozen=True)
class FenceCheck:
    """The evidence of one successful final fence check (secret-free)."""

    checked_at: datetime
    environment: str
    owner_id: str
    owner_kind: str
    fence_term: int
    safety_mode: str
    kill_switch_engaged: bool
    epoch_open: bool
    safety_state_source: str

    def to_record(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.astimezone(timezone.utc).isoformat(),
            "environment": self.environment,
            "owner_id": self.owner_id,
            "owner_kind": self.owner_kind,
            "fence_term": self.fence_term,
            "safety_mode": self.safety_mode,
            "kill_switch_engaged": self.kill_switch_engaged,
            "epoch_open": self.epoch_open,
            "safety_state_source": self.safety_state_source,
        }


class ProductionFence:
    """Re-check the persisted posture and the held writer token on every new-risk decision."""

    def __init__(
        self,
        *,
        control: BreakGlassControl,
        guard: WriterGuard,
        owner_id: str,
        environment: TradingEnvironment,
    ) -> None:
        if not isinstance(control, BreakGlassControl):
            raise ProductionFenceError("a BreakGlassControl is required for the production fence.")
        if not isinstance(guard, WriterGuard):
            raise ProductionFenceError(
                "a WriterGuard is required; the runtime must hold the writer lease before it can "
                "create new risk."
            )
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ProductionFenceError("the production fence requires a non-empty owner id.")
        self._control = control
        self._guard = guard
        self._owner_id = owner_id
        self._environment = environment

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def fence_term(self) -> int:
        return self._guard.fence.term

    def authorize_new_risk(self, *, now: datetime, correlation_id=None) -> FenceCheck:
        """Authorize one new-risk submission, or raise (never returns a cached approval)."""
        del correlation_id  # identity travels on the intent/execution request, not the fence.
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProductionFenceError("the fence requires an aware ``now`` timestamp.")

        # 1. The held token must still be the authoritative lease.
        record = self._guard.require_current()

        # 2. Re-read the persisted posture (journal first, cache cross-checked).
        load = self._control.load_safety_state()
        controller: SafetyController = load.controller

        # 3. No open break-glass epoch.
        epoch = self._control.load_epoch()
        epoch_open = bool(epoch is not None and epoch.is_open)
        if epoch_open:
            raise ProductionSafetyBlockedError(
                "break_glass_epoch_open: an operator break-glass epoch is open, so new risk is "
                "impossible until reconciliation, acknowledgement and an operator restore."
            )

        # 4. The persisted posture must permit new risk, exactly and freshly.
        if controller.kill_switch_engaged:
            raise ProductionSafetyBlockedError(
                "kill_switch_engaged: the persisted safety state has the kill switch engaged."
            )
        if not controller.allow_new_risk() or controller.mode is not SafetyMode.NORMAL:
            raise ProductionSafetyBlockedError(
                f"safety_mode_{controller.mode.value}: new risk is not permitted by the persisted "
                "safety state."
            )

        # 5. The authoritative lease must still be this runtime's normal lease.
        if record.owner_kind is not OwnerKind.NORMAL:
            raise ProductionSafetyBlockedError(
                "the writer lease is held by the emergency owner; a normal writer may not create "
                "new risk."
            )
        if record.owner_id != self._owner_id:
            raise StaleWriterFenceError(
                f"the writer lease is held by {record.owner_id!r}, not by this runtime "
                f"({self._owner_id!r}); this writer is stale."
            )

        return FenceCheck(
            checked_at=now.astimezone(timezone.utc),
            environment=self._environment.value,
            owner_id=record.owner_id,
            owner_kind=record.owner_kind.value,
            fence_term=record.term,
            safety_mode=controller.mode.value,
            kill_switch_engaged=controller.kill_switch_engaged,
            epoch_open=epoch_open,
            safety_state_source=load.source,
        )
