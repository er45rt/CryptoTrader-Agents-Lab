"""Transitive AST import isolation and exact-path audit mutation proofs."""
import ast
from importlib.util import resolve_name
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from crypto_trader.audit import checks

PACKAGE = Path(__file__).resolve().parents[1]
DERIVATIVES = {
    "crypto_trader.tools.binance_futures_public_tool",
    "crypto_trader.tools.derivatives_schema",
}
PROTECTED = ("risk", "execution", "production", "recovery", "persistence", "events", "paper",
             "safety", "breakglass", "validity", "mainnet")
REVERSE_PROTECTED = PROTECTED


def _under(module, packages):
    return any(module == "crypto_trader." + name or module.startswith("crypto_trader." + name + ".") for name in packages)


def import_graph(overrides=None):
    sources = {}
    packages = set()
    for path in PACKAGE.rglob("*.py"):
        relative_parts = path.relative_to(PACKAGE).parts
        if not relative_parts or relative_parts[0] == "tests" or "__pycache__" in relative_parts:
            continue
        parts = path.relative_to(PACKAGE).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
            packages.add(".".join(("crypto_trader", *parts)))
        sources[".".join(("crypto_trader", *parts))] = path.read_text(encoding="utf-8")
    sources.update(overrides or {})
    graph, opaque = {}, {}
    for module, source in sources.items():
        tree, imports, problems = ast.parse(source), set(), []
        importlib_aliases, import_functions = {"importlib"}, {"__import__"}
        builtin_aliases, sys_aliases = {"builtins"}, {"sys"}
        context = module if module in packages else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
                    if alias.name == "importlib":
                        importlib_aliases.add(alias.asname or alias.name)
                    if alias.name == "builtins":
                        builtin_aliases.add(alias.asname or alias.name)
                    if alias.name == "sys":
                        sys_aliases.add(alias.asname or alias.name)
                    if alias.name.startswith(("importlib.util", "importlib.machinery", "runpy", "pkgutil")):
                        problems.append(f"opaque loader import:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), context) if node.level else (node.module or "")
                imports.add(base)
                imports.update(base + "." + alias.name for alias in node.names if alias.name != "*")
                if base == "importlib":
                    import_functions.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
                    if any(alias.name in ("util", "machinery") for alias in node.names):
                        problems.append(f"opaque loader import:{node.lineno}")
                if base.startswith(("importlib.util", "importlib.machinery", "runpy", "pkgutil")):
                    problems.append(f"opaque loader import:{node.lineno}")
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "modules" and isinstance(node.value.value, ast.Name)
                    and node.value.value.id in sys_aliases):
                problems.append(f"opaque module registry access:{node.lineno}")
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            root = function
            while isinstance(root, ast.Attribute):
                root = root.value
            if (isinstance(root, ast.Name) and root.id in importlib_aliases
                    and not (isinstance(function, ast.Attribute) and function.attr == "import_module"
                             and isinstance(function.value, ast.Name))):
                problems.append(f"opaque importlib loader:{node.lineno}")
            if (isinstance(function, ast.Attribute) and function.attr == "__import__"
                    and isinstance(root, ast.Name) and root.id in builtin_aliases):
                problems.append(f"opaque builtin loader:{node.lineno}")
            dynamic = (isinstance(function, ast.Name) and function.id in import_functions) or (
                isinstance(function, ast.Attribute) and function.attr == "import_module"
                and isinstance(function.value, ast.Name) and function.value.id in importlib_aliases)
            if dynamic:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    name = node.args[0].value
                    if name.startswith("."):
                        # A dynamic relative import also needs an explicit package.
                        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                            name = resolve_name(name, node.args[1].value)
                        else:
                            problems.append(f"unresolved relative dynamic import:{node.lineno}")
                    imports.add(name)
                else:
                    problems.append(f"opaque dynamic import:{node.lineno}")
            if isinstance(function, ast.Name) and function.id in ("exec", "eval"):
                problems.append(f"opaque code loading:{node.lineno}")
            if (isinstance(function, ast.Name) and function.id == "getattr" and node.args
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in importlib_aliases):
                problems.append(f"opaque reflected import:{node.lineno}")
        expanded = set()
        for name in imports:
            if name.split(".")[-1] in {target.split(".")[-1] for target in DERIVATIVES}:
                expanded.update(target for target in DERIVATIVES if target.split(".")[-1] == name.split(".")[-1])
            if name in sources:
                expanded.add(name)
            segments = name.split(".")
            expanded.update(".".join(segments[:index]) for index in range(1, len(segments))
                            if ".".join(segments[:index]) in packages)
        graph[module], opaque[module] = expanded, problems
    return graph, opaque


