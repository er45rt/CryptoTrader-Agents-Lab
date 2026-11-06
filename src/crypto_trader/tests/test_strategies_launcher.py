"""D40 phase 1, item 6: Windows desktop shortcut - one double-click opens the UI, never a second server."""
from __future__ import annotations

import socket
from pathlib import Path

from crypto_trader.strategies.ui import launcher
from crypto_trader.tests.test_strategies_ui import live, ws  # noqa: F401  (server fixtures)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_a_running_ui_is_reused_the_browser_just_opens(live):
    opened, served = [], []
    assert launcher.already_running(live)
    rc = launcher.launch(["--port", str(live)], open_browser=opened.append, serve=served.append)
    assert rc == 0 and opened == [f"http://127.0.0.1:{live}/"] and served == []


def test_no_ui_running_starts_the_server_on_the_port(monkeypatch):
    port, served = _free_port(), []
    assert not launcher.already_running(port)
    rc = launcher.launch(["--port", str(port), "--workspace", "W"], open_browser=lambda u: None,
                         serve=lambda argv: served.append(argv) or 0)
    assert rc == 0 and served == [["--port", str(port), "--workspace", "W"]]


def test_another_program_on_the_port_is_not_mistaken_for_the_ui():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        assert not launcher.already_running(s.getsockname()[1], timeout=0.5)
    finally:
        s.close()


def test_shortcut_runs_pythonw_on_this_trees_launch_file_and_quotes_paths():
    script = launcher.shortcut_script(Path(r"C:\Users\O'Neil\Desktop\CryptoTrader.lnk"), Path(r"C:\v\pythonw.exe"),
                                      launcher.LAUNCH, Path(r"C:\Users\O'Neil"))
    assert "CreateShortcut('C:\\Users\\O''Neil\\Desktop\\CryptoTrader.lnk')" in script
    assert "$s.TargetPath = 'C:\\v\\pythonw.exe'" in script and f'"{launcher.LAUNCH}"' in script
    assert script.rstrip().endswith("$s.Save()")


def test_install_writes_one_shortcut_through_powershell_only(tmp_path):
    py = tmp_path / "python.exe"
    (tmp_path / "pythonw.exe").write_text("")
    calls = []
    lnk = launcher.install(desktop=tmp_path, python=py, run=lambda cmd, **k: calls.append(cmd))
    assert lnk == tmp_path / "CryptoTrader.lnk" and len(calls) == 1
    assert calls[0][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert str(tmp_path / "pythonw.exe") in calls[0][4]


def test_launch_file_puts_this_source_tree_first():
    assert launcher.LAUNCH.exists() and (launcher.SRC / "crypto_trader" / "strategies" / "ui" / "launch.pyw").exists()
    text = launcher.LAUNCH.read_text(encoding="utf-8")
    assert "sys.path.insert(0, str(Path(__file__).resolve().parents[3]))" in text
    assert Path(launcher.LAUNCH).resolve().parents[3] == launcher.SRC
