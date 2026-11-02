"""Frozen analysis outputs generated before adapter changes at approved base HEAD."""
import json
from pathlib import Path
import pytest

from crypto_trader.tests.provenance_fixture_scenarios import SCENARIOS, no_network, run_scenario

FIXTURES = Path(__file__).with_name("fixtures") / "adapter_provenance_base"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_analysis_matches_approved_base_golden(scenario, monkeypatch):
    expected = json.loads((FIXTURES / (scenario + ".json")).read_text(encoding="utf-8"))
    assert expected["generated_from_commit"] == "b3ce6364db48cd50a8c04e6098e2d0426ce18dec"
    assert run_scenario(scenario, monkeypatch) == expected["expected"]
