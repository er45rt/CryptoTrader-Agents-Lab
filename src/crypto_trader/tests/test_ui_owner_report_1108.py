"""Owner report 2026-11-08: the decision center's content (decision funnel, block reasons, decisions table) and the
dashboard status bar appeared visibly late. Measured: the data reached the page at ~60 ms but was painted at
~170-270 ms - laying out all 400 rows (3,200 elements, ~7 visible) blocked the frame ~100-157 ms, a 250 ms fade ran
on every card, and the table jumped when the data landed; the status bar arrived last into a zero-height box and
pushed the dashboard down 61 px. The 30 s refresh rebuilt every row even when nothing changed, and the refresh timer
was set after an await (it could replace the next page's timer)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from crypto_trader.strategies.ui import server

STATIC = Path(server.__file__).with_name("static")
DEC = (STATIC / "decisions.js").read_text(encoding="utf-8")
DASH = (STATIC / "dashboard.js").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
PAGE = DEC[DEC.index("PAGES.decisions = async () => {"):DEC.index("async function dcLoad")]


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def test_the_page_paints_without_the_entrance_fade_and_shows_the_data_area_in_one_go():
    assert PAGE.index("main.innerHTML = `<div class=\"no-anim\">`") < PAGE.index("header(")
    assert ".no-anim .card,.no-anim .kpi,.no-anim .kpi .v{animation:none}" in CSS and "animation:rise" in CSS
    assert '<div id="dc-data"${cached ? "" : " hidden"}>' in PAGE and 'loading("تحميل…")' in PAGE
    assert '<div class="skel"></div>' not in PAGE                 # no placeholder of the wrong height that later jumps


def test_the_refresh_timer_is_set_before_the_page_waits_and_a_repeat_visit_paints_first():
    first_await = PAGE.index("await ")
    assert PAGE.index("timer = setInterval(") < first_await
    assert "const cached = !!dcState.data && dcState.key === dcQuery();" in PAGE
    assert PAGE.index("dcRender(dcState.data, false, dcState.sig);") < first_await
    assert PAGE.index("dcRender(dcState.data, false, dcState.sig);") < PAGE.index("dcLoad(true, true)")


def test_only_the_first_rows_are_built_on_open():
    assert "const DC_FIRST_ROWS = 50;" in DEC and "d.items.map(" not in DEC
    assert 'insertAdjacentHTML("beforeend"' in DEC and "tw.onscroll" in PAGE and "dcMore()" in PAGE


HARNESS = r"""
class El { constructor(id) { this.id = id; this._h = ""; this.writes = 0; this.hidden = false; this.scrollTop = 0;
    this.textContent = ""; this.onclick = null; this.value = ""; }
  get innerHTML() { return this._h; } set innerHTML(v) { this._h = v; this.writes++; }
  insertAdjacentHTML(pos, h) {
    if (pos === "beforebegin") { const id = h.match(/id="([^"]+)"/)[1]; els[id] = new El(id); els[id]._h = h; return; }   // a new sibling
    this._h += h; this.writes++; }
  closest() { return els.tw; } remove() { delete els[this.id]; } }
let els = {};
// as PAGES.decisions draws it: a first visit has the loading line and a hidden data area; a repeat visit has neither
const fresh = (hidden) => { els = {}; for (const id of ["dc-rows", "dc-funnel", "dc-reasons", "dc-kinds", "dc-total", "dc-source",
  "dc-strategy", "dc-symbol", "dc-data", "tw"]) els[id] = new El(id); els["dc-data"].hidden = hidden;
  if (hidden) els["dc-wait"] = new El("dc-wait"); };
const $ = (sel) => els[sel.replace("#", "")] || null;
let current = "decisions";
const esc = (x) => String(x), errBox = (e) => "ERR:" + e.message, empty = () => "EMPTY", kindPill = (k) => k,
  oneLine = () => "x", tfAr = (x) => x, dcShow = () => {};
