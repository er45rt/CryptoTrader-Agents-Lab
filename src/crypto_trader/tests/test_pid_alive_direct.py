"""Owner 2026-11-08: the paper page, its detail view and the status bar appeared a fraction of a second late - each
paper-session alive check started tasklist (~260 ms measured). The check now asks Windows directly; same answers."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from crypto_trader.strategies.ui import store


@pytest.mark.skipif(os.name != "nt", reason="the Windows path")
def test_the_alive_check_answers_correctly_without_starting_a_program(monkeypatch):
    def no_programs(*a, **k):
        raise AssertionError("no program may be started for an alive check")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(store.subprocess, "run", no_programs)
        monkeypatch.setattr(store.subprocess, "Popen", no_programs)
        assert store._pid_alive(child.pid) is True
        assert store._pid_alive(os.getpid()) is True
        assert store._pid_alive(4) is True                       # the System process: access denied, still alive
    finally:
        child.kill()
        child.wait(30)
    assert store._pid_alive(child.pid) is False                  # ended
    assert store._pid_alive(4_000_000) is False                  # never existed


@pytest.mark.skipif(os.name != "nt", reason="the Windows path")
def test_the_alive_check_is_fast():
    t = time.perf_counter()
    for _ in range(50):
        store._pid_alive(os.getpid())
    assert (time.perf_counter() - t) / 50 < 0.02                 # tasklist took ~0.26 s per call
