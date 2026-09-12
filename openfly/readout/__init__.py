"""Readouts: spike counts to a trading decision.

    from openfly.readout import make_readout, load_readout
    readout = make_readout("reservoir", settings, brain)   # or "fixed"
    prediction = readout.predict(counts, brain, observation)

Both readouts accept either full-length count vectors (one entry per neuron of
the brain) or, after `bind_columns(neuron_ids)`, the column subset stored by
the feature cache.
"""

from __future__ import annotations

from pathlib import Path

from openfly.readout.fixed import FixedDecoder
from openfly.readout.reservoir import DEFAULT_POPULATIONS, ReservoirReadout

READOUT_NAMES = ("fixed", "reservoir")


def make_readout(name: str, settings: dict | None = None, brain=None):
    """Build a readout by name from the merged settings dict.

    settings["neural"] keys used: neural_ms, tau, horizon_minutes, and the
    optional populations (list of population names) and ridge_alpha.
    """
    neural = (settings or {}).get("neural", {}) if isinstance(settings, dict) else {}
    neural_ms = float(neural.get("neural_ms", 200.0))
    key = str(name).strip().lower()
    if key == "fixed":
        return FixedDecoder(neural_ms=neural_ms, threshold_hz=float(neural.get("fixed_threshold_hz", 2.0)))
    if key == "reservoir":
        return ReservoirReadout(
            populations=tuple(neural.get("populations", DEFAULT_POPULATIONS)),
            alpha=float(neural.get("ridge_alpha", 10.0)),
            tau=float(neural.get("tau", 0.1)),
            horizon_minutes=int(neural.get("horizon_minutes", 60)),
            neural_ms=neural_ms,
            brain=brain,
        )
    raise ValueError(f"unknown readout {name!r}; use one of {READOUT_NAMES}")


def load_readout(directory: str | Path, brain=None):
    """Load a readout saved with `save(dir)`; only the reservoir readout has state."""
    directory = Path(directory)
    meta = directory / "readout.json"
    if not meta.exists():
        raise FileNotFoundError(f"no readout.json under {directory}")
    import json

    kind = json.loads(meta.read_text(encoding="utf-8")).get("name", "reservoir")
    if kind == "fixed":
        return FixedDecoder.load(directory)
    return ReservoirReadout.load(directory, brain=brain)


__all__ = [
    "DEFAULT_POPULATIONS",
    "READOUT_NAMES",
    "FixedDecoder",
    "ReservoirReadout",
    "load_readout",
    "make_readout",
]
