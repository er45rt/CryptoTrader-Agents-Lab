"""Owner status bar 2026-11-08: demo-20261107T060623-b00a stopped with "FileNotFoundError: demo.json.tmp -> demo.json".
Every demo.json save used ONE shared temp name, and speed/start/pause/error saves ran without the demo's lock, so an
overlapping save renamed the other's temp file away. Saves now use their own temp name and run under the demo lock."""
from __future__ import annotations

import os
import threading
import time
from datetime import timedelta

from crypto_trader.strategies.ui import demo
from crypto_trader.tests.test_strategies_demo import env  # noqa: F401  (fixture)


def test_an_overlapping_save_never_loses_the_other_save_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "demo.json"
    real = os.replace
    calls = []

    def replace(src, dst):
        if not calls:                                       # writer A is between its write and its rename ...
            calls.append(src)
            t = threading.Thread(target=demo._atomic, args=(path, '{"b": 1}'))   # ... when writer B saves fully
            t.start()
            t.join()
        return real(src, dst)

    monkeypatch.setattr(demo.os, "replace", replace)
    demo._atomic(path, '{"a": 1}')                          # the shared temp name raised FileNotFoundError here
    assert path.read_text(encoding="utf-8") == '{"a": 1}'
    assert not list(tmp_path.glob("*.tmp"))


def test_speed_and_pause_saves_wait_for_the_running_step(env):  # noqa: F811
    _ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 30))["demo"]["name"]
    lock = dm._session_lock(name)
    lock.acquire()                                          # a step is in progress
    done = threading.Event()
    t = threading.Thread(target=lambda: (dm.set_speed(name, "max"), done.set()))
    t.start()
    time.sleep(0.3)
    assert not done.is_set() and dm._doc(name)["speed"] != "max"   # the speed save waits for the step
    lock.release()
    t.join(5)
    assert done.is_set() and dm._doc(name)["speed"] == "max"


def test_a_running_demo_survives_speed_changes_while_it_steps(env):  # noqa: F811
    _ws, dm, spec, data = env
    name = dm.create(spec=spec, data=data, start=demo.SEALED_FROM - timedelta(hours=4 * 40))["demo"]["name"]
    dm.start(name, "max")
    for i in range(40):
        dm.set_speed(name, "max" if i % 2 else "2")
    dm.pause(name)
    doc = dm._doc(name)
    assert doc["state"] in ("paused", "finished") and not doc.get("error"), doc
