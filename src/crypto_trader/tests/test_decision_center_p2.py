"""Phase 2 (owner decision D42): decision center - funnel, trade inspector, rejection reasons. Every number comes from
the recorded decision chain; nothing is inferred."""
from __future__ import annotations

import re
from pathlib import Path

from crypto_trader.strategies.ui import decisions
from crypto_trader.tests.test_strategies_decisions import root  # noqa: F401  (recorded paper sessions fixture)

JS = (Path(decisions.__file__).parent / "static" / "decisions.js").read_text(encoding="utf-8")


def test_the_funnel_counts_each_recorded_decision_once(root):
    for source in (None, "paper:ok", "paper:capped"):
        res = decisions.collect(root, source=source, limit=2000)
        fl, items = res["funnel"], res["items"]
        assert fl["evaluated"] == res["total"] == len(items)
        assert fl["evaluated"] == fl["missed"] + fl["no_signal"] + fl["signals"]
        assert fl["signals"] == sum(i["action"] in ("buy", "sell") for i in items)
        assert fl["missed"] == res["counts"]["missed"]
        assert fl["stopped"] == {"validity": res["counts"]["rejected_validity"], "risk": res["counts"]["rejected_risk"],
                                 "safety": res["counts"]["blocked_safety"]}
        assert fl["sent"] == sum(i["submission"] == "submitted_to_simulator" for i in items)
        assert fl["filled"] == sum(bool(i["fills"]) for i in items)
        assert fl["unclassified"] == 0
        assert fl["passed"]["safety"] == fl["sent"]          # every signal is either stopped at one stage or sent
    capped = decisions.collect(root, source="paper:capped", limit=2000)["funnel"]
    assert capped["signals"] > 0 and capped["passed"]["risk"] == 0 and capped["filled"] == 0
    assert capped["stopped"]["risk"] == capped["signals"]   # the order-notional limit stopped every buy


def test_the_funnel_is_independent_of_the_kind_tab(root):
    assert decisions.collect(root, kinds=["missed"])["funnel"] == decisions.collect(root)["funnel"]


def test_an_unknown_submission_is_unclassified_never_counted_as_passing():
    base = {"kind": "other", "action": "buy", "fills": []}
    fl = decisions.funnel([{**base, "submission": "something_new"}, {**base, "submission": "stopped_by_risk"},
                           {**base, "kind": "missed", "action": "hold", "submission": "hold"}])
    assert (fl["signals"], fl["unclassified"], fl["missed"]) == (2, 1, 1)
    assert fl["passed"] == {"validity": 1, "risk": 0, "safety": 0} and fl["sent"] == 0


def test_the_funnel_card_filters_the_list_to_existing_kinds_only():
    assert "dcFunnel(d.funnel)" in JS and 'id="dc-funnel"' in JS
    body = JS[JS.index("function dcFunnel"):]
    body = body[:body.index("\n}\n")]
    used = {k for group in re.findall(r'data-kinds="([a-z_,]+)"', body) for k in group.split(",")}
    used |= {k for k in re.findall(r'"(rejected_validity|rejected_risk|blocked_safety)"', body)}
    assert used and used <= set(decisions.KINDS), used


def test_the_inspector_links_each_entry_to_its_recorded_exit_and_names_its_records(root):
    items = sorted(decisions.collect(root, source="paper:ok", limit=2000)["items"], key=lambda i: i["at"])
    entries = [i for i in items if i["kind"] == "entry"]
    assert entries
    for e in entries:
        det = decisions.detail(root, e["id"])
        later_exits = [i for i in items if i["kind"] == "exit" and i["at"] > e["at"]]
        if later_exits:
            assert det["linked"]["id"] == later_exits[0]["id"] and det["linked"]["kind"] == "exit"
            back = decisions.detail(root, det["linked"]["id"])["linked"]
            assert back["kind"] == "entry" and back["at"] < det["linked"]["at"]
        else:
            assert det["linked"] is None
        assert det["records"] == {"decision": "paper/ok/report.json", "trace": "paper/ok/decision_trace.jsonl",
                                  "bars": "paper/ok/bars.jsonl"}
        for rel in det["records"].values():
            assert (root / rel).exists()
    no_trade = next(i for i in items if i["kind"] in ("no_entry", "keep", "missed"))
    assert decisions.detail(root, no_trade["id"])["linked"] is None


