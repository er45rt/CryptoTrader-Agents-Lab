"""Phase 14 scope boundary: the production runtime stays testnet-only, secret-free and fenced.

Phase 14 adds the first production *decision boundary* that can reach an authenticated venue. These
guards prove the addition stays narrow and does not weaken any Phase 1-13 invariant.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
PRODUCTION_DIR = PACKAGE_DIR / "production"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "admission.py",
    "backfill.py",
    "environment.py",
    "errors.py",
    "equity.py",
    "fence.py",
    "risk_state.py",
    "runtime.py",
}

#: Capability tokens that must never appear in the production runtime implementation.
_AUTHENTICATED_OR_PRODUCTION_TOKENS = (
    "api/v3/order",
    "api/v3/account",
    "x-mbx-apikey",
    "/sapi/",
    "fapi.",
    "binance.us",
    "listenkey",
    "recvwindow",
)

_CREDENTIAL_TOKENS = (
    "credentials_from_environment",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "api_key",
    "api_secret",
    "keyring",
    ".netrc",
    "dotenv",
)

_ORDER_CAPABILITY_TOKENS = (
    "place_order",
    "create_order",
    "cancel_order",
    "execute_trade",
)

_WITHDRAWAL_TOKENS = ("withdraw/apply", "universaltransfer")

_FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(nautilus_trader|nautilus|crewai|crypto_trader\.llm|crypto_trader\.crew|"
    r"crypto_trader\.paper|redis|kafka|pika|nats|sqlalchemy)\b",
    re.MULTILINE,
)


def _files() -> list[Path]:
    return [file for file in PRODUCTION_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def _repository_mutation_violations(file: Path) -> list[str]:
    """Reject obvious repository/process mutation capabilities; obfuscated dynamic imports are out of scope."""
    tree = ast.parse(file.read_text(encoding="utf-8"))
    violations: list[str] = []
    blocked_modules = {"subprocess", "git", "pygit2", "dulwich", "pty"}
    importlib_module_names = {"importlib"}
    import_module_names: set[str] = set()

    def dangerous_os_name(name: str) -> bool:
        return name in {"system", "popen"} or name.startswith(("exec", "spawn", "fork"))

    def blocked_module_name(value: object) -> bool:
        if not isinstance(value, str):
            return False
        return any(value == module or value.startswith(module + ".") for module in blocked_modules)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in blocked_modules:
                    violations.append(f"import {alias.name}")
                if alias.name == "importlib":
                    importlib_module_names.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module in blocked_modules:
                violations.append(f"from {node.module} import ...")
            if node.module == "os":
                for alias in node.names:
                    if dangerous_os_name(alias.name):
                        violations.append(f"from os import {alias.name}")
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and dangerous_os_name(node.attr)
            ):
                violations.append(f"os.{node.attr}")
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "os"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and dangerous_os_name(node.args[1].value)
            ):
                violations.append(f"getattr(os, {node.args[1].value!r})")

            first_arg = (
                node.args[0].value
                if node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                else None
            )
            if blocked_module_name(first_arg):
                if isinstance(node.func, ast.Name) and (
                    node.func.id == "__import__" or node.func.id in import_module_names
                ):
                    violations.append(f"dynamic import {first_arg}")
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in importlib_module_names
                ):
                    violations.append(f"dynamic import {first_arg}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            normalized = node.value.replace("\\", "/").lower()
            if ".git" in [part for part in normalized.split("/") if part]:
                violations.append(f".git path literal on line {node.lineno}")

    return violations


def test_the_production_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES
    for forbidden in ("oms", "broker", "simulator", "event_bus", "state_store", "redis"):
        assert not (PRODUCTION_DIR / forbidden).exists()


def test_the_production_runtime_carries_no_mainnet_or_credential_capability():
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in (*_AUTHENTICATED_OR_PRODUCTION_TOKENS, *_CREDENTIAL_TOKENS, *_ORDER_CAPABILITY_TOKENS):
            assert token.lower() not in text, f"{file.name} contains {token}"


def test_the_production_runtime_has_no_withdrawal_capability():
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in _WITHDRAWAL_TOKENS:
            assert token not in text, f"{file.name} contains {token}"


def test_the_production_runtime_has_no_engine_broker_or_llm_dependency():
    for file in _files():
        match = _FORBIDDEN_IMPORTS.search(file.read_text(encoding="utf-8"))
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"
    # The runtime integrates the Phase 11 adapter; the adapter must not depend back on it.
    adapter = (PACKAGE_DIR / "execution" / "binance_testnet").rglob("*.py")
    for file in adapter:
        assert "crypto_trader.production" not in file.read_text(encoding="utf-8")


def test_the_production_runtime_reads_no_credential_configuration_of_its_own():
    joined = "\n".join(file.read_text(encoding="utf-8") for file in _files())

    assert "CRYPTO_TRADER_ENV" in joined, "the non-secret environment selector is expected"
    for token in (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_API_KEY",
        "keyring",
        ".netrc",
        "putenv",
    ):
        assert token not in joined, f"the runtime reads credential configuration: {token}"


def test_the_production_environment_defaults_to_testnet_and_production_is_disabled():
    source = (PRODUCTION_DIR / "environment.py").read_text(encoding="utf-8")

    assert 'DEFAULT_ENVIRONMENT = "testnet"' in source
    assert re.search(r"^MAINNET_ENABLED = False$", source, re.MULTILINE)
    assert "owner authorization gate" in source


def test_the_final_fence_is_revalidated_inside_the_exclusive_submission_path():
    """Structural complement to the behavioral fencing/concurrency regression tests."""
    source = (PRODUCTION_DIR / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    runtime = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductionRuntime"
    )
    methods = {
        node.name: node
        for node in runtime.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    wrapper = methods["submit_prepared"]
    locked = methods["_submit_prepared_locked"]

    helper_calls = [
        node for node in ast.walk(runtime)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_submit_prepared_locked"
    ]
    assert len(helper_calls) == 1
    assert helper_calls[0].lineno >= wrapper.lineno
    assert helper_calls[0].end_lineno <= wrapper.end_lineno

    exclusive_blocks = [
        node for node in wrapper.body
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "exclusive"
            and any(
                keyword.arg == "purpose"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "submission"
                for keyword in item.context_expr.keywords
            )
            for item in node.items
        )
    ]
    assert len(exclusive_blocks) == 1
    block = exclusive_blocks[0]
    assert block.lineno <= helper_calls[0].lineno <= block.end_lineno

    authorize_calls = [
        node for node in ast.walk(locked)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "authorize_new_risk"
    ]
    assert len(authorize_calls) == 1

    durable_calls = [
        node for node in ast.walk(runtime)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "DurableVenueWritePort"
        and node.func.attr == "submit_durably"
    ]
    plain_calls = [
        node for node in ast.walk(runtime)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
        and node.func.value.attr == "_execution"
    ]
    assert len(durable_calls) == 1
    assert len(plain_calls) == 1
    venue_calls = durable_calls + plain_calls
    assert all(locked.lineno <= call.lineno <= locked.end_lineno for call in venue_calls)
    assert authorize_calls[0].lineno < min(call.lineno for call in venue_calls)
    assert not any(isinstance(node, ast.Await) for node in ast.walk(locked))


def test_phase14_adds_no_event_contract():
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
        "safety.operator_command.recorded",
        # Phase 15 additive contract (existing schemas unchanged).
        "stream.lifecycle.recorded",
        # Review F-03 additive contracts (existing schemas unchanged).
        "breakglass.reconciliation.intake",
        "breakglass.reconciliation.observed",
        "breakglass.reconciliation.settled",
    }


def test_domain_packages_do_not_depend_on_the_production_runtime():
    import_pattern = re.compile(
        r"^\s*(?:import|from)\s+crypto_trader\.production", re.MULTILINE
    )
    for package in ("events", "llm", "tools", "validity", "risk", "recovery", "safety", "paper"):
        for file in (PACKAGE_DIR / package).rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} depends on the production runtime"
            )


def test_the_production_package_has_no_repository_mutation_capability():
    files = _files()
    assert files, "production capability scan must inspect at least one source file"
    for file in files:
        violations = _repository_mutation_violations(file)
        assert not violations, f"{file.name} can mutate repository/process state: {violations}"
