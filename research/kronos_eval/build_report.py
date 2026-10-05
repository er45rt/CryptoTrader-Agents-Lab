from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
BASELINES = ("naive_last_price", "random_walk_drift", "momentum")
METHODS = ("kronos",) + BASELINES
SERIES_ORDER = (
    "BTCUSDT_5m",
    "BTCUSDT_15m",
    "BTCUSDT_1h",
    "ETHUSDT_5m",
    "ETHUSDT_15m",
    "ETHUSDT_1h",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{100.0 * float(value):.{digits}f}%"


def num(value: float | None, digits: int = 6) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):.{digits}g}"


def compound_return(rows: pd.DataFrame, method: str) -> float:
    net = rows[f"{method}_net_return"].to_numpy(dtype=float)
    if np.any(net <= -1.0):
        return float("nan")
    return float(np.prod(1.0 + net) - 1.0)


def series_gate(summary: dict[str, Any]) -> tuple[bool, dict[str, bool]]:
    kronos = summary["metrics"]["kronos"]
    positive_net = float(kronos["strategy_net_return_sum"]) > 0.0

    dm_ok = True
    bootstrap_ok = True
    for baseline in BASELINES:
        dm = summary["diebold_mariano_vs_baselines"].get(baseline, {})
        stat = dm.get("statistic")
        p_value = dm.get("p_value_one_sided")
        if stat is None or p_value is None or float(stat) <= 0.0 or float(p_value) >= 0.05:
            dm_ok = False

        boot = summary["bootstrap_net_advantage_vs_baselines"].get(baseline, {})
        low = boot.get("ci95_low")
        if low is None or float(low) <= 0.0:
            bootstrap_ok = False

    checks = {
        "positive_after_cost_net": positive_net,
        "dm_vs_every_baseline": dm_ok,
        "bootstrap_net_vs_every_baseline": bootstrap_ok,
    }
    return all(checks.values()), checks


