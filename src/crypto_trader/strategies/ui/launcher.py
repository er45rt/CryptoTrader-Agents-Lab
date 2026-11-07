"""Windows desktop shortcut for the local UI (owner decision D40, option a): one double-click, no terminal.

    python -m crypto_trader.strategies.ui.launcher --install      # creates "CryptoTrader.lnk" on the Desktop

The shortcut runs pythonw.exe (no console window) on this file's sibling `launch.pyw`, which puts THIS source tree
first on sys.path (so the shortcut always runs the code it was installed from, not another installed copy) and then:
  * if the UI already answers on 127.0.0.1:<port> running THIS code (same build fingerprint), it only opens the
    browser there (no second server); if it runs older code, that server - verified to be this UI by its answer and
    its command line - is stopped first and the current one started (owner report 2026-11-07);
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

__all__ = ["already_running", "running_system", "stop_stale", "launch", "shortcut_script", "install"]

LAUNCH = Path(__file__).with_name("launch.pyw")
SRC = Path(__file__).resolve().parents[3]          # .../src
DEFAULT_PORT = 8765


def running_system(port: int, timeout: float = 1.5) -> dict | None:
    """OUR UI's /api/system answer on the port (server header + system API), or None for nothing / any other listener."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/api/system", headers={"Host": "127.0.0.1"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        doc = json.loads(body)
        ok = resp.status == 200 and (resp.getheader("Server") or "").startswith("CTEngineUI/") and "workspace" in doc
        return doc if ok else None
    except (OSError, ValueError):
        return None


def already_running(port: int, timeout: float = 1.5) -> bool:
    """True only if OUR UI answers on the port, not just any listener."""
    return running_system(port, timeout) is not None


def _listener_pid(port: int, run=subprocess.run) -> int | None:
    out = run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] in (f"127.0.0.1:{port}", f"0.0.0.0:{port}") and parts[3].upper() == "LISTENING":
            return int(parts[4])
    return None


def _command_line(pid: int, run=subprocess.run) -> str:
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
               capture_output=True, text=True, check=False).stdout.strip()


def _norm(text: object) -> str:
    return str(text).replace("\\", "/").lower()


def _argv(cmdline: str) -> list[str]:
    """The process's arguments, split by Windows' own rules (CommandLineToArgvW); elsewhere by POSIX rules."""
    if os.name != "nt":
        import shlex
        return shlex.split(cmdline)
    import ctypes
    from ctypes import wintypes

    fn = ctypes.windll.shell32.CommandLineToArgvW
    fn.argtypes, fn.restype = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)], ctypes.POINTER(wintypes.LPWSTR)
    n = ctypes.c_int(0)
    arr = fn(cmdline, ctypes.byref(n))
    if not arr:
        return []
    try:
        return [arr[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(arr)


def _runs_this_launcher(cmdline: str) -> bool:
    """True only when the process is a Python interpreter whose script argument IS this tree's launch.pyw - not a
    command line that merely contains that path somewhere (Sol review 2026-11-07, round 2)."""
    argv = _argv(cmdline)
    return (len(argv) >= 2 and Path(argv[0]).name.lower() in ("python.exe", "pythonw.exe", "python", "pythonw")
            and _norm(argv[1]) == _norm(LAUNCH.resolve()))


def stop_stale(port: int, run=subprocess.run, wait: float = 15.0) -> bool:
    """Stop the UI server on the port that runs older code - only if its command line runs THIS source tree's
    launch.pyw (the desktop shortcut's own server). Any other process, including another checkout's UI or a server
    started by hand, is left alone (Sol review 2026-11-07). True once the port is free."""
    import time

    pid = _listener_pid(port, run)
    if pid is None:
        return False
    if not _runs_this_launcher(_command_line(pid, run)):
        return False
    run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if running_system(port, timeout=0.5) is None:
            return True
        time.sleep(0.3)
    return False


def launch(argv: list[str] | None = None, *, open_browser=webbrowser.open, serve=None, stop=stop_stale) -> int:
    from . import server

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    known, rest = ap.parse_known_args(argv or [])
    doc = running_system(known.port)
    if doc is not None:
        if doc.get("build") == server.build_id() or not stop(known.port):
            # same code: reuse it. An older one that could not be stopped: open it; the page then shows only the
            # explanation, never a broken page (app.js checkServerBuild).
            open_browser(f"http://127.0.0.1:{known.port}/")
            return 0
        given = any(a == "--workspace" or a.startswith("--workspace=") for a in rest)   # both argparse spellings
        if not given and doc.get("workspace"):
            rest = [*rest, "--workspace", str(doc["workspace"])]   # the restarted server keeps the same workspace
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
