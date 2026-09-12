"""Bar access for experiments: market store first, parquet files second.

`openfly.market.store.BarStore` (DuckDB at PATHS.market_db) is imported
lazily; when the module or the database is absent the loader reads the
parquet files under data/history written by `openfly.config.history_path`.
Nothing here fetches from the broker: a missing date is reported, never
filled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from openfly.config import PATHS, Paths, history_path
from openfly.experiments.sessions import IST, TradingCalendar
from openfly.interfaces import Bar

BAR_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "oi")
_INTERVAL_RE = re.compile(r"^(\d+)(m|h|D)$")


class MissingData(RuntimeError):
    """Raised when a requested date or series is not in the local store."""


def interval_minutes(interval: str) -> int:
    m = _INTERVAL_RE.match(str(interval))
    if not m:
        raise ValueError(f"unsupported interval {interval!r}")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return n
    if unit == "h":
        return 60 * n
    raise ValueError("daily intervals have no minute length")


def _bar_store(paths: Paths = PATHS):
    try:
        from openfly.market.store import BarStore
    except Exception:  # ImportError or a half-built module
        return None
    try:
        if not Path(paths.market_db).exists():
            return None
        return BarStore()
    except Exception:
        return None


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}).astype(
            {"timestamp": "datetime64[us, Asia/Kolkata]"}
        )
    out = df.copy()
    if "timestamp" not in out.columns:
        for alt in ("ts", "time", "datetime"):
            if alt in out.columns:
                out = out.rename(columns={alt: "timestamp"})
                break
    ts = pd.to_datetime(out["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(IST)
    else:
        ts = ts.dt.tz_convert(IST)
    out["timestamp"] = ts
    for c in ("open", "high", "low", "close", "volume", "oi"):
        if c not in out.columns:
            out[c] = 0.0
        out[c] = out[c].astype("float64")
    out = out[list(BAR_COLUMNS)].sort_values("timestamp").drop_duplicates("timestamp")
    return out.reset_index(drop=True)


def load_bars(
    exchange: str,
    symbol: str,
    interval: str,
    start: date | None = None,
    end: date | None = None,
    store=None,
    paths: Paths = PATHS,
) -> pd.DataFrame:
    """Bars for one symbol from the store or the parquet cache. Empty frame when absent."""
    if store is None:
        store = _bar_store(paths)
    if store is not None:
        try:
            frame = store.bars(exchange, symbol, interval, start, end)
            if frame is not None and len(frame):
                return _normalize(pd.DataFrame(frame))
        except Exception:
            pass
    path = history_path(exchange, symbol, interval, paths)
    if not path.exists():
        return _normalize(None)
    frame = _normalize(pd.read_parquet(path))
    if start is not None:
        frame = frame[frame["timestamp"].dt.date >= start]
    if end is not None:
        frame = frame[frame["timestamp"].dt.date <= end]
    return frame.reset_index(drop=True)


def resample(df1m: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Aggregate 1 minute bars to `interval` aligned to 09:15 (bar start labels)."""
    minutes = interval_minutes(interval)
    if minutes == 1:
        return df1m.reset_index(drop=True)
    if len(df1m) == 0:
        return df1m
    f = df1m.copy()
    key = f["timestamp"].dt.floor(f"{minutes}min")
    g = f.groupby(key, sort=True)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "oi": g["oi"].last(),
        }
    )
    out.index.name = "timestamp"
    return out.reset_index()


def to_bars(df: pd.DataFrame) -> list[Bar]:
    ts = df["timestamp"].dt.to_pydatetime() if len(df) else []
    return [
        Bar(timestamp=t, open=float(o), high=float(h), low=float(lo), close=float(c), volume=float(v))
        for t, o, h, lo, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"], strict=False)
    ]


def parse_option_symbol(symbol: str) -> dict:
    """NIFTY15SEP2623400CE -> underlying, expiry (date), strike, option_type."""
    m = re.match(r"^([A-Z]+?)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$", symbol.strip().upper())
    if not m:
        raise ValueError(f"not an option symbol: {symbol}")
    expiry = datetime.strptime(m.group(2), "%d%b%y").date()
    return {
        "underlying": m.group(1),
        "expiry": expiry,
        "strike": float(m.group(3)),
        "option_type": m.group(4),
        "symbol": symbol.strip().upper(),
    }


