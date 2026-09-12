"""Experiment runner: feature pass, readout fit and selection, evaluation with
controls, metrics, verdict and result.json in the GET /api/experiments/{id} shape.

    runner = ExperimentRunner(config, brain_factory, settings)
    result = runner.run()

config (docs/api-spec.md plus harness fields):
    encoder "A" | "B" | "C", readout "fixed" | "reservoir", plastic bool, neural_ms,
    train / validation / test as [start, end] or "start:end", limit_days (smoke runs),
    interval ("1m" default, from settings.neural.replay_interval), horizon_minutes,
    alphas, taus, population_grid, cache_populations, seed, name.
"""

from __future__ import annotations

import json
import time
import traceback
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from openfly.config import PATHS, Paths
from openfly.experiments.data import MarketData
from openfly.experiments.features import DEFAULT_CACHE_POPULATIONS, FeatureCache
from openfly.experiments.metrics import ACCURACY_THRESHOLD, cumulative_curve, window_metrics
from openfly.experiments.observations import ObservationBuilder, replay_interval
from openfly.experiments.quotes import minute_quotes_for
from openfly.experiments.sessions import IST
from openfly.experiments.simulator import (
    Rules,
    WindowResult,
    eligible_entry_rows,
    observation_rows,
    simulate_window,
)
from openfly.experiments.targets import build_targets, horizon_from_settings
from openfly.interfaces import Decision
from openfly.readout import make_readout
from openfly.readout.reservoir import ReservoirReadout
from openfly.sensory import make_encoder, resolve_eye_map

WINDOWS = ("train", "validation", "test")
CONTROLS = ("fixed_0920", "random_entry", "shuffled", "flat")
DEFAULT_ALPHAS = (1.0, 10.0, 100.0, 1000.0)
DEFAULT_TAUS = (0.05, 0.1, 0.2)
DEFAULT_POPULATION_GRID = (("DN", "MBON", "random2000"), ("DN",), ("MBON",), ("random2000",))


# ---------------------------------------------------------------------------
# Config helpers, registry
# ---------------------------------------------------------------------------


def parse_window(value) -> tuple[date, date]:
    if isinstance(value, str):
        a, b = value.split(":")
        return date.fromisoformat(a.strip()), date.fromisoformat(b.strip())
    a, b = value
    a = a if isinstance(a, date) else date.fromisoformat(str(a))
    b = b if isinstance(b, date) else date.fromisoformat(str(b))
    return a, b


def normalize_config(config: dict, settings: dict | None) -> dict:
    neural = (settings or {}).get("neural", {}) if isinstance(settings, dict) else {}
    out = dict(config or {})
    out["encoder"] = str(out.get("encoder", neural.get("encoder", "B")))
    out["readout"] = str(out.get("readout", neural.get("readout", "reservoir"))).lower()
    out["plastic"] = bool(out.get("plastic", neural.get("plastic", False)))
    out["neural_ms"] = float(out.get("neural_ms", neural.get("neural_ms", 200.0)))
    for w in WINDOWS:
        if w not in out:
            raise ValueError(f"config needs a {w} window")
        a, b = parse_window(out[w])
        out[w] = [a.isoformat(), b.isoformat()]
    out["limit_days"] = int(out["limit_days"]) if out.get("limit_days") else None
    out["interval"] = str(out.get("interval") or replay_interval(settings))
    out["horizon_minutes"] = int(out.get("horizon_minutes") or horizon_from_settings(settings))
    out["alphas"] = [float(a) for a in out.get("alphas", DEFAULT_ALPHAS)]
    out["taus"] = [float(t) for t in out.get("taus", DEFAULT_TAUS)]
    out["population_grid"] = [list(p) for p in out.get("population_grid", DEFAULT_POPULATION_GRID)]
    out["cache_populations"] = list(out.get("cache_populations", DEFAULT_CACHE_POPULATIONS))
    out["seed"] = int(out.get("seed", 0))
    out["fake_brain"] = bool(out.get("fake_brain", False))
    out.setdefault(
        "name",
        f"encoder {out['encoder']}, {out['readout']}, {'plastic' if out['plastic'] else 'frozen'}, {out['interval']}",
    )
    return out


