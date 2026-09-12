from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np
import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.experiments.observations import ObservationBuilder
from openfly.experiments.pricer import StraddlePricer
from openfly.interfaces import Bar, Stimulus
from openfly.sensory import (
    ENCODER_NAMES,
    EyeMap,
    default_eye_map,
    make_encoder,
    resolve_eye_map,
    stimulus_hash,
)
from openfly.sensory.encoders import BarsEncoder, ChartEncoder, FeatureEncoder
from tests.experiments.fakes import FakeBrain, make_bars, make_observation, synthetic_frames

ENCODERS = ("A", "B", "C")


@pytest.fixture(scope="module")
def brain():
    return FakeBrain(seed=1)


@pytest.fixture(scope="module")
def eye_map(brain):
    return resolve_eye_map(brain)


@pytest.mark.parametrize("name", ENCODERS)
def test_lengths_and_range(name, brain, eye_map):
    enc = make_encoder(name, DEFAULT_SETTINGS, eye_map)
    stim = enc.encode(make_observation(), brain)
    assert isinstance(stim, Stimulus)
    assert stim.r16.shape == (eye_map.n_r16,)
    assert stim.r8.shape == (eye_map.n_r8,)
    assert stim.r16.dtype == np.float32 and stim.r8.dtype == np.float32
    assert np.all(stim.r16 >= 0.0) and np.all(stim.r16 <= 1.0)
    assert np.all(stim.r8 >= 0.0) and np.all(stim.r8 <= 1.0)
    assert np.isfinite(stim.r16).all() and np.isfinite(stim.r8).all()


@pytest.mark.parametrize("name", ENCODERS)
def test_deterministic(name, brain, eye_map):
    enc = make_encoder(name, DEFAULT_SETTINGS, eye_map)
    obs = make_observation(seed=3)
    a = enc.encode(obs, brain)
    b = make_encoder(name, DEFAULT_SETTINGS, eye_map).encode(obs, brain)
    assert np.array_equal(a.r16, b.r16) and np.array_equal(a.r8, b.r8)
    assert stimulus_hash(a) == stimulus_hash(b)


@pytest.mark.parametrize("name", ENCODERS)
def test_only_the_past_matters(name, brain, eye_map):
    """Observations built from two histories that agree up to t give the same stimulus."""
    f1, v = synthetic_frames(days=3, seed=5)
    f2 = f1.copy()
    cut = 2 * 375 + 200  # a minute in the third day
    f2.loc[cut:, "close"] = f2.loc[cut:, "close"] + 300.0
    f2.loc[cut:, "high"] = f2.loc[cut:, "high"] + 300.0
    f2.loc[cut:, "low"] = f2.loc[cut:, "low"] + 300.0
    from openfly.experiments.data import MarketData

    m1 = MarketData(store=False, frame_1m=f1, vix_daily=v)
    m2 = MarketData(store=False, frame_1m=f2, vix_daily=v)
    pricer = StraddlePricer(factor=1.0)
    b1 = ObservationBuilder(m1, pricer=pricer, settings=DEFAULT_SETTINGS, interval="1m")
    b2 = ObservationBuilder(m2, pricer=pricer, settings=DEFAULT_SETTINGS, interval="1m")
    enc = make_encoder(name, DEFAULT_SETTINGS, eye_map)
    before = cut - 1  # observation whose bar closes exactly at the cut
    s1 = enc.encode(b1.observation(before), brain)
    s2 = enc.encode(b2.observation(before), brain)
    assert np.array_equal(s1.r16, s2.r16) and np.array_equal(s1.r8, s2.r8)
    after = cut + 5
    s3 = enc.encode(b1.observation(after), brain)
    s4 = enc.encode(b2.observation(after), brain)
    assert not (np.array_equal(s3.r16, s4.r16) and np.array_equal(s3.r8, s4.r8))


@pytest.mark.parametrize("name", ENCODERS)
def test_config_hash_stable(name, eye_map):
    h1 = make_encoder(name, DEFAULT_SETTINGS, eye_map).config_hash()
    h2 = make_encoder(name, DEFAULT_SETTINGS, eye_map).config_hash()
    assert h1 == h2 and len(h1) == 64
    settings = {"neural": {"encoder_params": {"columns": 20} if name == "B" else ({"bars": 30} if name == "A" else {"grid": 4})}}
    assert make_encoder(name, settings, eye_map).config_hash() != h1