def option_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    strike_txt = f"{int(round(strike))}" if float(strike).is_integer() else f"{strike:g}"
    return f"{underlying}{expiry.strftime('%d%b%y').upper()}{strike_txt}{option_type}"


def list_option_symbols(exchange: str = "NFO", paths: Paths = PATHS, store=None) -> list[str]:
    """Option contracts with cached bars (parquet directories, plus store symbols if listable)."""
    names: set[str] = set()
    root = paths.history / exchange
    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and (child / "1m.parquet").exists():
                try:
                    parse_option_symbol(child.name)
                    names.add(child.name)
                except ValueError:
                    continue
    if store is None:
        store = _bar_store(paths)
    if store is not None:
        for attr in ("symbols", "available_symbols"):
            fn = getattr(store, attr, None)
            if callable(fn):
                try:
                    for s in fn(exchange):
                        try:
                            parse_option_symbol(str(s))
                            names.add(str(s))
                        except ValueError:
                            continue
                except Exception:
                    pass
                break
    return sorted(names)


@dataclass
class DayView:
    """One session of 1 minute bars as arrays, aligned by row."""

    date: date
    timestamps: np.ndarray  # datetime64[ns] naive in IST wall time, bar starts
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    open: np.ndarray
    minute: np.ndarray  # minutes since 09:15 of each bar start

    @property
    def n(self) -> int:
        return int(len(self.close))

    def row_at_or_before(self, minute: int) -> int:
        """Last row whose bar start minute is <= `minute` (-1 if none)."""
        return int(np.searchsorted(self.minute, minute, side="right") - 1)

    def row_for_close_time(self, minute: int) -> int:
        """Row of the bar that completed at `minute` (start < minute), -1 if none."""
        return int(np.searchsorted(self.minute, minute, side="left") - 1)


