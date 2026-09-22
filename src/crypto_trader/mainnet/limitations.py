"""Documented limitations carried by the Phase 15 mainnet read-only dry-run.

Nothing in this module is a passing check: these are retained, explicitly recorded limitations that
must never be reported as fully solved.
"""

from __future__ import annotations

__all__ = [
    "DRY_RUN_RECONCILIATION_GAP",
    "FOK_DOCUMENTED_LIMITATIONS",
    "RESIDUAL_LIMITATIONS",
    "documented_limitations",
]

#: FOK is a Canary-stage constraint, not a permanent execution strategy.
FOK_DOCUMENTED_LIMITATIONS: tuple[str, ...] = (
    "FOK is a Canary-stage constraint, not the permanent optimal execution strategy.",
    "FOK may lower the execution/fill rate.",
    "FOK has opportunity cost: a missed fill is a missed trade, not a system failure.",
    "FOK avoids venue-side partial-fill/dust exposure during the Canary.",
    "FOK does NOT eliminate transport ambiguity: a lost response still has an unknown outcome.",
    "A LIMIT FOK may execute across multiple price levels up to the actual limit price.",
    "15 bps is a maximum allowed normal execution deviation, NOT realized expected slippage.",
)

#: The mainnet dry-run reconciliation gap (there is no empirical mainnet post-submit evidence).
DRY_RUN_RECONCILIATION_GAP: str = (
    "DOCUMENTED GAP: a mainnet read-only dry-run sends no orders, so empirical mainnet "
    "post-submit reconciliation cannot be proven in this phase. Mainnet reconciliation here is "
    "LOGIC-LEVEL plus READ-ONLY ACCOUNT-STATE validation, not empirical mainnet order-submission "
    "reconciliation. Actual post-submit ambiguity remains covered by prior Binance Testnet "
    "evidence, deterministic post-send timeout/reset fault injection and the recovery/"
    "reconciliation tests. No claim of validated empirical mainnet post-submit reconciliation is "
    "made, and no real-money ambiguous order is ever created in this phase."
)

#: Limitations carried forward from earlier phases; none of them is solved by Phase 15.
RESIDUAL_LIMITATIONS: tuple[str, ...] = (
    "residual TOCTOU window: local admission is never atomic with venue acceptance.",
    "dynamic latency is PARTIAL: the pinned engine version exposes static per-command latency "
    "only.",
    "L2 MBP cannot reveal hidden/iceberg liquidity or true venue queue rank.",
    DRY_RUN_RECONCILIATION_GAP,
    *FOK_DOCUMENTED_LIMITATIONS,
    "there is no IOC/GTC partial-fill/dust-handling path yet; it needs a separate design and "
    "explicit owner approval.",
    "the first empirical mainnet execution behavior occurs only in a future Controlled Live "
    "Canary; nothing in this phase validates it.",
)


def documented_limitations() -> dict[str, object]:
    """A JSON-able record of every carried-forward limitation (for reports and the soak state)."""
    return {
        "fok_limitations": list(FOK_DOCUMENTED_LIMITATIONS),
        "dry_run_reconciliation_gap": DRY_RUN_RECONCILIATION_GAP,
        "residual_limitations": list(RESIDUAL_LIMITATIONS),
    }
