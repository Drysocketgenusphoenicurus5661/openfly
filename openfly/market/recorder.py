"""Daily chain recorder and backfill.

For a trading date D the recorder:

1. makes sure the NIFTY and INDIAVIX 1 minute bars of D are stored;
2. picks the expiries to record: the current-week expiry as of D and, on an
   expiry day, also the following week's expiry (a caller may pass a list);
3. takes the day's index path (min and max 1 minute close) and records every
   listed strike from ``floor(min / step) * step - n * step`` to
   ``ceil(max / step) * step + n * step`` (``n`` = settings
   ``strategy.chain_strikes_each_side``, default 12), both CE and PE, at the
   ``strategy.chain_record_interval`` (1m), into ``BarStore.chains`` (and
   ``bars``), fetching only contracts whose coverage does not include D;
4. marks (D, expiry) in ``chain_days`` so a re-run is a no-op.

Days before a contract's listing return no bars: the recorder probes one
strike near the index first and marks the day ``empty`` when nothing comes
back, so a backfill does not spend dozens of calls on unlisted days. History
calls are throttled by the client (two per second) and retried there.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from openfly.config import DEFAULT_SETTINGS, SettingsStore
from openfly.interfaces import Contract
from openfly.market.chain import ChainResolver
from openfly.market.client import IST, OpenAlgoClient, OpenAlgoError
from openfly.market.history import HistoryCache
from openfly.market.session import SessionCalendar
from openfly.market.store import BarStore

logger = logging.getLogger("openfly.market.recorder")

ProgressCallback = Callable[[str], None]


class BrokerDeclined(OpenAlgoError):
    """The broker rejects history calls outright (expired session or missing permission)."""


@dataclass
class RecordReport:
    date: date
    is_trading_day: bool = True
    expiries: list[date] = field(default_factory=list)
    index_min: float | None = None
    index_max: float | None = None
    index_close: float | None = None
    strike_lo: float | None = None
    strike_hi: float | None = None
    fetched: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    already: int = 0
    seconds: float = 0.0
    declined: str | None = None

    @property
    def rows(self) -> int:
        return sum(self.fetched.values())

    @property
    def symbols(self) -> int:
        return len(self.fetched)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "is_trading_day": self.is_trading_day,
            "expiries": [e.isoformat() for e in self.expiries],
            "index_min": self.index_min,
            "index_max": self.index_max,
            "index_close": self.index_close,
            "strike_lo": self.strike_lo,
            "strike_hi": self.strike_hi,
            "symbols_fetched": self.symbols,
            "rows_stored": self.rows,
            "symbols_already_covered": self.already,
            "skipped": dict(self.skipped),
            "declined": self.declined,
            "seconds": round(self.seconds, 1),
        }


class Recorder:
    def __init__(
        self,
        client: OpenAlgoClient,
        cache: HistoryCache | None = None,
        resolver: ChainResolver | None = None,
        calendar: SessionCalendar | None = None,
        store: SettingsStore | dict[str, Any] | None = None,
        bar_store: BarStore | None = None,
    ):
        self.client = client
        if isinstance(store, dict):
            settings = store
        else:
            settings = (store or SettingsStore()).get() if store is not None else DEFAULT_SETTINGS
        strategy = settings.get("strategy", {})
        self.underlying = strategy.get("underlying", "NIFTY")
        self.index_exchange = strategy.get("index_exchange", "NSE_INDEX")
        self.options_exchange = strategy.get("options_exchange", "NFO")
        self.vix_symbol = strategy.get("vix_symbol", "INDIAVIX")
        self.strike_step = float(strategy.get("strike_step", 50))
        self.strikes_each_side = int(strategy.get("chain_strikes_each_side", 12))
        self.interval = str(strategy.get("chain_record_interval", "1m"))
        self.cache = cache or HistoryCache(client, store=bar_store)
        self.bars = self.cache.store
        self.calendar = calendar or SessionCalendar(client, settings)
        self.resolver = resolver or ChainResolver(client, settings, self.calendar)

    # strikes -------------------------------------------------------------

    def strike_range(self, index_min: float, index_max: float, n: int | None = None) -> tuple[float, float]:
        """``floor(min / step) * step - n * step`` to ``ceil(max / step) * step + n * step`` (prices rounded to paise)."""
        n = self.strikes_each_side if n is None else int(n)
        step = self.strike_step
        lo = math.floor(round(index_min, 2) / step) * step - n * step
        hi = math.ceil(round(index_max, 2) / step) * step + n * step
        return float(lo), float(hi)

    def expiries_for(self, d: date) -> list[date]:
        """Current-week expiry as of ``d``; on expiry day also the next week's."""
        expiries = [e for e in self.resolver.expiries() if e >= d]
        if not expiries:
            return []
        chosen = [expiries[0]]
        if expiries[0] == d and len(expiries) > 1:
            chosen.append(expiries[1])
        return chosen

    # recording -----------------------------------------------------------

    def record(
        self,
        day: date | str | None = None,
        strikes_each_side: int | None = None,
        expiries: list[date] | None = None,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RecordReport:
        """Record the chain of one trading date. Idempotent: covered contracts are not fetched again."""
        started = time.monotonic()
        if isinstance(day, str):
            d = date.fromisoformat(day[:10])
        else:
            d = day or datetime.now(IST).date()
        report = RecordReport(date=d)
        say = progress or (lambda text: None)
        if not self.calendar.is_trading_day(d):
            report.is_trading_day = False
            report.seconds = time.monotonic() - started
            return report

        # Index and VIX bars anchor the strikes and feed the replays.
        for symbol in (self.underlying, self.vix_symbol):
            key = f"{self.index_exchange}:{symbol}:1m"
            try:
                added = self.cache.update(self.index_exchange, symbol, "1m", d, d, force=force)
                if added or force:
                    report.fetched[key] = added
                else:
                    report.already += 1
            except OpenAlgoError as exc:
                report.skipped[key] = exc.message
                if "permission" in exc.message.lower():
                    report.declined = exc.message
                    report.seconds = time.monotonic() - started
                    return report
        index = self.bars.day(self.index_exchange, self.underlying, "1m", d)
        if len(index) == 0:
            report.skipped[f"{self.underlying}:chain"] = "no index bars for the date; chain not recorded"
            report.seconds = time.monotonic() - started
            return report
        report.index_min = float(index["close"].min())
        report.index_max = float(index["close"].max())
        report.index_close = float(index["close"].iloc[-1])
        lo, hi = self.strike_range(report.index_min, report.index_max, strikes_each_side)
        report.strike_lo, report.strike_hi = lo, hi

        try:
            chosen = list(expiries) if expiries else self.expiries_for(d)
        except OpenAlgoError as exc:
            report.skipped[f"{self.underlying}:chain"] = f"expiry list unavailable: {exc.message}"
            report.seconds = time.monotonic() - started
            return report
        report.expiries = chosen
        for expiry in chosen:
            try:
                self._record_expiry(report, d, expiry, lo, hi, force, say)
            except BrokerDeclined as exc:
                report.declined = exc.message
                say(f"{d}: broker declined history ({exc.message}); stopping")
                break
        report.seconds = time.monotonic() - started
        return report

    def _record_expiry(
        self,
        report: RecordReport,
        d: date,
        expiry: date,
        lo: float,
        hi: float,
        force: bool,
        say: ProgressCallback,
    ) -> None:
        tag = f"{d} {expiry:%d-%b-%y}".upper()
        status = self.bars.chain_day_status(d, expiry)
        if status and not force:
            done_lo, done_hi = status.get("min_strike"), status.get("max_strike")
            if status["status"] == "empty":
                report.skipped[tag] = "no bars from the broker on an earlier run (contract not listed yet)"
                return
            if status["status"] == "done" and done_lo is not None and done_lo <= lo and done_hi >= hi:
                report.already += int(status.get("symbols") or 0)
                say(f"{tag}: already recorded ({status.get('rows')} rows)")
                return
        try:
            chain = self.resolver.contracts(expiry)
        except OpenAlgoError as exc:
            report.skipped[tag] = f"search failed: {exc.message}"
            return
        strikes = [s for s in sorted(chain) if lo <= s <= hi]
        if not strikes:
            report.skipped[tag] = "no listed strikes in range"
            self.bars.mark_chain_day(d, expiry, lo, hi, 0, 0, "empty")
            return
        legs: list[Contract] = []
        for strike in strikes:
            row = chain[strike]
            legs.extend(leg for leg in (row.ce, row.pe) if leg is not None)

        # Probe one strike near the index before spending a call per leg.
        centre = (report.index_min or 0.0) / 2.0 + (report.index_max or 0.0) / 2.0
        probe = min(legs, key=lambda leg: (abs(leg.strike - centre), leg.option_type))
        if not self.bars.is_covered(probe.exchange, probe.symbol, self.interval, d) or force:
            rows = self._fetch_leg(report, d, expiry, probe, force)
            if rows == 0 and len(self.bars.day(probe.exchange, probe.symbol, self.interval, d)) == 0:
                report.skipped[tag] = f"no bars for {probe.symbol} on {d}; contract not listed yet"
                self.bars.mark_chain_day(d, expiry, lo, hi, 0, 0, "empty")
                say(f"{tag}: no data (probe {probe.symbol})")
                return
        total_rows = 0
        fetched = 0
        failed = 0
        try:
            for index, leg in enumerate(legs, start=1):
                if leg.symbol == probe.symbol and f"{leg.exchange}:{leg.symbol}:{self.interval}" in report.fetched:
                    continue
                if self.bars.is_covered(leg.exchange, leg.symbol, self.interval, d) and not force:
                    # Already fetched into bars (for example imported from parquet): copy, do not call the broker.
                    self.bars.copy_bars_to_chain(
                        d, expiry, leg.strike, leg.option_type, leg.symbol, leg.exchange, self.interval
                    )
                    report.already += 1
                    continue
                rows = self._fetch_leg(report, d, expiry, leg, force)
                if f"{leg.exchange}:{leg.symbol}:{self.interval}" in report.skipped:
                    failed += 1
                total_rows += rows
                fetched += 1
                if index % 10 == 0:
                    say(f"{tag}: {index}/{len(legs)} legs, {total_rows} rows")
        except BrokerDeclined:
            stored = self.bars.chain(d, expiry)
            symbols = int(stored["symbol"].nunique()) if len(stored) else 0
            self.bars.mark_chain_day(d, expiry, lo, hi, symbols, int(len(stored)), "partial")
            raise
        stored = self.bars.chain(d, expiry)
        symbols = int(stored["symbol"].nunique()) if len(stored) else 0
        status = "partial" if failed else "done"
        self.bars.mark_chain_day(d, expiry, lo, hi, symbols, int(len(stored)), status)
        say(f"{tag}: {status}, {fetched} legs fetched ({failed} failed), {symbols} symbols, {len(stored)} rows stored")

    def _fetch_leg(self, report: RecordReport, d: date, expiry: date, leg: Contract, force: bool) -> int:
        key = f"{leg.exchange}:{leg.symbol}:{self.interval}"
        try:
            frame = self.cache.fetch(leg.exchange, leg.symbol, self.interval, d, d)
        except OpenAlgoError as exc:
            report.skipped[key] = exc.message
            logger.warning("record %s skipped: %s", key, exc.message)
            if "permission" in exc.message.lower():
                raise BrokerDeclined(exc.message, exc.code, exc.endpoint) from exc
            return 0
        frame = HistoryCache.day(frame, d)
        self.bars.upsert_chain(d, expiry, leg.strike, leg.option_type, leg.symbol, frame)
        self.bars.upsert_bars(leg.exchange, leg.symbol, self.interval, frame)
        if d <= self.cache.settled_through():
            self.bars.add_coverage(leg.exchange, leg.symbol, self.interval, d, d)
        report.fetched[key] = int(len(frame))
        return int(len(frame))

    # backfill ------------------------------------------------------------

    def backfill_days(self, days: int = 30, listing_lead_days: int = 21) -> list[tuple[date, date]]:
        """(trading_date, expiry) pairs to record: listed expiries and their listed days within ``days``."""
        today = self.calendar.today()
        settled = self.cache.settled_through()
        earliest = today - timedelta(days=int(days))
        pairs: list[tuple[date, date]] = []
        for expiry in self.resolver.expiries():
            if expiry < earliest:
                continue
            first = max(earliest, expiry - timedelta(days=int(listing_lead_days)))
            last = min(expiry, settled)
            if last < first:
                continue
            d = first
            while d <= last:
                if self.calendar.is_trading_day(d):
                    pairs.append((d, expiry))
                d += timedelta(days=1)
        # Most recent days first, nearest expiry first: the useful data lands early.
        pairs.sort(key=lambda pair: (-pair[0].toordinal(), pair[1]))
        return pairs

    def backfill(
        self,
        days: int = 30,
        listing_lead_days: int = 21,
        progress: ProgressCallback | None = None,
        strikes_each_side: int | None = None,
    ) -> list[RecordReport]:
        """Record every listed (trading day, expiry) pair of the last ``days``; resumable through coverage."""
        say = progress or (lambda text: None)
        pairs = self.backfill_days(days, listing_lead_days)
        say(f"backfill: {len(pairs)} day-expiry pairs to check")
        reports: list[RecordReport] = []
        for number, (d, expiry) in enumerate(pairs, start=1):
            say(f"[{number}/{len(pairs)}] {d} expiry {expiry:%d-%b-%y}".upper())
            report = self.record(d, strikes_each_side=strikes_each_side, expiries=[expiry], progress=say)
            reports.append(report)
            if report.declined:
                say(f"backfill stopped: broker declined history ({report.declined}); rerun once the broker session is valid")
                break
        return reports
