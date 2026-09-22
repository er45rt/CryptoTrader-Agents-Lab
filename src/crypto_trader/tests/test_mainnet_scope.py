"""Phase 15 scope boundary: the mainnet adapter stays READ-ONLY, allowlisted and secret-safe.

Like the Phase 11 testnet exemption, Phase 15 narrows the older "no mainnet capability at all"
guard to exactly one directory. These guards prove the exemption is narrow: GET only, allowlisted
paths only, no submission/cancellation/replacement, no withdrawal, no credential value and no way
to downgrade the dry-run into live trading.
"""

from __future__ import annotations

import re
from pathlib import Path

import crypto_trader.mainnet as mainnet_package
from crypto_trader.execution.binance_testnet.client import PATH_ORDER
from crypto_trader.mainnet import (
    LOCKED_PILOT_ENVELOPE,
    MUTATION_PATH_MARKERS,
    READ_ONLY_PATHS,
    DryRunMode,
    assert_no_mutation_surface,
)
from crypto_trader.tests import mainnet_fixtures as fx

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
MAINNET_DIR = PACKAGE_DIR / "mainnet"
AUDIT_CHECKS = PACKAGE_DIR / "audit" / "checks.py"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "client.py",
    "credentials.py",
    "dryrun.py",
    "endpoints.py",
    "envelope.py",
    "errors.py",
    "faults.py",
    "filters.py",
    "limitations.py",
    "nosubmit.py",
    "permissions.py",
    "soak.py",
}


def _files() -> list[Path]:
    return [file for file in MAINNET_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def test_the_mainnet_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES
    for forbidden in ("oms", "broker", "simulator", "event_bus", "state_store", "redis"):
        assert not (MAINNET_DIR / forbidden).exists()


def test_the_mainnet_adapter_contains_no_order_mutation_literal():
    """It names the order path only through the authorised Phase 11 constant."""
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in ("api/v3/order", "x-mbx-apikey", "recvwindow", "listenkey"):
            assert token not in text, f"{file.name} contains {token}"
        for token in ("place" + "_order", "create" + "_order", "cancel" + "_order"):
            assert token not in text, f"{file.name} contains an order-mutation literal"


def test_the_mainnet_adapter_has_no_withdrawal_or_transfer_endpoint():
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in ("withdraw" + "/apply", "/sapi/v1/capital/" + "withdraw", "universal" + "transfer"):
            assert token not in text, f"{file.name} contains {token}"


def test_the_mainnet_adapter_reads_no_testnet_credentials_or_host():
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert "BINANCE_TESTNET_API_KEY" not in text
        assert "BINANCE_TESTNET_API_SECRET" not in text
        assert "testnet.binance.vision" not in text


def test_the_read_only_allowlist_contains_no_mutation_path():
    for path in READ_ONLY_PATHS:
        assert not any(marker.lower() in path.lower() for marker in MUTATION_PATH_MARKERS)
    assert PATH_ORDER in MUTATION_PATH_MARKERS
    assert "/sapi/v1/account/apiRestrictions" in READ_ONLY_PATHS


def test_the_mainnet_surfaces_have_no_mutation_method():
    assert_no_mutation_surface(fx.gate())
    assert_no_mutation_surface(fx.client(__import__("crypto_trader.execution.binance_testnet", fromlist=["ScriptedTransport"]).ScriptedTransport(responses=[])))


def test_the_dry_run_has_exactly_one_submit_call_site_and_it_is_the_refusal_path():
    source = (MAINNET_DIR / "dryrun.py").read_text(encoding="utf-8")

    assert source.count(".submit(") == 1
    assert "guard_would_submit" in source
    # The single submit call sits inside the negative path, not the success path.
    assert source.index(".submit(") > source.index("def try_to_submit")


def test_only_the_readonly_dry_run_mode_exists_and_no_live_mode_can_be_configured():
    assert list(DryRunMode) == [DryRunMode.MAINNET_READONLY_DRY_RUN]
    source = (MAINNET_DIR / "nosubmit.py").read_text(encoding="utf-8")
    assert "_FORBIDDEN_ALIASES" in source
    assert "live" in source


def test_the_locked_envelope_is_reference_only_in_this_phase():
    record = LOCKED_PILOT_ENVELOPE.to_record()

    assert record["activation"] == "reference_only"
    assert record["permission_to_trade"] is False
    assert record["margin_allowed"] is False
    assert record["futures_allowed"] is False
    assert record["withdrawals_allowed"] is False


def test_phase15_adds_no_event_contract():
    contracts = (PACKAGE_DIR / "events" / "contracts.py").read_text(encoding="utf-8")
    event_types = set(re.findall(r'= "([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"', contracts))

    assert event_types == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        "execution.fill.recorded",
        "recovery.state.changed",
        "validity.disposition.recorded",
        "risk.evaluation.recorded",
        "safety.state.changed",
        "safety.operator_command.recorded",
    }


def test_the_mainnet_adapter_has_no_engine_broker_or_llm_dependency():
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(nautilus_trader|nautilus|crewai|crypto_trader\.llm|crypto_trader\.crew|"
        r"crypto_trader\.paper|redis|kafka|pika|nats|sqlalchemy)\b",
        re.MULTILINE,
    )
    for file in _files():
        match = forbidden_imports.search(file.read_text(encoding="utf-8"))
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"


def test_domain_packages_do_not_depend_on_the_mainnet_adapter():
    import_pattern = re.compile(r"^\s*(?:import|from)\s+crypto_trader\.mainnet", re.MULTILINE)
    for package in ("events", "llm", "tools", "validity", "risk", "recovery", "safety", "paper"):
        for file in (PACKAGE_DIR / package).rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} depends on the mainnet adapter"
            )


def test_the_phase12_mainnet_guard_narrowing_is_explicit_and_reviewable():
    source = AUDIT_CHECKS.read_text(encoding="utf-8")

    assert "MAINNET_READONLY_DIR" in source
    assert "READ-ONLY" in source
    assert mainnet_package.__name__ == "crypto_trader.mainnet"


def test_no_push_was_performed_by_this_phase():
    git_dir = PROJECT_ROOT / ".git"
    config = (git_dir / "config").read_text(encoding="utf-8")

    for name, body in re.findall(
        r'\[branch "([^"]+)"\]\s*(.*?)(?=\n\[|\Z)', config, re.DOTALL
    ):
        if name.startswith("phase-15"):
            assert "remote =" not in body, f"the {name} branch has a push upstream"
    remote_refs = list((git_dir / "refs" / "remotes").rglob("*"))
    assert not any("phase-15" in ref.name for ref in remote_refs if ref.is_file())
