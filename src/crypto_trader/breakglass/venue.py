"""Venue-side emergency control (Phase 13): cancel-open only, Spot Testnet only.

This is the narrowest possible venue surface: it can CANCEL open orders and read order state. It
cannot create an order, cannot increase exposure, cannot liquidate and cannot move funds - the
break-glass path has no liquidation capability at all, by construction.

Before any authenticated request the control verifies, deterministically and value-free:

* the endpoint is the documented Binance Spot Testnet host (no other host is reachable);
* the clock is synchronized and inside the receive window;
* the account's withdrawal capability is unavailable/disabled (a live-capable key must fail closed);
* the safety state permits the requested operation.

An unconfirmed write is NEVER retried and never silently treated as success: it becomes
``UNKNOWN_PENDING_RECONCILIATION`` and the epoch escalates to ``recovery_required``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    BinanceSpotTestnetClient,
    BinanceTestnetError,
    TestnetOrderRuntime,
    TestnetVenueStateSource,
    UnsafeTestnetAccountError,
    WithdrawalPolicy,
    assert_no_mainnet_reference,
    is_testnet_host,
    require_testnet_base_url,
)
from crypto_trader.recovery import VenueOrderReport
from crypto_trader.safety import SafetyController

from .errors import BreakGlassConfigError, VenueControlError

__all__ = [
    "BinanceSpotTestnetEmergencyControl",
    "EmergencyPreflight",
    "EmergencyVenuePort",
    "PolicyOnlyVenueControl",
    "VenueControlOutcome",
    "unavailable_outcome",
]

VenueControlDisposition = Literal[
    "not_requested", "confirmed", "ambiguous", "unavailable", "rejected"
]


@dataclass(frozen=True)
class EmergencyPreflight:
    """The value-free result of the mandatory pre-flight checks (no credential material)."""

    venue_label: str
    endpoint_is_testnet: bool
    mainnet_hosts_blocked: bool
    clock_synchronized: bool
    clock_offset_ms: int
    withdrawals_available: bool
    withdrawal_policy_ok: bool
    safety_mode: str
    safety_allows_operation: bool
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return all(
            (
                self.endpoint_is_testnet,
                self.mainnet_hosts_blocked,
                self.clock_synchronized,
                self.withdrawal_policy_ok,
                self.safety_allows_operation,
            )
        )

    def to_record(self) -> dict[str, object]:
        return {
            "venue_label": self.venue_label,
            "endpoint_is_testnet": self.endpoint_is_testnet,
            "mainnet_hosts_blocked": self.mainnet_hosts_blocked,
            "clock_synchronized": self.clock_synchronized,
            "clock_offset_ms": self.clock_offset_ms,
            "withdrawals_available": self.withdrawals_available,
            "withdrawal_policy_ok": self.withdrawal_policy_ok,
            "safety_mode": self.safety_mode,
            "safety_allows_operation": self.safety_allows_operation,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class VenueControlOutcome:
    """The deterministic result of one venue-side emergency control attempt."""

    disposition: VenueControlDisposition
    reason_code: str
    detail: str
    cancelled_order_references: tuple[str, ...] = ()
    preflight: EmergencyPreflight | None = None

    @property
    def ambiguous(self) -> bool:
        return self.disposition == "ambiguous"

    @property
    def confirmed(self) -> bool:
        return self.disposition == "confirmed"

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free observability record."""
        return {
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "cancelled_order_references": list(self.cancelled_order_references),
            "preflight": None if self.preflight is None else self.preflight.to_record(),
        }


@runtime_checkable
class EmergencyVenuePort(Protocol):
    """The only venue surface the break-glass control may use."""

    def cancel_open_orders(
        self, *, correlation_id: UUID, occurred_at: datetime, safety: SafetyController
    ) -> VenueControlOutcome: ...

    def venue_reports(self) -> Sequence[VenueOrderReport]: ...


@dataclass(frozen=True)
class PolicyOnlyVenueControl:
    """The default control when no venue is attached: policy-only, deterministic, no I/O."""

    def cancel_open_orders(
        self, *, correlation_id: UUID, occurred_at: datetime, safety: SafetyController
    ) -> VenueControlOutcome:
        del correlation_id, occurred_at, safety
        return VenueControlOutcome(
            disposition="not_requested",
            reason_code="venue_control_not_attached",
            detail="no venue-side control was attached to this break-glass path; the safety-mode "
            "change was applied and cancelling open orders remains an operator action",
        )

    def venue_reports(self) -> Sequence[VenueOrderReport]:
        return ()


