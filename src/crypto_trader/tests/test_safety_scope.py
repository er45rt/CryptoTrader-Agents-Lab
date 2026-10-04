"""Phase 9 scope/security boundary: the safety layer stays engine-independent and safe."""

import re
from pathlib import Path

import crypto_trader.safety as safety_package

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
SAFETY_DIR = PACKAGE_DIR / "safety"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "breakglass.py",
    "controls.py",
    "errors.py",
    "integration.py",
    "journaling.py",
    "lane.py",
    "modes.py",
}


def _files() -> list[Path]:
    return [file for file in SAFETY_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def test_safety_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES

    for forbidden in ("oms", "simulator", "event_bus", "broker", "state_store", "redis"):
        assert not (SAFETY_DIR / forbidden).exists()


def test_safety_has_no_engine_llm_or_broker_dependency():
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(nautilus_trader|crewai|crypto_trader\.llm|redis|kafka|pika|nats|sqlalchemy)\b",
        re.MULTILINE,
    )
    for file in _files():
        text = file.read_text(encoding="utf-8")
        match = forbidden_imports.search(text)
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"


def test_safety_never_uses_pickle_or_dynamic_code_execution():
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert "import pickle" not in text
        assert "pickle.load" not in text
        assert "eval(" not in text
        assert "exec(" not in text


def test_safety_performs_no_network_or_storage_io():
    forbidden = ("requests.", "httpx", "socket.", "subprocess", "urllib", "sqlite3", "websocket")
    file_open = re.compile(r"(?<![\w.])open\s*\(")
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"safety/{file.name} performs I/O: {token}"
        assert not file_open.search(text), f"safety/{file.name} opens files directly"


def test_safety_introduces_no_order_placement_or_venue_capability():
    forbidden = (
        "create_order",
        "cancel_order",
        "place_order",
        "execute_trade",
        "api/v3/order",
        "api/v3/account",
        "x-mbx-apikey",
        "binance_api_key",
        "binance_api_secret",
        "binance_testnet",
        "withdraw",
        "hmac",
    )
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"safety/{file.name} contains {token}"


def test_break_glass_is_interface_plus_simulation_only():
    source = (SAFETY_DIR / "breakglass.py").read_text(encoding="utf-8")
    lowered = source.lower()

    assert "class BreakGlassPort(Protocol)" in source
    assert "class SimulatedBreakGlass" in source
    assert "import requests" not in lowered
    assert "requests.get" not in lowered
    assert "requests.post" not in lowered
    # The live break-glass path remains an explicit pre-live gap, not a shipped capability.
    assert "PRE-LIVE MANDATORY GAP" in source


def test_safety_reuses_the_phase3_state_and_only_adds_one_contract():
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
        "venue.trade.recorded",
        "account.equity.recorded",
        "recovery.state.changed",
        "validity.disposition.recorded",
        "risk.evaluation.recorded",
        "safety.state.changed",
        # Phase 13 additive contract (existing schemas unchanged).
        "safety.operator_command.recorded",
        # Phase 15 additive contract (existing schemas unchanged).
        "stream.lifecycle.recorded",
    }


def test_safety_public_api_is_engine_independent():
    exported = set(safety_package.__all__)

    assert {"SafetyController", "SafetyMode", "PrioritySafetyLane"} <= exported
    for name in exported:
        assert "nautilus" not in name.lower()


def test_safety_does_not_leak_into_protected_layers():
    """The safety layer sits above risk; the Phase 1/2/3 layers must not depend on it."""
    import_pattern = re.compile(r"^\s*(?:import|from)\s+crypto_trader\.safety", re.MULTILINE)
    for path in (PACKAGE_DIR / "events", PACKAGE_DIR / "llm", PACKAGE_DIR / "tools"):
        for file in path.rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} imports the safety layer"
            )
