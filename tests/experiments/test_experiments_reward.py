from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.experiments.pricer import StraddlePricer
from openfly.experiments.reward import RewardSeries, reward_for
from openfly.experiments.sessions import IST
from tests.experiments.fakes import synthetic_frames, synthetic_market


def _series(frame, vix, market=None, **kw):
    pricer = StraddlePricer(factor=1.0, calendar=market.calendar if market else None)
    return RewardSeries.build(frame, pricer, DEFAULT_SETTINGS, vix=vix, market=market, **kw)


def test_calm_hour_positive_violent_hour_negative():
    market = synthetic_market(days=2, seed=4, vol_per_minute=0.5)  # calm
    series = _series(market.index_1m, market.vix_open_series(), market)
    f = series.as_frame()
    calm = f[f["valid"] & (f["date"] == market.dates[0])]
    assert (calm["reward_raw"] > 0.5).all()
    d = market.dates[1]
    t = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(hour=10, minute=0)
    assert series.raw_at(t) > 0.5

    # violent: a steady 30 point per minute trend, 1800 points an hour, far beyond any premium
    f, v = synthetic_frames(days=2, seed=4, vol_per_minute=0.0)
    ramp = 30.0 * np.arange(len(f))
    for col in ("open", "high", "low", "close"):
        f[col] = f[col] + ramp
    from openfly.experiments.data import MarketData

    wild = MarketData(store=False, frame_1m=f, vix_daily=v)
    wild_series = _series(wild.index_1m, wild.vix_open_series(), wild)
    g = wild_series.as_frame()
    hot = g[g["valid"] & (g["date"] == wild.dates[0]) & (g["horizon_used"] == 60)]
    assert len(hot) > 200 and (hot["reward_raw"] < 0).all()
    assert hot["reward_raw"].min() >= -1.0 - 0.02 and calm["reward_raw"].max() <= 1.0


def test_zero_mean_after_baseline_over_a_window():
    f, v = synthetic_frames(days=30, seed=8, vol_per_minute=4.0)
    from openfly.experiments.data import MarketData

    market = MarketData(store=False, frame_1m=f, vix_daily=v)
    series = _series(market.index_1m, market.vix_open_series(), market)
    frame = series.as_frame()
    later = frame[frame["valid"] & (frame["date"] >= market.dates[10])]
    assert abs(later["reward"].mean()) < 0.25 * later["reward_raw"].std() + 0.02
    # baseline is finite everywhere and equals the trailing mean of raw rewards
    assert np.isfinite(frame["baseline"]).all()
    d = market.dates[25]
    prev = frame[(frame["date"] < d) & (frame["date"] >= market.dates[5]) & frame["valid"]]
    per_day = prev.groupby("date")["reward_raw"].mean()
    expected = per_day.mean()
    got = frame.loc[frame["date"] == d, "baseline"].iloc[0]
    assert got == pytest.approx(expected)


def test_no_future_information_beyond_horizon():
    f1, v = synthetic_frames(days=2, seed=9)
    f2 = f1.copy()
    change = 375 + 240
    for col in ("open", "high", "low", "close"):
        f2.loc[change:, col] = f2.loc[change:, col] + 200.0
    from openfly.experiments.data import MarketData

    m1 = MarketData(store=False, frame_1m=f1, vix_daily=v)
    m2 = MarketData(store=False, frame_1m=f2, vix_daily=v)
    s1 = _series(m1.index_1m, m1.vix_open_series(), m1).as_frame()
    s2 = _series(m2.index_1m, m2.vix_open_series(), m2).as_frame()
    d = m1.dates[1]
    a = s1[s1["date"] == d].reset_index(drop=True)
    b = s2[s2["date"] == d].reset_index(drop=True)
    minute = ((pd.to_datetime(a["timestamp"]).dt.hour * 60 + pd.to_datetime(a["timestamp"]).dt.minute) - (9 * 60 + 15)).to_numpy()
    safe = minute + 60 <= 240
    assert safe.sum() > 100
    assert np.allclose(a.loc[safe, "reward"], b.loc[safe, "reward"], equal_nan=True)
    reach = (minute + 60 > 240) & (minute <= 240) & a["valid"].to_numpy()
    assert not np.allclose(a.loc[reach, "reward_raw"], b.loc[reach, "reward_raw"])


def test_lookup_keys_and_trade_override():
    market = synthetic_market(days=2, seed=5)
    series = _series(market.index_1m, market.vix_open_series(), market)
    d = market.dates[1]
    t = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(hour=11, minute=30)
    by_dt = series.reward_for(t)
    by_str = series.reward_for(t.isoformat())
    by_ts = series.reward_for(pd.Timestamp(t))
    idx = series.index_for(t)
    by_idx = series.reward_for(idx)
    assert by_dt == by_str == by_ts == by_idx
    assert series(t) == by_dt
    baseline = series.baseline_at(t)
    assert series.reward_for(t, trade={"pnl_inr": -1650.0, "stop_distance_inr": 3300.0}) == pytest.approx(-0.5 - baseline)
    assert series.reward_for(t, trade=(9000.0, 3300.0)) == pytest.approx(1.0 - baseline)  # clipped
    assert reward_for(t, series=series) == by_dt
    with pytest.raises(KeyError):
        series.reward_for(t + timedelta(seconds=30))
