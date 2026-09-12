"""Kernel benchmark: wall seconds per 100 ms of neural time under fixed fields."""

from __future__ import annotations

from typing import Any

import numpy as np

from openfly.interfaces import Stimulus
from openfly.neural.brain import Brain

STIMULI = (("dark", 0.0), ("mid-grey", 0.5), ("white", 1.0))
REPORT_POPULATIONS = ("R1-R6", "R8", "lamina", "KC", "MBON", "DN", "central_complex", "random2000")

OBSERVATIONS_PER_PASS = 21_000
BUDGET_HOURS = 6.0
OBSERVATIONS_PER_DAY_1M = 375  # one observation per 1 minute bar, 09:15 to 15:30
DEFAULT_WARMUP_MS = 500.0  # activity keeps building for about 500 ms after onset


def uniform_stimulus(brain: Brain, luminance: float, pulses: tuple = ()) -> Stimulus:
    return Stimulus(
        r16=np.full(len(brain.r16), luminance, dtype=np.float64),
        r8=np.full(len(brain.r8), luminance, dtype=np.float64),
        pulses=pulses,
    )


def benchmark(
    brain: Brain, neural_ms: float = 500.0, warmup_ms: float = DEFAULT_WARMUP_MS
) -> list[dict[str, Any]]:
    """Run each uniform field from a fresh state; warm-up is untimed."""
    rows = []
    for name, luminance in STIMULI:
        brain.reset()
        stim = uniform_stimulus(brain, luminance)
        if warmup_ms > 0:
            brain.observe(stim, warmup_ms)
        res = brain.observe(stim, neural_ms)
        counts = res.counts
        seconds = res.compute_seconds
        rows.append(
            {
                "stimulus": name,
                "luminance": luminance,
                "neural_ms": res.neural_ms,
                "compute_seconds": seconds,
                "seconds_per_100ms": seconds / (res.neural_ms / 100.0),
                "spikes": int(counts.sum()),
                "spikes_per_second": float(counts.sum() / (res.neural_ms / 1000.0)),
                "neurons_spiking": int((counts > 0).sum()),
                "active_at_end": brain.active_count,
                "rates_hz": {
                    p: r
                    for p, r in brain.population_rates(counts, res.neural_ms).items()
                    if p in REPORT_POPULATIONS
                },
            }
        )
    return rows


def recommend_neural_ms(
    rows: list[dict[str, Any]],
    observations: int = OBSERVATIONS_PER_PASS,
    hours: float = BUDGET_HOURS,
) -> dict[str, float]:
    """Largest neural_ms per bar such that `observations` finish within `hours`."""
    worst = max(r["seconds_per_100ms"] for r in rows)
    budget_per_obs = hours * 3600.0 / observations
    raw = 100.0 * budget_per_obs / worst if worst > 0 else float("inf")
    rounded = float(int(raw // 10) * 10)
    recommended = max(10.0, min(rounded, 1000.0))
    per_100 = worst / 100.0
    return {
        "worst_seconds_per_100ms": worst,
        "budget_seconds_per_observation": budget_per_obs,
        "max_neural_ms": raw,
        "recommended_neural_ms": recommended,
        "seconds_per_observation_at_recommended": per_100 * recommended,
        "hours_per_pass_at_recommended": per_100 * recommended * observations / 3600.0,
        "seconds_per_1m_day_at_recommended": per_100 * recommended * OBSERVATIONS_PER_DAY_1M,
        "seconds_per_observation_at_200ms": per_100 * 200.0,
        "hours_per_pass_at_200ms": per_100 * 200.0 * observations / 3600.0,
        "seconds_per_1m_day_at_200ms": per_100 * 200.0 * OBSERVATIONS_PER_DAY_1M,
    }


def format_rows(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{'stimulus':10} {'neural ms':>9} {'wall s':>8} {'s/100ms':>8} {'spikes/s':>11} {'spiking':>8} {'active':>8}"
    ]
    for r in rows:
        lines.append(
            f"{r['stimulus']:10} {r['neural_ms']:9.1f} {r['compute_seconds']:8.3f} {r['seconds_per_100ms']:8.3f} "
            f"{r['spikes_per_second']:11,.0f} {r['neurons_spiking']:8,} {r['active_at_end']:8,}"
        )
    lines.append("Mean rates (Hz):")
    for r in rows:
        rates = ", ".join(f"{p} {v:.1f}" for p, v in r["rates_hz"].items())
        lines.append(f"  {r['stimulus']}: {rates}")
    return "\n".join(lines)
