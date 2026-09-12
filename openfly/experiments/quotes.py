"""Per-leg minute quotes for one trading day: recorded chain first, synthetic second.

    quotes = minute_quotes_for(date)              # MinuteQuotes
    sq = quotes(timestamp)                        # StraddleQuote at the ATM strike of that minute
    sq = quotes(timestamp, strike=23400.0)        # a specific strike
    quotes.pinned_strike = 23400.0                # hold that strike for every later call
    q = quotes.leg(timestamp, 23400.0, "CE")      # one leg
    q = leg_quote(date, timestamp, 23400.0, "PE") # module-level convenience with a per-date cache

Every quote carries `source`: "recorded" (from the stored option chain) or
"synthetic" (Black-Scholes from the index and INDIAVIX). Quote timestamps are
the close time of the last completed 1 minute bar at or before the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from openfly.config import PATHS, Paths
from openfly.experiments.data import DayView, MarketData, MissingData, _bar_store, option_symbol
from openfly.experiments.pricer import StraddlePricer, round_tick
from openfly.experiments.sessions import IST, SESSION_MINUTES, TradingCalendar, session_open
from openfly.interfaces import Quote, StraddleQuote

RECORDED = "recorded"
SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class SourcedQuote(Quote):
    source: str = SYNTHETIC


@dataclass(frozen=True)
class SourcedStraddleQuote(StraddleQuote):
    source: str = SYNTHETIC


def day_view_from_frame(trading_date: date, bars1m: pd.DataFrame) -> DayView:
    f = bars1m
    ts = pd.to_datetime(f["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    minute = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)).to_numpy().astype(np.int64)
    order = np.argsort(minute, kind="stable")
    return DayView(
        date=trading_date,
        timestamps=ts.dt.tz_localize(None).to_numpy()[order],
        close=f["close"].to_numpy(dtype=np.float64)[order],
        high=f["high"].to_numpy(dtype=np.float64)[order],
        low=f["low"].to_numpy(dtype=np.float64)[order],
        open=f["open"].to_numpy(dtype=np.float64)[order],
        minute=minute[order],
    )


def vix_value_for(vix_series, trading_date: date) -> float:
    """A float, or the last value of a Series (indexed by date or timestamp) at or before the date."""
    if vix_series is None:
        return float("nan")
    if isinstance(vix_series, (int, float, np.floating)):
        return float(vix_series)
    s = pd.Series(vix_series).dropna()
    if len(s) == 0:
        return float("nan")
    idx = s.index
    if isinstance(idx, pd.DatetimeIndex):
        keys = idx.tz_convert(IST).date if idx.tz is not None else idx.date
    else:
        keys = np.array([k.date() if isinstance(k, datetime) else k for k in idx])
    keys = np.asarray(keys)
    order = np.argsort(keys, kind="stable")
    keys = keys[order]
    values = s.to_numpy(dtype=np.float64)[order]
    i = int(np.searchsorted(keys, trading_date, side="right")) - 1
    return float(values[max(i, 0)])


def minute_of(ts, trading_date: date) -> float:
    """Minutes since 09:15 of `trading_date` for a datetime, pandas Timestamp or numpy datetime."""
    if isinstance(ts, (int, np.integer)):
        return float(ts)
    if isinstance(ts, np.datetime64):
        ts = pd.Timestamp(ts)
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return (ts - session_open(trading_date)).total_seconds() / 60.0


class MinuteQuotes:
    """Quotes for every strike at every minute of one session."""

    def __init__(
        self,
        day: DayView,
        expiry: date,
        pricer: StraddlePricer,
        vix: float,
        calendar: TradingCalendar | None = None,
        recorded: dict[tuple[float, str], tuple[np.ndarray, np.ndarray]] | None = None,
        underlying: str = "NIFTY",
        exchange: str = "NFO",
        tick: float = 0.05,
        spread: float = 0.05,
    ):
        self.day = day
        self.date = day.date
        self.expiry = expiry
        self.pricer = pricer
        self.vix = float(vix)
        self.calendar = calendar or pricer.calendar
        self.underlying = underlying
        self.exchange = exchange
        self.tick = float(tick)
        self.spread = float(spread)
        self.pinned_strike: float | None = None
        self._recorded = recorded or {}
        self._synthetic: dict[tuple[float, str], np.ndarray] = {}
        close_ts = session_open(self.date) + timedelta(minutes=1)
        full_sessions = self.calendar.sessions_between(self.date, expiry)
        remaining = np.clip((SESSION_MINUTES - (day.minute + 1)) / SESSION_MINUTES, 0.0, 1.0)
        self.days_to_expiry = full_sessions + remaining  # at each bar's close
        self._close_epoch = (
            np.array([(close_ts + timedelta(minutes=int(m))).timestamp() for m in day.minute], dtype=np.float64)
        )
        self.atm_strikes = self.pricer.atm_strike(day.close)

    # -- geometry ------------------------------------------------------------

    @property
    def n(self) -> int:
        return self.day.n

    def row_for(self, ts) -> int:
        """Row of the last bar completed at or before `ts` (clamped to 0)."""
        if isinstance(ts, (int, np.integer)):
            return int(min(max(int(ts), 0), self.n - 1))
        minute = minute_of(ts, self.date)
        row = int(np.searchsorted(self.day.minute, minute, side="left")) - 1
        return int(min(max(row, 0), self.n - 1))

    def close_time(self, row: int) -> datetime:
        return session_open(self.date) + timedelta(minutes=int(self.day.minute[row]) + 1)

    def atm_strike(self, ts) -> float:
        return float(self.atm_strikes[self.row_for(ts)])

    # -- paths ---------------------------------------------------------------

    def synthetic_path(self, strike: float, option_type: str) -> np.ndarray:
        key = (float(strike), option_type)
        if key not in self._synthetic:
            call, put = self.pricer.legs(self.day.close, float(strike), self.days_to_expiry, self.vix)
            self._synthetic[(float(strike), "CE")] = round_tick(call, self.tick)
            self._synthetic[(float(strike), "PE")] = round_tick(put, self.tick)
        return self._synthetic[key]

    def leg_path(self, strike: float, option_type: str) -> tuple[np.ndarray, np.ndarray]:
        """(ltp per row, synthetic mask per row) for one leg."""
        synthetic = self.synthetic_path(strike, option_type)
        rec = self._recorded.get((float(strike), option_type))
        if rec is None:
            return synthetic, np.ones(self.n, dtype=bool)
        values, missing = rec
        ltp = np.where(missing, synthetic, values)
        return ltp, missing.copy()

    def straddle_path(self, strike: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        c, mc = self.leg_path(strike, "CE")
        p, mp = self.leg_path(strike, "PE")
        return c, p, mc | mp

    @property
    def has_recorded(self) -> bool:
        return bool(self._recorded)

    @property
    def synthetic_fraction(self) -> float:
        """Fraction of minutes whose ATM legs come from the synthetic model."""
        if not self._recorded:
            return 1.0
        synthetic = 0
        for row in range(self.n):
            k = float(self.atm_strikes[row])
            for t in ("CE", "PE"):
                rec = self._recorded.get((k, t))
                if rec is None or rec[1][row]:
                    synthetic += 1
        return synthetic / float(2 * self.n)

    @property
    def source(self) -> str:
        frac = self.synthetic_fraction
        if frac >= 1.0:
            return SYNTHETIC
        if frac <= 0.0:
            return RECORDED
        return "mixed"

    # -- quotes --------------------------------------------------------------

    def leg_at_row(self, row: int, strike: float, option_type: str) -> SourcedQuote:
        ltp, mask = self.leg_path(strike, option_type)
        value = float(ltp[row])
        return SourcedQuote(
            symbol=option_symbol(self.underlying, self.expiry, strike, option_type),
            exchange=self.exchange,
            ltp=value,
            bid=max(0.0, round(value - self.spread, 2)),
            ask=round(value + self.spread, 2),
            timestamp=float(self._close_epoch[row]),
            source=SYNTHETIC if mask[row] else RECORDED,
        )

    def leg(self, ts, strike: float, option_type: str) -> SourcedQuote:
        return self.leg_at_row(self.row_for(ts), float(strike), option_type)

    def straddle_at_row(self, row: int, strike: float | None = None) -> SourcedStraddleQuote:
        if strike is None:
            strike = self.pinned_strike if self.pinned_strike is not None else float(self.atm_strikes[row])
        call = self.leg_at_row(row, float(strike), "CE")
        put = self.leg_at_row(row, float(strike), "PE")
        source = RECORDED if call.source == RECORDED and put.source == RECORDED else SYNTHETIC
        return SourcedStraddleQuote(call=call, put=put, strike=float(strike), expiry=self.expiry, source=source)

    def __call__(self, ts, strike: float | None = None) -> SourcedStraddleQuote:
        return self.straddle_at_row(self.row_for(ts), strike)


# ---------------------------------------------------------------------------
# Recorded chains from the market store
# ---------------------------------------------------------------------------


def _recorded_legs(chain: pd.DataFrame, day: DayView, expiry: date) -> dict[tuple[float, str], tuple[np.ndarray, np.ndarray]]:
    if chain is None or len(chain) == 0:
        return {}
    f = pd.DataFrame(chain)
    if "expiry" in f.columns:
        exp = pd.to_datetime(f["expiry"]).dt.date
        f = f[exp == expiry]
    if len(f) == 0:
        return {}
    ts = pd.to_datetime(f["ts"] if "ts" in f.columns else f["timestamp"])
    ts = ts.dt.tz_localize(IST) if ts.dt.tz is None else ts.dt.tz_convert(IST)
    minute = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)).to_numpy().astype(np.int64)
    rows = np.searchsorted(day.minute, minute, side="left")
    valid = (rows < day.n) & (rows >= 0)
    valid &= day.minute[np.clip(rows, 0, day.n - 1)] == minute
    f = f.assign(_row=rows)[valid]
    out: dict[tuple[float, str], tuple[np.ndarray, np.ndarray]] = {}
    for (strike, otype), g in f.groupby(["strike", "option_type"], sort=False):
        values = np.full(day.n, np.nan)
        values[g["_row"].to_numpy()] = g["close"].to_numpy(dtype=np.float64)
        # forward fill inside the day; rows before the first print stay synthetic
        filled = pd.Series(values).ffill().to_numpy()
        missing = ~np.isfinite(filled)
        out[(float(strike), str(otype))] = (np.where(missing, 0.0, filled), missing)
    return out


def _chain_for(store, trading_date: date, expiry: date | None):
    if store is None:
        return None
    fn = getattr(store, "chain", None)
    if not callable(fn):
        return None
    try:
        return fn(trading_date, expiry) if expiry is not None else fn(trading_date)
    except TypeError:
        try:
            return fn(trading_date)
        except Exception:
            return None
    except Exception:
        return None


def minute_quotes_for(
    trading_date: date,
    *,
    market: MarketData | None = None,
    store=None,
    pricer: StraddlePricer | None = None,
    settings: dict | None = None,
    calendar: TradingCalendar | None = None,
    prefer_recorded: bool = True,
    expiry: date | None = None,
    paths: Paths = PATHS,
) -> MinuteQuotes:
    """Quotes for `trading_date`: real chain legs when stored, synthetic otherwise."""
    market = market or MarketData(store=store, paths=paths)
    if not market.has_date(trading_date):
        raise MissingData(f"no index bars for {trading_date}")
    calendar = calendar or market.calendar
    strategy = (settings or {}).get("strategy", {}) if isinstance(settings, dict) else {}
    pricer = pricer or StraddlePricer(
        strike_step=float(strategy.get("strike_step", 50)), calendar=calendar, paths=paths
    )
    underlying = str(strategy.get("underlying", "NIFTY"))
    day = market.day_view(trading_date)
    vix = market.vix_open(trading_date)
    recorded = None
    if prefer_recorded:
        st = store if store is not None else (market.store if market.store is not None else _bar_store(paths))
        chain = _chain_for(st, trading_date, expiry)
        if chain is not None and len(chain):
            if expiry is None and "expiry" in pd.DataFrame(chain).columns:
                exps = sorted({e for e in pd.to_datetime(pd.DataFrame(chain)["expiry"]).dt.date if e >= trading_date})
                if exps:
                    expiry = exps[0]
            recorded = _recorded_legs(chain, day, expiry or calendar.next_expiry(trading_date))
    if expiry is None:
        expiry = calendar.next_expiry(trading_date)
    return MinuteQuotes(
        day,
        expiry,
        pricer,
        vix,
        calendar,
        recorded=recorded or None,
        underlying=underlying,
        exchange=str(strategy.get("options_exchange", "NFO")),
    )


@lru_cache(maxsize=64)
def _cached_quotes(trading_date: date, prefer_recorded: bool) -> MinuteQuotes:
    return minute_quotes_for(trading_date, prefer_recorded=prefer_recorded)


def leg_quote(trading_date: date, ts, strike: float, option_type: str, prefer_recorded: bool = True) -> SourcedQuote:
    """One leg's quote at `ts` for any strike (recorded when stored, else synthetic)."""
    return _cached_quotes(trading_date, prefer_recorded).leg(ts, float(strike), option_type.upper())


def straddle_quote(trading_date: date, ts, strike: float | None = None, prefer_recorded: bool = True) -> SourcedStraddleQuote:
    return _cached_quotes(trading_date, prefer_recorded)(ts, strike)


__all__ = [
    "RECORDED",
    "SYNTHETIC",
    "MinuteQuotes",
    "SourcedQuote",
    "SourcedStraddleQuote",
    "day_view_from_frame",
    "leg_quote",
    "minute_quotes_for",
    "straddle_quote",
    "vix_value_for",
]
