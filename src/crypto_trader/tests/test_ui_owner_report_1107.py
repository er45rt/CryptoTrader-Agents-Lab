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
        assert c["stop"] == [8765] and c["serve"] == [["--port", "8765", "--workspace", "W"]] and c["open"] == []
    rc, c = _launch({"workspace": "W"}, stop_ok=False)                              # could not stop: open, never 2nd
    assert c["serve"] == [] and c["open"] == ["http://127.0.0.1:8765/"]
    # Sol review 2026-11-07: the restarted server keeps the stopped server's workspace unless one is given
    rc, c = _launch({"workspace": "D:/ws-of-the-old-server", "build": "0" * 16})
    assert c["serve"] == [["--port", "8765", "--workspace", "D:/ws-of-the-old-server"]]


def test_an_outdated_server_never_renders_a_page_only_the_explanation():
    # Sol review 2026-11-07: a warning above a page that still failed ("reading 'stopped'") was not enough
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    boot = app[app.index('window.addEventListener("DOMContentLoaded"'):]
    assert boot.index("await checkServerBuild();") < boot.index("nav((location.hash")
    nav = app[app.index("function nav(page)"):app.index("function nav(page)") + 900]
    assert nav.index("if (serverStale !== false) {") < nav.index("(PAGES[page] || PAGES.home)()")
    assert "esc(STALE_TEXT)" in nav and "return;" in nav[nav.index("if (serverStale !== false) {"):]


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
    ui = _Run(f'"C:\\venv\\Scripts\\pythonw.exe" "{launcher.LAUNCH.resolve()}"')     # this tree's own shortcut server
    assert launcher.stop_stale(8765, run=ui, wait=1)
    assert ["taskkill", "/PID", "4242", "/F"] in ui.calls
    assert any("ProcessId=4242" in " ".join(c) for c in ui.calls if c[0] == "powershell")
    other = _Run(r'"C:\Program Files\Something\other.exe" --serve')
    assert not launcher.stop_stale(8765, run=other, wait=1) and not any(c[0] == "taskkill" for c in other.calls)
    # Sol review 2026-11-07: another checkout's UI, or a server started by hand, is never stopped
    for cmd in (r'"C:\venv\pythonw.exe" "D:\other-checkout\src\crypto_trader\strategies\ui\launch.pyw"',
                r'"C:\venv\python.exe" -m crypto_trader.strategies.ui --port 8765'):
        run = _Run(cmd)
        assert not launcher.stop_stale(8765, run=run, wait=1) and not any(c[0] == "taskkill" for c in run.calls), cmd
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
    assert "d.build && d.build === d.disk_build" in body
    assert "اختصار سطح المكتب" in app[app.index("const STALE_TEXT"):app.index("async function checkServerBuild")]


# R2 - the demo page replaced its whole live card every 2.5 s, even when nothing changed, and every new card replayed
#      its entrance animation from opacity 0: the page blinked.
def test_the_demo_refresh_keeps_an_unchanged_page_and_never_replays_the_entrance_animation():
    # updated after the Sol review 2026-11-07: the card is built once; each section is replaced only when its content
    # changed and the charts only when their data changed (a whole-card swap on any change was a needless redraw)
    js = (STATIC / "demo.js").read_text(encoding="utf-8")
    body = js[js.index("async function demoDraw"):js.index("async function demoAction")]
    assert body.count("box.innerHTML =") == 1 and "if (fresh) {" in body[:body.index("box.innerHTML =")]
    assert "if (demoState.secs[k] === html) continue;" in body
    assert "if (candleSig !== demoState.candles) {" in body and body.index("if (candleSig !== demoState.candles) {") < body.index("candles(")
    assert 'box.classList.add("no-anim")' in body and "box.onclick = demoAction" in body
    for k in ("head", "err", "prog", "kpis", "trades"):
        assert f'data-sec="{k}"' in body
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


# R4 - backtest result cards: the sparkline sat on the icon and title in Arabic. canvas{direction:ltr} turned the
#      spark's inset-inline-end into "right" on the RTL page. It is now placed by the page direction and the title row
#      keeps room for it.
def test_the_card_sparkline_is_placed_by_the_page_direction_and_never_over_the_title():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "canvas{direction:ltr}" in css                      # why inline-end alone was wrong on the canvas
    i_rule = css.index('html[dir="rtl"] .kpi canvas.spark{left:14px;right:auto}')
    assert css.index(".kpi canvas.spark{top:14px;") < i_rule      # later, so it wins over the inline-end placement
    assert 'html[dir="ltr"] .kpi canvas.spark{right:14px;left:auto}' in css
    assert ".kpi:has(canvas.spark) .top{padding-inline-end:92px}" in css     # 84px chart + gap: the title wraps
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '<canvas class="spark"' in app[app.index("function kpi("):app.index("const header")]


