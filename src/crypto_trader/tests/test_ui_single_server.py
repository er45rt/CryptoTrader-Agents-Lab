"""2026-11-08 incident: the shortcut's status check timed out against a busy UI, the launcher concluded no UI was running
and started a second server; Windows let both bind 127.0.0.1:8765 and the new server's managers rewrote demo state.
Now the server refuses to start - before creating anything - while the port accepts connections, and the launcher never
starts a server next to one that is merely slow."""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from crypto_trader.strategies.ui import launcher, server


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)                                                 # accepts at TCP level, never answers HTTP (a busy UI)
    return s


def test_a_second_server_is_refused_before_it_touches_the_workspace(tmp_path):
    s = _listener()
    try:
        demo_dir = tmp_path / "demo" / "demo-20261108T063845-5685"
        demo_dir.mkdir(parents=True)
        (demo_dir / "demo.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
        with pytest.raises(OSError, match="already in use"):
            server.make_server(tmp_path, s.getsockname()[1])
        assert json.loads((demo_dir / "demo.json").read_text(encoding="utf-8"))["state"] == "running"   # untouched
    finally:
        s.close()


def test_a_free_port_still_starts_and_a_quick_restart_still_binds(tmp_path):
    srv = server.make_server(tmp_path, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    assert server.port_in_use(port)
    srv.shutdown()
    srv.server_close()
    again = server.make_server(tmp_path, port)                 # the shortcut's restart path: same port right away
    again.server_close()


def test_the_launcher_never_starts_a_server_next_to_a_slow_one(monkeypatch):
    s = _listener()
    port = s.getsockname()[1]
    asked, opened, served = [], [], []

    def slow(p, timeout=1.5):
        asked.append(timeout)
        return None                                            # no HTTP answer in time

    monkeypatch.setattr(launcher, "running_system", slow)
    try:
        t0 = time.monotonic()
        rc = launcher.launch(["--port", str(port)], open_browser=opened.append,
                             serve=lambda argv: served.append(argv) or 0, stop=lambda p: True)
    finally:
        s.close()
    assert served == [] and rc == 1 and opened == [f"http://127.0.0.1:{port}/"]
    assert asked == [1.5, 15.0] and time.monotonic() - t0 < 10     # asked again patiently (stubbed, no real wait)


def test_a_slow_ui_that_answers_the_second_time_takes_the_normal_path(monkeypatch):
    s = _listener()
    port = s.getsockname()[1]
    answers = iter([None, {"workspace": "W", "build": server.build_id()}])
    monkeypatch.setattr(launcher, "running_system", lambda p, timeout=1.5: next(answers))
    opened, served = [], []
    try:
        rc = launcher.launch(["--port", str(port)], open_browser=opened.append, serve=served.append,
                             stop=lambda p: True)
    finally:
        s.close()
    assert rc == 0 and served == [] and opened == [f"http://127.0.0.1:{port}/"]   # same code: reused, nothing started


def test_nothing_listening_still_starts_the_ui(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    served = []
    rc = launcher.launch(["--port", str(port), "--workspace", "W"], open_browser=lambda u: None,
                         serve=lambda argv: served.append(argv) or 0)
    assert rc == 0 and served == [["--port", str(port), "--workspace", "W"]]
    assert Path(launcher.__file__).exists()