@pytest.mark.parametrize("name", ENCODERS)
def test_render_png(name, brain, eye_map):
    enc = make_encoder(name, DEFAULT_SETTINGS, eye_map)
    stim = enc.encode(make_observation(), brain)
    png = enc.render_png(stim, eye_map)
    assert isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n"
    # A fresh stimulus not matching the last chart falls back to the eye map rendering.
    other = Stimulus(r16=np.zeros_like(stim.r16), r8=np.zeros_like(stim.r8))
    assert enc.render_png(other, eye_map)[:8] == b"\x89PNG\r\n\x1a\n"


def test_names_and_factory(eye_map):
    assert ENCODER_NAMES["A"] == "chart" and ENCODER_NAMES["B"] == "bars" and ENCODER_NAMES["C"] == "features"
    assert isinstance(make_encoder("chart", None, eye_map), ChartEncoder)
    assert isinstance(make_encoder("bars", None, eye_map), BarsEncoder)
    assert isinstance(make_encoder("features", None, eye_map), FeatureEncoder)
    with pytest.raises(ValueError):
        make_encoder("D", None, eye_map)


def test_bars_encoder_column_geometry(brain):
    """Newest bar at the midline: u near 1 on the left eye, u near 0 on the right eye."""
    uv16 = np.array([[0.02, 0.5], [0.98, 0.5], [0.02, 0.5], [0.98, 0.5]], dtype=np.float32)
    eye16 = np.array([0, 0, 1, 1], dtype=np.int8)
    uv8 = np.array([[0.98, 0.5], [0.98, 0.5]], dtype=np.float32)
    em = EyeMap(uv_r16=uv16, eye_r16=eye16, uv_r8=uv8, eye_r8=np.array([0, 0], dtype=np.int8), r8_channel=np.array([1, 2], dtype=np.int8))
    enc = BarsEncoder(em, columns=30)
    bars = list(make_bars(n=120, seed=2, vol=2.0))
    last = bars[-1]
    bars[-1] = replace(last, close=last.close + 40.0, high=last.high + 40.0)  # strong rise on the newest bar
    obs = make_observation(tuple(bars))
    stim = enc.encode(obs, brain)
    left_periphery, left_centre, right_centre, right_periphery = stim.r16
    assert left_centre > 0.9 and right_centre > 0.9
    assert abs(left_periphery - right_periphery) < 1e-6  # both see the oldest bar
    # R8y (channel 1) carries the newest bar's range, R8p the slow channel around 0.5.
    assert stim.r8[0] > 0.0
    assert 0.0 <= stim.r8[1] <= 1.0


def test_bars_encoder_slow_channel_position():
    em = default_eye_map(FakeBrain(seed=2))
    enc = BarsEncoder(em)
    flat = make_observation(premium=200.0, entry_credit=None, position_lots=0)
    short = make_observation(premium=260.0, entry_credit=200.0, position_lots=-1)
    _, _, slow_flat = enc.column_values(flat)
    _, _, slow_short = enc.column_values(short)
    assert slow_short > slow_flat  # premium up since entry raises the slow channel


def test_chart_encoder_draws_line(brain, eye_map):
    enc = ChartEncoder(eye_map)
    obs = make_observation(seed=4)
    stim = enc.encode(obs, brain)
    background = float(np.max(stim.r16))
    assert (stim.r16 < background - 0.05).any()  # some cells fall on the line
    img = enc.render_chart(obs)
    assert img.size == (320, 180)
    arr = np.asarray(img)
    assert (arr[..., 2] > arr[..., 0]).any() or (arr[..., 0] > arr[..., 2]).any()  # blue or red segments present


def test_feature_encoder_values_in_range():
    enc = FeatureEncoder(default_eye_map(FakeBrain(seed=3)))
    values = enc.features(make_observation(position_lots=-1, minutes_since_open=180))
    assert values.shape == (9,)
    assert np.all(values >= 0.0) and np.all(values <= 1.0)
    assert values[8] == 1.0 and abs(values[6] - 180 / 375) < 1e-9


def test_default_eye_map_shapes():
    b = FakeBrain(seed=0)
    em = default_eye_map(b)
    assert em.n_r16 == len(b.populations["R1-R6"])
    assert em.n_r8 == len(b.populations["R8p"]) + len(b.populations["R8y"])
    assert set(np.unique(em.r8_channel)) <= {1, 2}
    assert date(2026, 7, 1) and isinstance(make_bars()[0], Bar)