def isolation_violations(overrides=None):
    graph, opaque = import_graph(overrides)
    violations = []
    for root in graph:
        forward, reverse = _under(root, PROTECTED), root in DERIVATIVES
        if not forward and not reverse:
            continue
        stack, visited = [(root, [root])], set()
        while stack:
            module, route = stack.pop()
            if module in visited:
                continue
            visited.add(module)
            if ((forward and module in DERIVATIVES) or (reverse and _under(module, REVERSE_PROTECTED))):
                violations.append(" -> ".join(route))
            for reason in opaque.get(module, []):
                violations.append(" -> ".join(route) + ":" + reason)
            stack.extend((dependency, route + [dependency]) for dependency in graph.get(module, []))
    return sorted(set(violations))


def test_import_graph_isolation_forward_and_reverse():
    assert isolation_violations() == []


@pytest.mark.parametrize("package", PROTECTED)
@pytest.mark.parametrize("statement", [
    "import crypto_trader.tools.binance_futures_public_tool",
    "from crypto_trader.tools import derivatives_schema",
    "from ..tools import derivatives_schema",
    "import derivatives_schema",
    "__import__('crypto_trader.tools.derivatives_schema')",
])
def test_future_forbidden_direct_or_relative_import_fails(package, statement):
    bad = isolation_violations({f"crypto_trader.{package}.future_violation": statement})
    assert bad and any("derivatives_schema" in item or "binance_futures_public_tool" in item for item in bad)


def test_future_transitive_dependency_import_fails():
    bad = isolation_violations({"crypto_trader.risk.future_violation": "from crypto_trader import intermediary",
                               "crypto_trader.intermediary": "from crypto_trader.tools import derivatives_schema"})
    assert any("intermediary -> crypto_trader.tools.derivatives_schema" in item for item in bad)


def test_future_reverse_domain_import_fails():
    bad = isolation_violations({"crypto_trader.tools.derivatives_schema": "from crypto_trader import execution"})
    assert any("derivatives_schema -> crypto_trader.execution" in item for item in bad)


@pytest.mark.parametrize("statement", [
    "from importlib import import_module as x\nx(variable)",
    "import importlib as x\nx.import_module(variable)",
    "from importlib.util import spec_from_file_location",
    "import runpy", "import pkgutil", "exec('pass')", "eval('0')",
    "import importlib\ngetattr(importlib, 'import_module')(variable)",
    "import importlib\nimportlib.util.spec_from_file_location('x', variable)",
    "from importlib import util", "from importlib import machinery",
    "import builtins\nbuiltins.__import__(variable)",
    "import importlib\nimportlib.__import__(variable)",
    "import sys\nsys.modules[variable]",
])
def test_future_opaque_loader_fails_closed(statement):
    assert isolation_violations({"crypto_trader.risk.future_violation": statement})


@pytest.mark.parametrize("wrapper", ["if TYPE_CHECKING:\n    {}", "def deferred():\n    {}", "try:\n    {}\nexcept ImportError:\n    pass"])
def test_import_graph_includes_deferred_and_guarded_imports(wrapper):
    assert isolation_violations({"crypto_trader.execution.future_violation": wrapper.format("from crypto_trader.tools import derivatives_schema")})


