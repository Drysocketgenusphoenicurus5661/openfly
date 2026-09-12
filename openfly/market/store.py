"""DuckDB bar store: every bar ever fetched from the broker, plus coverage and
recorded option chains.

Tables (file ``PATHS.market_db``, ``data/market.duckdb``):

- ``bars(exchange, symbol, interval, ts TIMESTAMPTZ, open, high, low, close,
  volume, oi)`` with primary key (exchange, symbol, interval, ts).
- ``coverage(exchange, symbol, interval, start_date, end_date, fetched_at)``:
  every date range already requested from the broker. Gaps are computed from
  coverage, not from bar presence, because holidays and unlisted days have no
  bars and must not be requested again.
- ``chains(trading_date, expiry, strike, option_type, symbol, ts, open, high,
  low, close, volume, oi)`` with primary key (symbol, ts): 1 minute bars of
  the recorded option chain, one row per contract per minute.
- ``chain_days(trading_date, expiry, min_strike, max_strike, symbols, rows,
  status, fetched_at)``: what the recorder completed, so a re-run is a no-op.
- ``meta(key, value)``: import bookkeeping.

Timestamps round-trip as tz-aware Asia/Kolkata: the session ``TimeZone`` is
set on every connection, so ``TIMESTAMPTZ`` columns come back as
``datetime64[us, Asia/Kolkata]`` and ``ts::DATE`` is the IST calendar date.

DuckDB allows one writer process at a time, so every operation opens a short
lived connection under a file lock (``market.duckdb.lock``). Use the store as
a context manager to hold one connection across many calls.

Parquet files under ``data/history`` (layout ``openfly.config.history_path``)
are imported on first open and whenever they change; ``export_parquet`` writes
them back for sharing.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from filelock import FileLock

from openfly.config import PATHS
from openfly.market.client import HISTORY_COLUMNS, IST, _empty_history

logger = logging.getLogger("openfly.market.store")

CHAIN_COLUMNS = [
    "trading_date",
    "expiry",
    "strike",
    "option_type",
    "symbol",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
]

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS bars (
        exchange VARCHAR NOT NULL, symbol VARCHAR NOT NULL, interval VARCHAR NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, oi DOUBLE,
        PRIMARY KEY (exchange, symbol, interval, ts))
    """,
    """
    CREATE TABLE IF NOT EXISTS coverage (
        exchange VARCHAR NOT NULL, symbol VARCHAR NOT NULL, interval VARCHAR NOT NULL,
        start_date DATE NOT NULL, end_date DATE NOT NULL, fetched_at TIMESTAMPTZ NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS chains (
        trading_date DATE NOT NULL, expiry DATE NOT NULL, strike DOUBLE NOT NULL,
        option_type VARCHAR NOT NULL, symbol VARCHAR NOT NULL, ts TIMESTAMPTZ NOT NULL,
        open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, oi DOUBLE,
        PRIMARY KEY (symbol, ts))
    """,
    """
    CREATE TABLE IF NOT EXISTS chain_days (
        trading_date DATE NOT NULL, expiry DATE NOT NULL,
        min_strike DOUBLE, max_strike DOUBLE, symbols INTEGER, rows INTEGER,
        status VARCHAR, fetched_at TIMESTAMPTZ,
        PRIMARY KEY (trading_date, expiry))
    """,
    "CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR)",
)