const dcFunnel = () => { els["dc-funnel"].innerHTML = "F"; }, dcReasons = () => { els["dc-reasons"].innerHTML = "R"; };
const DC = { kinds: { keep: ["keep", ""] } };
const dcState = { filters: { source: "", strategy: "", symbol: "", since: "", until: "", kinds: [] }, selected: null, auto: true,
  data: null, wide: false, reason: "", key: null, sig: null, drawn: null, shown: 0 };
let hook = async () => { throw new Error("no hook"); };
const calls = [];
const api = async (url) => { calls.push(url); return hook(url); };
const answer = (n, tag) => ({ items: Array.from({ length: n }, (_, k) => ({ id: "demo:d:" + tag + k, at: "2026-01-01T00:00:00",
  kind: "keep", strategy: "s", symbol: "BTCUSDT", timeframe: "4h", source: { kind: "demo" } })), total: 2519, counts: { keep: n },
  funnel: {}, reasons: {}, facets: { sources: [], strategies: [], symbols: [] } });
const rows = () => (els["dc-rows"]._h.match(/<tr data-dc=/g) || []).length;
""" + "\n"


def _run(scenario: str) -> dict:
    code = (DEC[DEC.index("const DC_FIRST_ROWS"):DEC.index("PAGES.decisions = async")]
            + DEC[DEC.index("async function dcLoad"):DEC.index("const KIND_ORDER")] + "\nconst KIND_ORDER = [\"keep\"];\n")
    js = HARNESS + code + "\n(async () => {\n" + scenario + "\n})().catch((e) => { console.log(JSON.stringify({ crash: String(e) })); });"
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_open_builds_50_rows_in_one_paint_and_scrolling_adds_the_rest_of_the_same_answer():
    got = _run("""
      fresh(true); hook = async () => answer(400, "a");
      await dcLoad();
      const first = rows(), data = !els["dc-data"].hidden, wait = !!els["dc-wait"];
      dcMore(); const more = rows();
      for (let i = 0; i < 20; i++) dcMore();
      console.log(JSON.stringify({ first, data, wait, more, all: rows(), shown: dcState.shown, calls: calls.length }));""")
    assert got == {"first": 50, "data": True, "wait": False, "more": 100, "all": 400, "shown": 400, "calls": 1}


def test_an_unchanged_refresh_leaves_the_page_untouched_and_a_changed_one_keeps_rows_and_scroll():
    got = _run("""
      fresh(true); hook = async () => answer(400, "a");
      await dcLoad(); dcMore(); els.tw.scrollTop = 900;
      const w = els["dc-rows"].writes;
      await dcLoad(true);                                   // the 30 s refresh, same answer
      const untouched = els["dc-rows"].writes === w && rows() === 100;
      hook = async () => answer(400, "b");                  // a new decision changed the answer
      await dcLoad(true);
      console.log(JSON.stringify({ untouched, rows: rows(), scroll: els.tw.scrollTop, fresh: els["dc-rows"]._h.includes("demo:d:b0") }));""")
    assert got == {"untouched": True, "rows": 100, "scroll": 900, "fresh": True}


def test_an_answer_for_filters_that_changed_meanwhile_is_never_drawn():
    got = _run("""
      fresh(true);
      hook = async () => { dcState.filters.symbol = "ETHUSDT"; return answer(400, "old"); };   // the owner changed a filter
      await dcLoad();
      console.log(JSON.stringify({ rows: rows(), hidden: els["dc-data"].hidden, kept: dcState.data }));""")
    assert got == {"rows": 0, "hidden": True, "kept": None}


def test_errors_are_shown_never_the_old_list_as_if_fresh():
    got = _run("""
      fresh(true); hook = async () => { throw new Error("down"); };
      await dcLoad();
      const first = els["dc-wait"]._h;
      fresh(false); hook = async () => answer(400, "a"); await dcLoad();
      hook = async () => { throw new Error("down"); };
      await dcLoad(true, true);                             // the background check after a cached paint
      const reval = els["dc-data"].hidden ? (els["dc-wait"] || {})._h : els["dc-rows"]._h;   // review finding 3: all of it off the screen
      const revalDrawn = dcState.drawn;
      fresh(false); hook = async () => answer(400, "a"); await dcLoad();
      hook = async () => { throw new Error("down"); };
      await dcLoad(true);                                   // an ordinary quiet refresh that fails stays quiet
      const quiet = rows(), stillDrawn = dcState.drawn === dcState.sig;
      dcMore();                                             // and scrolling still adds rows of the answer on screen
      console.log(JSON.stringify({ first, reval, revalDrawn, quiet, stillDrawn, more: rows() }));""")
    assert got == {"first": "ERR:down", "reval": '<div id="dc-wait">ERR:down</div>', "revalDrawn": None,
                   "quiet": 50, "stillDrawn": True, "more": 100}


def test_the_status_bar_keeps_its_place_and_its_last_answer():
    home = DASH[DASH.index("PAGES.home = async () => {"):DASH.index("async function dbStatus")]
    assert '<div id="db-status">${DB.status ? dbStatusHtml(DB.status) : \'<div class="stbar"><span class="pill">تحميل…</span></div>\'}</div>' in home
    body = DASH[DASH.index("async function dbStatus"):DASH.index("function dbStatusHtml")]
    assert body.index('api("/api/status")') < body.index("DB.status = d;") < body.index("dbStatusHtml(d)")
    fns = {name: re.search(rf"^const {name} = .*?;$", src, re.S | re.M).group(0)
           for name, src in (("esc", APP), ("isNum", APP), ("dbAt", DASH))}
    st = DASH[DASH.index("const DB_ST = {"):DASH.index("async function dbStatus")]
    html = DASH[DASH.index("function dbStatusHtml"):]
    html = html[:html.index("\n}\n") + 2]
    doc = {"at": "2026-11-08T12:00:00Z", "summary": {"bad": 1, "warn": 1, "info": 0, "ok": 1},
           "items": [{"level": "ok", "area": "engine", "code": "live_disabled"},
                     {"level": "warn", "area": "errors", "code": "ui_errors", "count": 1},
                     {"level": "bad", "area": "paper", "code": "session_error", "name": "s", "at": "t", "detail": "<b>x</b>"}]}
    js = "\n".join(fns.values()) + "\n" + st + "\n" + html + f"\nconsole.log(JSON.stringify(dbStatusHtml({json.dumps(doc)})));"
    out = json.loads(subprocess.run([_node(), "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    check=True).stdout.strip().splitlines()[-1])
    assert out.count('<div class="stbar">') == 1 and "1 مشكلة" in out and "تحديث" in out and "غرينتش" in out
    assert out.index("pill bad") < out.index("pill warn") < out.index("pill good") and "<b>x</b>" not in out


# Adversarial review of this change (2026-11-08, read-only workflow wf_ac28050d-d4c): findings 1-4.
def test_a_failed_first_load_recovers_on_the_next_refresh_even_with_an_identical_answer():
    # finding 1 (blocking) + 4: dcState.drawn / shown described an EARLIER visit, so an identical answer after a failed
    # first load was taken for "nothing changed" and the error stayed forever; a recovery built every earlier row
    got = _run("""
      fresh(false); hook = async () => answer(400, "a"); await dcLoad();
      for (let i = 0; i < 7; i++) dcMore();                  // the earlier visit scrolled to all 400 rows
      fresh(true); hook = async () => { throw new Error("down"); };
      await dcLoad();                                       // a new visit: the first load fails
      const err = els["dc-wait"] ? els["dc-wait"]._h : null;
      hook = async () => answer(400, "a");                  // the 30 s refresh: byte-identical to the earlier visit
      await dcLoad(true);
      console.log(JSON.stringify({ err, hidden: els["dc-data"].hidden, wait: !!els["dc-wait"], rows: rows() }));""")
    assert got == {"err": "ERR:down", "hidden": False, "wait": False, "rows": 50}


def test_an_error_of_a_superseded_request_never_replaces_the_newer_answer():
    # finding 2: a slow drill-down request that fails after the owner already chose other filters
    got = _run("""
      fresh(false); hook = async () => answer(400, "a"); await dcLoad();
      hook = async () => { dcState.filters.kinds = ["keep"]; throw new Error("down"); };
      await dcLoad();
      console.log(JSON.stringify({ rows: rows(), err: els["dc-rows"]._h.includes("ERR"), drawn: dcState.drawn === dcState.sig }));""")
    assert got == {"rows": 50, "err": False, "drawn": True}


def test_a_cached_paint_whose_check_fails_is_taken_off_the_screen():
    # finding 3: funnel, reasons, counts and rows from the cache must not stay as if current when the check fails
    got = _run("""
      fresh(false); hook = async () => answer(400, "a"); await dcLoad();          // the cached paint
      hook = async () => { throw new Error("down"); };
      await dcLoad(true, true);                                                  // its background check fails
      const after = { hidden: els["dc-data"].hidden, err: els["dc-wait"] ? els["dc-wait"]._h : null };
      hook = async () => answer(400, "a"); await dcLoad(true);                   // the next refresh succeeds
      console.log(JSON.stringify({ ...after, back: !els["dc-data"].hidden && !els["dc-wait"], rows: rows() }));""")
    assert got == {"hidden": True, "err": '<div id="dc-wait">ERR:down</div>', "back": True, "rows": 50}


# Owner 2026-11-08, "the same on every page": measured on all 12 pages - no page had a long frame, but every card and
# KPI faded in (fully visible only ~240-290 ms after the click), and the demo page jumped when its live card arrived.
def test_no_page_fades_in():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<main id="main" class="no-anim">' in html
    assert ".no-anim .card,.no-anim .kpi,.no-anim .kpi .v{animation:none}" in CSS
    assert not re.search(r"main\.className\s*=|main\.classList\.remove", APP + DEC + DASH)   # nothing takes it off


def test_the_demo_page_is_drawn_once_with_its_live_card():
    js = (STATIC / "demo.js").read_text(encoding="utf-8")
    page = js[js.index("PAGES.demo = async () => {"):js.index("async function demoDraw")]
    i_pre = page.index('const pre = demoState.active ? await api("/api/demo/session?name="')
    assert i_pre < page.index("main.innerHTML =") and "await demoDraw(true, pre);" in page
    draw = js[js.index("async function demoDraw"):js.index("async function demoAction")]
    assert "pre && pre.demo && pre.demo.name === name ? pre : await api(" in draw   # only the demo it was fetched for


# Removing the fade showed two jumps it had hidden (cards moved while still transparent): the paper session detail and
# the dashboard spaces/detail arrived after their page and pushed the rest down.
def test_the_paper_page_is_drawn_once_with_the_shown_session():
    page = APP[APP.index("PAGES.paper = async () => {"):APP.index("let shownSession")]
    i_pre = page.index('const preDetail = pick ? { name: pick.name, d: await api("/api/paper/session?name="')
    assert i_pre < page.index("main.innerHTML =") and "paperDetail(pick.name, preDetail);" in page
    det = APP[APP.index("async function paperDetail"):]
    det = det[:det.index("\n}\n")]
    assert "const draw = async (got = null) => { const d = got || await api(" in det
    assert "await draw(pre && pre.name === name ? pre.d : null);" in det          # only for the session it was fetched for


def test_the_dashboard_is_drawn_once_with_its_spaces_but_never_held_by_a_slow_one():
    home = DASH[DASH.index("PAGES.home = async () => {"):DASH.index("function dbPaint")]
    assert "const DB_WAIT_MS = 300;" in DASH
    i_wait = home.index("await Promise.race([Promise.all(loads), new Promise((r) => setTimeout(r, DB_WAIT_MS))]);")
    assert i_wait < home.index("main.innerHTML =")
    assert 'if (!drawn || current !== "home" || DB.visit !== visit) return;' in home      # later spaces fill their own card
    assert "DB_SPACES.forEach(([k]) => dbCard(k));" in home and home.index("main.innerHTML =") < home.rindex("dbDetail();")   # the drawn page's detail; the first one is the late-space callback


# Sol High review 2026-11-09 of W1..G2 (FAIL, 5 blocking findings) - each reproduced here.
def test_sol_1_an_exited_process_with_exit_code_259_is_not_alive():
    import os
    import sys

    from crypto_trader.strategies.ui import store

    if os.name != "nt":
        pytest.skip("the Windows path")
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])   # its handle stays open in this process
    assert child.wait(30) == 259
    assert store._pid_alive(child.pid) is False                       # exit code 259 is STILL_ACTIVE's value


def test_sol_2_an_older_status_answer_never_overwrites_a_newer_one():
    body = DASH[DASH.index("async function dbStatus"):DASH.index("function dbStatusHtml")]
    js = r"""
