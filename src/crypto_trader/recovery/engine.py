"""Deterministic recovery/reconciliation engine (Phase 8).

The engine is a *pure function* of its inputs: the same reconstructed local state and the same
venue reports always produce the same report. It never reads the wall clock, never sends
anything, and never retries a submission.

Decision rules (deterministic, in this order for each local order reference):

1. Submission was never attempted (no ``submission_attempted`` marker recorded before send) and
   the venue reports nothing -> ``NEVER_SUBMITTED``; the local order is canceled from journal
   evidence only (source ``APPLICATION_JOURNAL``).
2. Submission may have happened but the venue reports nothing -> ``AMBIGUOUS_PENDING_
   RECONCILIATION``; the local state is left EXACTLY as-is (a network timeout is never
   converted to a failed/rejected state) and new risk is blocked.
3. Several venue reports for one reference: consistent duplicates are deduplicated and
   reconciled (``VENUE_DUPLICATE_REPORT``); conflicting duplicates are ambiguous.
4. The local view is *ahead* of the venue (higher lifecycle rank or more filled quantity) ->
   ``LOCAL_AHEAD_OF_VENUE``; nothing is overwritten and new risk is blocked.
5. Otherwise the venue is ahead of or equal to the local view -> match, or adopt the venue
   state with an explicit adjustment (source ``VENUE_REPORT``) when policy permits.
6. A venue order our journal does not know about -> ``UNKNOWN_EXTERNAL_ORDER``; it is escalated,
   never adopted and never ignored, and new risk is blocked.

Every state change is recorded as a :class:`ReconciliationAdjustment` carrying its source, the
before state, the after state and a reason code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from .errors import ReconciliationError
from .models import (
    AdjustmentSource,
    LocalOrderState,
    OrderStateName,
    ReconciliationAdjustment,
    ReconciliationOutcome,
    ReconciliationReport,
    STATE_RANK,
    VenueOrderReport,
)
from .policy import ReconciliationPolicy

__all__ = ["ReconciliationEngine"]


@dataclass(frozen=True)
class _Resolution:
    outcome: ReconciliationOutcome
    reason_code: str
    adjustment: ReconciliationAdjustment | None = None

    @property
    def resolved(self) -> bool:
        return not self.outcome.is_unresolved


class ReconciliationEngine:
    """Deterministic reconciliation of local application state against the venue view."""

    def __init__(
        self, policy: ReconciliationPolicy, *, require_known_venue_identity: bool = False
    ) -> None:
        """``require_known_venue_identity`` (review G-4): an order whose venue order id was never
        journaled is never resolved from a venue report, which may answer for another order
        reusing its client order id. Its local state cannot vouch for it either: a cancel by
        client order id may have journaled another order's answer. Break-glass requires it;
        startup does not (an accepted residual)."""
        if not isinstance(policy, ReconciliationPolicy):
            raise ReconciliationError("a resolved ReconciliationPolicy is required (fail closed).")
        self._policy = policy
        self._require_known_venue_identity = bool(require_known_venue_identity)

    @property
    def policy(self) -> ReconciliationPolicy:
        return self._policy

    def reconcile(
        self,
        local_states: Sequence[LocalOrderState],
        venue_reports: Sequence[VenueOrderReport],
    ) -> ReconciliationReport:
        local_index = self._index_local(local_states)
        venue_index = self._index_venue(venue_reports)

        outcomes: dict[str, ReconciliationOutcome] = {}
        reason_codes: dict[str, str] = {}
        order_ids: dict[str, UUID] = {}
        correlations: dict[str, UUID] = {}
        adjustments: list[ReconciliationAdjustment] = []
        resolved: list[str] = []
        ambiguous: list[str] = []
        never_submitted: list[str] = []
        duplicate_reports: list[str] = []
        local_ahead: list[str] = []
        unknown_external: list[str] = []

        for reference in sorted(local_index):
            local = local_index[reference]
            order_ids[reference] = local.order_id
            if local.correlation_id is not None:
                correlations[reference] = local.correlation_id
            resolution = self._resolve_local(local, venue_index.get(reference, []))
            outcomes[reference] = resolution.outcome
            reason_codes[reference] = resolution.reason_code
            if resolution.adjustment is not None:
                adjustments.append(resolution.adjustment)
            if resolution.outcome is ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION:
                ambiguous.append(reference)
            elif resolution.outcome is ReconciliationOutcome.LOCAL_AHEAD_OF_VENUE:
                local_ahead.append(reference)
            elif resolution.outcome is ReconciliationOutcome.NEVER_SUBMITTED:
                never_submitted.append(reference)
                resolved.append(reference)
            elif resolution.outcome is ReconciliationOutcome.VENUE_DUPLICATE_REPORT:
                duplicate_reports.append(reference)
                resolved.append(reference)
            else:
                resolved.append(reference)

        for reference in sorted(set(venue_index) - set(local_index)):
            outcomes[reference] = ReconciliationOutcome.UNKNOWN_EXTERNAL_ORDER
            reason_codes[reference] = "unknown_external_order_escalated"
            unknown_external.append(reference)

        unresolved = [*ambiguous, *local_ahead, *unknown_external]
        return ReconciliationReport(
            outcomes=outcomes,
            reason_codes=reason_codes,
            order_ids=order_ids,
            correlations=correlations,
            adjustments=adjustments,
            resolved_orders=resolved,
            ambiguous_orders=ambiguous,
            unknown_external_orders=unknown_external,
            duplicate_reports=duplicate_reports,
            never_submitted=never_submitted,
            local_ahead_of_venue=local_ahead,
            requires_halt_new_risk=bool(unresolved),
        )

    # --- indexing ---

    @staticmethod
    def _index_local(local_states: Sequence[LocalOrderState]) -> dict[str, LocalOrderState]:
        index: dict[str, LocalOrderState] = {}
        for state in local_states:
            if not isinstance(state, LocalOrderState):
                raise ReconciliationError("local states must be LocalOrderState values.")
            if state.order_reference in index:
                raise ReconciliationError(
                    "duplicate local order reference "
                    f"{state.order_reference!r}: there can be exactly one authoritative local "
                    "writer per order."
                )
            index[state.order_reference] = state
        return index

    @staticmethod
    def _index_venue(venue_reports: Sequence[VenueOrderReport]) -> dict[str, list[VenueOrderReport]]:
        index: dict[str, list[VenueOrderReport]] = {}
        for report in venue_reports:
            if not isinstance(report, VenueOrderReport):
                raise ReconciliationError("venue reports must be VenueOrderReport values.")
            index.setdefault(report.order_reference, []).append(report)
        return index

    # --- resolution ---

    def _resolve_local(
        self,
        local: LocalOrderState,
        reports: Sequence[VenueOrderReport],
    ) -> _Resolution:
        if not reports:
            return self._resolve_without_venue_evidence(local)

        if len(reports) > 1:
            duplicate = True
            consistent = all(
                report.normalized_key() == reports[0].normalized_key() for report in reports
            )
            if not consistent:
                return _Resolution(
                    ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
                    "conflicting_duplicate_venue_reports",
                )
            resolution = self._resolve_from_venue(local, reports[0])
            if not resolution.resolved:
                return resolution
            return _Resolution(
                ReconciliationOutcome.VENUE_DUPLICATE_REPORT,
                "consistent_duplicate_venue_reports_deduplicated",
                resolution.adjustment,
            )

        return self._resolve_from_venue(local, reports[0])

    def _resolve_without_venue_evidence(self, local: LocalOrderState) -> _Resolution:
        if not local.submission_attempted and local.state.rank < STATE_RANK["submitted"]:
            # Proven safe: the journal shows no send was ever attempted, so no venue exposure
            # can exist. Cancel locally from journal evidence only.
            after = replace(
                local,
                state=OrderStateName.CANCELED,
                filled_quantity=local.filled_quantity,
                remaining_quantity=local.remaining_quantity,
            )
            adjustment = ReconciliationAdjustment(
                source=AdjustmentSource.APPLICATION_JOURNAL,
                before=local,
                after=after,
                reason_code="no_submission_attempt_recorded",
            )
            return _Resolution(
                ReconciliationOutcome.NEVER_SUBMITTED,
                "no_submission_attempt_recorded",
                adjustment,
            )

        if self._require_known_venue_identity and local.is_terminal:
            # Review SOL-F3: a read or cancel by client order id may have journaled another
            # order's answer as this order's end, so only a current venue report can prove it.
            return _Resolution(
                ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
                "venue_order_identity_unproven",
            )
        if local.is_terminal:
            # A terminal order whose terminal state came from our own recorded lifecycle/fills
            # needs no venue correction when the venue view simply has nothing to report. The
            # state is left exactly as-is.
            return _Resolution(
                ReconciliationOutcome.MATCHED,
                "terminal_order_no_venue_evidence",
            )

        # A submission may have reached the venue. NEVER convert this to failed/rejected and
        # NEVER resubmit blindly: keep the local state untouched and block new risk.
        return _Resolution(
            ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
            "ambiguous_submission_no_venue_evidence",
        )

    def _resolve_from_venue(
        self,
        local: LocalOrderState,
        report: VenueOrderReport,
    ) -> _Resolution:
        if (
            local.venue_order_id is not None
            and report.venue_order_id is not None
            and report.venue_order_id != local.venue_order_id
        ):
            # Review G-4: the venue answered for another order that reuses our client order id.
            return _Resolution(
                ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
                "venue_order_identity_mismatch",
            )
        if self._require_known_venue_identity and local.venue_order_id is None:
            return _Resolution(
                ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
                "venue_order_identity_unknown",
            )
        if self._require_known_venue_identity and report.venue_order_id is None:
            # Review SOL-F4: a report naming no venue order cannot prove ours.
            return _Resolution(
                ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
                "venue_order_identity_unreported",
            )
        local_ahead = (
            local.state.rank > report.state.rank
            or local.filled_quantity > report.filled_quantity
        )
        if local_ahead:
            return _Resolution(
                ReconciliationOutcome.LOCAL_AHEAD_OF_VENUE,
                "local_state_ahead_of_venue",
            )

        if report.state == local.state and self._quantities_match(local, report):
            return _Resolution(ReconciliationOutcome.MATCHED, "local_and_venue_agree")

        if not self._policy.allow_resolution_from_venue:
            return _Resolution(
                ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION,
                "venue_discrepancy_requires_operator",
            )

        after = replace(
            local,
            state=report.state,
            filled_quantity=report.filled_quantity,
            remaining_quantity=report.remaining_quantity,
        )
        adjustment = ReconciliationAdjustment(
            source=AdjustmentSource.VENUE_REPORT,
            before=local,
            after=after,
            reason_code="venue_state_adopted",
        )
        return _Resolution(
            ReconciliationOutcome.RESOLVED_FROM_VENUE,
            "venue_state_adopted",
            adjustment,
        )

    @staticmethod
    def _quantities_match(local: LocalOrderState, report: VenueOrderReport) -> bool:
        return (
            local.filled_quantity == report.filled_quantity
            and local.remaining_quantity == report.remaining_quantity
        )
