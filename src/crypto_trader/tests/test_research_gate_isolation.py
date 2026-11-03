"""P4-06: the research gate is sandboxed on its own (it lives outside validation/ by owner decision).

Checks the gate's direct imports, its use of public validation interfaces only, and its real
transitive import closure in a fresh isolated interpreter that must load THIS repository's code.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "research_gate.py"
SRC = GATE.parents[1]
_STDLIB_ALLOWED = {
    "__future__", "dataclasses", "datetime", "decimal", "enum", "hashlib", "json", "re", "typing",
}
_VALIDATION_ALLOWED = {
    "crypto_trader.validation.canonical", "crypto_trader.validation.contracts",
    "crypto_trader.validation.registry", "crypto_trader.validation.dsr",
    "crypto_trader.validation.pbo", "crypto_trader.validation.walk_forward",
    "crypto_trader.validation.holdout",
}
_FORBIDDEN_TOP_LEVEL = {
    "socket", "ssl", "http", "subprocess", "asyncio", "multiprocessing", "concurrent", "requests",
    "aiohttp", "websockets", "websocket", "httpx", "urllib3", "ccxt", "binance", "nautilus_trader",
    "crewai", "openai", "anthropic", "redis",
}


def _tree():
    return ast.parse(GATE.read_text(encoding="utf-8"))


def test_gate_imports_only_allowed_stdlib_and_named_validation_modules():
    imported_names = {}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in _STDLIB_ALLOWED, (alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, ("relative import", node.lineno)
            if node.module in _VALIDATION_ALLOWED:
                for alias in node.names:
                    assert not alias.name.startswith("_"), ("private name", alias.name, node.lineno)
                    imported_names[alias.asname or alias.name] = node.module
            else:
                assert node.module.split(".")[0] in _STDLIB_ALLOWED, (node.module, node.lineno)
    assert set(imported_names.values()) <= _VALIDATION_ALLOWED and imported_names


def test_gate_uses_no_private_attribute_or_dynamic_import_escape():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Attribute):
            assert not (node.attr.startswith("_") and not node.attr.startswith("__")), (node.attr, node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "compile", "__import__", "getattr", "setattr",
                                        "globals", "locals", "vars", "open"}, (node.func.id, node.lineno)
        if isinstance(node, ast.Name):
            assert node.id not in {"importlib", "__builtins__", "os", "sys"}, (node.id, node.lineno)


_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
before = set(sys.modules)
import crypto_trader, crypto_trader.research_gate as g
print(json.dumps({"package": crypto_trader.__file__, "gate": g.__file__,
                  "loaded": sorted(set(sys.modules) - before)}))
"""


def test_transitive_closure_in_a_fresh_isolated_interpreter_from_this_repository():
    done = subprocess.run([sys.executable, "-I", "-B", "-c", _PROBE, str(SRC)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    probe = json.loads(done.stdout.strip().splitlines()[-1])
    # The probe must have loaded THIS checkout, not an installed or sibling copy.
    assert Path(probe["gate"]).resolve() == GATE
    assert Path(probe["package"]).resolve() == SRC / "crypto_trader" / "__init__.py"
    ours = {m for m in probe["loaded"] if m == "crypto_trader" or m.startswith("crypto_trader.")}
    assert ours <= {"crypto_trader", "crypto_trader.research_gate", "crypto_trader.validation"} | _VALIDATION_ALLOWED
    assert "crypto_trader.research_gate" in ours and "crypto_trader.validation.holdout" in ours
    top = {m.split(".")[0] for m in probe["loaded"]}
    assert not top & _FORBIDDEN_TOP_LEVEL, top & _FORBIDDEN_TOP_LEVEL
    assert "urllib.request" not in probe["loaded"]


def test_gate_exposes_no_operational_surface():
    from crypto_trader import research_gate as gate
    names = {n.lower() for n in dir(gate)}
    for token in ("trade", "order", "promote", "deploy", "execute", "submit", "paper_session"):
        assert not any(token in n for n in names), token
    assert gate.OPERATIONAL_AUTHORIZATION == "NONE"


def test_gate_text_avoids_tokens_the_package_scans_forbid():
    text = GATE.read_text(encoding="utf-8")
    for token in ("nautilus_trader", "tinyfish", "import redis", "BINANCE_API", "deepseek_reasoner",
                  "fapi", "crypto_trader.execution", "crypto_trader.paper", "crypto_trader.production",
                  "crypto_trader.breakglass", "crypto_trader.mainnet"):
        assert token not in text, token
