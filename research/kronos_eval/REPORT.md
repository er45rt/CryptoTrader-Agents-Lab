# Kronos Offline Signal Evaluation

## Decision

**NO — do not integrate Kronos into CryptoTrader-Agents-Lab.** The pre-committed statistical/after-cost gate was not replicated on both BTCUSDT and ETHUSDT for any timeframe.

This decision uses only one-candle-ahead walk-forward forecasts whose target candles were unavailable to the model input. No paper, testnet, or live trading was used.

## Reproducibility pins

| Artifact | Exact revision | Revision time (UTC) | SHA-256 |
|---|---|---:|---|
| Kronos source | `083294bd84b901f4f3ced219937c00a4fa61a6d4` | 2025-09-16T02:36:13Z | n/a |
| Kronos-small | `901c26c1332695a2a8f243eb2f37243a37bea320` | 2025-09-09T14:10:26Z | `b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020` |
| Kronos-Tokenizer-base | `0e0117387f39004a9016484a186a908917e22426` | 2025-09-09T14:10:02Z | `59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee` |

All three pinned revisions pre-date the OOS target start (2025-10-01), which prevents the evaluated market period from being introduced by a later model/source revision.

## Evaluation design

- OOS target: 2025-10-01T00:00:00+00:00 to 2026-10-01T00:00:00+00:00 (end exclusive).
- Markets: BTCUSDT and ETHUSDT; frames: 5m, 15m, 1h; 89 deterministic walk-forward origins per series.
- Context: 512 candles; horizon: one candle; stochastic samples per signal: 20.
- Friction: 0.10% fee per side + 0.02% slippage per side = 0.24% round trip.
- Baselines: last-price/persistence (zero return), random walk with drift, and 12-candle simple momentum.
- Statistical tests: one-sided Diebold-Mariano on absolute return error; paired 5,000-resample bootstrap on after-cost net-return advantage.

## Dataset integrity

| Snapshot | Rows | First candle | Last candle | SHA-256 |
|---|---:|---|---|---|
| BTCUSDT_15m_2025-10-01_2026-10-01.csv | 35552 | 2025-09-25T16:00:00+00:00 | 2026-09-30T23:45:00+00:00 | `bf612c518c6c23ae809f471a8b433d9055ea3128f5644671039d67e7e37c1e86` |
| BTCUSDT_1h_2025-10-01_2026-10-01.csv | 9272 | 2025-09-09T16:00:00+00:00 | 2026-09-30T23:00:00+00:00 | `adc7ed444dd1fc36b9bc7b243e6eb914a5e14e704ee6f05d0aa27278ac9b64bd` |
| BTCUSDT_5m_2025-10-01_2026-10-01.csv | 105632 | 2025-09-29T05:20:00+00:00 | 2026-09-30T23:55:00+00:00 | `34d4849ad9927a36150379977de3019b8ca357a18a5277c690d078150cc2a73e` |
| ETHUSDT_15m_2025-10-01_2026-10-01.csv | 35552 | 2025-09-25T16:00:00+00:00 | 2026-09-30T23:45:00+00:00 | `4940961fdc85ba53081d73a08f9c9a0715ac9246ff1c21b44e40a1195c6e7952` |
| ETHUSDT_1h_2025-10-01_2026-10-01.csv | 9272 | 2025-09-09T16:00:00+00:00 | 2026-09-30T23:00:00+00:00 | `c50c83cebe7c13cfce48a85bde8a300257ce9aadcb1bede66c532959e4fd159f` |
| ETHUSDT_5m_2025-10-01_2026-10-01.csv | 105632 | 2025-09-29T05:20:00+00:00 | 2026-09-30T23:55:00+00:00 | `cdb5abdc72cbc51078b33055079c391c82739b0d5070ae760f311a5f784e5870` |

## Out-of-sample results

Naive last-price forecasts exactly zero return, so directional accuracy is reported as n/a; MAE is the meaningful comparison for that baseline.

Net sum is the arithmetic sum of per-position after-cost returns; compound is the sequential reuse-of-capital return across the sampled, non-overlapping origins.