def main() -> None:
    suite_path = RESULTS / "suite_summary.json"
    if not suite_path.is_file():
        raise FileNotFoundError("final suite_summary.json not found; backtest is not complete")

    suite = load_json(suite_path)
    if not suite.get("completed"):
        raise RuntimeError("suite summary is not marked completed")

    pins = load_json(ROOT / "pins.json")
    benchmark = load_json(ROOT / "benchmark.json")
    hardware = load_json(ROOT / "hardware.json")
    fast = load_json(ROOT / "fast_path_validation.json")
    data_manifest = load_json(ROOT / "data_manifest.json")

    series = suite["series"]
    gate_results: dict[str, bool] = {}
    gate_checks: dict[str, dict[str, bool]] = {}
    compounds: dict[str, dict[str, float]] = {}

    for key in SERIES_ORDER:
        summary = series[key]
        passed, checks = series_gate(summary)
        gate_results[key] = passed
        gate_checks[key] = checks
        rows = pd.read_csv(RESULTS / f"{key}_walkforward_rows.csv")
        compounds[key] = {method: compound_return(rows, method) for method in METHODS}

    replicated_timeframes: list[str] = []
    for interval in ("5m", "15m", "1h"):
        if gate_results[f"BTCUSDT_{interval}"] and gate_results[f"ETHUSDT_{interval}"]:
            replicated_timeframes.append(interval)
    global_pass = bool(replicated_timeframes)

    out: list[str] = []
    out.append("# Kronos Offline Signal Evaluation")
    out.append("")
    out.append("## Decision")
    out.append("")
    if global_pass:
        out.append(
            "**YES — limited proposals-only integration is eligible**, restricted to the "
            f"replicated passing timeframe(s): {', '.join(replicated_timeframes)}."
        )
    else:
        out.append(
            "**NO — do not integrate Kronos into CryptoTrader-Agents-Lab.** "
            "The pre-committed statistical/after-cost gate was not replicated on both "
            "BTCUSDT and ETHUSDT for any timeframe."
        )
    out.append("")
    out.append(
        "This decision uses only one-candle-ahead walk-forward forecasts whose target "
        "candles were unavailable to the model input. No paper, testnet, or live trading "
        "was used."
    )
    out.append("")

    out.append("## Reproducibility pins")
    out.append("")
    out.append("| Artifact | Exact revision | Revision time (UTC) | SHA-256 |")
    out.append("|---|---|---:|---|")
    out.append(
        f"| Kronos source | `{pins['code_commit']}` | {pins['code_commit_date_utc']} | n/a |"
    )
    out.append(
        f"| Kronos-small | `{pins['model_revision']}` | {pins['model_revision_date_utc']} | "
        f"`{pins['model_safetensors_sha256']}` |"
    )
    out.append(
        f"| Kronos-Tokenizer-base | `{pins['tokenizer_revision']}` | "
        f"{pins['tokenizer_revision_date_utc']} | `{pins['tokenizer_safetensors_sha256']}` |"
    )
    out.append("")
    out.append(
        "All three pinned revisions pre-date the OOS target start (2025-10-01), "
        "which prevents the evaluated market period from being introduced by a later "
        "model/source revision."
    )
    out.append("")

    out.append("## Evaluation design")
    out.append("")
    out.append(
        f"- OOS target: {suite['oos_target_start']} to {suite['oos_target_end_exclusive']} (end exclusive)."
    )
    out.append(
        f"- Markets: BTCUSDT and ETHUSDT; frames: 5m, 15m, 1h; "
        f"{suite['target_evaluation_points_per_series']} deterministic walk-forward origins per series."
    )
    out.append(
        f"- Context: {suite['context_size']} candles; horizon: one candle; "
        f"stochastic samples per signal: {suite['sample_count']}."
    )
    out.append(
        f"- Friction: {pct(suite['fee_per_side'])} fee per side + "
        f"{pct(suite['slippage_per_side'])} slippage per side = "
        f"{pct(suite['round_trip_cost'])} round trip."
    )
    out.append(
        "- Baselines: last-price/persistence (zero return), random walk with drift, "
        "and 12-candle simple momentum."
    )
    out.append(
        "- Statistical tests: one-sided Diebold-Mariano on absolute return error; "
        "paired 5,000-resample bootstrap on after-cost net-return advantage."
    )
    out.append("")

    out.append("## Dataset integrity")
    out.append("")
    out.append("| Snapshot | Rows | First candle | Last candle | SHA-256 |")
    out.append("|---|---:|---|---|---|")
    for name, item in data_manifest["files"].items():
        out.append(
            f"| {name} | {item['rows']} | {item['start']} | {item['end']} | "
            f"`{item['sha256']}` |"
        )
    out.append("")

    out.append("## Out-of-sample results")
    out.append("")
    out.append("Naive last-price forecasts exactly zero return, so directional accuracy is reported as n/a; MAE is the meaningful comparison for that baseline.")
    out.append("")
    out.append(
        "Net sum is the arithmetic sum of per-position after-cost returns; compound is "
        "the sequential reuse-of-capital return across the sampled, non-overlapping origins."
    )
    out.append("")
    out.append("| Series | Method | Direction accuracy | MAE(return) | Trades | Net sum | Compound |")
    out.append("|---|---|---:|---:|---:|---:|---:|")
    method_label = {
        "kronos": "Kronos",
        "naive_last_price": "Naive last price",
        "random_walk_drift": "Random walk + drift",
        "momentum": "Momentum(12)",
    }
    for key in SERIES_ORDER:
        summary = series[key]
        for method in METHODS:
            metrics = summary["metrics"][method]
            direction = "n/a" if method == "naive_last_price" else pct(metrics["direction_accuracy"])
            out.append(
                f"| {key} | {method_label[method]} | {direction} | "
                f"{num(metrics['mae_return'])} | {metrics['trades']} | "
                f"{pct(metrics['strategy_net_return_sum'])} | {pct(compounds[key][method])} |"
            )
    out.append("")

    out.append("## Statistical significance versus baselines")
    out.append("")
    out.append(
        "Positive DM statistic favors Kronos. A series passes only when DM p<0.05 and "
        "the paired-bootstrap 95% CI for after-cost net advantage is strictly positive "
        "against every baseline, with Kronos itself net-positive."
    )
    out.append("")
    out.append("| Series | Baseline | DM stat | DM p(one-sided) | Net advantage | 95% CI |")
    out.append("|---|---|---:|---:|---:|---:|")
    for key in SERIES_ORDER:
        summary = series[key]
        for baseline in BASELINES:
            dm = summary["diebold_mariano_vs_baselines"][baseline]
            boot = summary["bootstrap_net_advantage_vs_baselines"][baseline]
            out.append(
                f"| {key} | {method_label[baseline]} | {num(dm.get('statistic'), 5)} | "
                f"{num(dm.get('p_value_one_sided'), 5)} | "
                f"{pct(boot.get('mean_advantage'))} | "
                f"[{pct(boot.get('ci95_low'))}, {pct(boot.get('ci95_high'))}] |"
            )
    out.append("")

    out.append("## Gate by series")
    out.append("")
    out.append("| Series | Positive after-cost net | DM beats all 3 | Bootstrap beats all 3 | PASS |")
    out.append("|---|---:|---:|---:|---:|")
    for key in SERIES_ORDER:
        checks = gate_checks[key]
        out.append(
            f"| {key} | {'YES' if checks['positive_after_cost_net'] else 'NO'} | "
            f"{'YES' if checks['dm_vs_every_baseline'] else 'NO'} | "
            f"{'YES' if checks['bootstrap_net_vs_every_baseline'] else 'NO'} | "
            f"**{'YES' if gate_results[key] else 'NO'}** |"
        )
    out.append("")

    out.append("## Confidence calibration")
    out.append("")
    out.append("Confidence is a dispersion-derived score from 20 stochastic samples, not a calibrated probability of being correct.")
    out.append("")
    out.append("")
    out.append("| Series | Confidence bin | N | Mean confidence | Direction accuracy |")
    out.append("|---|---|---:|---:|---:|")
    for key in SERIES_ORDER:
        for bucket in series[key]["confidence_calibration"]:
            out.append(
                f"| {key} | [{bucket['confidence_min']:.1f}, {bucket['confidence_max']:.1f}] | "
                f"{bucket['count']} | {pct(bucket['mean_confidence'])} | "
                f"{pct(bucket['direction_accuracy'])} |"
            )
    out.append("")

    out.append("## Runtime on this MSI machine")
    out.append("")
    out.append(
        f"- Hardware: {hardware['cpu']}; {hardware['ram_gb']} GB RAM; "
        f"inference device: {hardware['inference_device']}."
    )
    out.append("")
    out.append(
        f"- First load + 20-sample signal: **{benchmark['load_plus_inference_seconds']:.2f}s**."
    )
    out.append(
        f"- Steady-state 20-sample signal: **{benchmark['steady_state_inference_seconds']:.2f}s**."
    )
    out.append(
        "- Shortest evaluated candle is 5m = 300s, so steady-state inference is "
        f"{300.0 / benchmark['steady_state_inference_seconds']:.1f}x faster than the candle duration."
    )
    out.append(
        f"- Fast-path equivalence check vs upstream one-step path: mean difference "
        f"{abs(fast['fast_mean'] - fast['reference_mean']):.3g}, "
        f"std difference {abs(fast['fast_std'] - fast['reference_std']):.3g}, "
        f"sorted max absolute sample difference {fast['sorted_max_abs_diff']:.3g}; "
        f"speedup {fast['speedup']:.2f}x."
    )
    out.append("")

    if global_pass:
        out.append("## Eligible integration plan")
        out.append("")
        out.append(
            "Kronos must remain an optional input to the proposal layer only. It must not "
            "bypass or modify Hard Risk Engine, OMS, Execution, Ledger, or Reconciliation."
        )
        out.append(
            "Start with a capped contribution of at most 10% of proposal score on only the "
            "replicated passing timeframe(s). Auto-disable it if a rolling 100-signal window "
            "loses positive after-cost edge, the paired-bootstrap lower bound versus the "
            "approved baseline falls to <=0, no-signal/error rate exceeds 5%, or p95 inference "
            "latency exceeds 25% of candle duration. Re-enable only after a fresh offline gate."
        )
        out.append("")
    else:
        out.append("## Integration action")
        out.append("")
        out.append(
            "**None.** Kronos remains research-only. Do not wire it into proposals, paper, "
            "testnet, or live paths unless a future, pre-registered offline evaluation clears "
            "the same gate."
        )
        out.append("")

    out.append("## Limitations")
    out.append("")
    out.append(
        "- The final grid uses 89 deterministic origins per symbol/timeframe, not every candle. "
        "This was fixed before results were inspected to keep the CPU-only run tractable while "
        "covering the full year and rotating intraday phases."
    )
    out.append(
        "- Slippage is modeled as a fixed 2 bps per side. Realized slippage depends on order size, "
        "market state, and execution style."
    )
    out.append(
        "- The P/L proxy is symmetric long/short for signal comparison; it is not a claim that "
        "the research path itself can execute shorts on Binance Spot."
    )
    out.append(
        "- Results apply to the pinned Kronos-small/tokenizer/source revisions only and must not "
        "be generalized to later weights or fine-tuned variants."
    )
    out.append("")

    (ROOT / "REPORT.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"decision={'YES' if global_pass else 'NO'}")
    print(f"passing_series={[k for k, v in gate_results.items() if v]}")
    print(f"replicated_timeframes={replicated_timeframes}")


if __name__ == "__main__":
    main()
