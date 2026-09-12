from __future__ import annotations

import json

import numpy as np

from openfly.config import DEFAULT_SETTINGS
from openfly.experiments.features import FeatureCache
from openfly.experiments.observations import ObservationBuilder
from openfly.experiments.pricer import StraddlePricer
from openfly.sensory import make_encoder, resolve_eye_map
from tests.experiments.fakes import FakeBrain, synthetic_market


class CountingBrain(FakeBrain):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def observe(self, stimulus, neural_ms):
        self.calls += 1
        return super().observe(stimulus, neural_ms)


def test_write_then_skip(tmp_path):
    market = synthetic_market(days=3, seed=2)
    pricer = StraddlePricer(factor=1.0, calendar=market.calendar)
    builder = ObservationBuilder(market, pricer=pricer, settings=DEFAULT_SETTINGS, interval="5m")
    brain = CountingBrain(seed=0)
    encoder = make_encoder("B", DEFAULT_SETTINGS, resolve_eye_map(brain))
    cache = FeatureCache(encoder, 200.0, brain, interval="5m", root=tmp_path)
    dates = market.dates[:2]
    events = []
    info = cache.run(builder, dates, progress=lambda done, total, d, cached: events.append((done, total, d, cached)))
    per_day = builder.n_observations(dates[0])
    assert info["simulated"] == 2 and info["cached"] == 0
    assert brain.calls == 2 * (per_day + 1)  # one warm-up observation per day
    assert all(cache.has(d) for d in dates)
    assert events and events[-1][0] == events[-1][1] == info["observations"]

    meta = json.loads((cache.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["columns"] == [int(c) for c in cache.columns]
    assert meta["interval"] == "5m" and meta["encoder_hash"] == encoder.config_hash()
    assert cache.dir.name.startswith(encoder.config_hash()[:16])

    # second pass: nothing simulated, the third day is new
    brain.calls = 0
    info2 = cache.run(builder, market.dates, progress=None)
    assert info2["cached"] == 2 and info2["simulated"] == 1
    assert brain.calls == per_day + 1

    feats = cache.load(market.dates)
    assert feats.counts.shape == (3 * per_day, len(cache.columns))
    assert feats.counts.dtype == np.int32
    assert len(feats.timestamps) == 3 * per_day and str(feats.timestamps.dt.tz) == "Asia/Kolkata"
    assert {"compute_seconds", "stimulus_hash", "sim_ms"} <= set(feats.extra.columns)
    assert any(c.startswith("rate_") for c in feats.extra.columns)
    assert feats.extra["stimulus_hash"].str.len().eq(64).all()
    day = cache.read_day(dates[0])
    assert np.array_equal(day.counts, feats.counts[:per_day])


def test_cache_key_includes_plastic_and_rejects_other_columns(tmp_path):
    market = synthetic_market(days=1, seed=3)
    brain = FakeBrain(seed=0)
    encoder = make_encoder("C", DEFAULT_SETTINGS, resolve_eye_map(brain))
    a = FeatureCache(encoder, 200.0, brain, interval="1m", root=tmp_path)
    b = FeatureCache(encoder, 200.0, brain, interval="1m", plastic=True, root=tmp_path)
    assert a.dir != b.dir and "_plastic" in b.dir.name
    assert a.dir.name.endswith("_fake") and b.dir.name.endswith("_fake")  # FakeBrain caches never collide with the real brain
    try:
        FeatureCache(encoder, 200.0, brain, populations=("KC",), interval="1m", root=tmp_path)
    except RuntimeError as exc:
        assert "different columns" in str(exc)
    else:
        raise AssertionError("a cache with other columns must be rejected")
    assert market.dates