| Series | Method | Direction accuracy | MAE(return) | Trades | Net sum | Compound |
|---|---|---:|---:|---:|---:|---:|
| BTCUSDT_5m | Kronos | 51.69% | 0.00116024 | 0 | 0.00% | 0.00% |
| BTCUSDT_5m | Naive last price | n/a | 0.00104136 | 0 | 0.00% | 0.00% |
| BTCUSDT_5m | Random walk + drift | 48.31% | 0.00104554 | 0 | 0.00% | 0.00% |
| BTCUSDT_5m | Momentum(12) | 43.82% | 0.00112429 | 0 | 0.00% | 0.00% |
| BTCUSDT_15m | Kronos | 37.08% | 0.00219844 | 6 | -0.91% | -0.91% |
| BTCUSDT_15m | Naive last price | n/a | 0.00180906 | 0 | 0.00% | 0.00% |
| BTCUSDT_15m | Random walk + drift | 48.31% | 0.00182327 | 0 | 0.00% | 0.00% |
| BTCUSDT_15m | Momentum(12) | 52.81% | 0.00183753 | 1 | -0.48% | -0.48% |
| BTCUSDT_1h | Kronos | 39.33% | 0.00413074 | 21 | -11.03% | -10.55% |
| BTCUSDT_1h | Naive last price | n/a | 0.00352756 | 0 | 0.00% | 0.00% |
| BTCUSDT_1h | Random walk + drift | 40.45% | 0.00356865 | 0 | 0.00% | 0.00% |
| BTCUSDT_1h | Momentum(12) | 43.82% | 0.00360387 | 3 | -0.16% | -0.17% |
| ETHUSDT_5m | Kronos | 53.93% | 0.0014218 | 3 | -1.13% | -1.12% |
| ETHUSDT_5m | Naive last price | n/a | 0.00127631 | 0 | 0.00% | 0.00% |
| ETHUSDT_5m | Random walk + drift | 61.80% | 0.0012751 | 0 | 0.00% | 0.00% |
| ETHUSDT_5m | Momentum(12) | 47.19% | 0.00139038 | 0 | 0.00% | 0.00% |
| ETHUSDT_15m | Kronos | 50.56% | 0.00251368 | 7 | -1.35% | -1.35% |
| ETHUSDT_15m | Naive last price | n/a | 0.00235036 | 0 | 0.00% | 0.00% |
| ETHUSDT_15m | Random walk + drift | 42.70% | 0.00236967 | 0 | 0.00% | 0.00% |
| ETHUSDT_15m | Momentum(12) | 52.81% | 0.00251404 | 4 | -1.70% | -1.69% |
| ETHUSDT_1h | Kronos | 58.43% | 0.0050901 | 38 | -4.62% | -4.61% |
| ETHUSDT_1h | Naive last price | n/a | 0.00448571 | 0 | 0.00% | 0.00% |
| ETHUSDT_1h | Random walk + drift | 41.57% | 0.00454148 | 0 | 0.00% | 0.00% |
| ETHUSDT_1h | Momentum(12) | 46.07% | 0.00476013 | 14 | -5.88% | -5.75% |

## Statistical significance versus baselines

Positive DM statistic favors Kronos. A series passes only when DM p<0.05 and the paired-bootstrap 95% CI for after-cost net advantage is strictly positive against every baseline, with Kronos itself net-positive.

| Series | Baseline | DM stat | DM p(one-sided) | Net advantage | 95% CI |
|---|---|---:|---:|---:|---:|
| BTCUSDT_5m | Naive last price | -1.8724 | 0.96942 | 0.00% | [0.00%, 0.00%] |
| BTCUSDT_5m | Random walk + drift | -1.7971 | 0.96384 | 0.00% | [0.00%, 0.00%] |
| BTCUSDT_5m | Momentum(12) | -0.48844 | 0.68738 | 0.00% | [0.00%, 0.00%] |
| BTCUSDT_15m | Naive last price | -2.7768 | 0.99726 | -0.01% | [-0.03%, 0.01%] |
| BTCUSDT_15m | Random walk + drift | -2.5605 | 0.99477 | -0.01% | [-0.03%, 0.01%] |
| BTCUSDT_15m | Momentum(12) | -1.9883 | 0.97661 | -0.00% | [-0.03%, 0.02%] |
| BTCUSDT_1h | Naive last price | -3.0718 | 0.99894 | -0.12% | [-0.24%, -0.03%] |
| BTCUSDT_1h | Random walk + drift | -2.8653 | 0.99792 | -0.12% | [-0.24%, -0.03%] |
| BTCUSDT_1h | Momentum(12) | -1.9441 | 0.97406 | -0.12% | [-0.24%, -0.02%] |
| ETHUSDT_5m | Naive last price | -1.4477 | 0.92616 | -0.01% | [-0.03%, 0.00%] |
| ETHUSDT_5m | Random walk + drift | -1.4426 | 0.92543 | -0.01% | [-0.03%, 0.00%] |
| ETHUSDT_5m | Momentum(12) | -0.2945 | 0.61581 | -0.01% | [-0.03%, 0.00%] |
| ETHUSDT_15m | Naive last price | -1.1851 | 0.88201 | -0.02% | [-0.04%, 0.00%] |
| ETHUSDT_15m | Random walk + drift | -1.0326 | 0.84911 | -0.02% | [-0.04%, 0.00%] |
| ETHUSDT_15m | Momentum(12) | 0.0020007 | 0.4992 | 0.00% | [-0.02%, 0.03%] |
| ETHUSDT_1h | Naive last price | -1.794 | 0.96359 | -0.05% | [-0.15%, 0.05%] |
| ETHUSDT_1h | Random walk + drift | -1.6432 | 0.94982 | -0.05% | [-0.15%, 0.05%] |
| ETHUSDT_1h | Momentum(12) | -0.77475 | 0.78076 | 0.01% | [-0.10%, 0.13%] |

