"""P5-01: the synthetic study runner is sandboxed on its own (direct, public-only, transitive)."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "research_study.py"
SRC = MODULE.parents[1]
_STDLIB_ALLOWED = {"__future__", "dataclasses", "decimal", "pathlib", "random", "typing"}
_LOCAL_ALLOWED = {
    "crypto_trader.research_gate", "crypto_trader.validation.canonical",
    "crypto_trader.validation.contracts", "crypto_trader.validation.registry",
    "crypto_trader.validation.research_path", "crypto_trader.validation.walk_forward",
    "crypto_trader.validation.holdout",
}
_FORBIDDEN_TOP_LEVEL = {
    "socket", "ssl", "http", "subprocess", "asyncio", "multiprocessing", "concurrent", "requests",
    "aiohttp", "websockets", "websocket", "httpx", "urllib3", "ccxt", "binance", "nautilus_trader",
    "crewai", "openai", "anthropic", "redis",
}


def _tree():
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def test_imports_only_allowed_stdlib_gate_and_named_validation_modules():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in _STDLIB_ALLOWED, (alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, ("relative import", node.lineno)
            if node.module in _LOCAL_ALLOWED:
                assert not any(a.name.startswith("_") for a in node.names), node.lineno
            else:
                assert node.module.split(".")[0] in _STDLIB_ALLOWED, (node.module, node.lineno)


def test_no_private_attribute_or_dynamic_escape():
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
import crypto_trader, crypto_trader.research_study as s
print(json.dumps({"package": crypto_trader.__file__, "module": s.__file__,
                  "loaded": sorted(set(sys.modules) - before)}))
"""


def test_transitive_closure_in_a_fresh_isolated_interpreter_from_this_repository():
    done = subprocess.run([sys.executable, "-I", "-B", "-c", _PROBE, str(SRC)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    probe = json.loads(done.stdout.strip().splitlines()[-1])
    assert Path(probe["module"]).resolve() == MODULE
    assert Path(probe["package"]).resolve() == SRC / "crypto_trader" / "__init__.py"
    ours = {m for m in probe["loaded"] if m == "crypto_trader" or m.startswith("crypto_trader.")}
    allowed = {"crypto_trader", "crypto_trader.research_study", "crypto_trader.research_gate",
               "crypto_trader.validation"}
    assert all(m in allowed or m.startswith("crypto_trader.validation.") for m in ours), ours
    for name in ("risk", "events", "paper", "paper_host", "execution", "production", "breakglass",
                 "mainnet", "llm", "collector", "crew"):
        assert not any(m.startswith(f"crypto_trader.{name}") for m in ours), name
    top = {m.split(".")[0] for m in probe["loaded"]}
    assert not top & _FORBIDDEN_TOP_LEVEL, top & _FORBIDDEN_TOP_LEVEL
    assert "urllib.request" not in probe["loaded"]


def test_text_avoids_tokens_the_package_scans_forbid():
    text = MODULE.read_text(encoding="utf-8")
    for token in ("nautilus_trader", "tinyfish", "import redis", "BINANCE_API", "deepseek_reasoner",
                  "fapi", "crypto_trader.execution", "crypto_trader.paper", "crypto_trader.risk",
                  "crypto_trader.events", "crypto_trader.crew", "crypto_trader.llm"):
        assert token not in text, token