@dataclass
class BinanceSpotTestnetEmergencyControl:
    """Emergency venue control over the authenticated Binance SPOT TESTNET adapter.

    Constructing this class never reads a credential: :meth:`from_environment` is the explicit
    entry point that loads the testnet key pair from the environment (the only supported source),
    and no credential value is ever stored, returned, printed or journaled here.
    """

    client: BinanceSpotTestnetClient
    runtime: TestnetOrderRuntime
    withdrawal_policy: WithdrawalPolicy | None = None
    symbol: str | None = None
    _preflight: EmergencyPreflight | None = field(default=None, init=False)
    _order_references: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.client is None or self.runtime is None:
            raise BreakGlassConfigError("a testnet client and runtime are required.")
        if self.runtime.client is not self.client:
            raise BreakGlassConfigError("the emergency runtime and control must share the same client.")
        runtime_symbol = self.runtime.instrument.symbol
        if self.symbol is None:
            self.symbol = runtime_symbol
        if self.symbol != runtime_symbol or self.client.symbol != runtime_symbol:
            raise BreakGlassConfigError(
                "the emergency client, runtime and control must target the same symbol."
            )
        if self.withdrawal_policy is None:
            self.withdrawal_policy = self.client.withdrawal_policy
        elif self.withdrawal_policy is not self.client.withdrawal_policy:
            raise BreakGlassConfigError(
                "the emergency control withdrawal policy must match the client policy."
            )

    # --- pre-flight -------------------------------------------------------

    def preflight(self, *, safety: SafetyController) -> EmergencyPreflight:
        """Run the mandatory pre-flight checks before any authenticated request."""
        if not isinstance(safety, SafetyController):
            raise BreakGlassConfigError("a SafetyController is required for the pre-flight.")
        base_url = require_testnet_base_url(self.client.base_url)
        endpoint_is_testnet = is_testnet_host(base_url)
        try:
            assert_no_mainnet_reference(base_url)
            mainnet_hosts_blocked = True
        except BinanceTestnetError:
            mainnet_hosts_blocked = False

        # Preserve the production environment proof rather than reimplementing a weaker subset:
        # this forces a fresh symbol-rule read, verifies canTrade, withdrawal policy and clock drift.
        environment = self.client.verify_environment(symbol=self.symbol)
        self.client.clock.require_acceptable_drift()
        account = environment.account
        withdrawals_available = account.withdrawals_available
        policy_ok = (
            not withdrawals_available
            or self.withdrawal_policy is WithdrawalPolicy.RECORD_ONLY
        )
        notes = list(environment.notes)
        notes.append("cancelling open orders is permitted in every safety mode: it cannot increase risk")
        preflight = EmergencyPreflight(
            venue_label=environment.venue_label,
            endpoint_is_testnet=endpoint_is_testnet,
            mainnet_hosts_blocked=mainnet_hosts_blocked,
            clock_synchronized=self.client.clock.is_synchronized,
            clock_offset_ms=environment.clock_offset_ms,
            withdrawals_available=withdrawals_available,
            withdrawal_policy_ok=policy_ok,
            safety_mode=safety.mode.value,
            safety_allows_operation=True,
            notes=tuple(notes),
        )
        self._preflight = preflight
        return preflight

    # --- control ----------------------------------------------------------

    def _remember_failed_cancel(self, exc: BaseException) -> tuple[str, ...]:
        """The venue-confirmed cancels a failed cancel-all completed before it failed.

        Only those are returned, so only those are journaled as cancelled. The order whose cancel
        failed is tracked with them so the venue is asked about it as evidence, but it is never
        recorded as cancelled: its cancel was not confirmed.
        """
        confirmed = tuple(
            dict.fromkeys(
                ref
                for ref in getattr(exc, "cancelled_order_references", ())
                if isinstance(ref, str) and ref.strip()
            )
        )
        unconfirmed = getattr(exc, "unconfirmed_order_reference", None)
        if isinstance(unconfirmed, str) and unconfirmed.strip():
            tracked = (*confirmed, unconfirmed)
        else:
            tracked = confirmed
        self._order_references.extend(
            reference for reference in tracked if reference not in self._order_references
        )
        return confirmed

    def cancel_open_orders(
        self, *, correlation_id: UUID, occurred_at: datetime, safety: SafetyController
    ) -> VenueControlOutcome:
        """Cancel every open order for the instrument. Never retried, never assumed successful."""
        del occurred_at
        self._preflight = None
        try:
            preflight = self.preflight(safety=safety)
        except BinanceTestnetError as exc:
            return VenueControlOutcome(
                disposition="unavailable",
                reason_code="venue_environment_unverified",
                detail=f"the venue environment could not be verified ({type(exc).__name__}); "
                "cancelling open orders could not be attempted",
            )
        try:
            orders = self.runtime.cancel_all()
        except AmbiguousOrderError as exc:
            return VenueControlOutcome(
                disposition="ambiguous",
                reason_code="UNKNOWN_PENDING_RECONCILIATION",
                detail="the cancel outcome is unknown; it was NOT retried and the epoch requires "
                "reconciliation",
                cancelled_order_references=self._remember_failed_cancel(exc),
                preflight=preflight,
            )
        except BinanceTestnetError as exc:
            return VenueControlOutcome(
                disposition="rejected",
                reason_code="venue_cancel_rejected",
                detail=f"the venue rejected the cancel deterministically ({type(exc).__name__}); "
                "reconciliation is still required to confirm the resulting order state",
                cancelled_order_references=self._remember_failed_cancel(exc),
                preflight=preflight,
            )
        references = tuple(order.client_order_id for order in orders)
        self._order_references.extend(
            reference for reference in references if reference not in self._order_references
        )
        del correlation_id
        return VenueControlOutcome(
            disposition="confirmed",
            reason_code="cancel_confirmed",
            detail=f"{len(references)} open order(s) were cancelled through the safety path",
            cancelled_order_references=references,
            preflight=preflight,
        )

    def venue_reports(self) -> Sequence[VenueOrderReport]:
        """The venue view used by Phase 8 reconciliation (evidence, never authority)."""
        source = TestnetVenueStateSource(
            client=self.client, order_references=tuple(self._order_references)
        )
        return source.reports()

    def track_order_references(self, references: Sequence[str]) -> None:
        """Record order references discovered from the journal so reconciliation can query them."""
        for reference in references:
            if reference and reference not in self._order_references:
                self._order_references.append(reference)

def unavailable_outcome(reason_code: str, detail: str) -> VenueControlOutcome:
    """A helper for callers that must report a venue control they could not even attempt."""
    if not reason_code or not detail:
        raise VenueControlError("an unavailable venue outcome requires a code and a detail.")
    return VenueControlOutcome(disposition="unavailable", reason_code=reason_code, detail=detail)