## Gate by series

| Series | Positive after-cost net | DM beats all 3 | Bootstrap beats all 3 | PASS |
|---|---:|---:|---:|---:|
| BTCUSDT_5m | NO | NO | NO | **NO** |
| BTCUSDT_15m | NO | NO | NO | **NO** |
| BTCUSDT_1h | NO | NO | NO | **NO** |
| ETHUSDT_5m | NO | NO | NO | **NO** |
| ETHUSDT_15m | NO | NO | NO | **NO** |
| ETHUSDT_1h | NO | NO | NO | **NO** |

## Confidence calibration

Confidence is a dispersion-derived score from 20 stochastic samples, not a calibrated probability of being correct.


| Series | Confidence bin | N | Mean confidence | Direction accuracy |
|---|---|---:|---:|---:|
| BTCUSDT_5m | [0.0, 0.2] | 26 | 10.00% | 61.54% |
| BTCUSDT_5m | [0.2, 0.4] | 34 | 29.42% | 50.00% |
| BTCUSDT_5m | [0.4, 0.6] | 22 | 48.71% | 40.91% |
| BTCUSDT_5m | [0.6, 0.8] | 7 | 64.82% | 57.14% |
| BTCUSDT_15m | [0.0, 0.2] | 25 | 11.50% | 44.00% |
| BTCUSDT_15m | [0.2, 0.4] | 35 | 30.61% | 34.29% |
| BTCUSDT_15m | [0.4, 0.6] | 23 | 50.63% | 30.43% |
| BTCUSDT_15m | [0.6, 0.8] | 6 | 64.55% | 50.00% |
| BTCUSDT_1h | [0.0, 0.2] | 24 | 11.31% | 25.00% |
| BTCUSDT_1h | [0.2, 0.4] | 37 | 30.44% | 40.54% |
| BTCUSDT_1h | [0.4, 0.6] | 23 | 46.29% | 52.17% |
| BTCUSDT_1h | [0.6, 0.8] | 5 | 65.51% | 40.00% |
| ETHUSDT_5m | [0.0, 0.2] | 26 | 10.70% | 57.69% |
| ETHUSDT_5m | [0.2, 0.4] | 35 | 30.37% | 57.14% |
| ETHUSDT_5m | [0.4, 0.6] | 24 | 47.11% | 45.83% |
| ETHUSDT_5m | [0.6, 0.8] | 4 | 65.21% | 50.00% |
| ETHUSDT_15m | [0.0, 0.2] | 25 | 10.65% | 44.00% |
| ETHUSDT_15m | [0.2, 0.4] | 40 | 29.91% | 55.00% |
| ETHUSDT_15m | [0.4, 0.6] | 21 | 49.88% | 52.38% |
| ETHUSDT_15m | [0.6, 0.8] | 3 | 67.48% | 33.33% |
| ETHUSDT_1h | [0.0, 0.2] | 14 | 12.33% | 50.00% |
| ETHUSDT_1h | [0.2, 0.4] | 40 | 30.07% | 52.50% |
| ETHUSDT_1h | [0.4, 0.6] | 27 | 48.24% | 62.96% |
| ETHUSDT_1h | [0.6, 0.8] | 8 | 64.35% | 87.50% |

## Runtime on this MSI machine

- Hardware: Intel(R) Core(TM) i7-3630QM CPU @ 2.40GHz; 31.9 GB RAM; inference device: cpu.

- First load + 20-sample signal: **13.43s**.
- Steady-state 20-sample signal: **3.91s**.
- Shortest evaluated candle is 5m = 300s, so steady-state inference is 76.6x faster than the candle duration.
- Fast-path equivalence check vs upstream one-step path: mean difference 2.71e-08, std difference 4.74e-09, sorted max absolute sample difference 7.13e-08; speedup 3.18x.

## Integration action

**None.** Kronos remains research-only. Do not wire it into proposals, paper, testnet, or live paths unless a future, pre-registered offline evaluation clears the same gate.

## Limitations

- The final grid uses 89 deterministic origins per symbol/timeframe, not every candle. This was fixed before results were inspected to keep the CPU-only run tractable while covering the full year and rotating intraday phases.
- Slippage is modeled as a fixed 2 bps per side. Realized slippage depends on order size, market state, and execution style.
- The P/L proxy is symmetric long/short for signal comparison; it is not a claim that the research path itself can execute shorts on Binance Spot.
- Results apply to the pinned Kronos-small/tokenizer/source revisions only and must not be generalized to later weights or fine-tuned variants.
