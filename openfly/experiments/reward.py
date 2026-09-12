"""One reward definition shared by the plastic arm and the readout target.

    r_t = clip(1 - realized_move(t, t + H) / implied_move(t), -1, +1) - cost_fraction

H is settings.neural.horizon_minutes (60), realized_move the absolute index
move in points over the next H minutes inside the session, implied_move the
synthetic ATM straddle premium of the selected contract
(settings.strategy.expiry_selection, monthly by default) at t, cost_fraction
the round-trip cost as a fraction of that premium (about 0.01). The reward delivered is the advantage
r_t minus a baseline: the mean of r over the trailing 20 trading days
(strictly before t's date; on the first day, the expanding mean of the same
day's earlier observations). Nothing uses information after t + H.

When a real straddle was open at t, the trade's realized P&L over its stop
distance in INR (clipped to [-1, 1]) replaces the counterfactual for the entry
observation:

    series = RewardSeries.build(bars_by_day, pricer, settings)
    series.reward_for(timestamp)                                  # counterfactual advantage
    series.reward_for(timestamp, trade={"pnl_inr": -1200.0, "stop_distance_inr": 3300.0})

Module-level `reward_for(key, trade=None, series=None)` builds a default
series from the local history on first use. `key` is a tz-aware datetime, a
pandas Timestamp, an ISO string or an integer observation index.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

import numpy as np
import pandas as pd

from openfly.experiments.costs import cost_fraction, costs_from_settings
from openfly.experiments.pricer import StraddlePricer
from openfly.experiments.sessions import IST, SESSION_MINUTES, TradingCalendar, expiry_selection

BASELINE_DAYS = 20


def _split_by_day(bars) -> dict[date, pd.DataFrame]:
    if isinstance(bars, Mapping):
        return {k: pd.DataFrame(v) for k, v in bars.items()}
    f = pd.DataFrame(bars)
    ts = pd.to_datetime(f["timestamp"])
    ts = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
    f = f.assign(timestamp=ts)
    return {d: g.reset_index(drop=True) for d, g in f.groupby(ts.dt.date, sort=True)}


def _vix_for(vix, d: date, market) -> float:
    if market is not None:
        return float(market.vix_open(d))
    from openfly.experiments.quotes import vix_value_for

    return vix_value_for(vix, d)


class RewardSeries:
    COLUMNS = (
        "timestamp", "date", "realized_points", "implied_points", "horizon_used",
        "cost_fraction", "reward_raw", "baseline", "reward", "valid",
    )

    def __init__(self, frame: pd.DataFrame, horizon_minutes: int, baseline_days: int = BASELINE_DAYS):
        self.frame = frame.reset_index(drop=True)
        self.horizon_minutes = int(horizon_minutes)
        self.baseline_days = int(baseline_days)
        self._ts = self.frame["timestamp"].to_numpy()
        self._index = {pd.Timestamp(t): i for i, t in enumerate(self.frame["timestamp"])}

    # -- construction --------------------------------------------------------

    @classmethod
    def build(
        cls,
        bars_by_day,
        pricer: StraddlePricer | None = None,
        settings: dict | None = None,
        *,
        market=None,
        vix=None,
        calendar: TradingCalendar | None = None,
        horizon_minutes: int | None = None,
        baseline_days: int = BASELINE_DAYS,
    ) -> RewardSeries:
        """Rewards for every bar close of `bars_by_day` ({date: bars} or one frame with a timestamp column).

        Bars may be of any interval; realized moves are measured on their
        closes, so 1 minute bars give the minute-exact definition. `vix` is a
        float or a Series indexed by date; when `market` is given its VIX at
        the open is used instead.
        """
        neural = (settings or {}).get("neural", {}) if isinstance(settings, dict) else {}
        strategy = (settings or {}).get("strategy", {}) if isinstance(settings, dict) else {}
        horizon = int(horizon_minutes or neural.get("horizon_minutes", 60))
        lot_size = int(strategy.get("lot_size", 65))
        lots = int(strategy.get("lots", 1))
        costs = costs_from_settings(settings)
        days = _split_by_day(bars_by_day)
        calendar = calendar or (market.calendar if market is not None else (pricer.calendar if pricer else TradingCalendar()))
        pricer = pricer or StraddlePricer(calendar=calendar)
        selection = expiry_selection(settings, default=calendar.selection)

        records = []
        for d in sorted(days):
            f = days[d]
            if len(f) == 0:
                continue
            ts = pd.to_datetime(f["timestamp"])
            ts = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
            start_minute = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)).to_numpy().astype(np.int64)
            bar_minutes = int(np.min(np.diff(start_minute))) if len(start_minute) > 1 else 1
            close_minute = start_minute + max(bar_minutes, 1)
            close = f["close"].to_numpy(dtype=np.float64)
            v = _vix_for(vix, d, market)
            expiry = calendar.select_expiry(d, selection)
            full = calendar.sessions_between(d, expiry)
            remaining = np.clip((SESSION_MINUTES - close_minute) / SESSION_MINUTES, 0.0, 1.0)
            dte = full + remaining
            premium = np.asarray(pricer.atm_premium(close, dte, v), dtype=np.float64)
            for i in range(len(close)):
                target = min(close_minute[i] + horizon, SESSION_MINUTES)
                j = int(np.searchsorted(close_minute, target, side="right")) - 1
                used = int(close_minute[j] - close_minute[i]) if j > i else 0
                realized = abs(close[j] - close[i]) if used > 0 else 0.0
                implied = float(premium[i])
                cf = cost_fraction(implied, lot_size, lots, costs)
                valid = used > 0 and implied > 0
                raw = float(np.clip(1.0 - realized / implied, -1.0, 1.0) - cf) if valid else np.nan
                records.append(
                    (
                        ts.iloc[i].to_pydatetime() + pd.Timedelta(minutes=max(bar_minutes, 1)),
                        d, realized, implied, used, cf, raw, np.nan, np.nan, valid,
                    )
                )
        frame = pd.DataFrame.from_records(records, columns=list(cls.COLUMNS))
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        series = cls(frame, horizon, baseline_days)
        series._compute_baseline()
        return series

    def _compute_baseline(self) -> None:
        f = self.frame
        dates = sorted(set(f["date"]))
        day_means: dict[date, float] = {}
        baseline = np.full(len(f), np.nan)
        for k, d in enumerate(dates):
            mask = (f["date"] == d).to_numpy()
            prev = [day_means[x] for x in dates[max(0, k - self.baseline_days) : k] if np.isfinite(day_means[x])]
            raw = f["reward_raw"].to_numpy()[mask]
            if prev:
                baseline[mask] = float(np.mean(prev))
            else:
                # First day: expanding mean of the same day's earlier observations (past only).
                acc = 0.0
                cnt = 0
                idx = np.flatnonzero(mask)
                for j, r in zip(idx, raw, strict=False):
                    baseline[j] = acc / cnt if cnt else 0.0
                    if np.isfinite(r):
                        acc += r
                        cnt += 1
            finite = raw[np.isfinite(raw)]
            day_means[d] = float(np.mean(finite)) if finite.size else np.nan
        f["baseline"] = baseline
        f["reward"] = f["reward_raw"] - f["baseline"]

    # -- lookup ----------------------------------------------------------------

    def __len__(self) -> int:
        return int(len(self.frame))

    def index_for(self, key) -> int:
        if isinstance(key, (int, np.integer)):
            i = int(key)
            if i < 0 or i >= len(self.frame):
                raise KeyError(f"observation index {i} out of range")
            return i
        if isinstance(key, str):
            key = pd.Timestamp(key)
        if isinstance(key, datetime) and not isinstance(key, pd.Timestamp):
            key = pd.Timestamp(key)
        if isinstance(key, pd.Timestamp):
            if key.tzinfo is None:
                key = key.tz_localize(IST)
            else:
                key = key.tz_convert(IST)
            hit = self._index.get(key)
            if hit is None:
                raise KeyError(f"no observation at {key}")
            return hit
        raise TypeError(f"unsupported key {type(key).__name__}")

    def raw_at(self, key) -> float:
        return float(self.frame["reward_raw"].iloc[self.index_for(key)])

    def baseline_at(self, key) -> float:
        return float(self.frame["baseline"].iloc[self.index_for(key)])

    def reward_for(self, key, trade=None) -> float:
        """Advantage at `key`; with `trade` the realized P&L over the stop distance replaces the counterfactual."""
        i = self.index_for(key)
        baseline = float(self.frame["baseline"].iloc[i])
        if trade is not None:
            if isinstance(trade, Mapping):
                pnl = float(trade.get("pnl_inr", trade.get("net_inr", 0.0)))
                stop = float(trade.get("stop_distance_inr", 0.0))
            elif hasattr(trade, "pnl_inr") or hasattr(trade, "net_inr"):
                pnl = float(getattr(trade, "pnl_inr", getattr(trade, "net_inr", 0.0)))
                stop = float(getattr(trade, "stop_distance_inr", 0.0))
            else:
                pnl, stop = (float(x) for x in trade)
            raw = float(np.clip(pnl / stop, -1.0, 1.0)) if stop > 0 else 0.0
            return raw - (baseline if np.isfinite(baseline) else 0.0)
        raw = float(self.frame["reward_raw"].iloc[i])
        if not np.isfinite(raw):
            return 0.0
        return raw - (baseline if np.isfinite(baseline) else 0.0)

    def __call__(self, key, trade=None) -> float:
        return self.reward_for(key, trade)

    def as_frame(self) -> pd.DataFrame:
        return self.frame.copy()


_DEFAULT: RewardSeries | None = None


def default_series(settings: dict | None = None, interval: str = "1m") -> RewardSeries:
    """A series over the whole local index history (built once, cached in the process)."""
    global _DEFAULT
    if _DEFAULT is None:
        from openfly.experiments.data import MarketData

        market = MarketData()
        pricer = StraddlePricer(calendar=market.calendar)
        _DEFAULT = RewardSeries.build(market.bars(interval), pricer, settings, market=market)
    return _DEFAULT


def reward_for(key, trade=None, series: RewardSeries | None = None, settings: dict | None = None) -> float:
    """Reward (advantage) for the observation at `key`; see RewardSeries.reward_for."""
    return (series or default_series(settings)).reward_for(key, trade)


__all__ = ["BASELINE_DAYS", "RewardSeries", "default_series", "reward_for"]
