from __future__ import annotations

import copy
import json

import pytest

from openfly.config import DEFAULT_SETTINGS, PATHS, Paths, history_path
from openfly.experiments.runner import ExperimentRunner, list_experiments, load_experiment
from tests.experiments.fakes import FakeBrain

HAVE_HISTORY = history_path("NSE_INDEX", "NIFTY", "1m").exists()
WINDOWS = {"train": "2025-08-08:2026-03-31", "validation": "2026-04-01:2026-06-30", "test": "2026-07-01:2026-09-11"}


@pytest.mark.skipif(not HAVE_HISTORY, reason="NIFTY history not present")
def test_smoke_run_writes_valid_result(tmp_path):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    paths = Paths(root=PATHS.root, data=PATHS.data, runs=tmp_path / "runs")
    paths.ensure()
    config = {"encoder": "B", "readout": "reservoir", "neural_ms": 200, "limit_days": 3, "interval": "5m", "fake_brain": True, **WINDOWS}
    events = []
    runner = ExperimentRunner(config, lambda: FakeBrain(seed=0), settings, paths=paths, cache_root=tmp_path / "features", progress=events.append)
    result = runner.run()
    assert result["state"] == "done" and events and events[-1]["stage"] == "done"

    written = json.loads((runner.dir / "result.json").read_text(encoding="utf-8"))
    assert written["id"] == runner.id and written["state"] == "done"
    for key in ("config", "metrics", "controls", "curves", "passed", "verdict", "observations", "selection"):
        assert key in written
    assert set(written["metrics"]) == {"train", "validation", "test"}
    for w, m in written["metrics"].items():
        for k in ("net_pnl_per_lot", "sharpe", "max_drawdown", "trades", "stop_hits", "stop_hits_leg", "target_hits", "accuracy", "accuracy_ci", "synthetic_fraction"):
            assert k in m, (w, k)
    assert set(written["controls"]) == {"fixed_0920", "random_entry", "shuffled", "flat"}
    assert written["controls"]["flat"]["net_pnl_per_lot"] == 0.0
    assert written["controls"]["random_entry"]["trades"] == written["metrics"]["test"]["trades"]
    curves = written["curves"]["test"]
    n = len(curves["t"])
    assert n == 3 and all(len(curves[k]) == n for k in ("strategy", "fixed_0920", "random_entry", "shuffled", "flat"))
    assert written["observations"]["interval"] == "5m" and written["observations"]["test"] == 3 * 75
    assert isinstance(written["passed"], bool) and isinstance(written["verdict"], str)
    assert written["selection"]["populations"] and written["selection"]["alpha"] in written["config"]["alphas"]
    assert (runner.dir / "readout" / "readout.npz").exists() and (runner.dir / "trades.json").exists()
    assert written["provenance"]["fake_brain"] is True

    listed = list_experiments(paths)
    assert listed and listed[0]["id"] == runner.id and listed[0]["state"] == "done"
    assert load_experiment(runner.id, paths)["verdict"] == written["verdict"]

    # a second run reuses the feature cache (nothing simulated)
    runner2 = ExperimentRunner(config, lambda: FakeBrain(seed=0), settings, paths=paths, cache_root=tmp_path / "features")
    result2 = runner2.run()
    assert result2["feature_pass"]["simulated"] == 0 and result2["feature_pass"]["cached"] == 9


@pytest.mark.skipif(not HAVE_HISTORY, reason="NIFTY history not present")
def test_fixed_readout_smoke(tmp_path):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    paths = Paths(root=PATHS.root, data=PATHS.data, runs=tmp_path / "runs")
    paths.ensure()
    config = {"encoder": "C", "readout": "fixed", "neural_ms": 200, "limit_days": 2, "interval": "5m", **WINDOWS}
    runner = ExperimentRunner(config, lambda: FakeBrain(seed=1), settings, paths=paths, cache_root=tmp_path / "features")
    result = runner.run()
    assert result["state"] == "done" and result["selection"]["criterion"].startswith("none")
    assert set(result["controls"]) == {"fixed_0920", "random_entry", "shuffled", "flat"}
    assert (runner.dir / "readout" / "readout.json").exists()
