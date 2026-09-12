"""Window metrics: P&L statistics per lot, daily Sharpe, drawdown, hit rates,
classification accuracy with a block bootstrap interval (blocks of 5 days).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

ACCURACY_THRESHOLD = 0.52


def sharpe(daily: pd.Series | np.ndarray) -> float:
    x = np.asarray(daily, dtype=np.float64)
    if x.size < 2:
        return 0.0
    sd = float(np.std(x, ddof=1))
    if sd <= 0:
        return 0.0
    return float(np.mean(x) / sd * math.sqrt(252.0))


def max_drawdown(daily: pd.Series | np.ndarray) -> float:
    x = np.asarray(daily, dtype=np.float64)
    if x.size == 0:
        return 0.0
    cum = np.cumsum(x)
    peak = np.maximum.accumulate(np.maximum(cum, 0.0))
    return float(np.min(cum - peak)) if cum.size else 0.0


def cumulative_curve(daily: pd.Series) -> tuple[list[str], list[float]]:
    if daily is None or len(daily) == 0:
        return [], []
    s = daily.sort_index()
    return [d.isoformat() for d in s.index], [float(v) for v in np.cumsum(s.to_numpy(dtype=np.float64))]


def block_bootstrap_accuracy(
    day_ids: np.ndarray,
    correct: np.ndarray,
    block_days: int = 5,
    n_boot: int = 1000,
    seed: int = 0,
    threshold: float = ACCURACY_THRESHOLD,
) -> dict:
    """Resample blocks of consecutive days with replacement; CI and one-sided p-value against `threshold`."""
    correct = np.asarray(correct, dtype=bool)
    day_ids = np.asarray(day_ids)
    if correct.size == 0:
        return {"accuracy": float("nan"), "ci": [float("nan"), float("nan")], "p_value": 1.0, "n": 0}
    days = sorted(set(day_ids.tolist()))
    acc = float(correct.mean())
    if len(days) < 2:
        return {"accuracy": acc, "ci": [acc, acc], "p_value": 1.0 if acc <= threshold else 0.0, "n": int(correct.size)}
    hits = np.array([correct[day_ids == d].sum() for d in days], dtype=np.float64)
    counts = np.array([(day_ids == d).sum() for d in days], dtype=np.float64)
    n_days = len(days)
    block = max(1, min(block_days, n_days // 2))  # shorter blocks for tiny windows so replicates vary
    n_blocks = int(math.ceil(n_days / block))
    starts = np.arange(0, n_days - block + 1)
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    for b in range(n_boot):
        chosen = rng.choice(starts, size=n_blocks, replace=True)
        idx = np.concatenate([np.arange(s, s + block) for s in chosen])[:n_days]
        h = hits[idx].sum()
        c = counts[idx].sum()
        boot[b] = h / c if c > 0 else np.nan
    boot = boot[np.isfinite(boot)]
    lo, hi = (float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))) if boot.size else (acc, acc)
    p = float(np.mean(boot <= threshold)) if boot.size else 1.0
    return {"accuracy": acc, "ci": [lo, hi], "p_value": p, "n": int(correct.size)}


def _mean_or_none(values) -> float | None:
    vals = [float(v) for v in values if v is not None and np.isfinite(v)]
    return float(np.mean(vals)) if vals else None


def trade_stats(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "stop_hits": 0, "stop_hits_leg": 0, "target_hits": 0, "lock_hits": 0,
            "exit_hits": 0, "square_offs": 0, "win_rate": 0.0, "avg_holding_minutes": 0.0,
            "gross_pnl_per_lot": 0.0, "costs_per_lot": 0.0, "avg_credit_points": 0.0,
            "mean_leg_stop_pct": None, "mean_combined_stop_pct": None, "mean_expected_move_points": None,
        }
    reasons = [t.exit_reason for t in trades]
    lots = max(1, int(getattr(trades[0], "lots", 1)))
    return {
        "trades": n,
        "stop_hits": sum(r == "STOP" for r in reasons),
        "stop_hits_leg": sum(1 for t in trades if t.leg_stops > 0),
        "target_hits": sum(r == "TARGET" or (t.call_reason == "TARGET" or t.put_reason == "TARGET") for r, t in zip(reasons, trades, strict=False)),
        "lock_hits": sum(r == "LOCK" for r in reasons),
        "exit_hits": sum(r == "EXIT" or t.call_reason == "EXIT" or t.put_reason == "EXIT" for r, t in zip(reasons, trades, strict=False)),
        "square_offs": sum(r == "SQUARE_OFF" or t.call_reason == "SQUARE_OFF" or t.put_reason == "SQUARE_OFF" for r, t in zip(reasons, trades, strict=False)),
        "win_rate": float(np.mean([t.net_inr > 0 for t in trades])),
        "avg_holding_minutes": float(np.mean([t.minutes_held for t in trades])),
        "gross_pnl_per_lot": float(sum(t.gross_inr for t in trades) / lots),
        "costs_per_lot": float(sum(t.cost_inr for t in trades) / lots),
        "avg_credit_points": float(np.mean([t.credit for t in trades])),
        "mean_leg_stop_pct": float(np.mean([(t.leg_stop_pct_ce + t.leg_stop_pct_pe) / 2.0 for t in trades])),
        "mean_combined_stop_pct": float(np.mean([t.combined_stop_pct for t in trades])),
        "mean_expected_move_points": _mean_or_none(
            [t.stop_basis.get("expected_move_points") for t in trades if isinstance(t.stop_basis, dict)]
        ),
    }


def window_metrics(
    result,
    lots: int = 1,
    correct: np.ndarray | None = None,
    day_ids: np.ndarray | None = None,
    ridge_correct: np.ndarray | None = None,
    seed: int = 0,
    n_observations: int | None = None,
    labels: np.ndarray | None = None,
) -> dict:
    """Metrics for one simulated window (a simulator.WindowResult).

    `correct` marks observations whose above-or-below-1 classification was
    right and `labels` the true labels; the bootstrap p-value is taken
    against max(0.52, majority-class rate) so an imbalanced label cannot pass
    by predicting the majority class alone.
    """
    daily = result.daily_pnl.sort_index() if len(result.daily_pnl) else pd.Series(dtype="float64")
    lots = max(1, int(lots))
    per_lot = daily / lots
    out = {
        "net_pnl_per_lot": float(per_lot.sum()) if len(per_lot) else 0.0,
        "sharpe": sharpe(per_lot),
        "max_drawdown": max_drawdown(per_lot),
        "days": int(len(result.days)),
        "synthetic_fraction": float(result.synthetic_fraction),
        "n_observations": int(n_observations) if n_observations is not None else None,
    }
    out.update(trade_stats(result.trades))
    if correct is not None and len(correct):
        threshold = ACCURACY_THRESHOLD
        if labels is not None and len(labels):
            base = float(np.mean(np.asarray(labels, dtype=bool)))
            majority = max(base, 1.0 - base)
            threshold = max(ACCURACY_THRESHOLD, majority)
            lab = np.asarray(labels, dtype=bool)
            cor = np.asarray(correct, dtype=bool)
            pos = lab.sum()
            neg = (~lab).sum()
            tpr = float(cor[lab].mean()) if pos else float("nan")
            tnr = float(cor[~lab].mean()) if neg else float("nan")
            out["base_rate_above_1"] = base
            out["balanced_accuracy"] = float(np.nanmean([tpr, tnr]))
        boot = block_bootstrap_accuracy(day_ids, correct, seed=seed, threshold=threshold)
        out["accuracy"] = boot["accuracy"]
        out["accuracy_ci"] = boot["ci"]
        out["accuracy_p_value"] = boot["p_value"]
        out["accuracy_threshold"] = threshold
        out["accuracy_n"] = boot["n"]
    else:
        out["accuracy"] = None
        out["accuracy_ci"] = None
        out["accuracy_p_value"] = None
        out["accuracy_threshold"] = ACCURACY_THRESHOLD
        out["accuracy_n"] = 0
    if ridge_correct is not None and len(ridge_correct):
        out["ridge_accuracy"] = float(np.mean(ridge_correct))
    return out
