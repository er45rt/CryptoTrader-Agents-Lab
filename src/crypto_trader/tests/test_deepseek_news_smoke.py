"""Opt-in live DeepSeek smoke test for the News and Sentiment Researcher (Phase 2).

Skipped unless both ``RUN_DEEPSEEK_SMOKE=1`` and ``DEEPSEEK_API_KEY`` are present.
Uses fixed SYNTHETIC news snippets and does NOT call the Serper/scrape tools (so it is
deterministic and cannot depend on an external news provider), and performs no trading
action. It exercises only the DeepSeek LLM path for the migrated news agent.

The research summary is intentionally prose/markdown, so this test asserts its reporting
expectations rather than a Pydantic schema.
"""

import os
import time

import pytest

from crypto_trader.llm import NEWS_PROFILE_NAME, get_llm

RUN_SMOKE = os.getenv("RUN_DEEPSEEK_SMOKE") == "1"
HAS_KEY = bool(os.getenv("DEEPSEEK_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (RUN_SMOKE and HAS_KEY),
    reason="Set RUN_DEEPSEEK_SMOKE=1 and DEEPSEEK_API_KEY to run the live news agent smoke test.",
)

FIXED_SYNTHETIC_NEWS = (
    "SYNTHUSDT news, fixed synthetic set for testing:\n"
    "1) (high credibility, 6h old) A tier-1 outlet reports a protocol upgrade shipped on schedule.\n"
    "2) (medium credibility, 20h old) A mid-tier outlet reports rising exchange inflows.\n"
    "3) (low credibility, 30h old) A blog post claims a rumoured partnership (unconfirmed).\n"
    "Social: engagement is moderate; sentiment skews mildly positive but mixed on the inflow story."
)

PROMPT = (
    "You are a crypto news and sentiment researcher. Using ONLY the synthetic news set below, "
    "produce a short Markdown research summary for SYNTHUSDT containing: recent news items with "
    "source credibility, a market sentiment assessment with a confidence level, social trends, "
    "and any catalysts or risks. Do not invent sources beyond the fixed set, and do not trade. "
    "Synthetic news set:\n" + FIXED_SYNTHETIC_NEWS + "\n"
)


def test_deepseek_news_smoke(capsys):
    llm = get_llm(NEWS_PROFILE_NAME, overrides={"max_tokens": 700, "timeout_seconds": 45})

    started = time.perf_counter()
    text = llm.call(PROMPT)
    wall_ms = (time.perf_counter() - started) * 1000.0

    metadata = dict(getattr(llm, "last_call_metadata", {}) or {})

    with capsys.disabled():
        print(
            "\nNEWS AGENT SMOKE METRICS: "
            f"provider={metadata.get('provider')} model={metadata.get('model')} "
            f"profile={metadata.get('profile')} success={metadata.get('success')} "
            f"attempts={metadata.get('attempts')} retries={metadata.get('retries')} "
            f"latency_ms={wall_ms:.1f} timeout_s={metadata.get('timeout_seconds')} "
            f"output_chars={len(text or '')} "
            "schema=not_applicable(news research output is prose) "
            "usage=unavailable(not exposed by crewai.LLM.call in 0.201.0)"
        )

    assert metadata.get("success") is True
    assert metadata.get("attempts", 0) >= 1
    assert isinstance(text, str) and text.strip()
    lowered = text.lower()
    assert "sentiment" in lowered
    assert "confidence" in lowered
