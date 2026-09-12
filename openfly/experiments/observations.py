"""Build MarketObservation objects from the index history, one per completed bar.

The observation interval (settings.neural.replay_interval, default "1m")
decides the bar size the encoders see: one observation per completed bar of
that interval, timestamp at the bar close, trailing window of `lookback`
bars (60 needed by the encoders plus warm-up for ATR and return std), INDIAVIX
known at the session open, the synthetic ATM straddle premium at that minute,
trading days to the selected expiry (settings.strategy.expiry_selection,
monthly by default) and minutes since open. Position fields are flat unless
the caller overrides them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import numpy as np

from openfly.experiments.data import MarketData, interval_minutes, to_bars
from openfly.experiments.pricer import StraddlePricer
from openfly.experiments.sessions import SESSION_MINUTES, expiry_selection, session_open
from openfly.interfaces import Bar, MarketObservation

LOOKBACK_BARS = 120


def replay_interval(settings: dict | None, default: str = "1m") -> str:
    neural = (settings or {}).get("neural", {}) if isinstance(settings, dict) else {}
    return str(neural.get("replay_interval", default) or default)


class ObservationBuilder:
    def __init__(
        self,
        market: MarketData,
        pricer: StraddlePricer | None = None,
        settings: dict | None = None,
        interval: str | None = None,
        lookback: int = LOOKBACK_BARS,
        vix_window: int = 20,
    ):
        self.market = market
        self.settings = settings or {}
        self.interval = interval or replay_interval(settings)
        self.minutes = interval_minutes(self.interval)
        self.lookback = int(lookback)
        self.vix_window = int(vix_window)
        self.calendar = market.calendar
        self.expiry_selection = expiry_selection(self.settings, default=self.calendar.selection)
        strategy = self.settings.get("strategy", {}) if isinstance(self.settings, dict) else {}
        self.pricer = pricer or StraddlePricer(
            strike_step=float(strategy.get("strike_step", 50)), calendar=self.calendar, paths=market.paths
        )
        frame = market.bars(self.interval)
        self.frame = frame
        ts = frame["timestamp"]
        self._dates = ts.dt.date.to_numpy()
        self._close = frame["close"].to_numpy(dtype=np.float64)
        self._start_minute = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)).to_numpy().astype(np.int64)
        self._bars: list[Bar] = to_bars(frame)
        self._expiry: dict[date, date] = {}
        self._sessions: dict[date, int] = {}
        self._close_ts = [b.timestamp + timedelta(minutes=self.minutes) for b in self._bars]
        dates = sorted(set(self._dates.tolist()))
        self._bounds = {
            d: (int(s), int(e))
            for d, s, e in zip(
                dates,
                np.searchsorted(self._dates, np.array(dates), side="left"),
                np.searchsorted(self._dates, np.array(dates), side="right"),
                strict=False,
            )
        }

    # -- geometry ------------------------------------------------------------

    def rows_for_date(self, d: date) -> tuple[int, int]:
        if d not in self._bounds:
            raise KeyError(f"no bars for {d}")
        return self._bounds[d]

    def n_observations(self, d: date) -> int:
        s, e = self.rows_for_date(d)
        return e - s

    def close_minute(self, row: int) -> int:
        """Minutes since 09:15 at which the bar of `row` completed."""
        return int(self._start_minute[row] + self.minutes)

    def close_time(self, row: int) -> datetime:
        return self._close_ts[row]

    def expiry_for(self, d: date) -> date:
        hit = self._expiry.get(d)
        if hit is None:
            hit = self.calendar.select_expiry(d, self.expiry_selection)
            self._expiry[d] = hit
        return hit

    def _sessions_to_expiry(self, d: date) -> int:
        hit = self._sessions.get(d)
        if hit is None:
            hit = self.calendar.sessions_between(d, self.expiry_for(d))
            self._sessions[d] = hit
        return hit

    def days_to_expiry(self, row: int) -> float:
        d = self._dates[row]
        remaining = min(1.0, max(0.0, (SESSION_MINUTES - self.close_minute(row)) / SESSION_MINUTES))
        return float(self._sessions_to_expiry(d) + remaining)

    def premium(self, row: int, vix: float | None = None) -> float:
        d = self._dates[row]
        vix = self.market.vix_open(d) if vix is None else vix
        return float(self.pricer.atm_premium(self._close[row], self.days_to_expiry(row), vix))

    # -- observations --------------------------------------------------------

    def observation(self, row: int, position_lots: int = 0, entry_credit: float | None = None) -> MarketObservation:
        d = self._dates[row]
        lo = max(0, row - self.lookback + 1)
        bars = tuple(self._bars[lo : row + 1])
        vix = self.market.vix_open(d)
        ts = self.close_time(row)
        return MarketObservation(
            timestamp=ts,
            index_bars=bars,
            vix=float(vix) if np.isfinite(vix) else 0.0,
            vix_bars=self.market.vix_bars_before(d, self.vix_window),
            straddle_premium=self.premium(row, vix),
            entry_credit=entry_credit,
            days_to_expiry=self.days_to_expiry(row),
            minutes_since_open=self.close_minute(row),
            position_lots=int(position_lots),
        )

    def day_observations(self, d: date) -> list[MarketObservation]:
        s, e = self.rows_for_date(d)
        return [self.observation(r) for r in range(s, e)]

    def day_rows(self, d: date) -> range:
        s, e = self.rows_for_date(d)
        return range(s, e)

    def warmup_observation(self, d: date) -> MarketObservation:
        """An observation at 09:15 built from the bars strictly before the session (for warming the brain)."""
        s, _ = self.rows_for_date(d)
        if s == 0:
            first = self.observation(0)
            return replace(first, timestamp=session_open(d), minutes_since_open=0)
        prev = self.observation(s - 1)
        return replace(
            prev,
            timestamp=session_open(d),
            minutes_since_open=0,
            days_to_expiry=float(self._sessions_to_expiry(d) + 1.0),
            vix=float(self.market.vix_open(d)) if np.isfinite(self.market.vix_open(d)) else prev.vix,
            vix_bars=self.market.vix_bars_before(d, self.vix_window),
        )

    def with_position(self, obs: MarketObservation, position_lots: int, entry_credit: float | None) -> MarketObservation:
        return replace(obs, position_lots=int(position_lots), entry_credit=entry_credit)
