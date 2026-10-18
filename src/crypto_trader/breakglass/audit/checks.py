"""Static and dynamic Phase 13 break-glass checks.

Every check is deterministic and offline: it either PASSES with evidence or FAILS with the offending
detail. No credential value is ever read, and the harness performs no network I/O and no writes
outside the caller's scratch workspace.

The forbidden endpoint patterns are *composed from fragments* here on purpose: the Phase 12/13 scope
guards assert that these tokens never appear outside the authorised Spot Testnet adapter, and this
harness must make that assertion without itself embedding the literal token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crypto_trader.events import (
    OPERATOR_COMMAND_EVENT_TYPE,
    ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
    RECONCILIATION_INTAKE_EVENT_TYPE,
    RECONCILIATION_OBSERVED_EVENT_TYPE,
    RECONCILIATION_SETTLED_EVENT_TYPE,
    STREAM_LIFECYCLE_EVENT_TYPE,
    VENUE_TRADE_EVENT_TYPE,
    ACCOUNT_EQUITY_EVENT_TYPE,
    OperatorCommandRecordPayload,
    V1_CONTRACTS,
)
from crypto_trader.execution.binance_testnet import (
    API_KEY_HEADER,
    MAINNET_HOST_MARKERS,
    require_testnet_base_url,
)
from crypto_trader.execution.binance_testnet.client import PATH_ACCOUNT, PATH_ORDER

from ..commands import BREAK_GLASS_ACTIONS, LIFECYCLE_COMMANDS

__all__ = ["BreakGlassItem", "PROJECT_ROOT", "run_break_glass_checks"]

PROJECT_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
BREAKGLASS_DIR = PACKAGE_DIR / "breakglass"
DOC_FILES = (PROJECT_ROOT / "README.md", PROJECT_ROOT / "crypto_trader_documentation.md")

#: The Phase 1-12 contracts as tagged: their payload field sets and versions must not change.
PHASE12_CONTRACT_FIELDS: dict[tuple[str, int], tuple[str, ...]] = {
    ("strategy.decision.recorded", 1): ("action", "confidence", "rationale"),
    ("trade.intent.created", 1): ("side", "quantity", "order_type", "limit_price", "rationale"),
    ("risk.decision.recorded", 1): ("approved", "reason", "approved_quantity"),
    ("order.state.changed", 1): ("state", "reason"),
    ("execution.fill.recorded", 1): (
        "quantity",
        "price",
        "fee",
        "fee_asset",
        "remaining_quantity",
        "liquidity",
    ),
    ("recovery.state.changed", 1): ("state", "reason", "affected_order_ids"),
    ("validity.disposition.recorded", 1): (
        "disposition",
        "reason_code",
        "intent_id",
        "policy_id",
        "decision_age_seconds",
        "market_event_ts",
        "decision_completed_at",
        "market_move_bps",
        "notes",
    ),
    ("risk.evaluation.recorded", 1): (
        "limits_version",
        "intent_id",
        "symbol",
        "side",
        "quantity",
        "price",
        "notional",
        "decision",
        "reason_codes",
        "rule_results",
        "reduce_only_mode",
        "risk_actions",
    ),
    ("safety.state.changed", 1): (
        "mode",
        "previous_mode",
        "reason_code",
        "reason",
        "trigger",
        "kill_switch_engaged",
    ),
}

#: Endpoint/token shapes that must never appear in the break-glass package (composed, not literal).
FORBIDDEN_WITHDRAWAL_TOKENS: tuple[str, ...] = (
    "withdraw" + "/apply",
    "/sa" + "pi/v1/capital/" + "withdraw",
    "universal" + "transfer",
    "wa" + "pi",
)

#: The engine module name, composed for the same reason (see the module docstring).
ENGINE_MODULE = "nautilus" + "_trader"

FORBIDDEN_CREDENTIAL_IDENTIFIERS: tuple[str, ...] = (
    "os.environ",
    "getenv(",
    "putenv",
    "keyring",
    ".netrc",
)

FORBIDDEN_LLM_TOKENS: tuple[str, ...] = (
    "crewai",
    "crypto_trader.llm",
    "crypto_trader.crew",
    "crypto_trader.paper",
)

REQUIRED_OPERATOR_RECORD_FIELDS: tuple[str, ...] = (
    "command_id",
    "command_type",
    "requested_at",
    "operator_label",
    "operator_identity",
    "reason",
    "source",
    "previous_safety_state",
    "resulting_safety_state",
    "disposition",
    "reason_code",
)

REQUIRED_DOCUMENTED_PHRASES: tuple[str, ...] = (
    "operator command",
    "break-glass",
    "fencing",
    "reconciliation",
    "operator acknowledgement",
    "not live authorization",
)


@dataclass(frozen=True)
class BreakGlassItem:
    """One Phase 13 break-glass audit row."""

    item_id: str
    area: str
    required: bool
    status: str  # "pass" | "fail" | "informational"
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocking_failure(self) -> bool:
        return self.required and self.status != "pass"

    def to_record(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "area": self.area,
            "required": self.required,
            "status": self.status,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


def _implementation_files() -> list[Path]:
    """Break-glass implementation modules (the audit harness itself is excluded)."""
    files: list[Path] = []
    for file in BREAKGLASS_DIR.rglob("*.py"):
        if "__pycache__" in file.parts:
            continue
        if file.is_relative_to(BREAKGLASS_DIR / "audit"):
            continue
        files.append(file)
    return files


def _production_files() -> list[Path]:
    return [
        file
        for file in PACKAGE_DIR.rglob("*.py")
        if "__pycache__" not in file.parts and "tests" not in file.parts
    ]


def _docs_text() -> str:
    raw = "\n".join(path.read_text(encoding="utf-8") for path in DOC_FILES if path.exists())
    return re.sub(r"\s+", " ", raw)


def run_break_glass_checks(workspace: Path) -> tuple[BreakGlassItem, ...]:
    """Run every Phase 13 break-glass check."""
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    items: list[BreakGlassItem] = []
    items.extend(_scope_checks())
    items.extend(_contract_checks())
    items.extend(_safety_semantics_checks(root))
    items.extend(_documentation_checks())
    items.extend(_informational_notes())
    return tuple(items)


# --- scope / isolation -----------------------------------------------------

def _scope_checks() -> list[BreakGlassItem]:
    implementation = _implementation_files()
    items: list[BreakGlassItem] = []

    llm_offenders: list[str] = []
    for file in implementation:
        text = file.read_text(encoding="utf-8")
        for token in FORBIDDEN_LLM_TOKENS:
            if token in text:
                llm_offenders.append(f"{file.name}:{token}")
    items.append(
        BreakGlassItem(
            "breakglass_no_llm_dependency",
            "Isolation",
            True,
            "pass" if not llm_offenders else "fail",
            "the break-glass implementation imports no CrewAI/LLM/agent/strategy code, so no model "
            "can decide, approve, translate or delay a break-glass action",
            {
                "offenders": llm_offenders,
                "files": sorted(file.name for file in implementation),
            },
        )
    )

    engine_offenders: list[str] = []
    engine_import = re.compile(
        rf"^\s*(?:import|from)\s+({ENGINE_MODULE}|nautilus)\b", re.MULTILINE
    )
    for file in implementation:
        if engine_import.search(file.read_text(encoding="utf-8")):
            engine_offenders.append(file.name)
    items.append(
        BreakGlassItem(
            "breakglass_engine_independent",
            "Isolation",
            True,
            "pass" if not engine_offenders else "fail",
            "the break-glass path depends only on domain components and the execution boundary",
            {"offenders": engine_offenders},
        )
    )

    mainnet_offenders: list[str] = []
    for file in implementation:
        text = file.read_text(encoding="utf-8").lower()
        for marker in MAINNET_HOST_MARKERS:
            if marker in text:
                mainnet_offenders.append(f"{file.name}:{marker}")
        if PATH_ORDER in text or PATH_ACCOUNT in text or API_KEY_HEADER.lower() in text:
            mainnet_offenders.append(f"{file.name}:authenticated-endpoint-token")
    guard_ok = True
    try:
        require_testnet_base_url("https://testnet.binance.vision")
        for marker in MAINNET_HOST_MARKERS:
            try:
                require_testnet_base_url(f"https://{marker}")
                guard_ok = False
            except Exception:
                continue
    except Exception:  # pragma: no cover - the documented testnet host must always be accepted
        guard_ok = False
    items.append(
        BreakGlassItem(
            "no_mainnet_endpoint",
            "Mainnet safety",
            True,
            "pass" if not mainnet_offenders and guard_ok else "fail",
            "no mainnet hostname or authenticated mainnet endpoint exists in the break-glass path, "
            "and the endpoint guard refuses every mainnet marker",
            {"offenders": mainnet_offenders, "endpoint_guard": guard_ok},
        )
    )

    withdrawal_offenders: list[str] = []
    for file in implementation:
        text = file.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_WITHDRAWAL_TOKENS:
            if token in text:
                withdrawal_offenders.append(f"{file.name}:withdrawal-endpoint")
    adapter_surface_clean = not any(
        "withdraw" in name.lower()
        and callable(
            getattr(
                __import__(
                    "crypto_trader.execution.binance_testnet",
                    fromlist=["BinanceSpotTestnetClient"],
                ).BinanceSpotTestnetClient,
                name,
            )
        )
        for name in dir(
            __import__(
                "crypto_trader.execution.binance_testnet", fromlist=["BinanceSpotTestnetClient"]
            ).BinanceSpotTestnetClient
        )
    )
    items.append(
        BreakGlassItem(
            "no_withdrawal_capability",
            "Mainnet safety",
            True,
            "pass" if not withdrawal_offenders and adapter_surface_clean else "fail",
            "no withdrawal endpoint or method exists in the break-glass path or the Spot Testnet "
            "adapter; the emergency pre-flight fails closed on a withdrawal-capable key",
            {
                "offenders": withdrawal_offenders,
                "adapter_surface_clean": adapter_surface_clean,
            },
        )
    )

    credential_offenders: list[str] = []
    for file in implementation:
        text = file.read_text(encoding="utf-8")
        for token in FORBIDDEN_CREDENTIAL_IDENTIFIERS:
            if token in text:
                credential_offenders.append(f"{file.name}:{token}")
    payload_fields = set(OperatorCommandRecordPayload.model_fields)
    payload_credential_fields = sorted(
        name
        for name in payload_fields
        if any(marker in name.lower() for marker in ("key", "secret", "token", "signature"))
    )
    items.append(
        BreakGlassItem(
            "no_secret_persistence",
            "Secrets",
            True,
            "pass" if not credential_offenders and not payload_credential_fields else "fail",
            "the break-glass path reads no environment variable and the operator command record has "
            "no credential-shaped field, so no secret can be persisted anywhere",
            {
                "offenders": credential_offenders,
                "credential_shaped_fields": payload_credential_fields,
                "state_files": ["safety_state.json", "ownership.json"],
            },
        )
    )

    items.append(_git_policy_item())
    return items


def _git_policy_item() -> BreakGlassItem:
    """Record non-secret repository identity metadata without gating break-glass readiness."""
    git_entry = PROJECT_ROOT / ".git"
    current_branch: str | None = None
    head: str | None = None
    metadata_error: str | None = None
    git_layout = "missing"

    try:
        head_path: Path | None = None
        if git_entry.is_dir():
            git_layout = "directory"
            head_path = git_entry / "HEAD"
        elif git_entry.is_file():
            git_layout = "gitdir_file"
            pointer = git_entry.read_text(encoding="utf-8", errors="replace").strip()
            if pointer.lower().startswith("gitdir:"):
                target = Path(pointer.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = (git_entry.parent / target).resolve()
                head_path = target / "HEAD"
        else:
            git_layout = "missing"

        if head_path is not None and head_path.is_file():
            head = head_path.read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref: refs/heads/"):
                current_branch = head[len("ref: refs/heads/"):]
    except OSError as exc:
        metadata_error = type(exc).__name__

    return BreakGlassItem(
        "repository_metadata",
        "Repository policy",
        False,
        "informational",
        "repository HEAD metadata is recorded for traceability only; source-control state does not "
        "determine break-glass readiness",
        {
            "current_branch": current_branch,
            "head": head,
            "git_layout": git_layout,
            "metadata_error": metadata_error,
        },
    )

# --- contracts -------------------------------------------------------------

def _contract_checks() -> list[BreakGlassItem]:
    items: list[BreakGlassItem] = []
    registered: dict[tuple[str, int], tuple[str, ...]] = {}
    for contract in V1_CONTRACTS:
        event_type = str(contract.model_fields["event_type"].default)
        version = int(contract.model_fields["schema_version"].default)
        registered[(event_type, version)] = tuple(sorted(contract.model_fields["payload"].annotation.model_fields))
    unchanged: list[str] = []
    for key, expected in PHASE12_CONTRACT_FIELDS.items():
        actual = registered.get(key)
        if actual != tuple(sorted(expected)):
            unchanged.append(f"{key[0]} v{key[1]}: {actual}")
    new_contracts = sorted(set(registered) - set(PHASE12_CONTRACT_FIELDS))
    expected_additive = sorted(
        [
            (OPERATOR_COMMAND_EVENT_TYPE, 1),
            (STREAM_LIFECYCLE_EVENT_TYPE, 1),
            (ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE, 1),
            (VENUE_TRADE_EVENT_TYPE, 1),
            (ACCOUNT_EQUITY_EVENT_TYPE, 1),
            (RECONCILIATION_INTAKE_EVENT_TYPE, 1),
            (RECONCILIATION_OBSERVED_EVENT_TYPE, 1),
            (RECONCILIATION_SETTLED_EVENT_TYPE, 1),
        ]
    )
    items.append(
        BreakGlassItem(
            "tagged_contracts_unmodified",
            "Event contracts",
            True,
            "pass" if not unchanged and new_contracts == expected_additive else "fail",
            "every Phase 1-12 tagged contract keeps its exact payload fields and version; later "
            "phases add only the reviewed additive v1 contracts (Phase 13 operator command, "
            "Phase 15 stream lifecycle, Phase 15 durable submission attempt, G4 venue trade metadata, "
            "G5b account equity chain, F-03 reconcile run intake, observation and settlement)",
            {
                "modified": unchanged,
                "additive": [f"{name} v{version}" for name, version in new_contracts],
            },
        )
    )

    payload_fields = set(OperatorCommandRecordPayload.model_fields)
    missing = sorted(set(REQUIRED_OPERATOR_RECORD_FIELDS) - payload_fields)
    items.append(
        BreakGlassItem(
            "operator_command_record_complete",
            "Operational control",
            True,
            "pass" if not missing else "fail",
            "the additive contract carries every mandatory operator-command field; correlation_id "
            "and causation_id live on the Phase 3 envelope, exactly as for every other contract",
            {"missing": missing, "fields": sorted(payload_fields)},
        )
    )

    actions = sorted(item.value for item in BREAK_GLASS_ACTIONS)
    lifecycle = sorted(item.value for item in LIFECYCLE_COMMANDS)
    expected_actions = [
        "cancel_open",
        "enter_degraded",
        "force_recovery_required",
        "halt_new_risk",
        "reduce_only",
    ]
    items.append(
        BreakGlassItem(
            "break_glass_action_surface",
            "Operational control",
            True,
            "pass" if actions == expected_actions and lifecycle else "fail",
            "the five required operational actions are supported and there is no liquidation action",
            {"actions": actions, "lifecycle_commands": lifecycle},
        )
    )
    return items


# --- safety semantics (dynamic) --------------------------------------------

def _safety_semantics_checks(workspace: Path) -> list[BreakGlassItem]:
    from crypto_trader.persistence import SQLiteEventJournal

    from ..control import BreakGlassControl
    from ..errors import StaleWriterFenceError
    from ..ownership import OwnerKind
    from .scenarios import BASE, _command, _workspace  # shared deterministic fixtures

    items: list[BreakGlassItem] = []

    root = _workspace(workspace, "checks_writer")
    single_writer = False
    restart_persisted = False
    auto_restore_prevented = False
    reconciliation_gated = False
    with SQLiteEventJournal(root / "journal.db") as journal:
        control = BreakGlassControl(journal=journal, state_dir=root / "state", clock=lambda: BASE)
        control.ownership_store.takeover(
            owner_id="normal:runtime",
            owner_kind=OwnerKind.NORMAL,
            acquired_at=BASE,
            reason_code="runtime_started",
        )
        guard = control.normal_writer_guard()
        control.handle(_command("halt_new_risk", seed="checks-1"))
        try:
            guard.require_current()
        except StaleWriterFenceError:
            single_writer = True
        denied = control.handle(_command("restore_normal", seed="checks-2"))
        reconciliation_gated = (
            denied.disposition == "rejected"
            and denied.reason_code == "restore_denied_reconciliation_pending"
        )
        auto_restore_prevented = control.status().new_risk_permitted is False
    with SQLiteEventJournal(root / "journal.db") as journal:
        restarted = BreakGlassControl(
            journal=journal, state_dir=root / "state", clock=lambda: BASE
        )
        status = restarted.status()
        restart_persisted = (
            status.safety_mode == "halt_new_risk"
            and status.kill_switch_engaged
            and status.epoch_open
            and status.new_risk_permitted is False
        )
    items.append(
        BreakGlassItem(
            "single_authoritative_writer",
            "Ownership",
            True,
            "pass" if single_writer else "fail",
            "an emergency takeover mints a new fencing term and invalidates the normal writer's "
            "token, so there is exactly one authoritative mutable-state writer",
            {"stale_normal_token_refused": single_writer},
        )
    )
    items.append(
        BreakGlassItem(
            "restart_preserves_safety_state",
            "Persistence",
            True,
            "pass" if restart_persisted else "fail",
            "a restart restores the exact restricted posture from the persisted state/journal",
            {"status": status.to_record()["safety"]},
        )
    )
    items.append(
        BreakGlassItem(
            "no_automatic_normal_restore",
            "Operational control",
            True,
            "pass" if auto_restore_prevented and reconciliation_gated else "fail",
            "normal is never restored automatically: it stays refused until reconciliation and an "
            "explicit operator acknowledgement have both completed",
            {
                "auto_restore_prevented": auto_restore_prevented,
                "restore_gated": reconciliation_gated,
            },
        )
    )
    return items


# --- documentation ---------------------------------------------------------

def _documentation_checks() -> list[BreakGlassItem]:
    docs = _docs_text()
    lowered = docs.lower()
    missing = [phrase for phrase in REQUIRED_DOCUMENTED_PHRASES if phrase not in lowered]
    items = [
        BreakGlassItem(
            "breakglass_documented",
            "Documentation",
            True,
            "pass" if not missing else "fail",
            "the operational break-glass path, its operator command record, fencing, reconciliation "
            "and acknowledgement gates are documented",
            {"missing_phrases": missing},
        )
    ]
    authorization_phrases = ("not live authorization", "does not authorize live trading")
    items.append(
        BreakGlassItem(
            "no_automatic_live_authorization",
            "Authorization",
            True,
            "pass" if any(phrase in lowered for phrase in authorization_phrases) else "fail",
            "break-glass readiness is documented as NOT live/mainnet authorization; this phase "
            "authorizes no real capital",
            {"phrases_checked": list(authorization_phrases)},
        )
    )
    return items


def _informational_notes() -> list[BreakGlassItem]:
    return [
        BreakGlassItem(
            "informational_authenticated_testnet_validation",
            "Known limitations",
            False,
            "informational",
            "the authenticated Spot Testnet break-glass validation is opt-in (CRYPTO_TRADER_RUN_TESTNET=1 "
            "plus BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET); when the credentials are "
            "absent the offline scripted-transport scenarios are the evidence and this stays a "
            "retained limitation rather than a pass",
            {"status": "conditional on operator-supplied testnet credentials"},
        ),
        BreakGlassItem(
            "informational_emergency_scope",
            "Known limitations",
            False,
            "informational",
            "break-glass cancels open orders and applies safety policy; it never liquidates and never "
            "moves funds, so an operator still has to decide what to do with resulting exposure",
            {"status": "by design (no automated liquidation)"},
        ),
    ]
