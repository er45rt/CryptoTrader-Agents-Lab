"""Phase 13 scope/security boundary for the operational break-glass path.

The break-glass path is the only place in this project that may drive authenticated venue control
outside the Phase 11 adapter, so these guards prove it stays narrow: testnet only, no mainnet, no
withdrawal, no liquidation, no secret, no LLM dependency and no repository-side effect.
"""

import re
from pathlib import Path

import crypto_trader.breakglass as breakglass_package

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
BREAKGLASS_DIR = PACKAGE_DIR / "breakglass"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "cli.py",
    "commands.py",
    "control.py",
    "epoch.py",
    "errors.py",
    "ownership.py",
    "state.py",
    "venue.py",
}
_AUTHORIZED_AUDIT_MODULES = {"__init__.py", "checks.py", "matrix.py", "scenarios.py"}


def _files() -> list[Path]:
    return [file for file in BREAKGLASS_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def _implementation_files() -> list[Path]:
    return [file for file in _files() if not file.is_relative_to(BREAKGLASS_DIR / "audit")]


def test_breakglass_package_contains_only_authorized_components():
    assert {file.name for file in _implementation_files()} == _AUTHORIZED_MODULES
    assert {
        file.name
        for file in (BREAKGLASS_DIR / "audit").glob("*.py")
        if "__pycache__" not in file.parts
    } == _AUTHORIZED_AUDIT_MODULES

    for forbidden in ("oms", "simulator", "event_bus", "broker", "state_store", "redis"):
        assert not (BREAKGLASS_DIR / forbidden).exists()


def test_breakglass_has_no_engine_or_llm_dependency():
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(nautilus_trader|nautilus|crewai|crypto_trader\.llm|crypto_trader\.crew|"
        r"crypto_trader\.paper|redis|kafka|pika|nats|sqlalchemy)\b",
        re.MULTILINE,
    )
    for file in _files():
        match = forbidden_imports.search(file.read_text(encoding="utf-8"))
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"


def test_breakglass_never_uses_pickle_or_dynamic_code_execution():
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert "import pickle" not in text
        assert "pickle.load" not in text
        assert "eval(" not in text
        assert "exec(" not in text
        assert "subprocess" not in text


def test_breakglass_reads_no_credentials_of_its_own():
    """Credentials may only ever come from the Phase 11 adapter's environment loader."""
    for file in _implementation_files():
        text = file.read_text(encoding="utf-8")
        for token in ("os.environ", "getenv(", "putenv", "keyring", ".netrc", "dotenv"):
            assert token not in text, f"{file.name} reads credentials directly: {token}"


def test_breakglass_introduces_no_mainnet_or_out_of_scope_endpoint():
    forbidden = (
        "api.binance.com",
        "fapi.",
        "dapi.",
        "eapi.",
        "/sapi/",
        "binance.us",
        "listenkey",
    )
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            if file.name in _AUTHORIZED_AUDIT_MODULES:
                # The audit harness NAMES the tokens it forbids (exactly like Phase 12's checks.py);
                # it never uses them. `test_breakglass_audit.py` proves the harness is offline.
                continue
            assert token not in text, f"{file.name} references {token}"


def test_breakglass_has_no_withdrawal_or_liquidation_capability():
    forbidden = (
        "withdraw" + "/apply",
        "/sapi/v1/capital/" + "withdraw",
        "universal" + "transfer",
        "flatten_all",
        "panic_liquidat",
        "liquidate(",
        "market_sell",
    )
    for file in _implementation_files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{file.name} contains {token}"


def test_breakglass_never_places_an_order():
    """The break-glass path may cancel and inspect; it may never create exposure."""
    forbidden = ("create_order", "place_order", "submit_order", "new_order", "execute_trade")
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{file.name} contains {token}"


def test_breakglass_only_cancels_through_the_authorised_testnet_adapter():
    venue = (BREAKGLASS_DIR / "venue.py").read_text(encoding="utf-8")

    assert "crypto_trader.execution.binance_testnet" in venue
    assert "RequestsTransport" not in venue  # the CLI owns the real transport, not the control
    assert "require_testnet_base_url" in venue
    assert "assert_no_mainnet_reference" in venue


def test_breakglass_public_api_is_engine_independent_and_testnet_scoped():
    exported = set(breakglass_package.__all__)

    assert {"BreakGlassControl", "OperatorCommandType", "OwnershipStore"} <= exported
    for name in exported:
        assert "nautilus" not in name.lower()


def test_domain_packages_do_not_depend_on_breakglass():
    import_pattern = re.compile(r"^\s*(?:import|from)\s+crypto_trader\.breakglass", re.MULTILINE)
    for package in ("events", "llm", "tools", "validity", "risk", "recovery", "safety", "paper"):
        for file in (PACKAGE_DIR / package).rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} depends on the break-glass path"
            )


def test_the_safety_layer_is_not_modified_by_phase13():
    """Phase 13 adds a control path beside the Phase 9 controls; it never rewrites them."""
    safety = (PACKAGE_DIR / "safety" / "breakglass.py").read_text(encoding="utf-8")

    assert "class BreakGlassPort(Protocol)" in safety
    assert "class SimulatedBreakGlass" in safety
    assert "PRE-LIVE MANDATORY GAP" in safety


def test_phase13_adds_exactly_one_additive_event_contract():
    contracts = (PACKAGE_DIR / "events" / "contracts.py").read_text(encoding="utf-8")
    event_types = set(re.findall(r'= "([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"', contracts))

    assert event_types == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        # Phase 15 durable ambiguity additive contract (existing schemas unchanged).
        "order.submission.attempted",
        "execution.fill.recorded",
        "recovery.state.changed",
        "validity.disposition.recorded",
        "risk.evaluation.recorded",
        "safety.state.changed",
        "safety.operator_command.recorded",
        "stream.lifecycle.recorded",
    }


def test_repository_push_policy_allows_only_origin_for_non_protected_work_branch():
    head = (PROJECT_ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    config = (PROJECT_ROOT / ".git" / "config").read_text(encoding="utf-8")

    assert branch not in {"main", "master"}
    assert not re.search(r"^\s*pushurl\s*=", config, re.MULTILINE | re.IGNORECASE)

    branch_remote = None
    for name, body in re.findall(
        r'\[branch "([^"]+)"\]\s*(.*?)(?=\n\[|\Z)', config, re.DOTALL
    ):
        if name != branch:
            continue
        match = re.search(r"^\s*remote\s*=\s*(.+)$", body, re.MULTILINE)
        if match is not None:
            branch_remote = match.group(1).strip()

    assert branch_remote in {None, "origin"}
