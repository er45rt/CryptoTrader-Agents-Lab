"""Sol D44 review B1: background polling must not keep a PIN session open. Only requests that follow the owner's own
input (marked X-CT-Active by the page) extend the idle timer, and the page locks itself after the idle time."""
from __future__ import annotations

import http.client
import threading
from pathlib import Path

from crypto_trader.strategies.ui import auth as amod
from crypto_trader.strategies.ui import server

APP = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_a_check_without_activity_never_extends_the_session(tmp_path):
    clock = Clock()
    a = amod.Auth(tmp_path, now=clock)
    a.set_pin("482915")
    tok = a.login("482915")
    for _ in range(30):                                   # 30 background polls, one a minute
        clock.t += 60
        if clock.t - 1_000_000.0 <= amod.IDLE_S:
            assert a.check(tok) and a.logged_in(tok)
    assert not a.check(tok)                               # 30 minutes of polling: locked after IDLE_S anyway


def test_owner_activity_extends_the_session(tmp_path):
    clock = Clock()
    a = amod.Auth(tmp_path, now=clock)
    a.set_pin("482915")
    tok = a.login("482915")
    clock.t += amod.IDLE_S - 5
    assert a.check(tok, touch=True)
    clock.t += amod.IDLE_S - 5
    assert a.check(tok)
    clock.t += 10
    assert not a.check(tok)


def _get(port, path, cookie, active):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": "127.0.0.1", "Cookie": cookie}
    if active:
        h["X-CT-Active"] = "1"
    c.request("GET", path, None, h)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_the_server_extends_only_requests_marked_active(tmp_path):
    srv = server.make_server(tmp_path, 0)
    clock = Clock()
    server.Handler.auth = amod.Auth(tmp_path, now=clock)
    server.Handler.auth.set_pin("482915")
    session = "ct_session=" + server.Handler.auth.login("482915")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        for _ in range(14):                               # the dashboard's background refresh, once a minute
            clock.t += 60
            assert _get(port, "/api/portfolio", session, active=False) == 200
        clock.t += 120                                    # 16 minutes after login, no input at all
        assert _get(port, "/api/portfolio", session, active=False) == 401
        session = "ct_session=" + server.Handler.auth.login("482915")
        clock.t += amod.IDLE_S - 5
        assert _get(port, "/api/portfolio", session, active=True) == 200      # a click
        clock.t += amod.IDLE_S - 5
        assert _get(port, "/api/auth/status", session, active=False) == 200
        assert _get(port, "/api/portfolio", session, active=False) == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_page_marks_only_requests_after_input_and_locks_itself():
    once = APP[APP.index("async function apiOnce"):APP.index("function errBox")]
    assert 'if (Date.now() - lastInput < ACTIVE_MS) opt.headers["X-CT-Active"] = "1"' in once
    assert "...opt.headers" in once                       # POSTs keep the activity mark too
    lock = APP[APP.index("const ACTIVE_MS"):APP.index("function loginPage")]
    for ev in ("pointerdown", "keydown", "wheel", "touchstart"):
        assert f'"{ev}"' in lock
    assert "idle >= limit" in lock and 'api("/api/auth/logout", {})' in lock and "loginPage(current)" in lock
    login = APP[APP.index("function loginPage"):APP.index("function paintLock")]
    assert "clearInterval(timer)" in login                # a locked page stops its background polling
