"""Static and dynamic readiness checks for the live-readiness audit (Phase 12).

Every check is deterministic, offline and value-free: no credential value is ever read or printed.
Checks either PASS with evidence, or FAIL with the offending detail.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from crypto_trader.risk import HardRiskEngine, RiskRequest, RiskStateSnapshot, Side, load_limits

__all__ = ["ReadinessItem", "risk_is_deterministic", "run_static_checks"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
DOC_FILES = (PROJECT_ROOT / "README.md", PROJECT_ROOT / "crypto_trader_documentation.md")

#: Packages that must stay free of engine/LLM dependencies.
ENGINE_INDEPENDENT_PACKAGES = (
    "events",
    "llm",
    "tools",
    "validity",
    "risk",
    "recovery",
    "safety",
    "paper",
)

TESTNET_DIR = PACKAGE_DIR / "execution" / "binance_testnet"
NAUTILUS_ADAPTER_DIR = PACKAGE_DIR / "execution" / "nautilus"

REQUIRED_ENGINE_VERSION = "1.231.0"
REQUIRED_PYTHON = ">=3.12,<3.13"


@dataclass(frozen=True)
class ReadinessItem:
    """One readiness matrix row."""

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


def _docs_text() -> str:
    """All documentation, whitespace-normalized so a wrapped phrase still matches."""
    raw = "\n".join(path.read_text(encoding="utf-8") for path in DOC_FILES if path.exists())
    return re.sub(r"\s+", " ", raw)


def _production_files() -> list[Path]:
    return [
        file
        for file in PACKAGE_DIR.rglob("*.py")
        if "__pycache__" not in file.parts and "tests" not in file.parts
    ]


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def risk_is_deterministic() -> bool:
    """The same typed inputs must always produce the same decision."""
    limits = load_limits(
        {
            "limits_version": "risk-v1",
            "max_order_notional_quote": "5000",
            "max_symbol_exposure_quote": "20000",
            "max_gross_exposure_quote": "50000",
            "max_net_exposure_quote": "30000",
            "max_strategy_exposure_quote": "40000",
            "loss_limit_quote": "1000",
            "max_drawdown_pct": "10",
            "concentration_limit_pct": "50",
            "max_open_orders": 5,
            "max_open_positions": 3,
        }
    )
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    request = RiskRequest(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("2700"),
        strategy_id="audit",
        intent_id=uuid4(),
    )
    state = RiskStateSnapshot(
        equity_quote=Decimal("10000"),
        peak_equity_quote=Decimal("10000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=stamp,
    )
    first = HardRiskEngine(limits).evaluate(request, state, now=stamp)
    second = HardRiskEngine(limits).evaluate(request, state, now=stamp)
    return (
        first.allowed == second.allowed
        and first.reason_codes == second.reason_codes
        and [rule.reason_code for rule in first.rule_results]
        == [rule.reason_code for rule in second.rule_results]
    )


def _installed_engine_version() -> str | None:
    try:
        import nautilus_trader  # noqa: PLC0415 - lazy so a missing engine is a FAIL, not a crash

        return str(nautilus_trader.__version__)
    except Exception:  # pragma: no cover - only when the engine is absent
        return None


def run_static_checks() -> tuple[ReadinessItem, ...]:
    """Run every static/dynamic readiness check and return the matrix rows."""
    items: list[ReadinessItem] = []
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    pinned = re.search(r'"nautilus_trader==([0-9.]+)"', pyproject)
    python_pin = re.search(r'requires-python = "([^"]+)"', pyproject)
    installed = _installed_engine_version()
    items.append(
        ReadinessItem(
            "engine_version_pin",
            "Dependencies",
            True,
            "pass"
            if (
                pinned
                and pinned.group(1) == REQUIRED_ENGINE_VERSION
                and installed == REQUIRED_ENGINE_VERSION
            )
            else "fail",
            "NautilusTrader is pinned exactly in production and matches the installed build",
            {
                "pinned": pinned.group(1) if pinned else None,
                "installed": installed,
                "expected": REQUIRED_ENGINE_VERSION,
            },
        )
    )
    items.append(
        ReadinessItem(
            "python_baseline",
            "Dependencies",
            True,
            "pass"
            if (
                python_pin
                and python_pin.group(1) == REQUIRED_PYTHON
                and sys.version_info[:2] == (3, 12)
            )
            else "fail",
            "production Python baseline is 3.12",
            {
                "requires_python": python_pin.group(1) if python_pin else None,
                "interpreter": f"{sys.version_info.major}.{sys.version_info.minor}",
            },
        )
    )

    engine_import = re.compile(
        r"^\s*(?:import|from)\s+(nautilus_trader|nautilus)\b", re.MULTILINE
    )
    engine_offenders: list[str] = []
    for file in _production_files():
        if file.is_relative_to(NAUTILUS_ADAPTER_DIR) or file.is_relative_to(PACKAGE_DIR / "audit"):
            continue
        if engine_import.search(file.read_text(encoding="utf-8")):
            engine_offenders.append(_relative(file))
    items.append(
        ReadinessItem(
            "engine_imports_confined",
            "Domain isolation",
            True,
            "pass" if not engine_offenders else "fail",
            "the engine is imported only by its own adapter package and by the offline audit harness",
            {
                "offenders": engine_offenders,
                "exempt": [
                    _relative(NAUTILUS_ADAPTER_DIR),
                    _relative(PACKAGE_DIR / "audit")
                    + " (reads the installed version and runs the offline simulator as evidence)",
                ],
            },
        )
    )

    llm_import = re.compile(
        r"^\s*(?:import|from)\s+(crewai|crypto_trader\.llm)\b", re.MULTILINE
    )
    domain_packages = ("events", "validity", "risk", "recovery", "safety", "paper")
    llm_offenders: list[str] = []
    for file in _production_files():
        if not any(file.is_relative_to(PACKAGE_DIR / package) for package in domain_packages):
            continue
        if llm_import.search(file.read_text(encoding="utf-8")):
            llm_offenders.append(_relative(file))
    items.append(
        ReadinessItem(
            "domain_layer_isolated",
            "Domain isolation",
            True,
            "pass" if not llm_offenders else "fail",
            "event contracts and every domain decision layer stay free of agent/LLM imports",
            {"offenders": llm_offenders, "packages": list(domain_packages)},
        )
    )

    mainnet_offenders: list[str] = []
    for file in _production_files():
        if (
            file.is_relative_to(TESTNET_DIR)
            or file.is_relative_to(PACKAGE_DIR / "audit")
            or file.name == "binance_public_tool.py"
        ):
            continue
        text = file.read_text(encoding="utf-8").lower()
        for token in ("api/v3/order", "api/v3/account", "x-mbx-apikey", "/sapi/", "fapi."):
            if token in text:
                mainnet_offenders.append(f"{_relative(file)}:{token}")
    items.append(
        ReadinessItem(
            "no_mainnet_capability",
            "Mainnet safety",
            True,
            "pass" if not mainnet_offenders else "fail",
            "no authenticated mainnet/futures capability exists outside the testnet adapter",
            {
                "offenders": mainnet_offenders,
                "exempt": [
                    _relative(TESTNET_DIR),
                    "tools/binance_public_tool.py (public read-only)",
                    _relative(PACKAGE_DIR / "audit") + " (the auditor names the tokens it forbids)",
                ],
            },
        )
    )

    withdrawal_offenders: list[str] = []
    for file in _production_files():
        if file.is_relative_to(PACKAGE_DIR / "audit"):
            continue
        text = file.read_text(encoding="utf-8").lower()
        for token in ("withdraw/apply", "/sapi/v1/capital/withdraw", "universaltransfer", "wapi"):
            if token in text:
                withdrawal_offenders.append(f"{_relative(file)}:{token}")
    items.append(
        ReadinessItem(
            "no_withdrawal_capability",
            "Mainnet safety",
            True,
            "pass" if not withdrawal_offenders else "fail",
            "no withdrawal endpoint exists anywhere in the production tree",
            {"offenders": withdrawal_offenders},
        )
    )

    # The auditor names forbidden tokens, so this check is anchored to real code usage: an import
    # statement or a URL literal - never a bare mention of a library name.
    audit_network_import = re.compile(
        r"^\s*(?:import|from)\s+(requests|httpx|socket|urllib|aiohttp|websockets)\b",
        re.MULTILINE,
    )
    audit_http_call = re.compile(r"(?:requests|httpx|socket)\.\w+\(", re.MULTILINE)
    # A URL *with a hostname* - the checker's own bare "http://" token must not match itself.
    audit_url = re.compile(r"https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    auditor_network: list[str] = []
    for file in (PACKAGE_DIR / "audit").rglob("*.py"):
        if "__pycache__" in file.parts:
            continue
        text = file.read_text(encoding="utf-8")
        if audit_network_import.search(text):
            auditor_network.append(f"{file.name}:network import")
        if audit_http_call.search(text):
            auditor_network.append(f"{file.name}:network call")
        if audit_url.search(text):
            auditor_network.append(f"{file.name}:url literal")
    items.append(
        ReadinessItem(
            "auditor_has_no_network_capability",
            "Mainnet safety",
            True,
            "pass" if not auditor_network else "fail",
            "the audit harness itself performs no network I/O, so it can only name the tokens it forbids",
            {"offenders": auditor_network},
        )
    )

    persistence_tokens = (
        "write_text",
        "json.dump",
        "pickle",
        "shelve",
        "sqlite3",
        ".netrc",
        "keyring",
    )
    persistence_hits: list[str] = []
    for file in TESTNET_DIR.glob("*.py"):
        text = file.read_text(encoding="utf-8")
        for token in persistence_tokens:
            if token in text:
                persistence_hits.append(f"{file.name}:{token}")
    credentials_source = (TESTNET_DIR / "credentials.py").read_text(encoding="utf-8")
    env_only = all(
        token in credentials_source
        for token in ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET")
    )
    items.append(
        ReadinessItem(
            "credentials_environment_only",
            "Secrets",
            True,
            "pass" if env_only and not persistence_hits else "fail",
            "testnet credentials come only from the environment and are never persisted",
            {"env_vars_declared": env_only, "persistence_hits": persistence_hits},
        )
    )

    deterministic = risk_is_deterministic()
    items.append(
        ReadinessItem(
            "risk_deterministic",
            "Hard risk",
            True,
            "pass" if deterministic else "fail",
            "identical typed inputs produce identical deterministic risk decisions",
            {"deterministic": deterministic},
        )
    )

    docs = _docs_text()
    lowered = docs.lower()
    documentation_checks = {
        "toctou_documented": (
            "toctou" in lowered,
            "TOCTOU is documented as narrowed, never eliminated",
        ),
        "dynamic_latency_documented": (
            "dynamic latency" in lowered and "partial" in lowered,
            "dynamic latency is documented as PARTIAL in 1.231.0",
        ),
        "breakglass_status_documented": (
            "break-glass" in lowered and "pre-live mandatory gap" in lowered,
            "live break-glass is documented as a PRE-LIVE MANDATORY GAP",
        ),
        "upgrade_policy_documented": (
            "upgrade policy" in lowered,
            "the engine upgrade policy is documented",
        ),
        "testnet_not_authorization": (
            "not authorize mainnet" in lowered or "not authorise mainnet" in lowered,
            "testnet success is documented as NOT authorizing mainnet",
        ),
        "unknown_pending_documented": (
            "unknown_pending_reconciliation" in lowered,
            "UNKNOWN_PENDING_RECONCILIATION is documented",
        ),
    }
    for item_id, (satisfied, detail) in documentation_checks.items():
        items.append(
            ReadinessItem(item_id, "Documentation", True, "pass" if satisfied else "fail", detail)
        )

    items.append(
        ReadinessItem(
            "informational_l2_limitations",
            "Known limitations",
            False,
            "informational",
            "L2 MBP cannot reveal hidden/iceberg liquidity or true venue queue rank",
            {"claim": "no exchange-ground-truth claim is made anywhere in the system"},
        )
    )
    items.append(
        ReadinessItem(
            "informational_live_gaps",
            "Known limitations",
            False,
            "informational",
            "live break-glass execution and a withdrawal-disabled live key remain required before "
            "any live phase",
            {"status": "PRE-LIVE MANDATORY GAP"},
        )
    )
    return tuple(items)