def _to_date(value: date | datetime | str | pd.Timestamp) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def weekdays(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def group_consecutive(days: list[date], max_gap_days: int = 3) -> list[tuple[date, date]]:
    """Group sorted days into (first, last) ranges where the calendar gap is at most ``max_gap_days``."""
    ranges: list[tuple[date, date]] = []
    for day in sorted(days):
        if ranges and (day - ranges[-1][1]).days <= max_gap_days:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _only_weekends_between(a: date, b: date) -> bool:
    """True when every calendar day strictly between ``a`` and ``b`` is a Saturday or Sunday."""
    d = a + timedelta(days=1)
    while d < b:
        if d.weekday() < 5:
            return False
        d += timedelta(days=1)
    return True


def coalesce(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Merge overlapping, adjacent and weekend-separated ranges."""
    merged: list[tuple[date, date]] = []
    for start, end in sorted(ranges):
        if merged and (start <= merged[-1][1] + timedelta(days=1) or _only_weekends_between(merged[-1][1], start)):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def normalise_bars(df: pd.DataFrame | None) -> pd.DataFrame:
    """Coerce any bar frame (index or column timestamps) into the canonical layout."""
    if df is None or len(df) == 0:
        return _empty_history()
    frame = df.copy()
    if "timestamp" not in frame.columns:
        if "ts" in frame.columns:
            frame = frame.rename(columns={"ts": "timestamp"})
        else:
            frame = frame.reset_index()
            if "timestamp" not in frame.columns and "index" in frame.columns:
                frame = frame.rename(columns={"index": "timestamp"})
    stamps = pd.to_datetime(frame["timestamp"])
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize(IST)
    else:
        stamps = stamps.dt.tz_convert(IST)
    frame["timestamp"] = stamps.dt.as_unit("us")
    for column in ("open", "high", "low", "close", "volume", "oi"):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame = frame.sort_values("timestamp", kind="mergesort").drop_duplicates("timestamp", keep="last")
    return frame.reset_index(drop=True)[HISTORY_COLUMNS]


class BarStore:
    """DuckDB-backed bar, coverage and chain storage.

    Experiments call ``BarStore().bars(exchange, symbol, interval, start, end)``
    and ``BarStore().available_dates(exchange, symbol, interval)``; replays call
    ``chain(trading_date, expiry)`` and ``atm_path(trading_date, expiry)``.
    """

    def __init__(
        self,
        path: Path | str = PATHS.market_db,
        history_root: Path | str | None = None,
        import_parquet: bool = True,
        timezone: str = "Asia/Kolkata",
        lock_timeout: float = 120.0,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if history_root is None:
            history_root = PATHS.history if self.path == PATHS.market_db else self.path.parent / "history"
        self.history_root = Path(history_root)
        self.timezone = timezone
        self._file_lock = FileLock(str(self.path) + ".lock", timeout=lock_timeout)
        self._thread_lock = threading.RLock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._depth = 0
        with self._connect() as con:
            for statement in _SCHEMA:
                con.execute(statement)
        if import_parquet and self.history_root.exists():
            try:
                self.import_parquet()
            except Exception:  # noqa: BLE001 - a bad parquet must not block the store
                logger.exception("parquet import failed")

    # connections ---------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._thread_lock:
            if self._con is not None:
                yield self._con
                return
            with self._file_lock:
                con = duckdb.connect(str(self.path))
                try:
                    con.execute(f"SET TimeZone='{self.timezone}'")
                    yield con
                finally:
                    con.close()

    def __enter__(self) -> BarStore:
        self._thread_lock.acquire()
        self._depth += 1
        if self._con is None:
            self._file_lock.acquire()
            self._con = duckdb.connect(str(self.path))
            self._con.execute(f"SET TimeZone='{self.timezone}'")
        return self

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None
                self._file_lock.release()
        self._thread_lock.release()

    def close(self) -> None:
        with self._thread_lock:
            if self._con is not None:
                self._con.close()
                self._con = None
                self._depth = 0
                if self._file_lock.is_locked:
                    self._file_lock.release()

    # meta ----------------------------------------------------------------

    def _meta_get(self, con: duckdb.DuckDBPyConnection, key: str) -> str | None:
        row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _meta_set(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])

    # bars ----------------------------------------------------------------

    def upsert_bars(self, exchange: str, symbol: str, interval: str, frame: pd.DataFrame) -> int:
        """Insert or replace bars; returns the number of new timestamps stored."""
        data = normalise_bars(frame)
        if len(data) == 0:
            return 0
        with self._connect() as con:
            before = self._count_bars(con, exchange, symbol, interval)
            con.register("bars_in", data)
            try:
                con.execute(
                    "INSERT OR REPLACE INTO bars "
                    "SELECT ?, ?, ?, timestamp, open, high, low, close, volume, oi FROM bars_in",
                    [exchange, symbol, interval],
                )
            finally:
                con.unregister("bars_in")
            after = self._count_bars(con, exchange, symbol, interval)
        return int(after - before)

    @staticmethod
    def _count_bars(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, interval: str) -> int:
        row = con.execute(
            "SELECT count(*) FROM bars WHERE exchange = ? AND symbol = ? AND interval = ?",
            [exchange, symbol, interval],
        ).fetchone()
        return int(row[0]) if row else 0

    def count(self, exchange: str, symbol: str, interval: str) -> int:
        with self._connect() as con:
            return self._count_bars(con, exchange, symbol, interval)

    def bars(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> pd.DataFrame:
        """Bars for [start, end] (IST calendar dates, inclusive; None means unbounded).

        Columns: timestamp (tz-aware Asia/Kolkata), open, high, low, close, volume, oi.
        """
        sql = (
            "SELECT ts AS timestamp, open, high, low, close, volume, oi FROM bars "
            "WHERE exchange = ? AND symbol = ? AND interval = ?"
        )
        params: list[Any] = [exchange, symbol, interval]
        if start is not None:
            sql += " AND ts::DATE >= ?"
            params.append(_to_date(start))
        if end is not None:
            sql += " AND ts::DATE <= ?"
            params.append(_to_date(end))
        sql += " ORDER BY ts"
        with self._connect() as con:
            frame = con.execute(sql, params).df()
        return normalise_bars(frame)

    def day(self, exchange: str, symbol: str, interval: str, day: date | str) -> pd.DataFrame:
        d = _to_date(day)
        return self.bars(exchange, symbol, interval, d, d)

    def available_dates(self, exchange: str, symbol: str, interval: str) -> list[date]:
        """Sorted IST calendar dates that have at least one bar."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT ts::DATE AS d FROM bars "
                "WHERE exchange = ? AND symbol = ? AND interval = ? ORDER BY d",
                [exchange, symbol, interval],
            ).fetchall()
        return [_to_date(row[0]) for row in rows]

    def symbols(self) -> list[tuple[str, str, str]]:
        """(exchange, symbol, interval) triples present in ``bars``."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT exchange, symbol, interval FROM bars ORDER BY exchange, symbol, interval"
            ).fetchall()
        return [(str(a), str(b), str(c)) for a, b, c in rows]

    def summary(self) -> list[dict[str, Any]]:
        """Per series: bars, first and last date, coverage ranges."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT exchange, symbol, interval, count(*) AS bars, min(ts)::DATE AS first_date, "
                "max(ts)::DATE AS last_date FROM bars GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
            ).fetchall()
        out = []
        for exchange, symbol, interval, bars, first, last in rows:
            out.append(
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "interval": interval,
                    "bars": int(bars),
                    "first_date": _to_date(first).isoformat(),
                    "last_date": _to_date(last).isoformat(),
                    "coverage": [(a.isoformat(), b.isoformat()) for a, b in self.coverage(exchange, symbol, interval)],
                }
            )
        return out

    # coverage ------------------------------------------------------------

    def add_coverage(self, exchange: str, symbol: str, interval: str, start: date | str, end: date | str) -> None:
        """Record that [start, end] was requested from the broker (coalesced with existing ranges)."""
        start_d, end_d = _to_date(start), _to_date(end)
        if end_d < start_d:
            return
        with self._connect() as con:
            existing = self._coverage(con, exchange, symbol, interval)
            merged = coalesce(existing + [(start_d, end_d)])
            con.execute("BEGIN")
            try:
                con.execute(
                    "DELETE FROM coverage WHERE exchange = ? AND symbol = ? AND interval = ?",
                    [exchange, symbol, interval],
                )
                # Naive IST wall clock: DuckDB interprets it in the session TimeZone and
                # binding tz-aware datetimes would require pytz, which is not a dependency.
                now = datetime.now(IST).replace(tzinfo=None)
                for a, b in merged:
                    con.execute(
                        "INSERT INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
                        [exchange, symbol, interval, a, b, now],
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    @staticmethod
    def _coverage(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, interval: str) -> list[tuple[date, date]]:
        rows = con.execute(
            "SELECT start_date, end_date FROM coverage WHERE exchange = ? AND symbol = ? AND interval = ? "
            "ORDER BY start_date",
            [exchange, symbol, interval],
        ).fetchall()
        return coalesce([(_to_date(a), _to_date(b)) for a, b in rows])

    def coverage(self, exchange: str, symbol: str, interval: str) -> list[tuple[date, date]]:
        """Coalesced date ranges already requested from the broker."""
        with self._connect() as con:
            return self._coverage(con, exchange, symbol, interval)

    def is_covered(self, exchange: str, symbol: str, interval: str, day: date | str) -> bool:
        d = _to_date(day)
        return any(a <= d <= b for a, b in self.coverage(exchange, symbol, interval))

    def missing_ranges(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        start: date | str,
        end: date | str,
        today: date | None = None,
    ) -> list[tuple[date, date]]:
        """Weekday ranges inside [start, end] that coverage does not include.

        ``today`` (when inside the range) is always returned unless coverage
        already includes it, because the session may still be running; the
        history layer only records coverage for today after the close.
        """
        start_d, end_d = _to_date(start), _to_date(end)
        if end_d < start_d:
            return []
        covered = self.coverage(exchange, symbol, interval)
        wanted = [
            day
            for day in weekdays(start_d, end_d)
            if not any(a <= day <= b for a, b in covered)
        ]
        if today is not None and start_d <= today <= end_d and today.weekday() >= 5:
            if not any(a <= today <= b for a, b in covered) and today not in wanted:
                wanted.append(today)
        return group_consecutive(wanted)

    # parquet import and export -------------------------------------------

    def import_parquet(self, root: Path | str | None = None, force: bool = False) -> list[dict[str, Any]]:
        """Import every ``<exchange>/<symbol>/<interval>.parquet`` under ``root`` (new or changed files only)."""
        base = Path(root) if root is not None else self.history_root
        results: list[dict[str, Any]] = []
        if not base.exists():
            return results
        for file in sorted(base.glob("*/*/*.parquet")):
            exchange, symbol = file.parent.parent.name, file.parent.name
            interval = file.stem
            stat = file.stat()
            signature = json.dumps({"size": stat.st_size, "mtime": int(stat.st_mtime)})
            key = f"imported:{exchange}/{symbol}/{interval}"
            with self._connect() as con:
                if not force and self._meta_get(con, key) == signature:
                    continue
                frame = normalise_bars(pd.read_parquet(file))
                added = self.upsert_bars(exchange, symbol, interval, frame)
                if len(frame):
                    first = frame["timestamp"].iloc[0].date()
                    last = frame["timestamp"].iloc[-1].date()
                    self.add_coverage(exchange, symbol, interval, first, last)
                self._meta_set(con, key, signature)
            results.append(
                {
                    "file": str(file),
                    "exchange": exchange,
                    "symbol": symbol,
                    "interval": interval,
                    "rows": int(len(frame)),
                    "added": int(added),
                }
            )
            logger.info("imported %s: %d rows, %d new", file, len(frame), added)
        return results

    def export_parquet(
        self, exchange: str, symbol: str, interval: str, path: Path | str | None = None
    ) -> Path:
        """Write the series to parquet (default location: ``history_path`` under the history root)."""
        target = Path(path) if path is not None else self.history_root / exchange / symbol / f"{interval}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame = self.bars(exchange, symbol, interval)
        frame.to_parquet(target, index=False)
        with self._connect() as con:
            stat = target.stat()
            self._meta_set(
                con,
                f"imported:{exchange}/{symbol}/{interval}",
                json.dumps({"size": stat.st_size, "mtime": int(stat.st_mtime)}),
            )
        return target

    # chains --------------------------------------------------------------

    def upsert_chain(
        self,
        trading_date: date | str,
        expiry: date | str,
        strike: float,
        option_type: str,
        symbol: str,
        frame: pd.DataFrame,
    ) -> int:
        """Store one contract's bars for one trading date into ``chains``; returns new rows."""
        data = normalise_bars(frame)
        if len(data) == 0:
            return 0
        d, e = _to_date(trading_date), _to_date(expiry)
        with self._connect() as con:
            before = con.execute("SELECT count(*) FROM chains WHERE symbol = ?", [symbol]).fetchone()[0]
            con.register("chain_in", data)
            try:
                con.execute(
                    "INSERT OR REPLACE INTO chains "
                    "SELECT ?, ?, ?, ?, ?, timestamp, open, high, low, close, volume, oi FROM chain_in",
                    [d, e, float(strike), option_type.upper(), symbol],
                )
            finally:
                con.unregister("chain_in")
            after = con.execute("SELECT count(*) FROM chains WHERE symbol = ?", [symbol]).fetchone()[0]
        return int(after - before)

    def copy_bars_to_chain(
        self,
        trading_date: date | str,
        expiry: date | str,
        strike: float,
        option_type: str,
        symbol: str,
        exchange: str = "NFO",
        interval: str = "1m",
    ) -> int:
        """Copy a contract's stored ``bars`` of one date into ``chains`` (no broker call)."""
        d, e = _to_date(trading_date), _to_date(expiry)
        with self._connect() as con:
            before = con.execute("SELECT count(*) FROM chains WHERE symbol = ?", [symbol]).fetchone()[0]
            con.execute(
                "INSERT OR REPLACE INTO chains "
                "SELECT ?, ?, ?, ?, symbol, ts, open, high, low, close, volume, oi FROM bars "
                "WHERE exchange = ? AND symbol = ? AND interval = ? AND ts::DATE = ?",
                [d, e, float(strike), option_type.upper(), exchange, symbol, interval, d],
            )
            after = con.execute("SELECT count(*) FROM chains WHERE symbol = ?", [symbol]).fetchone()[0]
        return int(after - before)

    def chain(self, trading_date: date | str, expiry: date | str | None = None) -> pd.DataFrame:
        """All recorded strikes and both legs for a trading date (nearest expiry when ``expiry`` is None).

        Columns: trading_date, expiry, strike, option_type, symbol, timestamp
        (tz-aware Asia/Kolkata), open, high, low, close, volume, oi.
        """
        d = _to_date(trading_date)
        sql = (
            "SELECT trading_date, expiry, strike, option_type, symbol, ts AS timestamp, "
            "open, high, low, close, volume, oi FROM chains WHERE trading_date = ?"
        )
        params: list[Any] = [d]
        if expiry is not None:
            sql += " AND expiry = ?"
            params.append(_to_date(expiry))
        else:
            sql += " AND expiry = (SELECT min(expiry) FROM chains WHERE trading_date = ?)"
            params.append(d)
        sql += " ORDER BY expiry, strike, option_type, ts"
        with self._connect() as con:
            frame = con.execute(sql, params).df()
        if len(frame) == 0:
            return pd.DataFrame(columns=CHAIN_COLUMNS)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_convert(IST).dt.as_unit("us")
        frame["trading_date"] = pd.to_datetime(frame["trading_date"]).dt.date
        frame["expiry"] = pd.to_datetime(frame["expiry"]).dt.date
        return frame[CHAIN_COLUMNS].reset_index(drop=True)

    def chain_expiries(self, trading_date: date | str) -> list[date]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT expiry FROM chains WHERE trading_date = ? ORDER BY expiry",
                [_to_date(trading_date)],
            ).fetchall()
        return [_to_date(row[0]) for row in rows]

    def chain_coverage(self) -> list[dict[str, Any]]:
        """Per (trading_date, expiry): strike range stored, symbols and rows."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT trading_date, expiry, min(strike), max(strike), count(DISTINCT symbol), count(*) "
                "FROM chains GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
        return [
            {
                "trading_date": _to_date(d).isoformat(),
                "expiry": _to_date(e).isoformat(),
                "min_strike": float(lo),
                "max_strike": float(hi),
                "symbols": int(symbols),
                "rows": int(count),
            }
            for d, e, lo, hi, symbols, count in rows
        ]

    def chain_day_status(self, trading_date: date | str, expiry: date | str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT min_strike, max_strike, symbols, rows, status, strftime(fetched_at, '%Y-%m-%dT%H:%M:%S%z') "
                "FROM chain_days WHERE trading_date = ? AND expiry = ?",
                [_to_date(trading_date), _to_date(expiry)],
            ).fetchone()
        if row is None:
            return None
        lo, hi, symbols, count, status, fetched_at = row
        return {
            "min_strike": None if lo is None else float(lo),
            "max_strike": None if hi is None else float(hi),
            "symbols": int(symbols or 0),
            "rows": int(count or 0),
            "status": status,
            "fetched_at": fetched_at,
        }

    def mark_chain_day(
        self,
        trading_date: date | str,
        expiry: date | str,
        min_strike: float | None,
        max_strike: float | None,
        symbols: int,
        rows: int,
        status: str,
    ) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO chain_days VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _to_date(trading_date),
                    _to_date(expiry),
                    min_strike,
                    max_strike,
                    int(symbols),
                    int(rows),
                    status,
                    datetime.now(IST).replace(tzinfo=None),
                ],
            )

    def chain_days(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT trading_date, expiry, min_strike, max_strike, symbols, rows, status "
                "FROM chain_days ORDER BY trading_date, expiry"
            ).fetchall()
        return [
            {
                "trading_date": _to_date(d).isoformat(),
                "expiry": _to_date(e).isoformat(),
                "min_strike": None if lo is None else float(lo),
                "max_strike": None if hi is None else float(hi),
                "symbols": int(symbols or 0),
                "rows": int(count or 0),
                "status": status,
            }
            for d, e, lo, hi, symbols, count, status in rows
        ]

    def atm_path(
        self,
        trading_date: date | str,
        expiry: date | str | None = None,
        step: float = 50.0,
        index_symbol: str = "NIFTY",
        index_exchange: str = "NSE_INDEX",
    ) -> pd.DataFrame:
        """Per-minute ATM strike from the stored chain.

        For every index 1 minute bar of the day: the recorded strike nearest the
        index close gives the synthetic forward (strike + CE close - PE close);
        the ATM is the ``step`` multiple nearest that forward (limited to the
        recorded strikes). Columns: timestamp, index_close, near_strike,
        forward, atm_strike, ce_close, pe_close, combined, ce_symbol, pe_symbol.
        """
        d = _to_date(trading_date)
        columns = [
            "timestamp",
            "index_close",
            "near_strike",
            "forward",
            "atm_strike",
            "ce_close",
            "pe_close",
            "combined",
            "ce_symbol",
            "pe_symbol",
        ]
        chain = self.chain(d, expiry)
        index = self.day(index_exchange, index_symbol, "1m", d)
        if len(chain) == 0 or len(index) == 0:
            return pd.DataFrame(columns=columns)
        ce = chain[chain["option_type"] == "CE"].pivot(index="timestamp", columns="strike", values="close")
        pe = chain[chain["option_type"] == "PE"].pivot(index="timestamp", columns="strike", values="close")
        strikes = sorted(set(ce.columns) & set(pe.columns))
        if not strikes:
            return pd.DataFrame(columns=columns)
        stamps = index["timestamp"]
        ce = ce[strikes].reindex(stamps, method="ffill")
        pe = pe[strikes].reindex(stamps, method="ffill")
        ce_mat = ce.to_numpy(dtype="float64")
        pe_mat = pe.to_numpy(dtype="float64")
        strike_arr = np.asarray(strikes, dtype="float64")
        closes = index["close"].to_numpy(dtype="float64")
        n = len(closes)
        rows_idx = np.arange(n)
        # Nearest strike to the index that has both legs quoted at that minute.
        both = ~np.isnan(ce_mat) & ~np.isnan(pe_mat)
        distance = np.abs(strike_arr[None, :] - closes[:, None])
        distance = np.where(both, distance, np.inf)
        near_idx = distance.argmin(axis=1)
        valid = np.isfinite(distance[rows_idx, near_idx])
        near_strike = strike_arr[near_idx]
        forward = near_strike + ce_mat[rows_idx, near_idx] - pe_mat[rows_idx, near_idx]
        atm = np.round(forward / step) * step
        atm_idx = np.abs(strike_arr[None, :] - atm[:, None])
        atm_idx = np.where(both, atm_idx, np.inf).argmin(axis=1)
        atm_strike = strike_arr[atm_idx]
        ce_close = ce_mat[rows_idx, atm_idx]
        pe_close = pe_mat[rows_idx, atm_idx]
        symbols_ce = chain[chain["option_type"] == "CE"].drop_duplicates("strike").set_index("strike")["symbol"]
        symbols_pe = chain[chain["option_type"] == "PE"].drop_duplicates("strike").set_index("strike")["symbol"]
        out = pd.DataFrame(
            {
                "timestamp": stamps.to_numpy(),
                "index_close": closes,
                "near_strike": np.where(valid, near_strike, np.nan),
                "forward": np.where(valid, forward, np.nan),
                "atm_strike": np.where(valid, atm_strike, np.nan),
                "ce_close": np.where(valid, ce_close, np.nan),
                "pe_close": np.where(valid, pe_close, np.nan),
            }
        )
        out["combined"] = out["ce_close"] + out["pe_close"]
        out["ce_symbol"] = [symbols_ce.get(s) if pd.notna(s) else None for s in out["atm_strike"]]
        out["pe_symbol"] = [symbols_pe.get(s) if pd.notna(s) else None for s in out["atm_strike"]]
        out["timestamp"] = pd.to_datetime(out["timestamp"]).dt.tz_convert(IST).dt.as_unit("us")
        return out[columns]