def test_more_bars_is_bounded(root):
    pick = decisions.collect(root, source="paper:ok", limit=2000)["items"][-1]
    small, wide = decisions.detail(root, pick["id"]), decisions.detail(root, pick["id"], window=10_000)
    assert len(wide["bars"]) >= len(small["bars"]) and len(wide["bars"]) <= 300 + 12 + 75 + 1


def test_the_inspector_shows_only_recorded_fields_and_opens_the_real_source():
    body = JS[JS.index("function dcInspector"):JS.index("function dcMark")]
    for field in ("i.strategy", "i.symbol", "i.bar_open", "i.price", "i.signal?.reason", "i.fills", "r.decision",
                  "d.linked"):
        assert field in body, field
    assert 'if (s.kind === "paper")' in body and 'if (s.kind === "demo")' in body and "/api/run?id=" in body
    assert "&window=200" in JS


def test_rejection_reasons_are_ranked_from_recorded_codes_and_drill_down_exactly(root):
    res = decisions.collect(root, limit=2000)
    rs, items = res["reasons"], res["items"]
    blocked = [i for i in items if decisions.block_reasons(i)]
    assert rs["blocked"] == len(blocked) > 0
    counts = [r["count"] for r in rs["rows"]]
    assert counts == sorted(counts, reverse=True)
    for r in rs["rows"]:
        carrying = [i for i in items if r["key"] in decisions.block_reasons(i)]
        assert r["count"] == len(carrying) and r["last_at"] == max(i["at"] for i in carrying)
        drill = decisions.collect(root, reason=r["key"], limit=2000)
        assert {i["id"] for i in drill["items"]} == {i["id"] for i in carrying}
        assert drill["reasons"] == rs and drill["funnel"] == res["funnel"]       # drill-down never changes the totals
    keys = {r["key"] for r in rs["rows"]}
    assert "risk:max_order_notional_exceeded" in keys and "missed:bar-arrived-late" in keys
    capped = decisions.collect(root, source="paper:capped", limit=2000)["reasons"]
    assert capped["rows"][0]["key"] == "risk:max_order_notional_exceeded" and capped["rows"][0]["strategies"] == ["dc-cross"]


def test_only_prevented_trades_have_block_reasons():
    base = {"strategy": "s", "at": "2026-01-01T00:00:00+00:00", "risk": None, "validity": None, "rationale": ""}
    assert decisions.block_reasons({**base, "kind": "no_entry", "rationale": "strategy:keep"}) == []
    assert decisions.block_reasons({**base, "kind": "entry"}) == []
    assert decisions.block_reasons({**base, "kind": "no_entry", "rationale": "strategy:no_cash"}) == ["cash:no_cash"]
    assert decisions.block_reasons({**base, "kind": "rejected_risk", "risk": {"decision": "rejected", "codes": []}}) == \
        ["risk:unrecorded"]
    two = {**base, "kind": "rejected_risk", "risk": {"decision": "rejected", "codes": ["a", "b"]}}
    rs = decisions.reasons([two])
    assert rs["blocked"] == 1 and [r["share"] for r in rs["rows"]] == [1, 1]


def test_the_reasons_card_never_calls_a_stopped_trade_a_missed_profit():
    body = JS[JS.index("const DC_STAGE"):JS.index("const dcT")]
    assert "dcReasons(d.reasons)" in JS and 'q.set("reason", dcState.reason)' in JS
    for word in ("ربح ضائع", "فرصة ضائعة", "خسرت فرصة", "missed profit"):
        assert word not in body


