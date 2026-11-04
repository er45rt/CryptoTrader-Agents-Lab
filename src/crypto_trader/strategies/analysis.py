"""Result analysis for the UI (D30): monthly returns, drawdown series, and a robustness verdict.

The verdict answers "is this profit real or luck?" with checks that need no extra data:
  1. beats buy-and-hold on Sharpe (same costs)
  2. positive net return in both halves of the period
  3. still positive when fees are doubled (re-run of the fast preview)
  4. out-of-sample: positive in the last 30% after being judged on the first 70%
  5. Deflated Sharpe Ratio >= 0.95 given how many experiments have been run (the experiment log),
     using the engine's validation.dsr normal_cdf / expected_max_normal.
Verdict: "promising" only if all five hold; otherwise "unproven" with the failing checks listed.
A verdict is research evidence about the past, never a promise of profit.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Callable, Sequence

from crypto_trader.validation.dsr import expected_max_normal, normal_cdf

__all__ = ["monthly_returns", "drawdown_series", "robustness_verdict", "downsample_bars", "dsr_probability"]


def monthly_returns(equity: Sequence[tuple[str, float]]) -> list[dict[str, Any]]:
    months: "OrderedDict[str, tuple[float, float]]" = OrderedDict()
    prev = None
    for stamp, value in equity:
        key = stamp[:7]
        start = months[key][0] if key in months else (prev if prev is not None else value)
        months[key] = (start, value)
        prev = value
    return [{"month": k, "return": (v[1] / v[0] - 1) if v[0] else 0.0} for k, v in months.items()]


def drawdown_series(equity: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
    peak = -math.inf
    out = []
    for stamp, value in equity:
        peak = max(peak, value)
        out.append((stamp, value / peak - 1 if peak > 0 else 0.0))
    return out


def _daily_like_returns(equity: Sequence[tuple[str, float]]) -> list[float]:
    vals = [v for _, v in equity]
    return [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]


def dsr_probability(returns: Sequence[float], n_trials: int) -> float | None:
    """Bailey & Lopez de Prado DSR on per-bar returns; None if undefined."""
    n = len(returns)
    if n < 30 or n_trials < 1:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return None
    sd = math.sqrt(var)
    sr = mean / sd
    m3 = sum((r - mean) ** 3 for r in returns) / n / sd ** 3
    m4 = sum((r - mean) ** 4 for r in returns) / n / sd ** 4
    # expected max of n_trials standard normals scales the benchmark Sharpe by the per-bar Sharpe spread
    emax, _ = expected_max_normal(max(1, n_trials))
    sr0 = float(emax) * math.sqrt(1.0 / (n - 1)) if n_trials > 1 else 0.0
    denom = 1 - m3 * sr + (m4 - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(normal_cdf(Decimal(str(z))))


def robustness_verdict(result: Any, *, rerun_with_fees: Callable[[Decimal], Any] | None, n_trials: int) -> dict[str, Any]:
    m, b = result.metrics, result.benchmark
    eq = result.equity_curve
    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, ok: bool | None, detail: str) -> None:
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail})

    s, bs = m.get("sharpe"), b.get("sharpe")
    add("beats_hold", "يتفوق على الاحتفاظ (Sharpe)", None if s is None or bs is None else s >= bs,
        f"{(s or 0):.2f} مقابل {(bs or 0):.2f}")
    half = len(eq) // 2
    if half >= 2:
        h1 = eq[half - 1][1] / eq[0][1] - 1
        h2 = eq[-1][1] / eq[half - 1][1] - 1
        add("both_halves", "موجب في النصفين", h1 > 0 and h2 > 0, f"{h1:.1%} / {h2:.1%}")
    else:
        add("both_halves", "موجب في النصفين", None, "بيانات قليلة")
    if rerun_with_fees is not None:
        doubled = rerun_with_fees(Decimal(str(result.settings.get("taker_fee", "0.001"))) * 2)
        add("fees_x2", "يصمد لو تضاعفت الرسوم", doubled.metrics["net_return"] > 0,
            f"العائد برسوم مضاعفة {doubled.metrics['net_return']:.1%}")
    cut = int(len(eq) * 0.7)
    if 2 <= cut < len(eq) - 2:
        ins = eq[cut - 1][1] / eq[0][1] - 1
        oos = eq[-1][1] / eq[cut - 1][1] - 1
        add("out_of_sample", "ينجح على آخر 30% (خارج العينة)", ins > 0 and oos > 0, f"داخل {ins:.1%} / خارج {oos:.1%}")
    else:
        add("out_of_sample", "ينجح على آخر 30% (خارج العينة)", None, "بيانات قليلة")
    p = dsr_probability(_daily_like_returns(eq), n_trials)
    add("dsr", f"DSR ≥ 0.95 بعد {n_trials} تجربة", None if p is None else p >= 0.95,
        "غير معرّف" if p is None else f"{p:.2f}")
    decided = [c for c in checks if c["ok"] is not None]
    verdict = "promising" if decided and len(decided) == len(checks) and all(c["ok"] for c in checks) else "unproven"
    return {"verdict": verdict, "label": "واعدة" if verdict == "promising" else "غير مثبتة", "checks": checks,
            "note": "حكم على الماضي فقط، وليس وعدًا بالربح."}


def downsample_bars(bars: Sequence[Any], limit: int = 1500) -> list[dict[str, Any]]:
    """OHLC for the chart; long series are aggregated so the browser stays responsive."""
    step = max(1, math.ceil(len(bars) / limit))
    out = []
    for i in range(0, len(bars), step):
        chunk = bars[i:i + step]
        out.append({"t": chunk[0].open_time.isoformat(), "o": float(chunk[0].open), "h": float(max(c.high for c in chunk)),
                    "l": float(min(c.low for c in chunk)), "c": float(chunk[-1].close)})
    return out