def new_experiment_id(config: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(IST)
    return f"exp_{now.strftime('%Y%m%d_%H%M%S')}_{str(config.get('encoder', 'b')).lower()}_{config.get('readout', 'reservoir')}"


def list_experiments(paths: Paths = PATHS) -> list[dict]:
    out = []
    root = paths.experiments
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        f = d / "result.json"
        if not f.exists():
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "id": r.get("id", d.name),
                "name": r.get("name"),
                "state": r.get("state"),
                "created_at": r.get("created_at"),
                "finished_at": r.get("finished_at"),
                "config": r.get("config"),
                "progress": r.get("progress"),
                "passed": r.get("passed"),
                "verdict": r.get("verdict"),
            }
        )
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def load_experiment(experiment_id: str, paths: Paths = PATHS) -> dict:
    f = paths.experiments / experiment_id / "result.json"
    if not f.exists():
        raise FileNotFoundError(f"no experiment {experiment_id}")
    return json.loads(f.read_text(encoding="utf-8"))


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if not np.isfinite(v) else v
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return str(obj)


def _clean(obj):
    """Replace NaN floats with None recursively so the JSON is strict."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, np.floating):
        v = float(obj)
        return None if not np.isfinite(v) else v
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ExperimentRunner:
    def __init__(
        self,
        config: dict,
        brain_factory,
        settings: dict,
        *,
        market: MarketData | None = None,
        paths: Paths = PATHS,
        progress=None,
        experiment_id: str | None = None,
        quotes_factory=None,
        cache_root: str | Path | None = None,
    ):
        self.settings = settings or {}
        self.config = normalize_config(config, self.settings)
        self.brain_factory = brain_factory
        self.paths = paths
        self.market = market
        self.progress_cb = progress
        self.id = experiment_id or new_experiment_id(self.config)
        self.dir = paths.experiments / self.id
        self.quotes_factory = quotes_factory
        self.cache_root = cache_root
        self.result: dict = {
            "id": self.id,
            "name": self.config["name"],
            "state": "created",
            "created_at": datetime.now(IST).isoformat(),
            "finished_at": None,
            "config": self.config,
            "progress": {"stage": "created", "done": 0, "total": 0},
            "observations": {},
            "metrics": {},
            "controls": {},
            "controls_by_window": {},
            "curves": {},
            "selection": {},
            "passed": False,
            "verdict": "not run",
            "provenance": {},
        }
        self._quotes: dict = {}

    # -- bookkeeping ---------------------------------------------------------

    def _write(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_clean(self.result), indent=1, default=_json_default)
        tmp = self.dir / "result.json.tmp"
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.dir / "result.json")

    def _progress(self, stage: str, done: int = 0, total: int = 0, **extra) -> None:
        self.result["state"] = "running"
        self.result["progress"] = {"stage": stage, "done": int(done), "total": int(total), **extra}
        if self.progress_cb:
            try:
                self.progress_cb(dict(self.result["progress"], id=self.id))
            except Exception:
                pass

    def quotes_for(self, d: date):
        if d not in self._quotes:
            if self.quotes_factory is not None:
                self._quotes[d] = self.quotes_factory(d)
            else:
                self._quotes[d] = minute_quotes_for(
                    d, market=self.market, pricer=self.builder.pricer, settings=self.settings, paths=self.paths
                )
        return self._quotes[d]

    # -- main ----------------------------------------------------------------

    def run(self) -> dict:
        t0 = time.time()
        try:
            self._run()
            self.result["state"] = "done"
        except Exception as exc:  # the result file must record the failure
            self.result["state"] = "error"
            self.result["error"] = {"type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc()}
            self.result["verdict"] = f"error: {exc}"
            self._write()
            raise
        finally:
            self.result["finished_at"] = datetime.now(IST).isoformat()
            self.result["elapsed_seconds"] = time.time() - t0
            self._write()
        return self.result

    def _run(self) -> None:
        cfg = self.config
        settings = self.settings
        self._progress("loading")
        self._write()
        market = self.market or MarketData(paths=self.paths)
        self.market = market
        windows = {w: market.require_dates(*parse_window(cfg[w]), limit=cfg["limit_days"]) for w in WINDOWS}
        all_dates = sorted({d for ds in windows.values() for d in ds})

        brain = self.brain_factory()
        eye_map = resolve_eye_map(brain)
        encoder = make_encoder(cfg["encoder"], settings, eye_map)
        self.builder = ObservationBuilder(market, settings=settings, interval=cfg["interval"])
        rules = Rules.from_settings(settings)
        interval_min = self.builder.minutes
        horizon = cfg["horizon_minutes"]
        lots = rules.lots

        cache = FeatureCache(
            encoder,
            cfg["neural_ms"],
            brain,
            populations=cfg["cache_populations"],
            interval=cfg["interval"],
            plastic=cfg["plastic"],
            root=self.cache_root,
            paths=self.paths,
        )
        self.result["provenance"] = {
            "encoder": encoder.name,
            "encoder_hash": encoder.config_hash(),
            "brain": _clean(_safe_provenance(brain)),
            "fake_brain": cfg["fake_brain"] or _safe_provenance(brain).get("kind") == "FakeBrain",
            "pricer": self.builder.pricer.params(),
            "calibration": self.builder.pricer.calibration,
            "cache_dir": str(cache.dir),
            "cache_columns": int(len(cache.columns)),
            "rules": rules.to_dict(),
            "windows": {w: [d.isoformat() for d in ds] for w, ds in windows.items()},
        }

        # (a) feature pass
        def cache_progress(done, total, d, cached):
            self._progress("features", done, total, date=d.isoformat(), cached=bool(cached))
            if done % 500 == 0 or cached:
                self._write()

        self._progress("features", 0, sum(self.builder.n_observations(d) for d in all_dates))
        self._write()
        pass_info = cache.run(self.builder, all_dates, progress=cache_progress)
        self.result["feature_pass"] = pass_info

        # targets and features per window
        self._progress("targets")
        self._write()
        frames: dict[str, pd.DataFrame] = {}
        counts: dict[str, np.ndarray] = {}
        for w, ds in windows.items():
            feats = cache.load(ds)
            targets = build_targets(
                market, ds, settings, builder=self.builder, horizon_minutes=horizon, rules=rules,
                quotes_for=self.quotes_for,
            )
            frame, X = _align(feats, targets)
            frames[w] = frame
            counts[w] = X
        self.result["observations"] = {
            "interval": cfg["interval"],
            "horizon_minutes": horizon,
            **{w: int(len(frames[w])) for w in WINDOWS},
            "total": int(sum(len(frames[w]) for w in WINDOWS)),
            "days": {w: len(windows[w]) for w in WINDOWS},
        }

        # (b) readout
        self._progress("fitting")
        self._write()
        if cfg["readout"] == "reservoir":
            readout, selection, predictions = self._fit_reservoir(brain, cache, frames, counts, windows, rules, interval_min)
        else:
            readout, selection, predictions = self._fixed_readout(brain, cache, frames, counts)
        self.result["selection"] = selection
        self.result["provenance"]["readout_hash"] = readout.config_hash()
        self.result["provenance"]["readout"] = readout.params()

        # (c) evaluation and (d) controls
        self._progress("evaluating")
        self._write()
        seed = cfg["seed"]
        metrics: dict[str, dict] = {}
        controls_by_window: dict[str, dict] = {}
        curves: dict[str, dict] = {}
        trades_out: dict[str, dict] = {}
        for w in WINDOWS:
            frame = frames[w]
            pred = predictions[w]
            days = [(d, self.quotes_for(d)) for d in windows[w]]
            signals = _signal_builder(frame, pred["entry"], pred["exit"])
            strat = simulate_window(days, rules, interval_min, signals=signals)
            valid = frame["valid"].to_numpy()
            label = frame["label"].to_numpy()
            day_ids = frame["date"].to_numpy()
            correct = (pred["class"][valid] == label[valid])
            ridge_correct = ((pred["roi"][valid] > 1.0) == label[valid])
            metrics[w] = window_metrics(
                strat, lots=lots, correct=correct, day_ids=day_ids[valid], ridge_correct=ridge_correct,
                seed=seed, n_observations=len(frame), labels=label[valid],
            )
            metrics[w]["mean_prediction"] = float(np.mean(pred["roi"])) if len(pred["roi"]) else None
            metrics[w]["enter_signals"] = int(np.sum(pred["entry"]))
            metrics[w]["exit_signals"] = int(np.sum(pred["exit"]))

            fixed = simulate_window(days, rules, interval_min, always_enter=True)
            random_rows = _random_entry_rows(days, rules, interval_min, len(strat.trades), seed)
            random = simulate_window(days, rules, interval_min, entry_rows_by_day=random_rows)
            flat = WindowResult(trades=[], daily_pnl=pd.Series({d: 0.0 for d in windows[w]}, dtype="float64"), synthetic_fraction=strat.synthetic_fraction, days=windows[w])
            shuffled_pred = predictions.get(f"shuffled_{w}")
            if shuffled_pred is not None:
                shuffled = simulate_window(days, rules, interval_min, signals=_signal_builder(frame, shuffled_pred["entry"], shuffled_pred["exit"]))
                sh_correct = (shuffled_pred["class"][valid] == label[valid])
                sh_metrics = window_metrics(shuffled, lots=lots, correct=sh_correct, day_ids=day_ids[valid], seed=seed, n_observations=len(frame), labels=label[valid])
            else:
                shuffled = flat
                sh_metrics = window_metrics(flat, lots=lots, n_observations=len(frame))
            controls_by_window[w] = {
                "fixed_0920": window_metrics(fixed, lots=lots, n_observations=len(frame)),
                "random_entry": window_metrics(random, lots=lots, n_observations=len(frame)),
                "shuffled": sh_metrics,
                "flat": window_metrics(flat, lots=lots, n_observations=len(frame)),
            }
            t, strategy_curve = cumulative_curve(_reindex(strat.daily_pnl, windows[w]) / lots)
            curves[w] = {
                "t": t,
                "strategy": strategy_curve,
                "fixed_0920": cumulative_curve(_reindex(fixed.daily_pnl, windows[w]) / lots)[1],
                "random_entry": cumulative_curve(_reindex(random.daily_pnl, windows[w]) / lots)[1],
                "shuffled": cumulative_curve(_reindex(shuffled.daily_pnl, windows[w]) / lots)[1],
                "flat": [0.0] * len(t),
            }
            trades_out[w] = {
                "strategy": [tr.to_dict() for tr in strat.trades],
                "fixed_0920": [tr.to_dict() for tr in fixed.trades],
                "random_entry": [tr.to_dict() for tr in random.trades],
                "shuffled": [tr.to_dict() for tr in shuffled.trades],
            }
        self.result["metrics"] = metrics
        self.result["controls_by_window"] = controls_by_window
        self.result["controls"] = controls_by_window["test"]
        self.result["curves"] = curves

        # (f) verdict
        passed, verdict = _verdict(metrics["test"], controls_by_window["test"])
        self.result["passed"] = passed
        self.result["verdict"] = verdict

        # (g) persist
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "trades.json").write_text(json.dumps(_clean(trades_out), indent=1, default=_json_default), encoding="utf-8")
        readout.save(self.dir / "readout")
        pred_dir = self.dir / "predictions"
        pred_dir.mkdir(exist_ok=True)
        for w in WINDOWS:
            frame = frames[w][["timestamp", "date", "day_row", "y", "label", "valid", "premium", "spot"]].copy()
            frame["roi"] = predictions[w]["roi"]
            frame["prob_above_1"] = predictions[w]["prob"]
            frame["entry"] = predictions[w]["entry"]
            frame["exit"] = predictions[w]["exit"]
            frame["date"] = frame["date"].astype(str)
            frame.to_parquet(pred_dir / f"{w}.parquet", index=False)
        self._progress("done", self.result["observations"]["total"], self.result["observations"]["total"])

    # -- readouts ------------------------------------------------------------

    def _fit_reservoir(self, brain, cache, frames, counts, windows, rules, interval_min):
        cfg = self.config
        settings = self.settings
        neural = settings.get("neural", {})
        horizon = cfg["horizon_minutes"]
        y_train = frames["train"]["y"].to_numpy(dtype=np.float64)
        grid = [tuple(p) for p in cfg["population_grid"] if all(name in cache.populations for name in p)]
        if not grid:
            grid = [tuple(cache.populations)]
        alphas = cfg["alphas"]
        taus = cfg["taus"]
        results = []
        fitted: dict[tuple, ReservoirReadout] = {}
        val_days = [(d, self.quotes_for(d)) for d in windows["validation"]]
        best = None
        for pops in grid:
            readout = ReservoirReadout(
                populations=pops, alpha=alphas[0], tau=taus[0], horizon_minutes=horizon,
                neural_ms=cfg["neural_ms"],
            )
            readout.resolve(brain)
            readout.bind_columns(cache.columns)
            readout.fit(counts["train"], y_train, classifier=False)
            fitted[pops] = readout
            for alpha in alphas:
                readout.refit_alpha(alpha)
                roi_val = readout.predict_batch(counts["validation"])["roi"]
                for tau in taus:
                    entry = roi_val < 1.0 - tau
                    exit_ = roi_val > 1.0 + tau
                    sim = simulate_window(val_days, rules, interval_min, signals=_signal_builder(frames["validation"], entry, exit_))
                    net = float(sim.daily_pnl.sum() / max(1, rules.lots))
                    row = {"populations": list(pops), "alpha": alpha, "tau": tau, "validation_net_pnl_per_lot": net, "trades": len(sim.trades), "features": readout.n_features}
                    results.append(row)
                    key = (net, alpha, -len(pops))
                    if best is None or key > best[0]:
                        best = (key, pops, alpha, tau)
                self._progress("fitting", len(results), len(grid) * len(alphas) * len(taus))
        _, pops, alpha, tau = best
        readout = fitted[pops]
        readout.refit_alpha(alpha)
        readout.tau = float(tau)
        readout.fit_classifier_from_counts(counts["train"], y_train)
        selection = {
            "populations": list(pops), "alpha": alpha, "tau": tau, "features": readout.n_features,
            "criterion": "validation net P&L per lot after costs", "grid": results,
        }
        predictions = {}
        for w in WINDOWS:
            p = readout.predict_batch(counts[w])
            predictions[w] = _predictions_from_roi(p["roi"], p["prob_above_1"], tau)
        # shuffled-label control: refit on permuted y with the chosen configuration
        rng = np.random.default_rng(cfg["seed"])
        y_perm = y_train.copy()
        finite = np.isfinite(y_perm)
        y_perm[finite] = rng.permutation(y_perm[finite])
        shuffled = ReservoirReadout(populations=pops, alpha=alpha, tau=tau, horizon_minutes=horizon, neural_ms=cfg["neural_ms"])
        shuffled.resolve(brain)
        shuffled.bind_columns(cache.columns)
        shuffled.fit(counts["train"], y_perm, classifier=True)
        for w in WINDOWS:
            p = shuffled.predict_batch(counts[w])
            predictions[f"shuffled_{w}"] = _predictions_from_roi(p["roi"], p["prob_above_1"], tau)
        neural.setdefault("tau", tau)
        return readout, selection, predictions

    def _fixed_readout(self, brain, cache, frames, counts):
        readout = make_readout("fixed", self.settings, brain)
        readout.bind_columns(cache.columns)
        predictions = {}
        rng = np.random.default_rng(self.config["seed"])
        for w in WINDOWS:
            out = readout.predict_batch(counts[w], brain)
            entry = out["signal"] == Decision.ENTER.value
            exit_ = out["signal"] == Decision.EXIT.value
            predictions[w] = {"roi": out["roi"], "prob": (out["roi"] > 1.0).astype(float), "class": out["roi"] > 1.0, "entry": entry, "exit": exit_}
            perm = rng.permutation(len(out["roi"]))
            predictions[f"shuffled_{w}"] = {
                "roi": out["roi"][perm], "prob": (out["roi"][perm] > 1.0).astype(float), "class": out["roi"][perm] > 1.0,
                "entry": entry[perm], "exit": exit_[perm],
            }
        selection = {"criterion": "none (fixed decoder)", "threshold_hz": readout.threshold_hz}
        return readout, selection, predictions


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_provenance(brain) -> dict:
    try:
        return dict(brain.provenance() or {})
    except Exception:
        return {}


def _align(feats, targets: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """Inner-join cached features and targets on the observation timestamp."""
    if len(feats) == 0 or len(targets) == 0:
        empty = targets.iloc[0:0].copy() if len(targets) else pd.DataFrame()
        return empty.assign(day_row=pd.Series(dtype="int64")), np.zeros((0, feats.counts.shape[1]), np.int32)
    left = pd.DataFrame({"timestamp": pd.to_datetime(feats.timestamps).dt.tz_convert("Asia/Kolkata"), "_i": np.arange(len(feats))})
    right = targets.copy()
    right["timestamp"] = pd.to_datetime(right["timestamp"]).dt.tz_convert("Asia/Kolkata")
    right = right.rename(columns={"row": "day_row"})
    merged = left.merge(right, on="timestamp", how="inner").sort_values("timestamp").reset_index(drop=True)
    X = feats.counts[merged["_i"].to_numpy()]
    merged = merged.drop(columns=["_i"])
    return merged, X


def _predictions_from_roi(roi: np.ndarray, prob: np.ndarray, tau: float) -> dict:
    roi = np.asarray(roi, dtype=np.float64)
    return {
        "roi": roi,
        "prob": np.asarray(prob, dtype=np.float64),
        "class": np.asarray(prob) > 0.5,
        "entry": roi < 1.0 - tau,
        "exit": roi > 1.0 + tau,
    }


def _signal_builder(frame: pd.DataFrame, entry: np.ndarray, exit_: np.ndarray):
    groups: dict = {}
    if len(frame):
        rows = frame["day_row"].to_numpy(dtype=np.int64)
        dates = frame["date"].to_numpy()
        for d in np.unique(dates):
            m = dates == d
            groups[d] = (rows[m], np.asarray(entry)[m], np.asarray(exit_)[m])

    def signals(d, quotes):
        e = np.zeros(quotes.n, dtype=bool)
        x = np.zeros(quotes.n, dtype=bool)
        g = groups.get(d)
        if g is not None:
            rows, en, ex = g
            ok = (rows >= 0) & (rows < quotes.n)
            e[rows[ok]] = en[ok]
            x[rows[ok]] = ex[ok]
        return e, x

    return signals


def _random_entry_rows(days, rules: Rules, interval_min: int, k: int, seed: int) -> dict:
    """k random (date, observation row) entry points in the trade window, matched on trade count."""
    rng = np.random.default_rng(seed + 17)
    out: dict = {d: np.zeros(0, dtype=np.int64) for d, _ in days}
    if k <= 0 or not days:
        return out
    pool = []
    for d, quotes in days:
        rows = eligible_entry_rows(quotes, rules, observation_rows(quotes, interval_min))
        pool.extend((d, int(r)) for r in rows)
    if not pool:
        return out
    chosen_idx = set()
    target = min(k, len(pool))
    for _ in range(6):
        need = target - _count_trades(days, rules, interval_min, out)
        if need <= 0:
            break
        candidates = [i for i in range(len(pool)) if i not in chosen_idx]
        if not candidates:
            break
        pick = rng.choice(candidates, size=min(len(candidates), max(need, 1)), replace=False)
        chosen_idx.update(int(i) for i in np.atleast_1d(pick))
        per_day: dict = {d: [] for d, _ in days}
        for i in chosen_idx:
            d, r = pool[i]
            per_day[d].append(r)
        out = {d: np.array(sorted(v), dtype=np.int64) for d, v in per_day.items()}
    return out


def _count_trades(days, rules, interval_min, rows_by_day) -> int:
    sim = simulate_window(days, rules, interval_min, entry_rows_by_day=rows_by_day)
    return len(sim.trades)


def _reindex(daily: pd.Series, dates: list[date]) -> pd.Series:
    s = pd.Series({d: 0.0 for d in dates}, dtype="float64")
    if len(daily):
        for d, v in daily.items():
            if d in s.index:
                s[d] = float(v)
    return s


def _verdict(test: dict, controls: dict) -> tuple[bool, str]:
    acc = test.get("accuracy")
    p = test.get("accuracy_p_value")
    ci = test.get("accuracy_ci") or [None, None]
    threshold = float(test.get("accuracy_threshold") or ACCURACY_THRESHOLD)
    net = test.get("net_pnl_per_lot", 0.0)
    fixed = controls["fixed_0920"]["net_pnl_per_lot"]
    random = controls["random_entry"]["net_pnl_per_lot"]
    reasons = []
    if acc is None or not np.isfinite(acc):
        reasons.append("no classification accuracy (no valid observations)")
    else:
        if acc <= threshold:
            what = f"{ACCURACY_THRESHOLD:.2f}" if threshold == ACCURACY_THRESHOLD else f"the majority-class rate {threshold:.3f}"
            reasons.append(f"accuracy {acc:.3f} not above {what}")
        if p is None or p >= 0.05:
            reasons.append(f"block-bootstrap p-value {p if p is None else round(p, 3)} not below 0.05")
    if net <= fixed:
        reasons.append(f"net P&L per lot {net:.0f} does not beat the fixed 09:20 straddle ({fixed:.0f})")
    if net <= random:
        reasons.append(f"net P&L per lot {net:.0f} does not beat random entry ({random:.0f})")
    if test.get("trades", 0) == 0:
        reasons.append("no trades on the test window")
    if reasons:
        return False, "no edge found: " + "; ".join(reasons)
    return True, (
        f"edge found: accuracy {acc:.3f} (95 percent interval {ci[0]:.3f} to {ci[1]:.3f}, p={p:.3f}), "
        f"net P&L per lot {net:.0f} beats fixed 09:20 ({fixed:.0f}) and random entry ({random:.0f})"
    )
