"""Feature cache: simulate once, fit many.

One directory per (encoder hash, neural ms, interval[, plastic]) under
PATHS.features, one parquet file per trading day holding, per observation:
timestamp (bar close), the spike counts of the cached populations (int32,
fixed-size list; the neuron ids of the columns are in meta.json), compute
seconds, the stimulus hash, cumulative simulated ms and the mean rate in Hz
of every brain population.

A pass over a window runs brain.observe for every observation of every day
not yet cached (after one discarded warm-up observation at 09:15) and writes
the day; a later run skips cached days, so an interrupted pass resumes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from openfly.config import PATHS, Paths
from openfly.experiments.observations import ObservationBuilder
from openfly.sensory.encoders import stimulus_hash

DEFAULT_CACHE_POPULATIONS = (
    "DN",
    "MBON",
    "random2000",
    "MBON07",
    "MBON11",
    "PAM11",
    "PPL101",
    "DNp20_L",
    "DNp20_R",
    "DNpe017",
)


@dataclass
class FeatureSet:
    timestamps: pd.Series  # tz-aware bar close times
    dates: np.ndarray  # python dates per row
    counts: np.ndarray  # int32 (n_obs, n_columns)
    extra: pd.DataFrame  # compute_seconds, stimulus_hash, sim_ms, rate_* columns
    columns: np.ndarray  # neuron ids per column

    def __len__(self) -> int:
        return int(self.counts.shape[0])


def cache_key(encoder_hash: str, neural_ms: float, interval: str, plastic: bool = False, fake: bool = False) -> str:
    key = f"{encoder_hash[:16]}_{int(round(float(neural_ms)))}_{interval}"
    if plastic:
        key += "_plastic"
    if fake:
        key += "_fake"
    return key


class FeatureCache:
    def __init__(
        self,
        encoder,
        neural_ms: float,
        brain,
        populations=DEFAULT_CACHE_POPULATIONS,
        interval: str = "1m",
        plastic: bool = False,
        root: str | Path | None = None,
        paths: Paths = PATHS,
    ):
        self.encoder = encoder
        self.neural_ms = float(neural_ms)
        self.brain = brain
        self.interval = str(interval)
        self.plastic = bool(plastic)
        fake = _safe_provenance(brain).get("kind") == "FakeBrain"
        self.key = cache_key(encoder.config_hash(), self.neural_ms, self.interval, self.plastic, fake)
        self.dir = Path(root) / self.key if root else paths.features / self.key
        self.dir.mkdir(parents=True, exist_ok=True)
        present = [p for p in populations if p in brain.populations]
        self.populations = tuple(present)
        ids = [np.asarray(brain.populations[p], dtype=np.int64) for p in present]
        self.columns = np.unique(np.concatenate(ids)) if ids else np.zeros(0, dtype=np.int64)
        self.rate_populations = tuple(sorted(brain.populations))
        self.meta_path = self.dir / "meta.json"
        self._write_or_check_meta()

    # -- metadata ------------------------------------------------------------

    def _meta(self) -> dict:
        provenance = {}
        try:
            provenance = self.brain.provenance() or {}
        except Exception:
            provenance = {}
        return {
            "encoder": self.encoder.name,
            "encoder_hash": self.encoder.config_hash(),
            "encoder_params": getattr(self.encoder, "params", lambda: {})(),
            "neural_ms": self.neural_ms,
            "interval": self.interval,
            "plastic": self.plastic,
            "populations": list(self.populations),
            "columns": [int(c) for c in self.columns],
            "population_columns": {
                p: [int(c) for c in np.asarray(self.brain.populations[p], dtype=np.int64)] for p in self.populations
            },
            "rate_populations": list(self.rate_populations),
            "brain": _jsonable(provenance),
            "version": 1,
        }

    def _write_or_check_meta(self) -> None:
        if self.meta_path.exists():
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            stored = np.asarray(meta.get("columns", []), dtype=np.int64)
            if stored.shape != self.columns.shape or not np.array_equal(stored, self.columns):
                raise RuntimeError(
                    f"feature cache {self.dir} was written with different columns; "
                    "choose other cache populations or remove the directory"
                )
            return
        self.meta_path.write_text(json.dumps(self._meta(), indent=1), encoding="utf-8")

    def path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.parquet"

    def has(self, d: date) -> bool:
        return self.path(d).exists()

    def cached_dates(self) -> list[date]:
        out = []
        for p in self.dir.glob("*.parquet"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)

    # -- io ------------------------------------------------------------------

    def write_day(
        self,
        d: date,
        timestamps,
        counts: np.ndarray,
        compute_seconds,
        stimulus_hashes,
        sim_ms,
        rates: dict[str, np.ndarray],
    ) -> Path:
        counts = np.ascontiguousarray(counts, dtype=np.int32)
        n, width = counts.shape
        ts = pd.to_datetime(pd.Series(list(timestamps)))
        arrays = {
            "timestamp": pa.array(ts.dt.tz_convert("Asia/Kolkata"), type=pa.timestamp("us", tz="Asia/Kolkata")),
            "counts": pa.FixedSizeListArray.from_arrays(pa.array(counts.ravel(), type=pa.int32()), width),
            "compute_seconds": pa.array(np.asarray(compute_seconds, dtype=np.float32)),
            "stimulus_hash": pa.array(list(stimulus_hashes), type=pa.string()),
            "sim_ms": pa.array(np.asarray(sim_ms, dtype=np.float64)),
        }
        for name in self.rate_populations:
            arrays[f"rate_{name}"] = pa.array(np.asarray(rates.get(name, np.zeros(n)), dtype=np.float32))
        table = pa.table(arrays)
        tmp = self.path(d).with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(self.path(d))
        return self.path(d)

    def read_day(self, d: date) -> FeatureSet:
        table = pq.read_table(self.path(d))
        width = len(self.columns)
        col = table.column("counts").combine_chunks()
        flat = col.values.to_numpy(zero_copy_only=False)
        counts = np.ascontiguousarray(flat, dtype=np.int32).reshape(-1, width) if width else np.zeros((table.num_rows, 0), np.int32)
        extra_cols = [c for c in table.column_names if c not in ("timestamp", "counts")]
        extra = table.select(extra_cols).to_pandas()
        ts = table.column("timestamp").to_pandas()
        ts = pd.Series(pd.to_datetime(ts))
        ts = ts.dt.tz_localize("Asia/Kolkata") if ts.dt.tz is None else ts.dt.tz_convert("Asia/Kolkata")
        return FeatureSet(
            timestamps=ts.reset_index(drop=True),
            dates=np.array([d] * table.num_rows, dtype=object),
            counts=counts,
            extra=extra.reset_index(drop=True),
            columns=self.columns,
        )

    def load(self, dates: list[date]) -> FeatureSet:
        parts = [self.read_day(d) for d in dates if self.has(d)]
        if not parts:
            return FeatureSet(
                timestamps=pd.Series(dtype="datetime64[us, Asia/Kolkata]"),
                dates=np.zeros(0, dtype=object),
                counts=np.zeros((0, len(self.columns)), dtype=np.int32),
                extra=pd.DataFrame(),
                columns=self.columns,
            )
        return FeatureSet(
            timestamps=pd.concat([p.timestamps for p in parts], ignore_index=True),
            dates=np.concatenate([p.dates for p in parts]),
            counts=np.concatenate([p.counts for p in parts], axis=0),
            extra=pd.concat([p.extra for p in parts], ignore_index=True),
            columns=self.columns,
        )

    # -- the pass ------------------------------------------------------------

    def run(self, builder: ObservationBuilder, dates: list[date], progress=None) -> dict:
        """Simulate every missing day of `dates`. `progress(done, total, date, cached)` is optional."""
        total = sum(builder.n_observations(d) for d in dates)
        done = 0
        simulated = 0
        skipped = 0
        seconds = float(self.neural_ms) / 1000.0
        t0 = time.time()
        pops = {name: np.asarray(self.brain.populations[name], dtype=np.int64) for name in self.rate_populations}
        for d in dates:
            n_day = builder.n_observations(d)
            if self.has(d):
                done += n_day
                skipped += 1
                if progress:
                    progress(done, total, d, True)
                continue
            warm = builder.warmup_observation(d)
            self.brain.observe(self.encoder.encode(warm, self.brain), self.neural_ms)
            timestamps, rows, comp, hashes, sims = [], [], [], [], []
            rates = {name: [] for name in self.rate_populations}
            for obs in builder.day_observations(d):
                stim = self.encoder.encode(obs, self.brain)
                res = self.brain.observe(stim, self.neural_ms)
                counts = np.asarray(res.counts)
                timestamps.append(obs.timestamp)
                rows.append(counts[self.columns].astype(np.int32))
                comp.append(float(res.compute_seconds))
                hashes.append(stimulus_hash(stim))
                sims.append(float(res.sim_ms))
                for name, ids in pops.items():
                    rates[name].append(float(counts[ids].mean() / seconds) if ids.size else 0.0)
                done += 1
                if progress and (done % 50 == 0):
                    progress(done, total, d, False)
            matrix = np.stack(rows) if rows else np.zeros((0, len(self.columns)), np.int32)
            self.write_day(d, timestamps, matrix, comp, hashes, sims, {k: np.asarray(v) for k, v in rates.items()})
            simulated += 1
            if progress:
                progress(done, total, d, False)
        return {
            "days": len(dates),
            "simulated": simulated,
            "cached": skipped,
            "observations": total,
            "seconds": time.time() - t0,
            "dir": str(self.dir),
        }


def _safe_provenance(brain) -> dict:
    try:
        return dict(brain.provenance() or {})
    except Exception:
        return {}


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    return obj
