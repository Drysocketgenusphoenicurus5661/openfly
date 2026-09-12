"""History layer: a thin cache over :class:`BarStore` and ``OpenAlgoClient.history``.

``get(exchange, symbol, interval, start, end)`` computes the missing date
ranges from the store's coverage table, fetches only those from the broker
(rate limited, chunked, retried by the client), upserts the bars, records the
coverage and returns the frame from a DuckDB query. Coverage for the current
IST day is recorded only after the session has settled (15:45), so a call
during market hours refreshes today's bars and a call after the close is a
no-op the next time.

Also provides the pandas transforms used everywhere: ``resample`` (1m to 5m
and friends, bins anchored at 09:15 IST) and ``day``.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from openfly.config import PATHS, Paths
from openfly.market.client import IST, OpenAlgoClient, OpenAlgoError, _empty_history
from openfly.market.store import BarStore, normalise_bars

logger = logging.getLogger("openfly.market.history")

DEFAULT_CHUNK_DAYS: dict[str, int] = {
    "1m": 60,
    "3m": 100,
    "5m": 100,
    "10m": 100,
    "15m": 200,
    "30m": 200,
    "1h": 365,
    "D": 2000,
}

SESSION_OPEN = time(9, 15)
SETTLED_TIME = time(15, 45)


def _to_date(value: date | datetime | str | pd.Timestamp) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def interval_minutes(interval: str) -> int:
    match = re.fullmatch(r"(\d+)\s*(m|min|h)", interval.strip().lower())
    if not match:
        raise ValueError(f"cannot resample to interval {interval!r}; use like 5m, 15m, 1h")
    count = int(match.group(1))
    return count * 60 if match.group(2) == "h" else count


class HistoryCache:
    """Fetch-only-what-is-missing access to bars, backed by DuckDB."""

    def __init__(
        self,
        client: OpenAlgoClient | None = None,
        store: BarStore | None = None,
        paths: Paths = PATHS,
        chunk_days: dict[str, int] | None = None,
        now: datetime | None = None,
        settled_time: time = SETTLED_TIME,
    ):
        self.client = client
        self.paths = paths
        self.store = store or BarStore(paths.market_db, history_root=paths.history)
        self.chunk_days = {**DEFAULT_CHUNK_DAYS, **(chunk_days or {})}
        self._now = now
        self.settled_time = settled_time

    # time ----------------------------------------------------------------

    def now(self) -> datetime:
        return self._now or datetime.now(IST)

    def today(self) -> date:
        return self.now().date()

    def settled_through(self) -> date:
        """Last date whose session is complete: today after ``settled_time``, else yesterday."""
        moment = self.now()
        if moment.time() >= self.settled_time:
            return moment.date()
        return moment.date() - timedelta(days=1)

    # store passthroughs --------------------------------------------------

    def load(self, exchange: str, symbol: str, interval: str) -> pd.DataFrame:
        return self.store.bars(exchange, symbol, interval)

    def save(self, exchange: str, symbol: str, interval: str, df: pd.DataFrame) -> int:
        return self.store.upsert_bars(exchange, symbol, interval, df)

    def available_dates(self, exchange: str, symbol: str, interval: str) -> list[date]:
        return self.store.available_dates(exchange, symbol, interval)

    def coverage(self, exchange: str, symbol: str, interval: str) -> list[tuple[date, date]]:
        return self.store.coverage(exchange, symbol, interval)

    def symbols(self) -> list[tuple[str, str, str]]:
        return self.store.symbols()

    def export(self, exchange: str, symbol: str, interval: str, path: Any = None):
        return self.store.export_parquet(exchange, symbol, interval, path)

    def day_bars(self, exchange: str, symbol: str, interval: str, day: date | str) -> pd.DataFrame:
        return self.store.day(exchange, symbol, interval, day)

    def missing_ranges(
        self, exchange: str, symbol: str, interval: str, start: date | str, end: date | str
    ) -> list[tuple[date, date]]:
        return self.store.missing_ranges(exchange, symbol, interval, _to_date(start), _to_date(end), self.today())

    # broker --------------------------------------------------------------

    def fetch(self, exchange: str, symbol: str, interval: str, start: date, end: date) -> pd.DataFrame:
        """Fetch [start, end] from the broker in chunks the broker accepts; does not store."""
        if self.client is None:
            raise OpenAlgoError("history cache has no client; cannot fetch", endpoint="history")
        chunk = self.chunk_days.get(interval, 100 if interval.upper() != "D" else 2000)
        pieces: list[pd.DataFrame] = []
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=chunk - 1))
            pieces.append(self.client.history(symbol, exchange, interval, cursor, stop))
            cursor = stop + timedelta(days=1)
        if not pieces:
            return _empty_history()
        return normalise_bars(pd.concat(pieces, ignore_index=True))

    def update(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        start: date | str,
        end: date | str,
        force: bool = False,
    ) -> int:
        """Bring the store up to date for [start, end]; returns new bars stored.

        Ranges the coverage table already includes are not requested again.
        With ``force`` the whole range is refetched and upserted.
        """
        start_d, end_d = _to_date(start), _to_date(end)
        if end_d < start_d:
            return 0
        ranges = [(start_d, end_d)] if force else self.missing_ranges(exchange, symbol, interval, start_d, end_d)
        if not ranges:
            return 0
        added = 0
        settled = self.settled_through()
        for a, b in ranges:
            logger.info("history fetch %s %s %s %s to %s", exchange, symbol, interval, a, b)
            piece = self.fetch(exchange, symbol, interval, a, b)
            added += self.store.upsert_bars(exchange, symbol, interval, piece)
            covered_end = min(b, settled)
            if covered_end >= a:
                self.store.add_coverage(exchange, symbol, interval, a, covered_end)
        return added

    def get(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        start: date | str,
        end: date | str,
        fetch: bool = True,
    ) -> pd.DataFrame:
        """Bars for [start, end] inclusive, fetching only what coverage lacks (when a client is set)."""
        start_d, end_d = _to_date(start), _to_date(end)
        if fetch and self.client is not None:
            try:
                self.update(exchange, symbol, interval, start_d, end_d)
            except OpenAlgoError as exc:
                logger.warning("history update failed for %s %s %s: %s", exchange, symbol, interval, exc)
        return self.store.bars(exchange, symbol, interval, start_d, end_d)

    # pandas transforms ---------------------------------------------------

    @staticmethod
    def normalise(df: pd.DataFrame | None) -> pd.DataFrame:
        return normalise_bars(df)

    @staticmethod
    def merge(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
        """Union by timestamp; rows from ``incoming`` replace duplicates."""
        a = normalise_bars(existing)
        b = normalise_bars(incoming)
        if len(a) == 0:
            return b
        if len(b) == 0:
            return a
        return normalise_bars(pd.concat([a, b], ignore_index=True))

    @staticmethod
    def day(df: pd.DataFrame, day: date | str) -> pd.DataFrame:
        """Rows of one IST calendar date."""
        target = _to_date(day)
        frame = normalise_bars(df)
        if len(frame) == 0:
            return frame
        return frame.loc[frame["timestamp"].dt.date == target].reset_index(drop=True)

    @staticmethod
    def resample(df_1m: pd.DataFrame, interval: str = "5m") -> pd.DataFrame:
        """Aggregate 1 minute bars to ``interval`` with bins anchored at 09:15 IST.

        Bar timestamps are bin starts: for 5m the bins are 09:15, 09:20, ...,
        15:25, the last covering 15:25 to 15:29. Empty bins are dropped, so
        overnight gaps do not produce rows. Volume sums, oi takes the last value.
        """
        minutes = interval_minutes(interval)
        frame = normalise_bars(df_1m)
        if len(frame) == 0:
            return frame
        indexed = frame.set_index("timestamp")
        agg = indexed.resample(
            f"{minutes}min",
            closed="left",
            label="left",
            origin="start_day",
            offset=f"{SESSION_OPEN.hour}h{SESSION_OPEN.minute}min",
        ).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"})
        agg = agg.dropna(subset=["open"]).reset_index()
        return normalise_bars(agg)

    @staticmethod
    def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
        """Compact records for the API: t (ISO with offset), o, h, l, c, v, oi."""
        frame = normalise_bars(df)
        out = []
        for row in frame.itertuples(index=False):
            out.append(
                {
                    "t": row.timestamp.isoformat(),
                    "o": float(row.open),
                    "h": float(row.high),
                    "l": float(row.low),
                    "c": float(row.close),
                    "v": float(row.volume),
                    "oi": float(row.oi),
                }
            )
        return out
