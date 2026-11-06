"""Windows desktop shortcut for the local UI (owner decision D40, option a): one double-click, no terminal.

    python -m crypto_trader.strategies.ui.launcher --install      # creates "CryptoTrader.lnk" on the Desktop

The shortcut runs pythonw.exe (no console window) on this file's sibling `launch.pyw`, which puts THIS source tree
first on sys.path (so the shortcut always runs the code it was installed from, not another installed copy) and then:
  * if the UI already answers on 127.0.0.1:<port>, it only opens the browser there (no second server);
  * otherwise it starts the server (loopback only, real trading disabled as before) and opens the browser.
Creating the .lnk uses Windows' own WScript.Shell through PowerShell; nothing else on the system is changed.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

__all__ = ["already_running", "launch", "shortcut_script", "install"]

LAUNCH = Path(__file__).with_name("launch.pyw")
SRC = Path(__file__).resolve().parents[3]          # .../src
DEFAULT_PORT = 8765


def already_running(port: int, timeout: float = 1.5) -> bool:
    """True only if OUR UI answers on the port (server header + system API), not just any listener."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/api/system", headers={"Host": "127.0.0.1"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status == 200 and (resp.getheader("Server") or "").startswith("CTEngineUI/") \
            and "workspace" in json.loads(body)
    except (OSError, ValueError):
        return False


def launch(argv: list[str] | None = None, *, open_browser=webbrowser.open, serve=None) -> int:
    from . import server

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    known, rest = ap.parse_known_args(argv or [])
    if already_running(known.port):
        open_browser(f"http://127.0.0.1:{known.port}/")
        return 0
    return (serve or server.main)(["--port", str(known.port), *rest])


def _ps_quote(text: object) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def shortcut_script(lnk: Path, pythonw: Path, launch_file: Path, workdir: Path) -> str:
    return ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(lnk) + "); "
            "$s.TargetPath = " + _ps_quote(pythonw) + "; "
            "$s.Arguments = " + _ps_quote(f'"{launch_file}"') + "; "
            "$s.WorkingDirectory = " + _ps_quote(workdir) + "; "
            "$s.Description = 'CryptoTrader - local UI (real trading disabled)'; "
            "$s.Save()")


def install(desktop: Path | None = None, python: Path | None = None, run=subprocess.run) -> Path:
    if os.name != "nt":
        raise SystemExit("The desktop shortcut is for Windows.")
    python = Path(python or sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if not pythonw.exists():
        raise SystemExit(f"pythonw.exe not found next to {python}.")
    desktop = Path(desktop or Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop")
    lnk = desktop / "CryptoTrader.lnk"
    run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
         shortcut_script(lnk, pythonw, LAUNCH, Path.home())], check=True, capture_output=True)
    return lnk


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m crypto_trader.strategies.ui.launcher")
    ap.add_argument("--install", action="store_true", help="create the Desktop shortcut")
    a, rest = ap.parse_known_args(argv)
    if a.install:
        print(f"Shortcut created: {install()}")
        return 0
    return launch(rest)


if __name__ == "__main__":
    sys.exit(main())
