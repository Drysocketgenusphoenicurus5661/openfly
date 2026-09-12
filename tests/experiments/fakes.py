"""Test doubles shared by the sensory, readout and experiment tests.

FakeBrain and FakeEyeMap live in openfly.experiments.fakebrain (so the CLI's
--fake-brain can use them); this module re-exports them and adds builders
for synthetic observations and a synthetic MarketData.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from openfly.experiments.data import MarketData
from openfly.experiments.fakebrain import FakeBrain, FakeEyeMap
from openfly.interfaces import Bar, MarketObservation

IST = ZoneInfo("Asia/Kolkata")

__all__ = ["FakeBrain", "FakeEyeMap", "make_bars", "make_observation", "synthetic_market", "IST"]


def make_bars(n: int = 120, start: float = 23400.0, seed: int = 0, step_minutes: int = 5, drift: float = 0.0,
              vol: float = 8.0, day: date = date(2026, 7, 1)) -> tuple[Bar, ...]:
    rng = np.random.default_rng(seed)
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=9, minute=15)
    closes = start + np.cumsum(rng.normal(drift, vol, size=n))
    bars = []
    prev = start
    for i, c in enumerate(closes):
        hi = max(prev, c) + abs(rng.normal(0, vol / 2))
        lo = min(prev, c) - abs(rng.normal(0, vol / 2))
        bars.append(Bar(timestamp=t0 + timedelta(minutes=step_minutes * i), open=prev, high=hi, low=lo, close=float(c), volume=0.0))
        prev = float(c)
    return tuple(bars)


def make_observation(bars: tuple[Bar, ...] | None = None, *, vix: float = 12.3, premium: float | None = 204.0,
                     entry_credit: float | None = None, position_lots: int = 0, days_to_expiry: float = 2.4,
                     minutes_since_open: int = 60, seed: int = 0, n_vix: int = 20) -> MarketObservation:
    bars = bars if bars is not None else make_bars(seed=seed)
    rng = np.random.default_rng(seed + 100)
    vb = []
    for i in range(n_vix):
        v = vix + rng.normal(0, 0.4)
        vb.append(Bar(timestamp=datetime(2026, 6, 1, tzinfo=IST) + timedelta(days=i), open=v, high=v + 0.2, low=v - 0.2, close=v, volume=0.0))
    return MarketObservation(
        timestamp=bars[-1].timestamp + timedelta(minutes=5),
        index_bars=bars,
        vix=vix,
        vix_bars=tuple(vb),
        straddle_premium=premium,
        entry_credit=entry_credit,
        days_to_expiry=days_to_expiry,
        minutes_since_open=minutes_since_open,
        position_lots=position_lots,
    )


def synthetic_frames(days: int = 5, start: date = date(2026, 7, 1), seed: int = 0, level: float = 23400.0,
                     vol_per_minute: float = 6.0, minutes: int = 375) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A random-walk 1 minute index frame over consecutive weekdays plus a daily VIX frame."""
    rng = np.random.default_rng(seed)
    rows = []
    vix_rows = []
    d = start
    made = 0
    price = level
    while made < days:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        t0 = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(hour=9, minute=15)
        for m in range(minutes):
            o = price
            c = price + rng.normal(0.0, vol_per_minute)
            hi = max(o, c) + abs(rng.normal(0, vol_per_minute / 2))
            lo = min(o, c) - abs(rng.normal(0, vol_per_minute / 2))
            rows.append((t0 + timedelta(minutes=m), o, hi, lo, c, 0.0, 0.0))
            price = c
        vix_rows.append((datetime.combine(d, datetime.min.time(), tzinfo=IST), 12.0 + rng.normal(0, 0.3), 12.5, 11.5, 12.0 + rng.normal(0, 0.3), 0.0, 0.0))
        made += 1
        d += timedelta(days=1)
    cols = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    f = pd.DataFrame(rows, columns=cols)
    v = pd.DataFrame(vix_rows, columns=cols)
    return f, v


def synthetic_market(days: int = 5, seed: int = 0, **kw) -> MarketData:
    f, v = synthetic_frames(days=days, seed=seed, **kw)
    return MarketData(store=False, frame_1m=f, vix_daily=v)