# R5 - paper session sma-cross-btc-4h, 58 failed price fetches: the page named a product as the cause without showing
#      evidence. Each connection cause now carries its kind (local refusal / network failure / Binance API error) from
#      the recorded error only; the session line shows the last successful cycle; the outdated-server message (R1) no
#      longer sticks over the page content.
def test_connection_causes_are_classified_from_the_recorded_error_only():
    import re
    import shutil
    import subprocess

    import pytest

    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    fn = re.search(r"^function netCause\(text\) \{.*?^\}", src, re.S | re.M).group(0)
    assert "Avast" not in fn
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = {
        "local": "PermissionError: [Errno 13] Permission denied: '\\\\.\\aswMonFltProxy\\5cb2265d41ce0e6c'",
        "dns": "URLError: <urlopen error [Errno 11001] getaddrinfo failed>",
        "timeout": "TimeoutError: The read operation timed out",
        "api": "HTTPError: HTTP Error 429: Too Many Requests",
        "other_perm": "PermissionError: [Errno 13] Permission denied: 'C:/x/state.json'",
    }
    js = fn + "\nconst C = " + json.dumps(cases) + ";\nconsole.log(JSON.stringify(Object.fromEntries(Object.entries(C).map(([k, v]) => [k, netCause(v)]))));"
    got = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    check=True).stdout.strip().splitlines()[-1])
    assert got["local"].startswith("رفض محلي:") and "(aswMonFltProxy)" in got["local"] and "مو انقطاع شبكة ولا خطأ من Binance" in got["local"]
    assert got["dns"].startswith("انقطاع شبكة:") and got["timeout"].startswith("انقطاع شبكة:")
    assert got["api"].startswith("خطأ من Binance (API):")
    assert not got["other_perm"].startswith("رفض محلي")          # a file permission is not a network block


def test_the_session_error_line_shows_last_attempt_and_last_success_and_the_banner_is_in_flow():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    line = app[app.index("الجلسة ما تقدر تجيب الأسعار"):app.index("الجلسة ما تقدر تجيب الأسعار") + 700]
    assert "d.error.at" in line and "d.error.consecutive" in line and "s.cycle_at" in line and "آخر دورة ناجحة" in line
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rule = css[css.index(".stale-server{"):].split("}")[0]
    assert "sticky" not in rule and "fixed" not in rule and "absolute" not in rule
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    body = i18n[i18n.index("function trErr"):]
    assert body.index('LANG === "en" && net0') < body.index("English: the original message")


# Sol review 2026-11-07, round 2 - H1: ownership is the script argument itself, not a path found anywhere in the command
#      line; an explicit --workspace=PATH is honoured.
def test_only_a_python_running_exactly_this_launch_file_is_ours():
    me = str(launcher.LAUNCH.resolve())
    assert launcher._runs_this_launcher(f'"C:\\venv\\Scripts\\pythonw.exe" "{me}"')
    assert launcher._runs_this_launcher(f'"C:/VENV/PYTHONW.EXE" "{me.upper().replace(chr(92), "/")}"')
    for forged in (f'"C:\\venv\\python.exe" -m crypto_trader.strategies.ui --workspace "{me}-data"',
                   f'"C:\\venv\\pythonw.exe" "D:\\other\\src\\crypto_trader\\strategies\\ui\\launch.pyw" --workspace "{me}-data"',
                   f'"C:\\tools\\other.exe" "{me}"'):
        assert not launcher._runs_this_launcher(forged), forged


def test_an_explicit_workspace_in_either_spelling_is_kept():
    for given in (["--workspace", "D:/mine"], ["--workspace=D:/mine"]):
        calls = {"serve": []}
        orig = launcher.running_system
        launcher.running_system = lambda port, timeout=1.5: {"workspace": "D:/old", "build": "0" * 16}
        try:
            launcher.launch(["--port", "8765", *given], open_browser=lambda u: None,
                            serve=lambda argv: calls["serve"].append(argv) or 0, stop=lambda port: True)
        finally:
            launcher.running_system = orig
        assert calls["serve"] == [["--port", "8765", *given]], given


# Sol review 2026-11-07, round 2 - H2: no page runs before the server check has answered, and a failed check shows its
#      error, never a page.
def test_no_page_runs_until_the_server_check_has_answered():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "let serverStale = null, serverCheckError = null;" in app
    nav = app[app.index("function nav(page)"):app.index("function nav(page)") + 900]
    assert nav.index("if (serverStale !== false) {") < nav.index("(PAGES[page] || PAGES.home)()")
    assert 'loading("التحقق من نسخة الخادم…")' in nav and "errBox(serverCheckError)" in nav
    check = app[app.index("async function checkServerBuild"):]
    assert "catch (e) { serverCheckError = e; serverStale = true; return; }" in check


# Sol review 2026-11-07, round 2 - H3: each chart has its own signature, and the card's buttons act on the demo that
#      card shows (switching demos while the old card is still visible never touches the newly selected one).
def test_each_chart_redraws_alone_and_buttons_act_on_their_own_card():
    js = (STATIC / "demo.js").read_text(encoding="utf-8")
    body = js[js.index("async function demoDraw"):js.index("async function demoAction")]
    i_c, i_e = body.index("if (candleSig !== demoState.candles) {"), body.index("if (eqSig !== demoState.eq) {")
    assert i_c < body.index("candles($(\"#dch\")") < i_e < body.index("lines($(\"#deq\")")
    assert "box.dataset.demo = name;" in body and "demoState.charts" not in js
    act = js[js.index("async function demoAction"):]
    assert "name = box?.dataset.demo" in act and "demoState.active" not in act.split("if (act === \"delete\")")[0]
