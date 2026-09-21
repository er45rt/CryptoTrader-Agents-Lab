"""Gate 2/9/10: anti-corruption scope boundary for Phase 4."""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"

#: Protected layers that must never import or reference the engine.
PROTECTED_PATHS = {
    "phase3_events": PACKAGE_DIR / "events",
    "phase2_llm": PACKAGE_DIR / "llm",
    "phase1_tools": PACKAGE_DIR / "tools",
    "crew_agents": PACKAGE_DIR / "crew.py",
    "main_entrypoint": PACKAGE_DIR / "main.py",
}


def _python_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return [file for file in path.rglob("*.py") if "__pycache__" not in file.parts]


def test_protected_layers_contain_zero_nautilus_references():
    offending: dict[str, list[str]] = {}
    for label, path in PROTECTED_PATHS.items():
        hits = [
            str(file.relative_to(PROJECT_ROOT))
            for file in _python_files(path)
            if "nautilus" in file.read_text(encoding="utf-8").lower()
        ]
        if hits:
            offending[label] = hits

    assert offending == {}, f"Nautilus leaked into protected layers: {offending}"


def test_engine_independent_execution_modules_contain_no_nautilus_imports():
    import_pattern = re.compile(r"^\s*(import|from)\s+nautilus", re.MULTILINE)
    for name in ("ports.py", "errors.py", "fake.py", "__init__.py"):
        text = (PACKAGE_DIR / "execution" / name).read_text(encoding="utf-8")
        assert not import_pattern.search(text), f"execution/{name} must stay engine-independent"


def test_engine_imports_are_confined_to_the_nautilus_adapter_package():
    """Production code outside the adapter must not import the engine (tests may)."""
    allowed = PACKAGE_DIR / "execution" / "nautilus"
    tests_dir = PACKAGE_DIR / "tests"
    offenders = []
    for file in PACKAGE_DIR.rglob("*.py"):
        if "__pycache__" in file.parts:
            continue
        if file.is_relative_to(allowed):
            continue
        if file.is_relative_to(tests_dir):
            continue
        if "nautilus_trader" in file.read_text(encoding="utf-8"):
            offenders.append(str(file.relative_to(PROJECT_ROOT)))

    assert offenders == [], f"engine imported outside the adapter: {offenders}"


def test_nautilus_is_pinned_exactly_in_the_production_dependency_set():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert re.search(r'"nautilus_trader==1\.231\.0"', pyproject), "engine must be pinned exactly"
    assert 'requires-python = ">=3.12,<3.13"' in pyproject


def test_dynamic_latency_is_not_advertised_as_supported():
    """Spike B classified dynamic latency as PARTIAL; production must not claim otherwise."""
    adapter_text = "\n".join(
        file.read_text(encoding="utf-8") for file in (PACKAGE_DIR / "execution" / "nautilus").rglob("*.py")
    ).lower()

    for claim in ("dynamic latency is supported", "state-dependent latency is supported", "adaptive latency"):
        assert claim not in adapter_text


def test_no_authenticated_or_live_execution_capability_was_introduced():
    forbidden = (
        "x-mbx-apikey",
        "binance_api_key",
        "binance_api_secret",
        "api/v3/order",
        "api/v3/account",
        "create_order",
        "place_order",
        "execute_trade",
        "withdraw",
    )
    offenders = []
    for file in (PACKAGE_DIR / "execution").rglob("*.py"):
        text = file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            if token in text:
                offenders.append(f"{file.name}:{token}")

    assert offenders == [], f"unexpected execution capability: {offenders}"


def test_phase3_contracts_are_unchanged_by_phase4():
    """Phase 4 adds no new domain contracts and does not edit the existing ones."""
    contracts = (PACKAGE_DIR / "events" / "contracts.py").read_text(encoding="utf-8")
    event_types = set(re.findall(r'= "(strategy\.decision\.recorded|trade\.intent\.created|risk\.decision\.recorded|order\.state\.changed|execution\.fill\.recorded|recovery\.state\.changed)"', contracts))

    assert event_types == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        "execution.fill.recorded",
        "recovery.state.changed",
    }
