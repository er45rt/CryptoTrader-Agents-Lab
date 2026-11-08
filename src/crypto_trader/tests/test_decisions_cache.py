"""Owner 2026-11-08: the decision center re-read every finished demo (~2,500 decisions, ~150 ms) on each open and each
30 s refresh. Decisions are now kept per session folder and re-read only when one of their files changed: same
answers, a running session still updates at once."""
from __future__ import annotations

from datetime import timedelta

from crypto_trader.strategies.ui import decisions, demo
from crypto_trader.tests.test_strategies_demo import env  # noqa: F401  (fixture)


def _counting(monkeypatch):
    calls = []
    real = decisions._items

    def spy(*a, **k):
        calls.append(a[0]["name"])
        return real(*a, **k)

    monkeypatch.setattr(decisions, "_items", spy)
    return calls


def test_unchanged_sessions_are_not_read_again_and_answers_are_identical(env, monkeypatch):  # noqa: F811
    ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 40))["demo"]["name"]
    dm.step(name, 10)
    decisions._CACHE.clear()
    first = decisions.collect(ws.root, limit=400)
    calls = _counting(monkeypatch)
    again = decisions.collect(ws.root, limit=400)
    assert again == first and calls == []                     # nothing re-read, identical answer
    decisions._CACHE.clear()
    assert decisions.collect(ws.root, limit=400) == first and calls == [name]   # a cold read gives the same answer


def test_a_session_that_records_a_new_decision_is_read_again_at_once(env, monkeypatch):  # noqa: F811
    ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 40))["demo"]["name"]
    dm.step(name, 5)
    before = decisions.collect(ws.root, limit=400)["total"]
    calls = _counting(monkeypatch)
    dm.step(name, 3)                                          # report.json and the trace change
    after = decisions.collect(ws.root, limit=400)["total"]
    assert after > before and calls == [name]


def test_a_changed_trace_alone_also_counts_as_a_change(env, monkeypatch):  # noqa: F811
    ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 40))["demo"]["name"]
    dm.step(name, 3)
    decisions.collect(ws.root, limit=400)
    calls = _counting(monkeypatch)
    trace = dm.folder(name) / "decision_trace.jsonl"
    trace.write_text(trace.read_text(encoding="utf-8") + "\n", encoding="utf-8")   # size changes
    decisions.collect(ws.root, limit=400)
    assert calls == [name]