def _audit_files(monkeypatch, tmp_path, files, inventory_errors=()):
    package = tmp_path / "src" / "crypto_trader"
    # The full auditor also inspects the already-authorized credential module's
    # source. Copy its code into the fixture; no credential values are read.
    credential_source = (checks.TESTNET_DIR / "credentials.py").read_text(encoding="utf-8")
    credentials_path = package / "execution" / "binance_testnet" / "credentials.py"
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text(credential_source, encoding="utf-8")
    paths = []
    for name, content in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        paths.append(path)
    monkeypatch.setattr(checks, "PACKAGE_DIR", package)
    monkeypatch.setattr(checks, "FUTURES_PUBLIC_ADAPTER", package / "tools" / "binance_futures_public_tool.py")
    monkeypatch.setattr(checks, "TESTNET_DIR", package / "execution" / "binance_testnet")
    monkeypatch.setattr(checks, "MAINNET_READONLY_DIR", package / "mainnet")
    monkeypatch.setattr(checks, "NAUTILUS_ADAPTER_DIR", package / "execution" / "nautilus")
    monkeypatch.setattr(checks, "_relative", lambda path: str(path.relative_to(tmp_path)))
    monkeypatch.setattr(checks, "_production_files", lambda: paths)
    monkeypatch.setattr(checks, "_inventory_errors", lambda _files: list(inventory_errors))
    return next(item for item in checks.run_static_checks() if item.item_id == "no_mainnet_capability")


def test_exact_public_futures_adapter_audit_exemption(monkeypatch, tmp_path):
    row = _audit_files(monkeypatch, tmp_path, {"tools/binance_futures_public_tool.py": 'HOST="https://fapi.binance.com"'})
    assert row.status == "pass" and row.evidence["offenders"] == []


@pytest.mark.parametrize("path", [
    "risk/host.py", "execution/host.py", "production/host.py", "recovery/host.py",
    "execution/binance_testnet/host.py", "mainnet/host.py", "tools/other.py",
    "risk/binance_futures_public_tool.py", "tools/binance_futures_public_tool_copy.py",
    "tools/binance_public_tool.py",
    "audit/other.py",
])
def test_any_unauthorized_futures_host_remains_audit_offender(monkeypatch, tmp_path, path):
    row = _audit_files(monkeypatch, tmp_path, {path: 'HOST="https://fapi.binance.com"'})
    assert row.status == "fail" and len(row.evidence["offenders"]) == 1
    assert row.evidence["offenders"][0].endswith(":fapi.")


@pytest.mark.parametrize("token", ["/fapi/v1/order", "X-MBX-APIKEY", "signature", "listenKey", "/sapi/", "withdraw/apply"])
def test_public_adapter_exemption_never_allows_authentication_or_mutation(monkeypatch, tmp_path, token):
    row = _audit_files(monkeypatch, tmp_path, {"tools/binance_futures_public_tool.py": f'TOKEN="{token}"'})
    if token == "withdraw/apply":
        row = next(item for item in checks.run_static_checks() if item.item_id == "no_withdrawal_capability")
    assert row.status == "fail" and row.evidence["offenders"]


@pytest.mark.parametrize("token", ["signature", "listenKey", "fapi.binance.com"])
def test_schema_has_no_host_or_credential_exception(monkeypatch, tmp_path, token):
    row = _audit_files(monkeypatch, tmp_path, {"tools/derivatives_schema.py": f'TOKEN="{token}"'})
    assert row.status == "fail" and row.evidence["offenders"]


def test_resolved_alias_does_not_inherit_adapter_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: tmp_path / "src/crypto_trader/tools/binance_futures_public_tool.py")
    row = _audit_files(monkeypatch, tmp_path, {"risk/alias.py": 'HOST="fapi.binance.com"'})
    assert row.status == "fail"


@pytest.mark.parametrize("relative", ["tools/binance_futures_public_tool.py", "tools", "."])
def test_simulated_reparse_component_cannot_receive_exception(monkeypatch, tmp_path, relative):
    package = tmp_path / "src/crypto_trader"
    flagged = package / relative
    original = Path.lstat
    def fake_lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT) if path == flagged else result
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    row = _audit_files(monkeypatch, tmp_path, {"tools/binance_futures_public_tool.py": 'HOST="fapi.binance.com"'})
    assert row.status == "fail"


