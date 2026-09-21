"""Structured output contract for the canary Trading Plan Strategist.

The strategist keeps producing its human-readable plan, and additionally emits one
machine-readable JSON block with the action-relevant fields. This module owns the
schema and the validation used to enforce it:

* :func:`parse_trading_plan` validates task output against :class:`TradingPlan`.
* :func:`trading_plan_guardrail` is wired into the strategist task so invalid output
  triggers a bounded repair through CrewAI's guardrail retry, and a permanent
  failure aborts the task (fail closed) instead of passing bad data downstream.

Invalid values are rejected outright - no silent coercion of materially invalid
trading fields (prices, stop placement, allocations, position sizing).

NOTE: this module intentionally does not use ``from __future__ import annotations``.
CrewAI inspects the guardrail's return annotation at runtime and requires a real
``tuple[bool, Any]``, which string annotations would break.
"""

import json
import math
import re
from typing import Any, Iterator, Literal

from crewai.tasks.task_output import TaskOutput
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "StructuredOutputError",
    "TakeProfitTarget",
    "TradingPlan",
    "parse_trading_plan",
    "trading_plan_guardrail",
]

# Guard against unbounded repair prompts / error spam.
_MAX_REPORTED_ERRORS = 8

_FENCED_BLOCK = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9]{5,20}$")


