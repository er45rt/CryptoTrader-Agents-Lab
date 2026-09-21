"""Tests for the canary strategist structured-output contract (Phase 2).

No network or LLM calls: parsing/validation is pure, and the end-to-end fail-closed
test stubs the agent's task execution while running CrewAI's real guardrail, retry,
and task pipeline.
"""

import json
from pathlib import Path

import pytest
import yaml
from crewai import Agent, Task
from crewai.tasks.task_output import TaskOutput

from crypto_trader.llm import (
    StructuredOutputError,
    TakeProfitTarget,
    TradingPlan,
    parse_trading_plan,
    trading_plan_guardrail,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def valid_plan_dict() -> dict:
    return {
        "symbol": "ETHUSDT",
        "bias": "bullish",
        "confidence": "medium",
        "human_readable_plan": "# ETHUSDT plan\n\nRationale, sizing, entries, exits.",
        "position_size_pct": 3.0,
        "entry_low": 2500.0,
        "entry_high": 2510.0,
        "stop_loss": 2450.0,
        "take_profit_targets": [
            {"label": "tp1", "level": 2600.0, "allocation_pct": 50.0},
            {"label": "tp2", "level": 2700.0, "allocation_pct": 50.0},
        ],
        "max_loss_pct": 1.0,
        "risk_reward_ratio": 1.8,
        "plan_validity": "48 hours, reassess if the range breaks",
        "invalidation_conditions": ["daily close below 2450"],
    }


def fenced(payload: dict) -> str:
    return "Here is the plan.\n\n```json\n" + json.dumps(payload) + "\n```\n"


def task_output(raw: str) -> TaskOutput:
    return TaskOutput(name="develop plan", description="d", expected_output="e", raw=raw, agent="strategist")


# --- Parsing and validation -------------------------------------------------

def test_valid_fenced_plan_parses():
    plan = parse_trading_plan(fenced(valid_plan_dict()))

    assert isinstance(plan, TradingPlan)
    assert plan.symbol == "ETHUSDT"
    assert plan.bias == "bullish"
    assert len(plan.take_profit_targets) == 2
    assert isinstance(plan.take_profit_targets[0], TakeProfitTarget)


def test_valid_bare_json_object_parses():
    text = "Analysis first...\n" + json.dumps(valid_plan_dict())

    assert parse_trading_plan(text).confidence == "medium"


def test_last_valid_block_wins_when_schema_example_is_echoed_first():
    invalid_example = valid_plan_dict()
    invalid_example["bias"] = "sideways"  # not in the literal set
    text = fenced(invalid_example) + "\nFinal answer:\n" + fenced(valid_plan_dict())

    assert parse_trading_plan(text).bias == "bullish"


def test_human_readable_plan_is_kept_separate_from_machine_fields():
    payload = valid_plan_dict()
    payload["human_readable_plan"] = "# Heading\n\nLong prose explanation."

    plan = parse_trading_plan(fenced(payload))

    assert plan.human_readable_plan.startswith("# Heading")
    assert plan.position_size_pct == 3.0


@pytest.mark.parametrize(
    "mutate,expected_fragment",
    [
        (lambda p: p.update(bias="sideways"), "bias"),
        (lambda p: p.update(position_size_pct="not-a-number"), "position_size_pct"),
        (lambda p: p.update(position_size_pct=-1), "position_size_pct"),
        (lambda p: p.update(entry_low=2600.0), "entry_low"),
        (lambda p: p.update(stop_loss=2505.0), "stop_loss"),
        (lambda p: p.update(take_profit_targets=[{"level": 2600.0, "allocation_pct": 10.0}]), "allocation"),
        (lambda p: p.update(max_loss_pct=0), "max_loss_pct"),
        (lambda p: p.update(risk_reward_ratio=-0.5), "risk_reward_ratio"),
        (lambda p: p.update(invalidation_conditions=[]), "invalidation_conditions"),
        (lambda p: p.update(extra_field="surprise"), "extra_field"),
        (lambda p: p.update(symbol=""), "symbol"),
        (lambda p: p.update(human_readable_plan="   "), "human_readable_plan"),
    ],
)
def test_materially_invalid_fields_are_rejected(mutate, expected_fragment):
    payload = valid_plan_dict()
    mutate(payload)

    with pytest.raises(StructuredOutputError) as excinfo:
        parse_trading_plan(fenced(payload))

    assert expected_fragment in str(excinfo.value)


def test_missing_json_block_is_rejected():
    with pytest.raises(StructuredOutputError) as excinfo:
        parse_trading_plan("Just prose, no machine-readable block.")

    assert "json" in str(excinfo.value).lower()


def test_empty_output_is_rejected():
    with pytest.raises(StructuredOutputError):
        parse_trading_plan("   ")


def test_validation_errors_are_captured_for_diagnostics():
    payload = valid_plan_dict()
    payload["confidence"] = "certain"

    with pytest.raises(StructuredOutputError) as excinfo:
        parse_trading_plan(fenced(payload))

    assert excinfo.value.validation_errors


# --- Guardrail behavior ----------------------------------------------------

def test_guardrail_passes_valid_output_and_preserves_raw_text():
    raw = fenced(valid_plan_dict())
    output = task_output(raw)

    success, result = trading_plan_guardrail(output)

    assert success is True
    assert result == raw  # human-readable output is not rewritten


def test_guardrail_reports_failure_for_bounded_repair():
    success, message = trading_plan_guardrail(task_output("no structured block here"))

    assert success is False
    assert isinstance(message, str)
    assert "json" in message.lower()


def test_guardrail_handles_empty_and_none_raw():
    assert trading_plan_guardrail(task_output(""))[0] is False
    output = task_output("")
    output.raw = None
    assert trading_plan_guardrail(output)[0] is False


# --- Task-level integration: bounded repair then fail closed ---------------

def _agent_with_stubbed_output(monkeypatch, responses):
    """Build a real Agent whose task execution is scripted, avoiding any LLM call."""
    scripted = {"index": 0, "calls": 0}

    def fake_execute_task(self, task=None, context=None, tools=None):
        scripted["calls"] += 1
        index = min(scripted["index"], len(responses) - 1)
        scripted["index"] += 1
        return responses[index]

    monkeypatch.setattr(Agent, "execute_task", fake_execute_task)
    agent = Agent(role="strategist", goal="g", backstory="b", llm="openai/gpt-4o-mini", verbose=False)
    return agent, scripted


def test_task_accepts_valid_structured_output(monkeypatch):
    raw = fenced(valid_plan_dict())
    agent, scripted = _agent_with_stubbed_output(monkeypatch, [raw])
    task = Task(
        description="d",
        expected_output="e",
        agent=agent,
        guardrail=trading_plan_guardrail,
        guardrail_max_retries=1,
    )

    output = task.execute_sync(agent=agent)

    assert output.raw == raw
    assert scripted["calls"] == 1  # no repair needed


def test_task_fails_closed_after_bounded_repair(monkeypatch):
    agent, scripted = _agent_with_stubbed_output(monkeypatch, ["still not json"])
    task = Task(
        description="d",
        expected_output="e",
        agent=agent,
        guardrail=trading_plan_guardrail,
        guardrail_max_retries=1,
    )

    with pytest.raises(Exception) as excinfo:
        task.execute_sync(agent=agent)

    assert "guardrail validation" in str(excinfo.value)
    assert scripted["calls"] == 2  # original attempt + exactly one bounded repair


def test_task_is_repaired_once_when_second_attempt_is_valid(monkeypatch):
    raw = fenced(valid_plan_dict())
    agent, scripted = _agent_with_stubbed_output(monkeypatch, ["bad output", raw])
    task = Task(
        description="d",
        expected_output="e",
        agent=agent,
        guardrail=trading_plan_guardrail,
        guardrail_max_retries=1,
    )

    output = task.execute_sync(agent=agent)

    assert output.raw == raw
    assert scripted["calls"] == 2


# --- Schema / task-description drift guard ---------------------------------

def test_task_description_documents_every_schema_field():
    tasks = yaml.safe_load((CONFIG_DIR / "tasks.yaml").read_text(encoding="utf-8"))
    description = tasks["develop_trading_plan_task"]["description"]

    required_fields = [
        name for name, field in TradingPlan.model_fields.items() if field.is_required()
    ]
    assert required_fields, "expected the schema to declare required fields"
    for field_name in required_fields:
        assert field_name in description, f"{field_name} missing from task description"