def _pinned_legacy_policy_bytes():
    import hashlib
    content = (checks.TESTNET_DIR / "endpoints.py").read_bytes()
    assert hashlib.sha256(content).hexdigest() == checks.LEGACY_TESTNET_ENDPOINT_POLICY_SHA256
    return content


def test_existing_authorized_testnet_denylist_exact_bytes_are_allowed(monkeypatch, tmp_path):
    content = _pinned_legacy_policy_bytes()
    row = _audit_files(monkeypatch, tmp_path, {"execution/binance_testnet/endpoints.py": content})
    assert row.status == "pass" and row.evidence["offenders"] == []
    assert any(checks.LEGACY_TESTNET_ENDPOINT_POLICY_SHA256 in value for value in row.evidence["exempt"])


@pytest.mark.parametrize("mutation", ["flip", "append", "crlf", "replace"])
def test_modified_legacy_denylist_is_not_exempt(monkeypatch, tmp_path, mutation):
    content = _pinned_legacy_policy_bytes()
    variants = {"flip": bytes([content[0] ^ 1]) + content[1:], "append": content + b"\n# change\n",
                "crlf": content.replace(b"\n", b"\r\n"), "replace": b'HOST="fapi.binance.com"'}
    row = _audit_files(monkeypatch, tmp_path, {"execution/binance_testnet/endpoints.py": variants[mutation]})
    assert row.status == "fail" and row.evidence["offenders"][0].endswith("endpoints.py:fapi.")


@pytest.mark.parametrize("path", ["execution/binance_testnet/endpoints_copy.py",
    "execution/binance_testnet/sub/endpoints.py", "execution/endpoints.py", "risk/endpoints.py",
    "mainnet/endpoints.py", "tools/endpoints.py"])
def test_pinned_legacy_bytes_at_other_paths_are_offenders(monkeypatch, tmp_path, path):
    row = _audit_files(monkeypatch, tmp_path, {path: _pinned_legacy_policy_bytes()})
    assert row.status == "fail" and row.evidence["offenders"][0].endswith(":fapi.")


@pytest.mark.parametrize("relative", ["execution/binance_testnet/endpoints.py", "execution/binance_testnet", "execution", "."])
def test_legacy_exception_rejects_simulated_reparse_ancestors(monkeypatch, tmp_path, relative):
    content = _pinned_legacy_policy_bytes()
    flagged = tmp_path / "src/crypto_trader" / relative
    original = Path.lstat
    def fake_lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT) if path == flagged else result
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    row = _audit_files(monkeypatch, tmp_path, {"execution/binance_testnet/endpoints.py": content})
    assert row.status == "fail"


def test_resolved_alias_does_not_inherit_legacy_exception(monkeypatch, tmp_path):
    content = _pinned_legacy_policy_bytes()
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: tmp_path / "src/crypto_trader/execution/binance_testnet/endpoints.py")
    row = _audit_files(monkeypatch, tmp_path, {"risk/legacy_alias.py": content})
    assert row.status == "fail"


def test_unreadable_legacy_policy_fails_closed(monkeypatch, tmp_path):
    content = _pinned_legacy_policy_bytes()
    original = Path.read_bytes
    def denied(path):
        if path == tmp_path / "src/crypto_trader/execution/binance_testnet/endpoints.py":
            raise OSError("unreadable fixture")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", denied)
    row = _audit_files(monkeypatch, tmp_path, {"execution/binance_testnet/endpoints.py": content})
    assert row.status == "fail" and row.evidence["offenders"][0].endswith(":unreadable_source")


def test_real_tree_audit_preserves_pinned_legacy_exception():
    _pinned_legacy_policy_bytes()
    row = next(item for item in checks.run_static_checks() if item.item_id == "no_mainnet_capability")
    assert row.status == "pass" and row.evidence["offenders"] == []
    assert any(checks.LEGACY_TESTNET_ENDPOINT_POLICY_SHA256 in value for value in row.evidence["exempt"])