class StructuredOutputError(RuntimeError):
    """Raised when task output cannot be parsed into the required structure."""

    def __init__(self, message: str, *, validation_errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.validation_errors = validation_errors or []


def _require_finite_positive(value: float, *, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number.")
    if number <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")
    return number


class TakeProfitTarget(BaseModel):
    """One take-profit level with its share of the position."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    level: float = Field(description="Take-profit price level.")
    allocation_pct: float = Field(description="Percentage of the position to close at this level.")

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: float) -> float:
        return _require_finite_positive(value, field_name="take_profit_targets[].level")

    @field_validator("allocation_pct")
    @classmethod
    def _validate_allocation(cls, value: float) -> float:
        number = _require_finite_positive(value, field_name="take_profit_targets[].allocation_pct")
        if number > 100:
            raise ValueError("take_profit_targets[].allocation_pct must be <= 100.")
        return number


class TradingPlan(BaseModel):
    """Machine-actionable trading plan produced by the canary strategist.

    The business role is unchanged: this mirrors the plan components the
    strategist already produces (rationale, sizing, entry, exit, risk, validity).
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="Trading symbol, e.g. ETHUSDT.")
    bias: Literal["bullish", "bearish", "neutral"] = Field(description="Directional stance.")
    confidence: Literal["low", "medium", "high"] = Field(description="Confidence in the plan.")
    human_readable_plan: str = Field(
        description="Full human-readable markdown plan, kept separate from the machine-readable fields."
    )
    position_size_pct: float = Field(description="Recommended position size as a percentage of account equity.")
    entry_low: float = Field(description="Lower bound of the entry zone.")
    entry_high: float = Field(description="Upper bound of the entry zone.")
    stop_loss: float = Field(description="Stop-loss price.")
    take_profit_targets: list[TakeProfitTarget] = Field(
        description="One or more take-profit levels with allocation percentages."
    )
    max_loss_pct: float = Field(description="Maximum acceptable loss, as a percentage of account equity.")
    risk_reward_ratio: float = Field(description="Expected reward divided by expected risk.")
    plan_validity: str = Field(description="How long the plan stays valid, and under what conditions.")
    invalidation_conditions: list[str] = Field(
        description="Conditions that invalidate the plan and force reassessment."
    )
    disclaimer: str | None = Field(default=None, description="Optional human-readable disclaimer.")

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not isinstance(value, str) or not _SYMBOL_PATTERN.match(value.strip()):
            raise ValueError("symbol must be an alphanumeric market symbol such as ETHUSDT.")
        return value.strip().upper()

    @field_validator("human_readable_plan", "plan_validity")
    @classmethod
    def _validate_non_empty_text(cls, value: str, info: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string.")
        return value

    @field_validator("entry_low", "entry_high", "stop_loss")
    @classmethod
    def _validate_price(cls, value: float, info: Any) -> float:
        return _require_finite_positive(value, field_name=info.field_name)

    @field_validator("position_size_pct", "max_loss_pct")
    @classmethod
    def _validate_percent(cls, value: float, info: Any) -> float:
        number = _require_finite_positive(value, field_name=info.field_name)
        if number > 100:
            raise ValueError(f"{info.field_name} must be <= 100.")
        return number

    @field_validator("risk_reward_ratio")
    @classmethod
    def _validate_risk_reward(cls, value: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("risk_reward_ratio must be a number.")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("risk_reward_ratio must be a finite number.")
        if number < 0:
            raise ValueError("risk_reward_ratio must not be negative.")
        return number

    @field_validator("invalidation_conditions")
    @classmethod
    def _validate_invalidations(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("invalidation_conditions must contain at least one condition.")
        for index, condition in enumerate(value):
            if not isinstance(condition, str) or not condition.strip():
                raise ValueError(f"invalidation_conditions[{index}] must be a non-empty string.")
        return value

    @model_validator(mode="after")
    def _validate_internal_consistency(self) -> "TradingPlan":
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must be less than or equal to entry_high.")
        if self.entry_low <= self.stop_loss <= self.entry_high:
            raise ValueError(
                "stop_loss must sit outside the entry range (entry_low..entry_high); "
                "a stop inside the entry zone is not executable as a risk boundary."
            )
        allocation_total = sum(target.allocation_pct for target in self.take_profit_targets)
        if abs(allocation_total - 100.0) > 1.0:
            raise ValueError(
                f"take_profit_targets allocations must sum to 100% (got {allocation_total:.4f}%)."
            )
        return self


def _iter_json_candidates(text: str) -> Iterator[dict[str, Any]]:
    """Yield JSON objects found in ``text``, most-recent-first within each pass."""
    fenced: list[str] = [match.group("body").strip() for match in _FENCED_BLOCK.finditer(text)]
    for body in reversed(fenced):
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            yield parsed

    decoder = json.JSONDecoder()
    bare: list[dict[str, Any]] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            break
        try:
            parsed, end = decoder.raw_decode(text[start:])
        except ValueError:
            index = start + 1
            continue
        if isinstance(parsed, dict):
            bare.append(parsed)
        index = start + max(1, end)
    for payload in reversed(bare):
        yield payload


def _format_validation_error(error: ValidationError) -> str:
    details = []
    for item in error.errors()[:_MAX_REPORTED_ERRORS]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        details.append(f"{location}: {item.get('msg')}")
    return "; ".join(details)


def parse_trading_plan(text: str) -> TradingPlan:
    """Extract and validate the machine-readable trading plan from task output.

    Raises:
        StructuredOutputError: If no JSON object is present or none of the candidate
            objects satisfies :class:`TradingPlan`.
    """
    if not isinstance(text, str) or not text.strip():
        raise StructuredOutputError(
            "Task output is empty; expected a machine-readable JSON block matching the trading plan schema."
        )

    last_error: ValidationError | None = None
    found_candidate = False
    for payload in _iter_json_candidates(text):
        found_candidate = True
        try:
            return TradingPlan.model_validate(payload)
        except ValidationError as exc:
            last_error = exc

    if not found_candidate:
        raise StructuredOutputError(
            "No machine-readable JSON object found in the task output. Expected a fenced ```json block "
            "matching the trading plan schema."
        )

    assert last_error is not None  # pragma: no cover - defensive
    raise StructuredOutputError(
        "Trading plan JSON failed schema validation: " + _format_validation_error(last_error),
        validation_errors=last_error.errors(),
    )


def trading_plan_guardrail(output: TaskOutput) -> tuple[bool, Any]:
    """Task guardrail enforcing the structured plan contract.

    Returns ``(True, raw_output)`` when the output validates - the original text is
    preserved so human-readable output and file writes are unchanged. Returns
    ``(False, error)`` when it does not, which CrewAI feeds back to the agent as a
    bounded repair attempt; exhausting the retry budget aborts the task.
    """
    raw = getattr(output, "raw", "") or ""
    try:
        parse_trading_plan(raw)
    except StructuredOutputError as exc:
        return False, str(exc)
    return True, raw
