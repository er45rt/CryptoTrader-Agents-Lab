"""Opt-in live DeepSeek smoke test for the Binance Market Data Analyst (Phase 2).

Skipped unless both ``RUN_DEEPSEEK_SMOKE=1`` and ``DEEPSEEK_API_KEY`` are present.
Uses a fixed SYNTHETIC tool payload, so it performs no Binance call at all (public or
authenticated) and no trading action - it exercises only the DeepSeek LLM path for the
migrated market-data agent.

Structured market data comes from the validated Binance tool payload, not from a task
schema, so this test asserts the agent's reporting expectations rather than schema validity.
"""

import json
import os
import time

import pytest

from crypto_trader.llm import MARKET_PROFILE_NAME, get_llm

RUN_SMOKE = os.getenv("RUN_DEEPSEEK_SMOKE") == "1"
HAS_KEY = bool(os.getenv("DEEPSEEK_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (RUN_SMOKE and HAS_KEY),
    reason="Set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to run the live market agent smoke test.",
)

# Fixed synthetic payload mirroring the Binance public tool's validated response shape.
FIXED_SYNTHETIC_TOOL_RESULT = {
    "status": "ok",
    "provider": "binance_public",
    "validation": {
        "candles_received": 3,
        "range_start_open_time_ms": 1767225600000,
        "range_end_open_time_ms": 1767232800000,
        "symbol": "SYNTHUSDT",
    },
    "data": [
        [1767225600000, "100.00", "102.00", "99.50", "101.00", "1000.000", 1767232799999],
        [1767229200000, "101.00", "103.50", "100.50", "103.00", "1200.000", 1767236399999],
        [1767232800000, "103.00", "104.50", "102.80", "104.00", "900.000", 1767239999999],
    ],
}

PROMPT = (
    "You are a Binance market data analyst. You have already received the following validated "
    "tool result (synthetic test data; do not invent extra figures). Report: data completeness, "
    "the timestamp range covered, the number of candles received, and any anomalies. Respond "
    "with a short Markdown summary that includes the symbol, the candle count, and the tool "
    "status. Fixed tool result: " + json.dumps(FIXED_SYNTHETIC_TOOL_RESULT) + "\n"
)


def test_deepseek_market_smoke(capsys):
    llm = get_llm(MARKET_PROFILE_NAME, overrides={"max_tokens": 600, "timeout_seconds": 45})

    started = time.perf_counter()
    text = llm.call(PROMPT)
    wall_ms = (time.perf_counter() - started) * 1000.0

    metadata = dict(getattr(llm, "last_call_metadata", {}) or {})

    with capsys.disabled():
        print(
            "\nMARKET AGENT SMOKE METRICS: "
            f"provider={metadata.get('provider')} model={metadata.get('model')} "
            f"profile={metadata.get('profile')} success={metadata.get('success')} "
            f"attempts={metadata.get('attempts')} retries={metadata.get('retries')} "
            f"latency_ms={wall_ms:.1f} timeout_s={metadata.get('timeout_seconds')} "
            f"output_chars={len(text or '')} "
            "schema=not_applicable(market data schema comes from the validated Binance tool) "
            "usage=unavailable(not exposed by crewai.LLM.call in 0.201.0)"
        )

    assert metadata.get("success") is True
    assert metadata.get("attempts", 0) >= 1
    assert isinstance(text, str) and text.strip()
    assert "SYNTHUSDT" in text
    assert "3" in text  # three synthetic candles reported
