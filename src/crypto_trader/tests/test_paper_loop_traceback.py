"""Paper session 2026-11-08: a cycle failed for hours with only "PermissionError: ... \\\\.\\aswMonFltProxy\\..." in the log, so
the failing operation could not be identified. The loop now logs the full traceback once per distinct error in the
process; repeats keep the one-line message; last_error.json keeps its fields."""
from __future__ import annotations

import json

from crypto_trader.strategies import paper_live as pl


def _open_the_device():
    raise PermissionError(13, "Permission denied", r"\\.\aswMonFltProxy\53f6256412707de7")


def test_the_traceback_names_the_failing_operation_once_per_distinct_error(tmp_path, monkeypatch):
    errors = iter([_open_the_device, _open_the_device, lambda: (_ for _ in ()).throw(OSError("other")), _open_the_device])

    def cycle(folder, cafile=None):
        next(errors)()

    monkeypatch.setattr(pl, "paper_cycle", cycle)
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    log = []
    pl.run_paper_loop(tmp_path, poll_seconds=0, max_cycles=4, log=log.append)
    traces = [m for m in log if m.startswith("Traceback")]
    assert len(traces) == 2                                      # the device error once, the other error once
    assert "_open_the_device" in traces[0] and "PermissionError" in traces[0] and "aswMonFltProxy" in traces[0]
    assert "OSError: other" in traces[1]
    assert sum(m.startswith("cycle failed, will retry: PermissionError") for m in log) == 3   # every failure still logged
    doc = json.loads((tmp_path / "last_error.json").read_text(encoding="utf-8"))
    assert set(doc) == {"at", "error", "consecutive"} and doc["consecutive"] == 4
