"""Owner report 2026-11-07 (the real UI, opened from the desktop shortcut):
R1 - the shortcut reused a server started before the update: new pages on old server code -> the decision center
     failed ("reading 'stopped'") and the dashboard script was not served (empty page).
"""
from __future__ import annotations

import json
from pathlib import Path

from crypto_trader.strategies.ui import demo, launcher, server
from crypto_trader.tests.test_strategies_ui import live, ws  # noqa: F401  (server fixtures)

STATIC = Path(server.__file__).with_name("static")


def test_a_server_reports_the_code_it_runs_and_the_code_on_disk(live):
    doc = launcher.running_system(live)
    assert doc["build"] == server.BUILD == doc["disk_build"] == server.build_id() and len(doc["build"]) == 16


def _launch(doc, *, stop_ok=True):
    calls = {"open": [], "serve": [], "stop": []}
    orig = launcher.running_system
    launcher.running_system = lambda port, timeout=1.5: doc
    try:
        rc = launcher.launch(["--port", "8765"], open_browser=calls["open"].append,
                             serve=lambda argv: calls["serve"].append(argv) or 0,
                             stop=lambda port: calls["stop"].append(port) or stop_ok)
    finally:
        launcher.running_system = orig
    return rc, calls


def test_the_shortcut_reuses_only_a_server_running_the_current_code():
    rc, c = _launch({"workspace": "W", "build": server.build_id()})
    assert rc == 0 and c == {"open": ["http://127.0.0.1:8765/"], "serve": [], "stop": []}
    for old in ({"workspace": "W"}, {"workspace": "W", "build": "0" * 16}):        # pre-update server / other build
        rc, c = _launch(old)
        assert c["stop"] == [8765] and c["serve"] == [["--port", "8765"]] and c["open"] == []
    rc, c = _launch({"workspace": "W"}, stop_ok=False)                              # could not stop: open, never 2nd
    assert c["serve"] == [] and c["open"] == ["http://127.0.0.1:8765/"]


class _Run:
    def __init__(self, cmdline):
        self.cmdline, self.calls = cmdline, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        out = ""
        if args[0] == "netstat":
            out = ("  TCP    127.0.0.1:8765     0.0.0.0:0      LISTENING       4242\n"
                   "  TCP    127.0.0.1:9999     0.0.0.0:0      LISTENING       77\n")
        elif args[0] == "powershell":
            out = self.cmdline
        return type("R", (), {"stdout": out, "returncode": 0})()


def test_only_this_ui_process_is_ever_stopped(monkeypatch):
    monkeypatch.setattr(launcher, "running_system", lambda port, timeout=1.5: None)     # port frees after the stop
    ui = _Run(r'"C:\venv\Scripts\pythonw.exe" "C:\Users\Jamal\Documents\ct-d29-engine\src\crypto_trader\strategies\ui\launch.pyw"')
    assert launcher.stop_stale(8765, run=ui, wait=1)
    assert ["taskkill", "/PID", "4242", "/F"] in ui.calls
    assert any("ProcessId=4242" in " ".join(c) for c in ui.calls if c[0] == "powershell")
    other = _Run(r'"C:\Program Files\Something\other.exe" --serve')
    assert not launcher.stop_stale(8765, run=other, wait=1) and not any(c[0] == "taskkill" for c in other.calls)
    nothing = _Run("")
    assert not launcher.stop_stale(1234, run=nothing, wait=1) and not any(c[0] == "taskkill" for c in nothing.calls)


def test_a_restarted_server_shows_interrupted_demos_as_paused_not_running(tmp_path):
    base = tmp_path / "demo"
    for name, state in (("demo-20260101T000000-aaaa", "running"), ("demo-20260101T000000-bbbb", "finished")):
        (base / name).mkdir(parents=True)
        (base / name / "demo.json").write_text(json.dumps({"state": state, "cursor": 7}), encoding="utf-8")
    demo.DemoManager(tmp_path)
    got = {f.name: json.loads((f / "demo.json").read_text(encoding="utf-8")) for f in base.iterdir()}
    assert got["demo-20260101T000000-aaaa"] == {"state": "paused", "cursor": 7}          # resumable where it stopped
    assert got["demo-20260101T000000-bbbb"]["state"] == "finished"


def test_every_page_says_plainly_when_the_server_is_older_than_the_pages():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    body = app[app.index("async function checkServerBuild"):]
    assert "checkServerBuild();" in app and 'api("/api/system")' in body
    assert "d.build && d.build === d.disk_build" in body and "اختصار سطح المكتب" in body


# R2 - the demo page replaced its whole live card every 2.5 s, even when nothing changed, and every new card replayed
#      its entrance animation from opacity 0: the page blinked.
def test_the_demo_refresh_keeps_an_unchanged_page_and_never_replays_the_entrance_animation():
    js = (STATIC / "demo.js").read_text(encoding="utf-8")
    body = js[js.index("async function demoDraw"):]
    i_skip, i_swap = body.index("if (!full && sig === demoState.sig && box.firstElementChild) return;"), body.index("box.innerHTML = html;")
    assert i_skip < i_swap and body.index('box.classList.toggle("no-anim", !!box.firstElementChild);') < i_swap
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".no-anim .card,.no-anim .kpi,.no-anim .kpi .v{animation:none}" in css
    assert "animation:rise" in css and "animation:pop" in css      # first appearance still animates


# R3 - with an empty ledger the dashboard's portfolio card said "no priced assets" (reads like a price problem); it
#      now says there are no transactions yet and names the ledger file the shortcut's workspace uses.
def test_an_empty_portfolio_says_why_and_which_ledger(live, ws):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", live, timeout=30)
    c.request("GET", "/api/portfolio", headers={"Host": "127.0.0.1"})
    doc = json.loads(c.getresponse().read())
    c.close()
    assert doc["n_transactions"] == 0 and doc["storage"].endswith("ledger.json")
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    card = js[js.index('if (k === "portfolio") {'):js.index('if (k === "demo") {')]
    assert "if (!d.n_transactions)" in card and "ما فيه عمليات مسجّلة في محفظتك بعد" in card and "d.storage" in card
