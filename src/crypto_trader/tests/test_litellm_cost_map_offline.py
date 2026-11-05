"""D37: litellm never fetches its model cost map from the network in the offline test process.

litellm 1.74.9 calls get_model_cost_map() on first import; unless LITELLM_LOCAL_MODEL_COST_MAP is set it first tries
httpx.get(raw.githubusercontent.com/...), falling back to the bundled backup only when that fails. conftest.py sets the
flag before any test module imports litellm (directly or through crewai).
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
    import json, socket
    attempts = []
    def refuse(*a, **k):
        attempts.append([str(x) for x in a[:2]])
        raise OSError("network refused by offline probe")
    socket.socket.connect = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    import importlib.resources
    import litellm
    with importlib.resources.open_text("litellm", "model_prices_and_context_window_backup.json") as f:
        bundled = json.load(f)
    gh = [a for a in attempts if "githubusercontent.com" in a[0]]
    print(json.dumps({"github_attempts": gh, "cost_map_is_bundled": litellm.model_cost == bundled,
                      "n_models": len(litellm.model_cost)}))
""")


def _probe(flag: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_LOCAL_MODEL_COST_MAP"}
    if flag is not None:
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = flag
    env["PYTHONPATH"] = str(SRC)
    out = subprocess.run([sys.executable, "-B", "-c", _PROBE], capture_output=True, text=True, env=env, timeout=180,
                         check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_test_process_uses_the_bundled_cost_map():
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"
    import importlib.resources

    import litellm

    with importlib.resources.open_text("litellm", "model_prices_and_context_window_backup.json") as f:
        assert litellm.model_cost == json.load(f)


def test_fresh_process_with_the_flag_never_dials_github_and_keeps_the_full_cost_map():
    res = _probe("True")
    assert res["github_attempts"] == [] and res["cost_map_is_bundled"] and res["n_models"] > 100, res


def test_negative_control_without_the_flag_the_guard_catches_the_fetch():
    # proves the probe bites: without the flag litellm tries GitHub first (refused here, then falls back)
    res = _probe(None)
    assert res["github_attempts"], res
