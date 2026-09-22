"""Technical live-readiness matrix and verdict (Phase 12).

The verdict is derived mechanically: it can only be
``TECHNICALLY READY FOR OWNER REVIEW`` when every REQUIRED readiness item passes and every
fault-injection scenario passes. Technical readiness is NOT live authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .checks import ReadinessItem, run_static_checks
from .scenarios import ScenarioResult, run_all_scenarios

__all__ = ["AuditReport", "VERDICT_NOT_READY", "VERDICT_READY", "run_audit"]

VERDICT_READY: Literal["TECHNICALLY READY FOR OWNER REVIEW"] = (
    "TECHNICALLY READY FOR OWNER REVIEW"
)
VERDICT_NOT_READY: Literal["NOT READY"] = "NOT READY"


@dataclass(frozen=True)
class AuditReport:
    """The complete Phase 12 audit result."""

    verdict: str
    items: tuple[ReadinessItem, ...]
    scenarios: tuple[ScenarioResult, ...]
    suite_result: str | None = None
    live_testnet_result: str | None = None
    branch: str | None = None
    commit: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocking_failures(self) -> tuple[ReadinessItem, ...]:
        return tuple(item for item in self.items if item.is_blocking_failure)

    @property
    def failed_scenarios(self) -> tuple[ScenarioResult, ...]:
        return tuple(scenario for scenario in self.scenarios if not scenario.passed)

    @property
    def is_ready(self) -> bool:
        return self.verdict == VERDICT_READY

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free audit record."""
        return {
            "verdict": self.verdict,
            "branch": self.branch,
            "commit": self.commit,
            "suite_result": self.suite_result,
            "live_testnet_result": self.live_testnet_result,
            "counts": {
                "items": len(self.items),
                "required": sum(1 for item in self.items if item.required),
                "blocking_failures": len(self.blocking_failures),
                "scenarios": len(self.scenarios),
                "failed_scenarios": len(self.failed_scenarios),
            },
            "items": [item.to_record() for item in self.items],
            "scenarios": [scenario.to_record() for scenario in self.scenarios],
            "notes": list(self.notes),
        }

    def to_markdown(self) -> str:
        """A human-reviewable matrix."""
        lines = [
            "# Phase 12 - technical live-readiness audit",
            "",
            f"Verdict: **{self.verdict}**",
            "",
            "Technical readiness is not live authorization.",
            "",
            "## Readiness matrix",
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
                "## Fault-injection scenarios",
                "",
                "| scenario | status | detail |",
                "| --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{scenario.scenario_id}` | {scenario.status} | {scenario.detail} |"
            for scenario in self.scenarios
        )
        return "\n".join(lines) + "\n"


def run_audit(
    workspace: Path,
    *,
    suite_result: str | None = None,
    live_testnet_result: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
) -> AuditReport:
    """Run the whole audit: fault-injection scenarios plus every static readiness check."""
    scenarios = run_all_scenarios(workspace)
    items = run_static_checks()
    blocking = [item for item in items if item.is_blocking_failure]
    failed = [scenario for scenario in scenarios if not scenario.passed]
    verdict = VERDICT_READY if not blocking and not failed else VERDICT_NOT_READY
    notes = (
        "no mainnet order, no real capital and no withdrawal was used by this audit",
        "all fault-injection scenarios ran offline against the real components",
        "technical readiness is not live authorization",
    )
    return AuditReport(
        verdict=verdict,
        items=items,
        scenarios=scenarios,
        suite_result=suite_result,
        live_testnet_result=live_testnet_result,
        branch=branch,
        commit=commit,
        notes=notes,
    )
