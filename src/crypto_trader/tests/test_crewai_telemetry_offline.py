"""D36: CrewAI telemetry never runs in the offline test process (Sol High flakes diagnosis, failure 3).

CrewAI 0.201 builds its telemetry singleton (OTLP exporter + background BatchSpanProcessor) on first import unless
CREWAI_DISABLE_TELEMETRY is already set; the exporter then dials telemetry.crewai.com:4319 on its own schedule, which the
offline tests' network guards caught at random teardowns. conftest.py sets the flag before any test module imports crewai.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]

_PROBE = textwrap.dedent("""
    import json, socket, sys
    attempts = []
    def refuse(*a, **k):
        attempts.append([str(x) for x in a[:2]])
        raise OSError("network refused by offline probe")
    socket.socket.connect = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    import crewai.events.event_listener  # noqa: F401  (builds the telemetry singleton, as any crewai use does)
    from crewai.telemetry import Telemetry
    t = Telemetry()
    if t.ready:                                    # emit one span and flush it now, under the guard
        with t.provider.get_tracer("d36-probe").start_as_current_span("probe"):
            pass
        t.provider.force_flush(10_000)
    tele = [a for a in attempts if "telemetry.crewai.com" in a[0]]
    print(json.dumps({"ready": t.ready, "has_provider": hasattr(t, "provider"), "telemetry_attempts": tele,
                      "other_attempts": [a for a in attempts if a not in tele]}))
""")


def _probe(flag: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED",
                                                            "CREWAI_DISABLE_TRACKING")}
    if flag is not None:
        env["CREWAI_DISABLE_TELEMETRY"] = flag
    env["PYTHONPATH"] = str(SRC)
    out = subprocess.run([sys.executable, "-B", "-c", _PROBE], capture_output=True, text=True, env=env, timeout=180,
                         check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_test_process_has_crewai_telemetry_disabled_before_any_import():
    # in THIS pytest process: whichever test imported crewai first, the singleton must not be armed
    assert os.environ.get("CREWAI_DISABLE_TELEMETRY") == "true"
    from crewai.telemetry import Telemetry

    t = Telemetry()
    assert t.ready is False and not hasattr(t, "provider")


def test_fresh_process_with_the_flag_never_builds_an_exporter_or_dials_out():
    # scope: the telemetry exporter. Other import-time attempts (e.g. litellm's model cost map) are reported in
    # other_attempts and are tracked separately; they are not asserted here.
    res = _probe("true")
    assert (res["ready"], res["has_provider"], res["telemetry_attempts"]) == (False, False, []), res


def test_negative_control_without_the_flag_the_guard_catches_the_export():
    # proves the probe bites: without the flag CrewAI arms its exporter and a flushed span tries the network
    res = _probe(None)
    assert res["ready"] is True and res["has_provider"] is True
    assert res["telemetry_attempts"], res
