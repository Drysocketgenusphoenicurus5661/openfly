from __future__ import annotations

import numpy as np
import pandas as pd

from openfly.config import DEFAULT_SETTINGS
from openfly.experiments.data import MarketData
from openfly.experiments.observations import ObservationBuilder
from openfly.experiments.pricer import StraddlePricer
from openfly.experiments.quotes import minute_quotes_for
from openfly.experiments.simulator import Rules
from openfly.experiments.targets import build_targets
from tests.experiments.fakes import synthetic_frames


def _targets(frame, vix, interval="1m", horizon=60):
    market = MarketData(store=False, frame_1m=frame, vix_daily=vix)
    pricer = StraddlePricer(factor=1.0, calendar=market.calendar)
    builder = ObservationBuilder(market, pricer=pricer, settings=DEFAULT_SETTINGS, interval=interval)
    rules = Rules.from_settings(DEFAULT_SETTINGS)
    quotes = {d: minute_quotes_for(d, market=market, pricer=pricer, settings=DEFAULT_SETTINGS, prefer_recorded=False) for d in market.dates}
    return build_targets(market, market.dates, DEFAULT_SETTINGS, builder=builder, horizon_minutes=horizon, rules=rules,
                         quotes_for=quotes.__getitem__), market


def test_no_lookahead_beyond_horizon():
    f1, v = synthetic_frames(days=2, seed=7)
    f2 = f1.copy()
    change_minute = 200  # second day, minute 200 onwards is different
    cut = 375 + change_minute
    for col in ("open", "high", "low", "close"):
        f2.loc[cut:, col] = f2.loc[cut:, col] + 150.0
    t1, _ = _targets(f1, v)
    t2, _ = _targets(f2, v)
    day2 = t1["date"] == t1["date"].iloc[-1]
    a = t1[day2].reset_index(drop=True)
    b = t2[day2].reset_index(drop=True)
    # rows whose t + H stays before the change are identical
    safe = a["minute"] + 60 < change_minute
    assert safe.sum() > 50
    assert np.allclose(a.loc[safe, "y"], b.loc[safe, "y"], equal_nan=True)
    assert np.allclose(a.loc[safe, "pnl_inr"], b.loc[safe, "pnl_inr"], equal_nan=True)
    # rows that reach into the change differ
    touched = (a["minute"] + 60 >= change_minute) & (a["minute"] < change_minute) & a["valid"]
    assert touched.sum() > 0 and not np.allclose(a.loc[touched, "y"], b.loc[touched, "y"])


def test_realized_and_implied_definitions():
    f, v = synthetic_frames(days=1, seed=3)
    t, market = _targets(f, v)
    day = market.day_view(market.dates[0])
    row = t.iloc[100]
    assert row["valid"]
    r = int(row["row"])
    end = day.row_for_close_time(int(row["minute"]) + 60)
    assert row["realized_points"] == abs(day.close[end] - day.close[r])
    assert row["horizon_used"] == 60
    expected_implied = row["premium"] * np.sqrt(60 / row["minutes_to_expiry"])
    assert row["implied_points"] == expected_implied
    assert row["y"] == row["realized_points"] / row["implied_points"]
    assert bool(row["label"]) == (row["y"] > 1.0)
    # last observation of the day has no forward window
    last = t.iloc[-1]
    assert not last["valid"] and pd.isna(last["y"])
    # observations near the close use a shorter horizon
    late = t[(t["minute"] > 320) & t["valid"]]
    assert (late["horizon_used"] < 60).all()


def test_flat_path_decays_in_favour_of_the_short_straddle():
    f, v = synthetic_frames(days=1, seed=0, vol_per_minute=0.0)
    t, _ = _targets(f, v)
    valid = t[t["pnl_valid"]]
    full = valid[valid["horizon_used"] == 60]
    assert len(full) > 250 and (full["pnl_points"] > 0).all()  # decay beats the spread over a full hour
    assert (valid["exit_reason"] == "HORIZON").all()
    assert (valid["realized_points"] == 0).all() and (valid["y"] == 0).all()


def test_five_minute_interval_gives_fewer_rows():
    f, v = synthetic_frames(days=1, seed=1)
    t1, _ = _targets(f, v, interval="1m")
    t5, _ = _targets(f, v, interval="5m")
    assert len(t1) == 375 and len(t5) == 75
    m5 = t5.set_index("minute")["y"]
    m1 = t1.set_index("minute")["y"]
    common = m5.index.intersection(m1.index)
    assert np.allclose(m5.loc[common], m1.loc[common], equal_nan=True)