class MarketData:
    """Index and VIX history with per-day views and a trading calendar."""

    def __init__(
        self,
        store=None,
        index_exchange: str = "NSE_INDEX",
        index_symbol: str = "NIFTY",
        vix_symbol: str = "INDIAVIX",
        paths: Paths = PATHS,
        frame_1m: pd.DataFrame | None = None,
        vix_daily: pd.DataFrame | None = None,
    ):
        self.paths = paths
        self.index_exchange = index_exchange
        self.index_symbol = index_symbol
        self.vix_symbol = vix_symbol
        # store=False means "no store at all" (tests); None means "find one if present".
        self.store = None if store is False else (store if store is not None else _bar_store(paths))
        self.index_1m = _normalize(frame_1m) if frame_1m is not None else load_bars(
            index_exchange, index_symbol, "1m", store=self.store, paths=paths
        )
        if len(self.index_1m) == 0:
            raise MissingData(f"no 1m bars for {index_exchange}:{index_symbol} in the store or data/history")
        self.vix_daily = _normalize(vix_daily) if vix_daily is not None else load_bars(
            index_exchange, vix_symbol, "D", store=self.store, paths=paths
        )
        dates = self.index_1m["timestamp"].dt.date.to_numpy()
        self._dates_per_row = dates
        self.dates: list[date] = sorted(set(dates.tolist()))
        self.calendar = TradingCalendar(self.dates)
        starts = np.searchsorted(dates, np.array(self.dates), side="left")
        ends = np.searchsorted(dates, np.array(self.dates), side="right")
        self._bounds = {d: (int(s), int(e)) for d, s, e in zip(self.dates, starts, ends, strict=False)}
        self._frames: dict[str, pd.DataFrame] = {"1m": self.index_1m}
        self._vix_dates = self.vix_daily["timestamp"].dt.date.to_numpy() if len(self.vix_daily) else np.array([])
        self._views: dict[date, DayView] = {}
        self._vix_cache: dict[tuple[date, int], tuple[Bar, ...]] = {}

    # -- dates ---------------------------------------------------------------

    def has_date(self, d: date) -> bool:
        return d in self._bounds

    def dates_between(self, start: date, end: date) -> list[date]:
        return [d for d in self.dates if start <= d <= end]

    def require_dates(self, start: date, end: date, limit: int | None = None) -> list[date]:
        found = self.dates_between(start, end)
        if not found:
            raise MissingData(f"no index bars between {start} and {end}; run the market fetch first")
        if limit:
            found = found[: int(limit)]
        return found

    # -- bars ----------------------------------------------------------------

    def bars(self, interval: str = "1m") -> pd.DataFrame:
        if interval not in self._frames:
            self._frames[interval] = resample(self.index_1m, interval)
        return self._frames[interval]

    def day_1m(self, d: date) -> pd.DataFrame:
        if d not in self._bounds:
            raise MissingData(f"no index bars for {d}")
        s, e = self._bounds[d]
        return self.index_1m.iloc[s:e]

    def day_view(self, d: date) -> DayView:
        view = self._views.get(d)
        if view is not None:
            return view
        f = self.day_1m(d)
        ts = f["timestamp"]
        naive = ts.dt.tz_localize(None).to_numpy()
        minute = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)).to_numpy().astype(np.int64)
        view = DayView(
            date=d,
            timestamps=naive,
            close=f["close"].to_numpy(dtype=np.float64),
            high=f["high"].to_numpy(dtype=np.float64),
            low=f["low"].to_numpy(dtype=np.float64),
            open=f["open"].to_numpy(dtype=np.float64),
            minute=minute,
        )
        if len(self._views) > 512:
            self._views.clear()
        self._views[d] = view
        return view

    def day_bars(self, d: date, interval: str = "1m") -> pd.DataFrame:
        f = self.bars(interval)
        dd = f["timestamp"].dt.date.to_numpy()
        s = int(np.searchsorted(dd, d, side="left"))
        e = int(np.searchsorted(dd, d, side="right"))
        return f.iloc[s:e]

    # -- VIX -----------------------------------------------------------------

    def vix_open(self, d: date) -> float:
        """VIX known at the open of `d`: that day's open, else the last prior close."""
        if len(self.vix_daily) == 0:
            return float("nan")
        i = int(np.searchsorted(self._vix_dates, d, side="left"))
        if i < len(self._vix_dates) and self._vix_dates[i] == d:
            value = float(self.vix_daily["open"].iloc[i])
            if np.isfinite(value) and value > 0:
                return value
            return float(self.vix_daily["close"].iloc[i - 1]) if i > 0 else float(self.vix_daily["close"].iloc[i])
        if i == 0:
            return float(self.vix_daily["close"].iloc[0])
        return float(self.vix_daily["close"].iloc[i - 1])

    def vix_open_series(self) -> pd.Series:
        """Series indexed by date with the VIX known at each session open."""
        if len(self.vix_daily) == 0:
            return pd.Series(dtype="float64")
        opens = self.vix_daily["open"].to_numpy(dtype=np.float64)
        closes = self.vix_daily["close"].to_numpy(dtype=np.float64)
        values = np.where(np.isfinite(opens) & (opens > 0), opens, np.r_[closes[:1], closes[:-1]])
        return pd.Series(values, index=pd.Index(self._vix_dates, name="date"))

    def vix_bars_before(self, d: date, n: int = 20) -> tuple[Bar, ...]:
        key = (d, int(n))
        hit = self._vix_cache.get(key)
        if hit is not None:
            return hit
        if len(self.vix_daily) == 0:
            return ()
        i = int(np.searchsorted(self._vix_dates, d, side="left"))
        lo = max(0, i - n)
        bars = tuple(to_bars(self.vix_daily.iloc[lo:i]))
        if len(self._vix_cache) > 1024:
            self._vix_cache.clear()
        self._vix_cache[key] = bars
        return bars


def load_market(store=None, paths: Paths = PATHS) -> MarketData:
    return MarketData(store=store, paths=paths)
