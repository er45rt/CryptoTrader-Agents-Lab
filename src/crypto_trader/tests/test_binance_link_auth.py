"""Owner decision D44, K2: local PIN login - salted scrypt hash only, attempt limit with a persistent lock, auto-lock
after inactivity, HttpOnly SameSite=Strict session cookie; enforced only once a PIN exists."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from crypto_trader.strategies.api import StrategyError
from crypto_trader.strategies.ui import auth as amod
from crypto_trader.strategies.ui import server


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_the_pin_is_stored_only_as_a_salted_hash(tmp_path):
    a = amod.Auth(tmp_path)
    assert not a.enabled() and a.check(None)                      # no PIN yet: login not in use
    a.set_pin("482915")
    raw = (tmp_path / "auth.json").read_text(encoding="utf-8")
    assert "482915" not in raw and json.loads(raw)["n"] == 2 ** 14 and len(json.loads(raw)["salt"]) == 32
    b = amod.Auth(tmp_path)
    b.set_pin  # noqa: B018
    other = json.loads(raw)
    a2 = amod.Auth(tmp_path / "x")
    (tmp_path / "x").mkdir()
    a2.set_pin("482915")
    assert json.loads((tmp_path / "x" / "auth.json").read_text())["hash"] != other["hash"]   # salted
    for bad in ("12345", "1234567890123", "12ab56", "", None):
        with pytest.raises(StrategyError, match="من 6 إلى 12 رقم"):
            amod.Auth(tmp_path / "y").set_pin(bad)


def test_wrong_pins_lock_the_login_and_the_lock_survives_a_restart(tmp_path):
    clock = Clock()
    a = amod.Auth(tmp_path, now=clock)
    a.set_pin("482915")
    for i in range(amod.MAX_FAILS - 1):
        with pytest.raises(StrategyError, match="غير صحيح"):
            a.login("000000")
    with pytest.raises(StrategyError, match="قُفل الدخول"):
        a.login("111111")
    b = amod.Auth(tmp_path, now=clock)                           # a restarted server keeps the lock
    with pytest.raises(StrategyError, match="مقفل مؤقتًا"):
        b.login("482915")                                         # even the right PIN waits
    clock.t += amod.LOCK_S + 1
    assert b.login("482915")
    assert json.loads((tmp_path / "auth.json").read_text())["fails"] == 0


def test_sessions_auto_lock_after_inactivity_and_a_new_pin_ends_them(tmp_path):
    clock = Clock()
    a = amod.Auth(tmp_path, now=clock)
    a.set_pin("482915")
    tok = a.login("482915")
    assert a.check(tok) and not a.check("forged") and not a.check(None)
    clock.t += amod.IDLE_S - 5
    assert a.check(tok)                                           # activity keeps it open
    clock.t += amod.IDLE_S + 1
    assert not a.check(tok)                                       # idle: locked
    tok = a.login("482915")
    with pytest.raises(StrategyError, match="سجّل الدخول أولًا"):
        a.set_pin("777777")                                       # changing the PIN needs a session
    a.set_pin("777777", token=tok)
    assert not a.check(tok)                                       # every session ended
    a.logout("whatever")


def _call(port, method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    raw = r.read()
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        doc = None                                   # a static file, not JSON
    out = (r.status, doc, r.getheader("Set-Cookie"))
    c.close()
    return out


def test_once_a_pin_exists_every_private_api_needs_the_session_cookie(tmp_path):
    srv = server.make_server(tmp_path, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert _call(port, "GET", "/api/portfolio")[0] == 200            # no PIN: unchanged behaviour
        st, _, cookie = _call(port, "POST", "/api/auth/setup", {"pin": "482915"})
        assert st == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "482915" not in cookie
        session = cookie.split(";")[0]
        for path in ("/api/portfolio", "/api/decisions", "/api/status", "/api/dca", "/api/runs"):
            st, doc, _ = _call(port, "GET", path)
            assert st == 401 and doc == {"error": "login required", "auth": True}, path
            assert _call(port, "GET", path, cookie=session)[0] == 200, path
        assert _call(port, "POST", "/api/portfolio/add", {"type": "buy"})[0] == 401
        assert _call(port, "GET", "/api/system")[0] == 200 and _call(port, "GET", "/app.js")[0] == 200
        assert _call(port, "GET", "/api/auth/status", cookie=session)[1] == {"enabled": True, "logged_in": True}
        assert _call(port, "POST", "/api/auth/setup", {"pin": "999999"})[0] == 400      # no session: no change
        _, _, cleared = _call(port, "POST", "/api/auth/logout", {}, cookie=session)
        assert "Max-Age=0" in cleared and _call(port, "GET", "/api/portfolio", cookie=session)[0] == 401
        assert _call(port, "POST", "/api/auth/login", {"pin": "000000"})[0] == 400
        st, _, cookie = _call(port, "POST", "/api/auth/login", {"pin": "482915"})
        assert st == 200 and _call(port, "GET", "/api/portfolio", cookie=cookie.split(";")[0])[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()
