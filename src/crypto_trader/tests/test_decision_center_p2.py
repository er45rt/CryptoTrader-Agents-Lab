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
