"""Opt-in live DeepSeek smoke test for the Comprehensive Crypto Analyst (Phase 2).

Skipped unless both ``RUN_DEEPSEEK_SMOKE=1`` and ``DEEPSEEK_API_KEY`` are present, so the
default suite stays offline and free. When it runs it performs exactly ONE low-cost chat
request through the centralized factory using a fixed, fully synthetic input - no live
market data, no authenticated Binance call, no trading actions.

The analyst has no Pydantic output schema by design (its Markdown report is consumed by the
strategist as LLM context, not machine-parsed), so this smoke test asserts the structural
expectations of its existing responsibility instead of schema validity.
"""

import json
import os
import time

import pytest

from crypto_trader.llm import ANALYST_PROFILE_NAME, get_llm

RUN_SMOKE = os.getenv("RUN_DEEPSEEK_SMOKE") == "1"
HAS_KEY = bool(os.getenv("DEEPSEEK_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (RUN_SMOKE and HAS_KEY),
    reason="Set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to run the live analyst smoke test.",
)

# Fixed synthetic input: deterministic, no live market data, no credentials.
FIXED_SYNTHETIC_INPUT = {
    "symbol": "SYNTHUSDT",
    "current_datetime": "2026-01-01 00:00:00 UTC",
    "klines_summary": (
        "72 x 4h candles. Close ranged 98.0-104.5, latest 103.2. Volume steady, "
        "no gaps, timestamps sequential."
    ),
    "price_ticker": "last 103.2, bid 103.19, ask 103.21",
    "stats_24hr": {
        "open": 99.1,
        "high": 104.5,
        "low": 98.0,
        "last": 103.2,
        "volume": "1,250,000 SYNTHUSDT",
        "change_pct": 4.14,
    },
    "news_research": (
        "(synthetic) Two mid-tier outlets report a partnership announcement 18 hours ago; "
        "sentiment moderately positive, low-to-medium source credibility."
    ),
}

PROMPT = (
    "You are a comprehensive crypto analyst. Using ONLY the synthetic input below, produce "
    "a Markdown analysis report with these sections: 1. Executive Summary with confidence "
    "level; 2. Data Quality Assessment; 3. Technical Analysis (trends, support/resistance, "
    "volatility); 4. Fundamental Analysis (news/sentiment correlation); 5. Key Risk Factors; "
    "6. Conclusion with confidence-weighted recommendations. Every major conclusion must "
    "state a confidence level (High/Medium/Low) and its rationale. Do not place orders.\n\n"
    "Synthetic input: " + json.dumps(FIXED_SYNTHETIC_INPUT) + "\n"
)


def test_deepseek_analyst_smoke(capsys):
    llm = get_llm(
        ANALYST_PROFILE_NAME,
        overrides={"max_tokens": 900, "timeout_seconds": 60},
    )

    started = time.perf_counter()
    text = llm.call(PROMPT)
    wall_ms = (time.perf_counter() - started) * 1000.0

    metadata = dict(getattr(llm, "last_call_metadata", {}) or {})

    with capsys.disabled():
        print(
            "\nANALYST SMOKE METRICS: "
            f"provider={metadata.get('provider')} model={metadata.get('model')} "
            f"profile={metadata.get('profile')} success={metadata.get('success')} "
            f"attempts={metadata.get('attempts')} retries={metadata.get('retries')} "
            f"latency_ms={wall_ms:.1f} timeout_s={metadata.get('timeout_seconds')} "
            f"output_chars={len(text or '')} "
            "schema=not_applicable(no machine schema for analyst Markdown output) "
            "usage=unavailable(not exposed by crewai.LLM.call in 0.201.0)"
        )

    assert metadata.get("success") is True
    assert metadata.get("attempts", 0) >= 1
    assert isinstance(text, str) and text.strip(), "analyst returned empty output"

    # Structural expectations of its existing responsibility (not a schema).
    lowered = text.lower()
    assert "confidence" in lowered
    assert any(marker in lowered for marker in ("support", "resistance"))
