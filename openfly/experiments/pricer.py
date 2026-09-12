"""Synthetic ATM straddle pricer.

Black-Scholes (rate 0, no dividends) call and put at the ATM strike (index
rounded to the strike step) with IV = (INDIAVIX / 100) x calibration factor
and time to expiry counted in trading time: sessions to the weekly expiry
including the fraction of the current session, over 252 sessions a year.

`calibrate()` fits the factor against the listed contracts in the cache and
stores it in data/features/calibration.json; `StraddlePricer()` picks that
file up by default.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

from openfly.config import PATHS, Paths
from openfly.experiments.sessions import (
    IST,
    SESSION_MINUTES,
    TradingCalendar,
    years_to_expiry,
)

DEFAULT_FACTOR = 1.0  # replaced by data/features/calibration.json when present


def calibration_path(paths: Paths = PATHS) -> Path:
    return paths.features / "calibration.json"


def load_calibration(path: str | Path | None = None, paths: Paths = PATHS) -> dict | None:
    p = Path(path) if path else calibration_path(paths)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bs_prices(S, K, T_years, sigma):
    """Vectorized Black-Scholes call and put (rate 0). Degenerate inputs give intrinsic value."""
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T_years, dtype=np.float64)
    sig = np.asarray(sigma, dtype=np.float64)
    S, K, T, sig = np.broadcast_arrays(S, K, T, sig)
    shape = S.shape
    S, K, T, sig = (np.ravel(a).astype(np.float64) for a in (S, K, T, sig))
    call = np.maximum(S - K, 0.0)
    put = np.maximum(K - S, 0.0)
    ok = (T > 0) & (sig > 0) & (S > 0) & (K > 0)
    if np.any(ok):
        s, k, t, v = S[ok], K[ok], T[ok], sig[ok]
        vs = v * np.sqrt(t)
        d1 = (np.log(s / k) + 0.5 * vs * vs) / vs
        d2 = d1 - vs
        c = s * ndtr(d1) - k * ndtr(d2)
        call[ok] = c
        put[ok] = c - s + k
    return call.reshape(shape), put.reshape(shape)


def round_tick(x, tick: float = 0.05):
    return np.round(np.asarray(x, dtype=np.float64) / tick) * tick


class StraddlePricer:
    """ATM straddle prices from index level, INDIAVIX and trading time to expiry."""

    def __init__(
        self,
        factor: float | None = None,
        strike_step: float = 50.0,
        calendar: TradingCalendar | None = None,
        paths: Paths = PATHS,
    ):
        self.calibration = load_calibration(paths=paths) if factor is None else None
        if factor is None:
            factor = float(self.calibration["factor"]) if self.calibration else DEFAULT_FACTOR
        self.factor = float(factor)
        self.strike_step = float(strike_step)
        self.calendar = calendar or TradingCalendar()

    def params(self) -> dict:
        return {"factor": self.factor, "strike_step": self.strike_step, "day_count": "trading/252"}

    def iv(self, vix) -> np.ndarray:
        return np.asarray(vix, dtype=np.float64) / 100.0 * self.factor

    def atm_strike(self, S) -> np.ndarray | float:
        out = np.round(np.asarray(S, dtype=np.float64) / self.strike_step) * self.strike_step
        return float(out) if out.ndim == 0 else out

    def legs(self, S, K, days_to_expiry, vix):
        """Call and put prices for `days_to_expiry` trading days (vectorized)."""
        days = np.asarray(days_to_expiry, dtype=np.float64)
        T = np.maximum(days * SESSION_MINUTES, 1.0) / (SESSION_MINUTES * 252.0)
        return bs_prices(S, K, T, self.iv(vix))

    def straddle(self, S, K, days_to_expiry, vix):
        c, p = self.legs(S, K, days_to_expiry, vix)
        return c + p

    def atm_premium(self, S, days_to_expiry, vix):
        return self.straddle(S, self.atm_strike(S), days_to_expiry, vix)

    def premium_at(self, ts: datetime, S: float, vix: float, expiry: date | None = None) -> float:
        days = self.calendar.days_to_expiry(ts, expiry)
        return float(self.atm_premium(S, days, vix))


# ---------------------------------------------------------------------------
# Calibration against listed contracts
# ---------------------------------------------------------------------------


def _calibration_rows(market, store, symbols: list[str], moneyness_pct: float) -> pd.DataFrame:
    from openfly.experiments.data import load_bars, parse_option_symbol

    frames = []
    index = market.index_1m[["timestamp", "close"]].rename(columns={"close": "spot"})
    for symbol in symbols:
        meta = parse_option_symbol(symbol)
        bars = load_bars("NFO", symbol, "1m", store=store, paths=market.paths)
        if len(bars) == 0:
            continue
        f = bars[["timestamp", "close", "volume"]].merge(index, on="timestamp", how="inner")
        if len(f) == 0:
            continue
        if (f["volume"] > 0).any():
            f = f[f["volume"] > 0]
        minute = (f["timestamp"].dt.hour * 60 + f["timestamp"].dt.minute) - (9 * 60 + 15)
        f = f[(minute >= 0) & (minute < SESSION_MINUTES)]
        f = f[(np.abs(f["spot"] - meta["strike"]) / f["spot"] * 100.0) <= moneyness_pct]
        if len(f) == 0:
            continue
        f = f.copy()
        f["symbol"] = symbol
        f["strike"] = meta["strike"]
        f["option_type"] = meta["option_type"]
        f["expiry"] = meta["expiry"]
        dates = f["timestamp"].dt.date
        f["vix"] = [market.vix_open(d) for d in dates]
        close_ts = f["timestamp"] + pd.Timedelta(minutes=1)
        f["days"] = [
            market.calendar.days_to_expiry(t.to_pydatetime(), meta["expiry"]) for t in close_ts
        ]
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def calibrate(
    cache=None,
    *,
    symbols: list[str] | None = None,
    moneyness_pct: float = 1.0,
    write: bool = True,
    path: str | Path | None = None,
    paths: Paths = PATHS,
) -> dict:
    """Fit the IV factor so synthetic prices match the recorded listed contracts.

    `cache` may be None, a MarketData, or a store with `bars(...)`. The result
    (factor, fit statistics, contracts used) is written to
    data/features/calibration.json unless `write` is False.
    """
    from openfly.experiments.data import MarketData, list_option_symbols

    store = None
    market = None
    if cache is not None and hasattr(cache, "index_1m"):
        market = cache
        store = getattr(cache, "store", None)
    elif cache is not None and callable(getattr(cache, "bars", None)):
        store = cache
    if market is None:
        market = MarketData(store=store, paths=paths)
    if symbols is None:
        symbols = list_option_symbols("NFO", paths=paths, store=store)
    rows = _calibration_rows(market, store, symbols, moneyness_pct)
    if len(rows) == 0:
        raise RuntimeError("no listed option bars overlap the index history; nothing to calibrate")

    S = rows["spot"].to_numpy(dtype=np.float64)
    K = rows["strike"].to_numpy(dtype=np.float64)
    T = np.array([years_to_expiry(d) for d in rows["days"].to_numpy()], dtype=np.float64)
    vix = rows["vix"].to_numpy(dtype=np.float64) / 100.0
    rec = rows["close"].to_numpy(dtype=np.float64)
    is_call = (rows["option_type"] == "CE").to_numpy()

    def model(factor: float) -> np.ndarray:
        c, p = bs_prices(S, K, T, vix * factor)
        return np.where(is_call, c, p)

    def objective(factor: float) -> float:
        m = model(factor)
        return float(np.mean(((m - rec) / np.maximum(rec, 1.0)) ** 2))

    from scipy.optimize import minimize_scalar

    res = minimize_scalar(objective, bounds=(0.2, 3.0), method="bounded", options={"xatol": 1e-4})
    factor = float(res.x)
    fitted = model(factor)
    err = fitted - rec
    per_day = []
    for d, g in rows.assign(fitted=fitted).groupby(rows["timestamp"].dt.date):
        per_day.append(
            {
                "date": d.isoformat(),
                "rows": int(len(g)),
                "recorded_mean": float(g["close"].mean()),
                "fitted_mean": float(g["fitted"].mean()),
                "days_to_expiry_mean": float(g["days"].mean()),
            }
        )
    result = {
        "factor": factor,
        "day_count": "trading sessions over 252, fraction of the current session included",
        "fitted_at": datetime.now(IST).isoformat(),
        "contracts": sorted(set(rows["symbol"])),
        "rows": int(len(rows)),
        "moneyness_pct": moneyness_pct,
        "rmse_points": float(np.sqrt(np.mean(err**2))),
        "mean_abs_pct_error": float(np.mean(np.abs(err) / np.maximum(rec, 1.0)) * 100.0),
        "objective": float(res.fun),
        "per_day": per_day,
    }
    if write:
        p = Path(path) if path else calibration_path(paths)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["path"] = str(p)
    return result


# ---------------------------------------------------------------------------
# Minute quotes and move helpers
# ---------------------------------------------------------------------------


def synthetic_minute_quotes(
    trading_date: date,
    bars1m: pd.DataFrame,
    vix_series,
    expiry: date | None = None,
    pricer: StraddlePricer | None = None,
    calendar: TradingCalendar | None = None,
    underlying: str = "NIFTY",
):
    """Callable(timestamp, strike=None) -> StraddleQuote for one day, fully synthetic.

    `bars1m` holds that day's 1 minute index bars (timestamp, open, high,
    low, close); `vix_series` is a float, or a Series indexed by date (or by
    timestamp) giving the VIX known at the session open. The returned object
    is a `MinuteQuotes`; see openfly.experiments.quotes for its full surface
    (per-leg quotes, `pinned_strike`, `leg_path`).
    """
    from openfly.experiments.quotes import MinuteQuotes, day_view_from_frame, vix_value_for

    calendar = calendar or (pricer.calendar if pricer else TradingCalendar())
    pricer = pricer or StraddlePricer(calendar=calendar)
    view = day_view_from_frame(trading_date, bars1m)
    vix = vix_value_for(vix_series, trading_date)
    if expiry is None:
        expiry = calendar.next_expiry(trading_date)
    return MinuteQuotes(view, expiry, pricer, vix, calendar, recorded=None, underlying=underlying)


def implied_move_points(premium, horizon_minutes: float | None = None, minutes_to_expiry: float | None = None):
    """The combined premium is the implied move to expiry; with a horizon it is
    scaled by sqrt(horizon / trading minutes to expiry), capped at the premium."""
    premium = np.asarray(premium, dtype=np.float64)
    if horizon_minutes is None or minutes_to_expiry is None:
        return float(premium) if premium.ndim == 0 else premium
    mte = np.maximum(np.asarray(minutes_to_expiry, dtype=np.float64), 1.0)
    scale = np.sqrt(np.minimum(1.0, np.asarray(horizon_minutes, dtype=np.float64) / mte))
    out = premium * scale
    return float(out) if out.ndim == 0 else out


def realized_move_points(bars, horizon: int) -> float:
    """Absolute close-to-close move from the first bar over `horizon` bars (clipped to the data)."""
    if isinstance(bars, pd.DataFrame):
        closes = bars["close"].to_numpy(dtype=np.float64)
    elif len(bars) and hasattr(bars[0], "close"):
        closes = np.array([b.close for b in bars], dtype=np.float64)
    else:
        closes = np.asarray(bars, dtype=np.float64)
    if closes.size == 0:
        return 0.0
    end = min(int(horizon), closes.size - 1)
    return float(abs(closes[end] - closes[0]))


def straddle_value(S: float, K: float, days_to_expiry: float, vix: float, factor: float | None = None) -> float:
    """Convenience: one straddle price without building a pricer."""
    return float(StraddlePricer(factor=factor).straddle(S, K, days_to_expiry, vix))


__all__ = [
    "DEFAULT_FACTOR",
    "StraddlePricer",
    "bs_prices",
    "calibrate",
    "calibration_path",
    "implied_move_points",
    "load_calibration",
    "realized_move_points",
    "round_tick",
    "straddle_value",
    "synthetic_minute_quotes",
]

