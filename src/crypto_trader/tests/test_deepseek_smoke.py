"""Opt-in live DeepSeek canary smoke test (Phase 2, Step 8).

Skipped unless both ``RUN_DEEPSEEK_SMOKE=1`` and ``DEEPSEEK_API_KEY`` are present, so
the default suite stays offline and free. When it does run, it performs exactly ONE
low-cost chat request through the centralized factory, then validates the structured
output. It never prints the API key and never touches Binance.
"""

import json
import os
import time

import pytest

from crypto_trader.llm import CANARY_PROFILE_NAME, get_llm, parse_trading_plan

RUN_SMOKE = os.getenv("RUN_DEEPSEEK_SMOKE") == "1"
HAS_KEY = bool(os.getenv("DEEPSEEK_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (RUN_SMOKE and HAS_KEY),
    reason="Set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to run the live DeepSeek canary smoke test.",
)

# Fixed, deterministic input so runs are comparable.
FIXED_TEST_INPUT = {
    "symbol": "ETHUSDT",
    "kline_interval": "4h",
    "kline_limit": 72,
    "current_datetime": "2026-01-01 00:00:00 UTC",
}

# Built by concatenation: the prompt itself contains literal JSON braces, which would
# be interpreted as format fields by str.format or an f-string.
PROMPT = (
    "You are a crypto trading plan strategist. Using only the fixed context below, "
    "produce (a) a short human-readable markdown plan and (b) one fenced ```json block with "
    "exactly these keys: symbol, bias (bullish|bearish|neutral), confidence (low|medium|high), "
    "human_readable_plan, position_size_pct, entry_low, entry_high, stop_loss, "
    "take_profit_targets (array of objects with keys label, level, allocation_pct; "
    "allocations summing to 100), max_loss_pct, risk_reward_ratio, plan_validity, "
    "invalidation_conditions (non-empty array). Use JSON numbers for numeric fields "
    "and no extra keys.\n\n"
    "Fixed context: " + json.dumps(FIXED_TEST_INPUT) + "\n"
    "Assume ETHUSDT is trading near 2500 with support at 2450 and resistance at 2650.\n"
)


def test_deepseek_canary_smoke(capsys):
    llm = get_llm(
        CANARY_PROFILE_NAME,
        overrides={"max_tokens": 900, "timeout_seconds": 60},
    )

    started = time.perf_counter()
    text = llm.call(PROMPT)
    wall_ms = (time.perf_counter() - started) * 1000.0

    metadata = dict(getattr(llm, "last_call_metadata", {}) or {})
    usage = _extract_usage(text)

    with capsys.disabled():
        print(
            "\nDEEPSEEK SMOKE METRICS: "
            f"provider={metadata.get('provider')} model={metadata.get('model')} "
            f"success={metadata.get('success')} attempts={metadata.get('attempts')} "
            f"retries={metadata.get('retries')} latency_ms={wall_ms:.1f} "
            f"timeout_s={metadata.get('timeout_seconds')} usage={usage}"
        )

    assert metadata.get("success") is True, "canary call did not report success"
    assert metadata.get("attempts", 0) >= 1

    # Structured-output validity is the point of the canary: the same parser the
    # task guardrail uses must accept the live response.
    plan = parse_trading_plan(text)
    assert plan.symbol == "ETHUSDT"
    assert plan.bias in {"bullish", "bearish", "neutral"}


def _extract_usage(_text: str) -> str:
    """Token/cost reporting is limited by what the CrewAI LLM call exposes.

    CrewAI's ``LLM.call`` returns only text (usage is not surfaced on the return
    value in 0.201.0), so token accounting is reported as unavailable rather than
    guessed at.
    """
    return "unavailable (not exposed by crewai.LLM.call in 0.201.0)"
