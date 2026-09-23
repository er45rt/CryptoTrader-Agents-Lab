"""Phase 10 scope/security boundary: shadow/paper stays engine-independent and non-live."""

import re
from pathlib import Path

import crypto_trader.paper as paper_package

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
PAPER_DIR = PACKAGE_DIR / "paper"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "accounting.py",
    "config.py",
    "decisions.py",
    "errors.py",
    "feed.py",
    "modes.py",
    "report.py",
    "session.py",
    "simulation.py",
}


def _files() -> list[Path]:
    return [file for file in PAPER_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def test_paper_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES

    for forbidden in ("oms", "simulator", "event_bus", "broker", "state_store", "redis"):
        assert not (PAPER_DIR / forbidden).exists()


def test_paper_has_no_engine_llm_or_broker_dependency():
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(nautilus_trader|nautilus|crewai|crypto_trader\.llm|crypto_trader\.execution\.nautilus"
        r"|redis|kafka|pika|nats|sqlalchemy)\b",
        re.MULTILINE,
    )
    for file in _files():
        text = file.read_text(encoding="utf-8")
        match = forbidden_imports.search(text)
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"


def test_paper_never_uses_pickle_or_dynamic_code_execution():
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert "import pickle" not in text
        assert "pickle.load" not in text
        assert "eval(" not in text
        assert "exec(" not in text


def test_paper_performs_no_network_io_and_no_venue_client():
    """Import-level check: the paper layer may discuss the network, never perform it."""
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+(requests|httpx|socket|subprocess|urllib|websocket|aiohttp)\b",
        re.MULTILINE,
    )
    forbidden_calls = (
        "requests.get(",
        "requests.post(",
        "requests.request(",
        "httpx.",
        "socket.socket(",
        "subprocess.",
        "urllib.request",
        "websocket.",
        "binance.com",
        "api/v3",
        "hmac.new",
        "hmac(",
    )
    for file in _files():
        text = file.read_text(encoding="utf-8")
        match = forbidden_imports.search(text)
        assert match is None, f"paper/{file.name} imports network code: {match.group(1)}"
        lowered = text.lower()
        for token in forbidden_calls:
            assert token not in lowered, f"paper/{file.name} contains {token}"


def test_paper_introduces_no_credential_or_authenticated_capability():
    forbidden = (
        "api_key",
        "apikey",
        "api_secret",
        "secret_key",
        "x-mbx-apikey",
        "binance_api_key",
        "binance_api_secret",
        "binance_testnet",
        "hmac",
        "withdraw",
        "create_order",
        "place_order",
        "execute_trade",
    )
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"paper/{file.name} contains {token}"


def test_paper_public_api_is_engine_independent():
    exported = set(paper_package.__all__)

    assert {"TradingSession", "TradingMode", "RunReport", "ExecutionSimulator"} <= exported
    for name in exported:
        assert "nautilus" not in name.lower()


def test_only_infrastructure_may_import_the_engine():
    """The engine stays behind the adapter package, exactly as Phase 4 required."""
    allowed = PACKAGE_DIR / "execution" / "nautilus"
    tests_dir = PACKAGE_DIR / "tests"
    audit_dir = PACKAGE_DIR / "audit"
    offenders = []
    for file in PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in file.parts:
            continue
        if file.is_relative_to(allowed) or file.is_relative_to(tests_dir):
            continue
        if file.is_relative_to(audit_dir):
            # Phase 12's offline audit reads the installed engine version and runs the offline
            # simulator as evidence; it has no network capability (see test_audit_scope.py).
            continue
        if "nautilus_trader" in file.read_text(encoding="utf-8"):
            offenders.append(str(file.relative_to(PROJECT_ROOT)))

    assert offenders == []


def test_paper_does_not_leak_into_protected_layers():
    import_pattern = re.compile(r"^\s*(?:import|from)\s+crypto_trader\.paper", re.MULTILINE)
    for path in (PACKAGE_DIR / "events", PACKAGE_DIR / "llm", PACKAGE_DIR / "tools"):
        for file in path.rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} imports the paper layer"
            )


def test_paper_adds_no_new_event_contract():
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
        # Phase 13 additive contract (existing schemas unchanged).
        "safety.operator_command.recorded",
        # Phase 15 additive contract (existing schemas unchanged).
        "stream.lifecycle.recorded",
    }
