from __future__ import annotations

import numpy as np
import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.interfaces import Decision
from openfly.readout import load_readout, make_readout
from openfly.readout.reservoir import ReservoirReadout
from tests.experiments.fakes import FakeBrain, make_observation


def planted(n_obs: int = 600, n_feat: int = 40, seed: int = 0):
    rng = np.random.default_rng(seed)
    counts = rng.poisson(8.0, size=(n_obs, n_feat))
    w = np.zeros(n_feat)
    w[:5] = [0.8, -0.6, 0.5, 0.4, -0.3]
    z = np.log1p(counts)
    z = (z - z.mean(axis=0)) / (z.std(axis=0) + 1e-9)
    y = 1.0 + 0.25 * (z @ w) + rng.normal(0, 0.05, size=n_obs)
    return counts, y


def test_untrained_predicts_hold():
    r = ReservoirReadout()
    p = r.predict(np.zeros(10, dtype=np.int32), None, make_observation())
    assert p.decision == Decision.HOLD and p.confidence == 0.0 and p.realized_over_implied == 1.0
    assert p.details["fitted"] is False


def test_recovers_planted_signal():
    X, y = planted()
    r = ReservoirReadout(alpha=1.0, tau=0.1)
    r.fit(X[:400], y[:400])
    out = r.predict_batch(X[400:])
    corr = np.corrcoef(out["roi"], y[400:])[0, 1]
    assert corr > 0.9
    acc = np.mean((out["prob_above_1"] > 0.5) == (y[400:] > 1.0))
    assert acc > 0.8
    single = r.predict(X[450], None, make_observation())
    assert single.realized_over_implied == pytest.approx(out["roi"][50])
    assert single.details["fitted"] and single.details["features"] == 40
    assert isinstance(single.details["top_populations"], list)


def test_hysteresis_policy():
    r = ReservoirReadout(tau=0.1)
    assert r.policy(0.85, in_position=False) == Decision.ENTER
    assert r.policy(0.85, in_position=True) == Decision.HOLD
    assert r.policy(1.15, in_position=True) == Decision.EXIT
    assert r.policy(1.15, in_position=False) == Decision.HOLD
    assert r.policy(0.95, in_position=False) == Decision.HOLD
    assert r.policy(1.05, in_position=True) == Decision.HOLD
    assert r.signal(0.85) == Decision.ENTER and r.signal(1.15) == Decision.EXIT and r.signal(1.0) == Decision.HOLD


def test_policy_through_predict_uses_position():
    X, y = planted(seed=2)
    r = ReservoirReadout(alpha=1.0, tau=0.05)
    r.fit(X, y)
    out = r.predict_batch(X)
    low = int(np.argmin(out["roi"]))
    high = int(np.argmax(out["roi"]))
    assert out["roi"][low] < 0.95 and out["roi"][high] > 1.05
    assert r.predict(X[low], None, make_observation(position_lots=0)).decision == Decision.ENTER
    assert r.predict(X[low], None, make_observation(position_lots=-1, entry_credit=200.0)).decision == Decision.HOLD
    assert r.predict(X[high], None, make_observation(position_lots=-1, entry_credit=200.0)).decision == Decision.EXIT
    assert r.predict(X[high], None, make_observation(position_lots=0)).decision == Decision.HOLD


def test_save_load_round_trip(tmp_path):
    X, y = planted(seed=3)
    r = ReservoirReadout(populations=("DN", "MBON"), alpha=10.0, tau=0.2, horizon_minutes=30)
    r.fit(X, y)
    r.save(tmp_path / "readout")
    loaded = load_readout(tmp_path / "readout")
    assert isinstance(loaded, ReservoirReadout)
    assert loaded.config_hash() == r.config_hash()
    assert loaded.fitted and loaded.tau == 0.2 and loaded.horizon_minutes == 30
    a = r.predict_batch(X[:20])
    b = loaded.predict_batch(X[:20])
    assert np.allclose(a["roi"], b["roi"]) and np.allclose(a["prob_above_1"], b["prob_above_1"])
    assert (tmp_path / "readout" / "readout.npz").exists() and (tmp_path / "readout" / "readout.json").exists()


def test_brain_populations_and_bound_columns():
    brain = FakeBrain(seed=1)
    r = make_readout("reservoir", DEFAULT_SETTINGS, brain)
    assert r.n_features == len(np.unique(np.concatenate([brain.populations[p] for p in ("DN", "MBON", "random2000")])))
    cols = np.unique(np.concatenate([brain.populations[p] for p in ("DN", "MBON", "random2000", "KC")]))
    r.bind_columns(cols)
    rng = np.random.default_rng(0)
    full = rng.poisson(3.0, size=(300, brain.n)).astype(np.int32)
    hidden = np.log1p(full[:, r.neuron_index[:3]]).sum(axis=1)
    y = 0.5 + 0.2 * (hidden - hidden.mean())
    r.fit(full[:, cols], y)  # cache-shaped counts
    from_cache = r.predict_batch(full[:5, cols])["roi"]
    from_full = r.predict_batch(full[:5], brain)["roi"]
    assert np.allclose(from_cache, from_full)
    p = r.predict(full[0], brain, make_observation())
    assert p.details["top_populations"] and all(name in ("DN", "MBON", "random2000") for name, _ in p.details["top_populations"])


def test_refit_alpha_changes_weights():
    X, y = planted(seed=4)
    r = ReservoirReadout(alpha=1.0)
    r.fit(X, y, classifier=False)
    w1 = r.coef.copy()
    r.refit_alpha(1000.0)
    assert np.linalg.norm(r.coef) < np.linalg.norm(w1)
