"""Owner 2026-11-08: opening the dashboard flashed three terminal windows. The window-less pythonw UI ran tasklist (one
call per paper-session check: /api/paper, /api/paper/session, /api/status) without CREATE_NO_WINDOW; the launcher's
netstat/powershell/taskkill calls did the same during a shortcut restart. Every such call now hides its window."""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from crypto_trader.strategies.ui import launcher, store

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_the_paper_alive_check_hides_its_window_and_still_works(monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append((cmd[0], k.get("creationflags")))
        return real(cmd, *a, **k)

    monkeypatch.setattr(store.subprocess, "run", spy)
    assert store._pid_alive(os.getpid()) is True
    if os.name == "nt":
        assert seen == [("tasklist", NO_WINDOW)] and NO_WINDOW != 0


class _Run:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args[0], kw.get("creationflags")))
        out = ""
        if args[0] == "netstat":
            out = "  TCP    127.0.0.1:8765     0.0.0.0:0      LISTENING       4242\n"
        elif args[0] == "powershell":
            out = f'"C:/venv/pythonw.exe" "{launcher.LAUNCH.resolve()}"'
        return type("R", (), {"stdout": out, "returncode": 0})()


def test_every_launcher_command_hides_its_window(tmp_path, monkeypatch):
    run = _Run()
    monkeypatch.setattr(launcher, "running_system", lambda port, timeout=1.5: None)
    launcher.stop_stale(8765, run=run, wait=0.1)
    (tmp_path / "pythonw.exe").write_text("")                  # install() needs it next to python.exe
    launcher.install(desktop=tmp_path, python=tmp_path / "python.exe", run=run)
    assert [c[0] for c in run.calls] == ["netstat", "powershell", "taskkill", "powershell"]
    assert all(flags == launcher.NO_WINDOW for _, flags in run.calls), run.calls


@pytest.mark.parametrize("module", [store, launcher])
def test_no_console_call_in_the_ui_lacks_the_flag(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in ("subprocess.run", "run") and node.args \
                and isinstance(node.args[0], ast.List):
            if not any(k.arg == "creationflags" for k in node.keywords):
                missing.append((node.lineno, ast.unparse(node.args[0])[:40]))
    assert missing == []