let current = "home"; const els = { "db-status": { innerHTML: "", isConnected: true } };
const $ = (s) => els[s.replace("#", "")] || null; const errBox = (e) => "ERR";
const DB = { status: null }; const dbStatusHtml = (d) => "BAR:" + d.v;
const resolvers = []; const api = () => new Promise((r) => resolvers.push(r));
""" + body + r"""
(async () => {
  const a = dbStatus(), b = dbStatus();          // an older request (a) and a newer one (b)
  resolvers[1]({ v: "new" }); await b;
  resolvers[0]({ v: "old" }); await a;           // the older answer lands last
  console.log(JSON.stringify({ bar: els["db-status"].innerHTML, kept: DB.status.v }));
})();"""
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"bar": "BAR:new", "kept": "new"}


def test_sol_3_an_earlier_visits_failed_check_never_hides_a_newer_visit():
    got = _run("""
      fresh(false); dcState.visit = 1;
      let failOld; hook = () => new Promise((_, rej) => { failOld = rej; });
      const old = dcLoad(true, true);                       // visit 1: cached paint, its background check hangs
      fresh(false); dcState.visit = 2;                      // the owner leaves and comes back: visit 2, same filters
      hook = async () => answer(400, "new"); await dcLoad(true, true);
      failOld(new Error("down")); await old;                // the earlier check fails last
      console.log(JSON.stringify({ hidden: els["dc-data"].hidden, wait: !!els["dc-wait"], rows: rows() }));""")
    assert got == {"hidden": False, "wait": False, "rows": 50}


def test_sol_4_5_leaving_demo_or_paper_while_it_loads_never_draws_over_the_next_page():
    demo = (STATIC / "demo.js").read_text(encoding="utf-8")
    page = demo[demo.index("PAGES.demo = async () => {"):demo.index("async function demoDraw")]
    i = page.index("const pre = demoState.active ? await api(")
    assert page.index('if (current !== "demo") return;', i) < page.index("main.innerHTML =")
    paper = APP[APP.index("PAGES.paper = async () => {"):APP.index("let shownSession")]
    i = paper.index("const preDetail = pick ?")
    assert paper.index('if (current !== "paper") return;', i) < paper.index("main.innerHTML =")


# Review backlog (2026-11-08 adversarial review, finding 7): a date the browser accepts but Date cannot read (e.g. a
# 5-digit year) made dcQuery throw: the old list stayed and returning to the page broke it until a reload.
def test_an_unreadable_date_is_refused_at_the_filter_and_never_breaks_the_query():
    code = DEC[DEC.index("function dcIso(v)"):DEC.index("const dcRow")]
    js = """const dcState = { filters: { source: "", strategy: "", symbol: "", since: "20260-01-01T00:00", until: "2026-13-01T00:00",
      kinds: [] }, reason: "" };\n""" + code + """
console.log(JSON.stringify({ q: dcQuery(), ok: dcIso("2026-01-05T10:00"), bad: [dcIso("20260-01-01T00:00"), dcIso(""), dcIso("x")] }));"""
    out = json.loads(subprocess.run([_node(), "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    check=True).stdout.strip().splitlines()[-1])
    assert out == {"q": "limit=400", "ok": "2026-01-05T10:00:00.000Z", "bad": [None, None, None]}
    page = DEC[DEC.index("PAGES.decisions = async () => {"):DEC.index("async function dcLoad")]
    refuse = page[page.index('if ((k === "since" || k === "until") && el.value && !dcIso(el.value)) {'):]
    refuse = refuse[:refuse.index("return;")]
    assert "el.value = f[k];" in refuse and '$("#dc-msg").innerHTML' in refuse and "dcLoad" not in refuse
    assert '<div id="dc-msg"></div>' in page
