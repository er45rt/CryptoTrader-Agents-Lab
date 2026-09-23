"""Phase 8 scope/security boundary: the recovery layer stays engine-independent and safe."""

import re
from pathlib import Path

import crypto_trader.recovery as recovery_package

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
RECOVERY_DIR = PACKAGE_DIR / "recovery"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "engine.py",
    "errors.py",
    "journaling.py",
    "models.py",
    "policy.py",
    "reconstruction.py",
    "runtime.py",
}


def _files() -> list[Path]:
    return [file for file in RECOVERY_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def test_recovery_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES

    for forbidden in ("oms", "simulator", "event_bus", "broker", "state_store", "redis"):
        assert not (RECOVERY_DIR / forbidden).exists()


def test_recovery_has_no_engine_llm_or_broker_dependency():
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(nautilus_trader|crewai|crypto_trader\.llm|redis|kafka|pika|nats|sqlalchemy)\b",
        re.MULTILINE,
    )
    for file in _files():
        text = file.read_text(encoding="utf-8")
        match = forbidden_imports.search(text)
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"


def test_recovery_never_uses_pickle_or_dynamic_code_execution():
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert "import pickle" not in text
        assert "pickle.load" not in text
        assert "eval(" not in text
        assert "exec(" not in text


def test_recovery_performs_no_network_or_storage_io():
    forbidden = ("requests.", "httpx", "socket.", "subprocess", "urllib", "sqlite3")
    file_open = re.compile(r"(?<![\w.])open\s*\(")
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"recovery/{file.name} performs I/O: {token}"
        assert not file_open.search(text), f"recovery/{file.name} opens files directly"


def test_recovery_introduces_no_order_placement_capability():
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
        "withdraw",
    )
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"recovery/{file.name} contains {token}"


def test_recovery_reuses_the_phase3_recovery_contract_without_adding_one():
    contracts = (PACKAGE_DIR / "events" / "contracts.py").read_text(encoding="utf-8")
    event_types = set(
        re.findall(r'= "([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"', contracts)
    )

        # Phase 8 adds NO new event contract of its own: the Phase 3 contracts and the additive
        # Phase 6 / Phase 7 / Phase 9 / Phase 13 / Phase 15 contracts are the only ones present.
        # Phase 15 durable ambiguity adds exactly one more additive contract, consumed by
        # reconstruction; recovery still adds none of its own.
    assert event_types == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        "order.submission.attempted",
        "execution.fill.recorded",
        "recovery.state.changed",
        "validity.disposition.recorded",
        "risk.evaluation.recorded",
        "safety.state.changed",
        "safety.operator_command.recorded",
        "stream.lifecycle.recorded",
    }
    source = "\n".join(file.read_text(encoding="utf-8") for file in _files())
    assert "from crypto_trader.events import" in source
    assert "RecoveryStateChanged" in source


def test_recovery_public_api_is_engine_independent():
    exported = set(recovery_package.__all__)

    assert {"ReconciliationEngine", "RestartRecovery", "LocalOrderState"} <= exported
    for name in exported:
        assert "nautilus" not in name.lower()


def test_no_engine_import_outside_the_nautilus_adapter():
    """Guards the whole production tree: recovery must not smuggle the engine in."""
    allowed = PACKAGE_DIR / "execution" / "nautilus"
    tests_dir = PACKAGE_DIR / "tests"
    offenders = [
        str(file.relative_to(PROJECT_ROOT))
        for file in RECOVERY_DIR.rglob("*.py")
        if "nautilus_trader" in file.read_text(encoding="utf-8")
        and not file.is_relative_to(allowed)
        and not file.is_relative_to(tests_dir)
    ]

    assert offenders == []