def test_empty_inventory_fails_closed(monkeypatch, tmp_path):
    assert "inventory:empty" in checks._inventory_errors([])
    row = _audit_files(monkeypatch, tmp_path, {}, inventory_errors=("inventory:empty",))
    assert row.status == "fail"
    assert "inventory:empty" in row.evidence["offenders"]


def test_production_inventory_exclusions_are_package_relative(monkeypatch, tmp_path):
    package = tmp_path / "tests" / "checkout" / "src" / "crypto_trader"
    good = package / "risk" / "rule.py"
    ignored_test = package / "tests" / "test_fake.py"
    ignored_cache = package / "risk" / "__pycache__" / "cached.py"
    for path in (good, ignored_test, ignored_cache):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(checks, "PACKAGE_DIR", package)
    found = set(checks._production_files())
    assert good in found
    assert ignored_test not in found
    assert ignored_cache not in found


@pytest.mark.parametrize("path", [
    "execution/binance_testnet/future.py",
    "mainnet/future.py",
    "audit/other.py",
    "tools/binance_public_tool.py",
    "risk/future.py",
    "execution/future.py",
    "production/future.py",
    "recovery/future.py",
])
@pytest.mark.parametrize("token", [
    "/fapi/v1/order",
    "/fapi/v2/account",
    "/fapi/v3/positionRisk",
    "/fapi/v1/leverage",
    "/dapi/v1/order",
    "/papi/v1/um/order",
])
def test_legacy_exemptions_never_authorize_futures_sensitive_tokens(monkeypatch, tmp_path, path, token):
    row = _audit_files(monkeypatch, tmp_path, {path: f'TOKEN="{token}"'})
    assert row.status == "fail"
    assert row.evidence["offenders"]


def test_audit_self_futures_token_exception_is_exact_regular_path(monkeypatch, tmp_path):
    row = _audit_files(monkeypatch, tmp_path, {"audit/checks.py": 'TOKEN="fapi.binance.com"'})
    assert row.status == "pass"

    package = tmp_path / "src" / "crypto_trader"
    flagged = package / "audit" / "checks.py"
    original = Path.lstat

    def fake_lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == flagged:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    row = _audit_files(monkeypatch, tmp_path, {"audit/checks.py": 'TOKEN="fapi.binance.com"'})
    assert row.status == "fail"


@pytest.mark.parametrize(("path", "token"), [
    ("execution/binance_testnet/future.py", "/fapi/v1/countDownCancelAll"),
    ("risk/future.py", "/fapi/v1/multiAssetsMargin"),
    ("production/future.py", "/dapi/v1/openOrders"),
    ("recovery/future.py", "/papi/v1/um/conditional/order"),
])
def test_unlisted_versioned_futures_routes_fail_closed(monkeypatch, tmp_path, path, token):
    row = _audit_files(monkeypatch, tmp_path, {path: f'ROUTE="{token}"'})
    assert row.status == "fail"
    assert any(token.lower() in offender.lower() for offender in row.evidence["offenders"])


@pytest.mark.parametrize("route", [
    "/fapi/v1/fundingRate",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/fundingInfo",
    "/fapi/v1/openInterest",
])
def test_exact_public_adapter_allows_only_known_public_versioned_routes(monkeypatch, tmp_path, route):
    row = _audit_files(
        monkeypatch,
        tmp_path,
        {"tools/binance_futures_public_tool.py": f'ROUTE="{route}"'},
    )
    assert row.status == "pass"
    assert row.evidence["offenders"] == []


def test_exact_public_adapter_rejects_unlisted_versioned_route(monkeypatch, tmp_path):
    token = "/fapi/v1/countDownCancelAll"
    row = _audit_files(
        monkeypatch,
        tmp_path,
        {"tools/binance_futures_public_tool.py": f'ROUTE="{token}"'},
    )
    assert row.status == "fail"
    assert any(token.lower() in offender.lower() for offender in row.evidence["offenders"])
