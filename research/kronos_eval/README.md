# Kronos offline evaluation

This directory is a research-only harness. It is not imported by production, paper,
testnet, OMS, execution, risk, ledger, or reconciliation code.

## Pinned experiment

- Kronos code commit: `083294bd84b901f4f3ced219937c00a4fa61a6d4` (2025-09-16, before OOS start)
- Model: `NeoQuasar/Kronos-small` at revision
  `901c26c1332695a2a8f243eb2f37243a37bea320` (2025-09-09)
- Tokenizer: `NeoQuasar/Kronos-Tokenizer-base` at revision
  `0e0117387f39004a9016484a186a908917e22426` (2025-09-09)
- Context cap: 512 candles
- Stochastic samples per signal: 20

`pins.json` additionally records SHA-256 hashes for the downloaded safetensors.
The source and weight revisions pre-date the OOS target start on 2025-10-01.

## Reproduce

Use Python 3.12 and install `requirements.txt`. Set `PYTHONPATH=src` from the
repository root, then run:

1. `python research/kronos_eval/prepare_source.py`
2. `python research/kronos_eval/prepare_weights.py`
3. `python research/kronos_eval/prepare_data.py`
4. `python research/kronos_eval/run_backtests.py`

Large source/model snapshots, historical CSVs, and raw result rows are ignored by
Git. The final evidence is summarized in the committed evaluation report.

## Evaluation design

The out-of-sample target period is one full year, 2025-10-01 through 2026-09-30
inclusive, for BTCUSDT and ETHUSDT at 5m, 15m, and 1h. Each snapshot also includes
exactly 512 earlier candles as warm-up, so the first OOS target remains 2025-10-01
instead of consuming part of the target year. Each forecast is one candle ahead and
receives exactly the preceding 512 candles. The target candle is never passed to the
signal provider. The test suite includes both a direct boundary test and a
future-mutation test proving that future candles cannot alter already-formed forecasts.

The runner uses a deterministic, evenly spaced walk-forward grid with 89 origins per
symbol/timeframe. This keeps the CPU-only experiment bounded while spanning the full
OOS year. The derived ~4.1-day stride rotates through intraday clock phases; 73 origins
was rejected before the final run because it would imply an exact five-day cadence and
alias the same clock phase. The sampling plan and seed are fixed before results are
inspected.

Baselines are last-price (zero-return), random walk with drift, and 12-candle simple
momentum. Strategy P/L uses the same threshold rule for all methods and charges
0.10% fee per side plus 0.02% slippage per side (0.24% round trip).

## Pre-committed decision gate

A symbol/timeframe passes only if Kronos:

1. has positive net P/L after the modeled fee and slippage;
2. has lower absolute-return error than **every** baseline with one-sided
   Diebold-Mariano `p < 0.05`; and
3. has a paired-bootstrap 95% confidence interval strictly above zero for its
   after-cost net-return advantage versus **every** baseline.

The global answer is **YES** only if the same timeframe passes on both BTCUSDT and
ETHUSDT. Otherwise the answer is **NO** and no integration is proposed. This rule is
fixed before the final 89-origin results are inspected.
