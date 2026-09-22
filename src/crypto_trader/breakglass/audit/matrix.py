"""The Phase 13 break-glass matrix and its mechanical verdict.

The verdict is derived, never asserted: it is ``BREAK-GLASS READY FOR OWNER REVIEW`` only when every
REQUIRED check passes and every scenario passes. Retained limitations are reported as
*informational* rows and are never counted as passes.

Readiness is NOT live authorization: this phase performs no mainnet call, uses no real capital and
enables no withdrawal, and the owner remains the only decider for any live phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .checks import BreakGlassItem, run_break_glass_checks
from .scenarios import BreakGlassScenarioResult, run_all_scenarios

__all__ = [
    "VERDICT_NOT_READY",
    "VERDICT_READY",
    "BreakGlassAuditReport",
    "run_break_glass_audit",
]

VERDICT_READY: Literal["BREAK-GLASS READY FOR OWNER REVIEW"] = (
    "BREAK-GLASS READY FOR OWNER REVIEW"
)
VERDICT_NOT_READY: Literal["BREAK-GLASS NOT READY"] = "BREAK-GLASS NOT READY"


@dataclass(frozen=True)
class BreakGlassAuditReport:
    """The complete Phase 13 pre-live break-glass audit result."""

    verdict: str
    items: tuple[BreakGlassItem, ...]
    scenarios: tuple[BreakGlassScenarioResult, ...]
    focused_tests: str | None = None
    suite_result: str | None = None
    live_testnet_result: str | None = None
    branch: str | None = None
    commit: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    limitations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocking_failures(self) -> tuple[BreakGlassItem, ...]:
        return tuple(item for item in self.items if item.is_blocking_failure)

    @property
    def failed_scenarios(self) -> tuple[BreakGlassScenarioResult, ...]:
        return tuple(scenario for scenario in self.scenarios if not scenario.passed)

    @property
    def informational(self) -> tuple[BreakGlassItem, ...]:
        return tuple(item for item in self.items if not item.required)

    @property
    def is_ready(self) -> bool:
        return self.verdict == VERDICT_READY

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free audit record."""
        return {
            "verdict": self.verdict,
            "branch": self.branch,
            "commit": self.commit,
            "focused_tests": self.focused_tests,
            "suite_result": self.suite_result,
            "live_testnet_result": self.live_testnet_result,
            "counts": {
                "items": len(self.items),
                "required": sum(1 for item in self.items if item.required),
                "blocking_failures": len(self.blocking_failures),
                "informational": len(self.informational),
                "scenarios": len(self.scenarios),
                "failed_scenarios": len(self.failed_scenarios),
            },
            "items": [item.to_record() for item in self.items],
            "scenarios": [scenario.to_record() for scenario in self.scenarios],
            "limitations": list(self.limitations),
            "notes": list(self.notes),
        }

    def to_markdown(self) -> str:
        """A human-reviewable matrix."""
        lines = [
            "# Phase 13 - operational pre-live break-glass hardening",
            "",
            f"Verdict: **{self.verdict}**",
            "",
            "Break-glass readiness is not live authorization and does not enable mainnet trading.",
            "",
            "## Break-glass checks",
            "",
            "| item | area | required | status | detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| `{item.item_id}` | {item.area} | {'yes' if item.required else 'no'} | "
            f"{item.status} | {item.detail} |"
            for item in self.items
        )
        lines.extend(
            [
                "",
                "## Break-glass scenarios (operational + fault injection)",
                "",
                "| scenario | status | detail |",
                "| --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{scenario.scenario_id}` | {scenario.status} | {scenario.detail} |"
            for scenario in self.scenarios
        )
        lines.extend(["", "## Retained limitations", ""])
        lines.extend(f"- {limitation}" for limitation in self.limitations)
        return "\n".join(lines) + "\n"


def run_break_glass_audit(
    workspace: Path,
    *,
    focused_tests: str | None = None,
    suite_result: str | None = None,
    live_testnet_result: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
) -> BreakGlassAuditReport:
    """Run the whole Phase 13 audit: scenarios plus every static/semantic check."""
    workspace.mkdir(parents=True, exist_ok=True)
    scenarios = run_all_scenarios(workspace / "scenarios")
    items = run_break_glass_checks(workspace / "checks")
    blocking = [item for item in items if item.is_blocking_failure]
    failed = [scenario for scenario in scenarios if not scenario.passed]
    verdict = VERDICT_READY if not blocking and not failed else VERDICT_NOT_READY
    limitations = tuple(item.detail for item in items if not item.required)
    notes = (
        "offline and scripted only: no mainnet order, no real capital, no withdrawal and no "
        "liquidation was used by this audit",
        "every scenario injects a fault into the real components; only the transport is scripted",
        "break-glass readiness is not live authorization",
    )
    return BreakGlassAuditReport(
        verdict=verdict,
        items=items,
        scenarios=scenarios,
        focused_tests=focused_tests,
        suite_result=suite_result,
        live_testnet_result=live_testnet_result,
        branch=branch,
        commit=commit,
        notes=notes,
        limitations=limitations,
    )