def test_a_decision_detail_reads_only_its_own_session(root, monkeypatch):
    # D42 performance: detail() parsed every session of the same kind; measured 282 ms at 40 demo sessions
    pick = next(i for i in decisions.collect(root, source="paper:ok", limit=2000)["items"] if i["kind"] == "entry")
    seen = []
    real = decisions._items
    monkeypatch.setattr(decisions, "_items", lambda source, *a: seen.append(source["name"]) or real(source, *a))
    assert decisions.detail(root, pick["id"])["item"]["id"] == pick["id"]
    assert seen == ["ok"]


def test_trade_links_follow_the_recorded_position_when_it_is_scaled(monkeypatch, tmp_path):
    # Sol D42 blocker 4: filled buy5 -> buy5 -> sell5 -> sell5 linked 1->3 and 4->2 (nearest opposite decision); the
    # engine's own trade list (runtime.py) has one trade, opened by 1 and closed by 4
    def item(n, action):
        return {"id": f"paper:s:r{n}", "source": {"kind": "paper", "name": "s"}, "kind": "entry" if action == "buy" else "exit",
                "action": action, "at": f"2026-01-0{n}T00:00:00+00:00", "fills": [{"quantity": "5", "price": "1"}],
                "exit_kind": None, "evidence": {"trace": None}}

    items = [item(1, "buy"), item(2, "buy"), item(3, "sell"), item(4, "sell")]
    ts = decisions.trades(items)
    assert len(ts) == 1 and ts[0]["open"]["id"] == "paper:s:r1" and ts[0]["close"]["id"] == "paper:s:r4"
    monkeypatch.setattr(decisions, "_paper", lambda root, only=None: items)
    got = {n: decisions.detail(tmp_path, f"paper:s:r{n}") for n in (1, 2, 3, 4)}
    assert (got[1]["role"], got[1]["linked"]["id"]) == ("open", "paper:s:r4")
    assert (got[2]["role"], got[2]["linked"]["id"]) == ("add", "paper:s:r1")
    assert (got[3]["role"], got[3]["linked"]["id"]) == ("reduce", "paper:s:r1")
    assert (got[4]["role"], got[4]["linked"]["id"]) == ("close", "paper:s:r1")
    reopened = items + [item(5, "buy")]                    # a buy after the close opens a new, still open trade
    assert [(t["open"]["id"], t["close"]) for t in decisions.trades(reopened)][1] == ("paper:s:r5", None)
    assert "DC_ROLE[d.role]" in JS and 'd.role === "open"' in JS


def test_each_drill_down_clears_the_other_so_a_shown_count_opens_its_decisions(root):
    # Sol D42 blocker 5: a reason filter kept while clicking a funnel stop count (or the reverse) opened 0 decisions
    res = decisions.collect(root, limit=2000)
    risk = next(r for r in res["reasons"]["rows"] if r["stage"] == "risk")
    both = decisions.collect(root, kinds=["missed"], reason=risk["key"], limit=2000)
    assert both["total"] == 0                                 # why the page must never send both at once
    handlers = {
        "funnel": JS[JS.index("function dcFunnel"):JS.index("const DC_STAGE")],
        "reasons": JS[JS.index("function dcReasons"):JS.index("const dcT")],
        "tabs": JS[JS.index("kt.onclick"):JS.index("$(\"#dc-total\")")],
    }
    assert 'dcState.reason = ""' in handlers["funnel"] and 'dcState.reason = ""' in handlers["tabs"]
    assert "dcState.filters.kinds = []" in handlers["reasons"]
    for stage, kind in (("risk", "rejected_risk"), ("missed", "missed")):
        row = next(r for r in res["reasons"]["rows"] if r["stage"] == stage)
        assert decisions.collect(root, reason=row["key"], limit=2000)["total"] == row["count"] > 0
        assert decisions.collect(root, kinds=[kind], limit=2000)["total"] == res["counts"][kind]
