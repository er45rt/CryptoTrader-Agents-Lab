"""Sol review 2026-11-08 (D1, owner words "every write of demo.json under the same demo's lock"): the constructor's
recovery and create() still wrote demo.json without the demo lock. Every write site must now be inside a
`with self._session_lock(...)` block, or inside _step_locked (which only runs while _step holds that lock)."""
from __future__ import annotations

import ast
from pathlib import Path

from crypto_trader.strategies.ui import demo

SRC = Path(demo.__file__).read_text(encoding="utf-8")


def _writes_demo_json(node: ast.Call) -> bool:
    return (isinstance(node.func, ast.Name) and node.func.id == "_atomic" and node.args
            and "demo.json" in ast.unparse(node.args[0]))


def _under_lock(chain: list[ast.AST]) -> bool:
    for anc in reversed(chain):
        if isinstance(anc, ast.With) and any("_session_lock(" in ast.unparse(i.context_expr) for i in anc.items):
            return True
        if isinstance(anc, ast.FunctionDef):
            return anc.name == "_step_locked"
    return False


def _sites():
    out = []

    def walk(node, chain):
        if isinstance(node, ast.Call) and _writes_demo_json(node):
            out.append((node.lineno, _under_lock(chain)))
        for child in ast.iter_child_nodes(node):
            walk(child, chain + [node])

    walk(ast.parse(SRC), [])
    return out


def test_every_demo_json_write_is_under_the_demo_lock():
    sites = _sites()
    assert len(sites) >= 9                                   # constructor, create, step, start x2, error, pause, speed
    assert [line for line, locked in sites if not locked] == []


def test_step_locked_is_only_called_while_step_holds_the_lock():
    tree = ast.parse(SRC)
    callers = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
               for c in ast.walk(f) if isinstance(c, ast.Call) and ast.unparse(c.func) == "self._step_locked"]
    assert callers == ["_step"]
    step = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_step")
    assert "with self._session_lock(name):" in ast.unparse(step)
