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
