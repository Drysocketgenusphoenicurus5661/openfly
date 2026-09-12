"""Daily chain recorder and backfill.

For a trading date D the recorder:

1. makes sure the NIFTY and INDIAVIX 1 minute bars of D are stored;
2. picks the expiries to record: the weekly expiry as of D and the monthly
   expiry as of D (settings ``strategy.expiry_selection`` decides which one
   the strategy trades, replays may need either), plus the following one on
   an expiry day; a caller may pass an explicit list;
3. takes the day's index path (min and max 1 minute close) and records every
   listed strike from ``floor(min / step) * step - n * step`` to
   ``ceil(max / step) * step + n * step`` (``n`` = settings
   ``strategy.chain_strikes_each_side``, default 12), both CE and PE, at the
   ``strategy.chain_record_interval`` (1m), into ``BarStore.chains`` (and
   ``bars``), fetching only contracts whose coverage does not include D;
4. marks (D, expiry) in ``chain_days`` so a re-run is a no-op.

Backfill windows: weekly contracts list about two weeks before expiry, so
weekly expiries are recorded for the last ``strategy.weekly_backfill_days``
(7) trading days only; the current and next monthly expiries for up to
``strategy.monthly_backfill_days`` (90) trading days, newest first, stopping
an expiry at the first day whose probe strike returns no bars (not yet
listed) and stopping a symbol after its first empty response for older
dates. History calls are throttled by the client (two per second) and
retried there; a broker that declines history stops the run.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from openfly.config import DEFAULT_SETTINGS, SettingsStore
from openfly.interfaces import Contract
from openfly.market.chain import ChainResolver, monthly_expiries
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
    statuses: dict[str, str] = field(default_factory=dict)
    already: int = 0
    seconds: float = 0.0
    declined: str | None = None

    @property
    def rows(self) -> int:
        return sum(self.fetched.values())

    @property
    def symbols(self) -> int:
        return len(self.fetched)

    def status_of(self, expiry: date) -> str | None:
        return self.statuses.get(expiry.isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "is_trading_day": self.is_trading_day,
            "expiries": [e.isoformat() for e in self.expiries],
            "statuses": dict(self.statuses),
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
        workers: int = 3,
    ):
        self.client = client
        # Broker history calls are latency-bound (about two seconds each), so legs are
        # downloaded by a few threads while the token bucket still caps the rate.
        self.workers = max(1, int(workers))
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
        self.weekly_backfill_days = int(strategy.get("weekly_backfill_days", 7))
        self.monthly_backfill_days = int(strategy.get("monthly_backfill_days", 90))
        self.cache = cache or HistoryCache(client, store=bar_store)
        self.bars = self.cache.store
        self.calendar = calendar or SessionCalendar(client, settings)
        self.resolver = resolver or ChainResolver(client, settings, self.calendar)
        # symbol -> earliest date at which the broker returned bars; older dates are skipped.
        self._exhausted: dict[str, date] = {}

    # strikes and expiries ------------------------------------------------

    def strike_range(self, index_min: float, index_max: float, n: int | None = None) -> tuple[float, float]:
        """``floor(min / step) * step - n * step`` to ``ceil(max / step) * step + n * step`` (prices rounded to paise)."""
        n = self.strikes_each_side if n is None else int(n)
        step = self.strike_step
        lo = math.floor(round(index_min, 2) / step) * step - n * step
        hi = math.ceil(round(index_max, 2) / step) * step + n * step
        return float(lo), float(hi)

    def expiries_for(self, d: date) -> list[date]:
        """Weekly and monthly expiries as of ``d``; on an expiry day also the following one of that kind."""
        listed = [e for e in self.resolver.expiries() if e >= d]
        if not listed:
            return []
        chosen: list[date] = []
        for candidates in (listed, monthly_expiries(listed)):
            if not candidates:
                continue
            chosen.append(candidates[0])
            if candidates[0] == d and len(candidates) > 1:
                chosen.append(candidates[1])
        return sorted(set(chosen))

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
        # One DuckDB connection for the whole day instead of one per store call.
        with self.bars:
            self._record_day(report, d, strikes_each_side, expiries, force, say)
        report.seconds = time.monotonic() - started
        return report

    def _record_day(
        self,
        report: RecordReport,
        d: date,
        strikes_each_side: int | None,
        expiries: list[date] | None,
        force: bool,
        say: ProgressCallback,
    ) -> None:
        started = time.monotonic()
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
                    return
        index = self.bars.day(self.index_exchange, self.underlying, "1m", d)
        if len(index) == 0:
            report.skipped[f"{self.underlying}:chain"] = "no index bars for the date; chain not recorded"
            report.seconds = time.monotonic() - started
            return
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
            return
        report.expiries = chosen
        for expiry in chosen:
            try:
                self._record_expiry(report, d, expiry, lo, hi, force, say)
            except BrokerDeclined as exc:
                report.declined = exc.message
                say(f"{d}: broker declined history ({exc.message}); stopping")
                break
        report.seconds = time.monotonic() - started

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
                report.statuses[expiry.isoformat()] = "empty"
                return
            if status["status"] == "done" and done_lo is not None and done_lo <= lo and done_hi >= hi:
                report.already += int(status.get("symbols") or 0)
                report.statuses[expiry.isoformat()] = "done"
                say(f"{tag}: already recorded ({status.get('rows')} rows)")
                return
        try:
            chain = self.resolver.contracts(expiry)
        except OpenAlgoError as exc:
            report.skipped[tag] = f"search failed: {exc.message}"
            report.statuses[expiry.isoformat()] = "error"
            return
        strikes = [s for s in sorted(chain) if lo <= s <= hi]
        if not strikes:
            report.skipped[tag] = "no listed strikes in range"
            self.bars.mark_chain_day(d, expiry, lo, hi, 0, 0, "empty")
            report.statuses[expiry.isoformat()] = "empty"
            return
        legs: list[Contract] = []
        for strike in strikes:
            row = chain[strike]
            legs.extend(leg for leg in (row.ce, row.pe) if leg is not None)

        # Probe one strike near the index before spending a call per leg.
        centre = (report.index_min or 0.0) / 2.0 + (report.index_max or 0.0) / 2.0
        probe = min(legs, key=lambda leg: (abs(leg.strike - centre), leg.option_type))
        if (not self.bars.is_covered(probe.exchange, probe.symbol, self.interval, d) or force) and not self._skip_older(probe.symbol, d):
            rows = self._fetch_leg(report, d, expiry, probe, force)
            if rows == 0 and len(self.bars.day(probe.exchange, probe.symbol, self.interval, d)) == 0:
                report.skipped[tag] = f"no bars for {probe.symbol} on {d}; contract not listed yet"
                self.bars.mark_chain_day(d, expiry, lo, hi, 0, 0, "empty")
                report.statuses[expiry.isoformat()] = "empty"
                say(f"{tag}: no data (probe {probe.symbol})")
                return
        to_fetch: list[Contract] = []
        for leg in legs:
            key = f"{leg.exchange}:{leg.symbol}:{self.interval}"
            if leg.symbol == probe.symbol and key in report.fetched:
                continue
            if self.bars.is_covered(leg.exchange, leg.symbol, self.interval, d) and not force:
                # Already fetched into bars (for example imported from parquet): copy, do not call the broker.
                self.bars.copy_bars_to_chain(
                    d, expiry, leg.strike, leg.option_type, leg.symbol, leg.exchange, self.interval
                )
                report.already += 1
                continue
            if self._skip_older(leg.symbol, d):
                # The broker had nothing for this symbol on a later date: it was not listed yet.
                if d <= self.cache.settled_through():
                    self.bars.add_coverage(leg.exchange, leg.symbol, self.interval, d, d)
                report.skipped[key] = "not listed yet (empty on a later date)"
                continue
            to_fetch.append(leg)
        total_rows = 0
        fetched = 0
        failed = 0
        try:
            # Threads only download; every store write happens on this thread.
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self._download_leg, d, leg): leg for leg in to_fetch}
                try:
                    for future in as_completed(futures):
                        leg = futures[future]
                        key = f"{leg.exchange}:{leg.symbol}:{self.interval}"
                        fetched += 1
                        try:
                            frame = future.result()
                        except BrokerDeclined:
                            raise
                        except OpenAlgoError as exc:
                            report.skipped[key] = exc.message
                            logger.warning("record %s skipped: %s", key, exc.message)
                            failed += 1
                            continue
                        total_rows += self._store_leg(report, d, expiry, leg, frame)
                        if fetched % 10 == 0:
                            say(f"{tag}: {fetched}/{len(to_fetch)} legs, {total_rows} rows")
                except BrokerDeclined:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
        except BrokerDeclined:
            stored = self.bars.chain(d, expiry)
            symbols = int(stored["symbol"].nunique()) if len(stored) else 0
            self.bars.mark_chain_day(d, expiry, lo, hi, symbols, int(len(stored)), "partial")
            report.statuses[expiry.isoformat()] = "partial"
            raise
        stored = self.bars.chain(d, expiry)
        symbols = int(stored["symbol"].nunique()) if len(stored) else 0
        status = "partial" if failed else "done"
        self.bars.mark_chain_day(d, expiry, lo, hi, symbols, int(len(stored)), status)
        report.statuses[expiry.isoformat()] = status
        say(f"{tag}: {status}, {fetched} legs fetched ({failed} failed), {symbols} symbols, {len(stored)} rows stored")

    def _skip_older(self, symbol: str, d: date) -> bool:
        first = self._exhausted.get(symbol)
        return first is not None and d < first

    def _download_leg(self, d: date, leg: Contract):
        """Network only (safe on a worker thread): one day of bars for one contract."""
        try:
            frame = self.cache.fetch(leg.exchange, leg.symbol, self.interval, d, d)
        except OpenAlgoError as exc:
            if "permission" in exc.message.lower():
                raise BrokerDeclined(exc.message, exc.code, exc.endpoint) from exc
            raise
        return HistoryCache.day(frame, d)

    def _fetch_leg(self, report: RecordReport, d: date, expiry: date, leg: Contract, force: bool) -> int:
        """Download and store one leg on the calling thread (used for the probe)."""
        key = f"{leg.exchange}:{leg.symbol}:{self.interval}"
        try:
            frame = self._download_leg(d, leg)
        except BrokerDeclined:
            report.skipped[key] = "broker declined history"
            raise
        except OpenAlgoError as exc:
            report.skipped[key] = exc.message
            logger.warning("record %s skipped: %s", key, exc.message)
            return 0
        return self._store_leg(report, d, expiry, leg, frame)

    def _store_leg(self, report: RecordReport, d: date, expiry: date, leg: Contract, frame) -> int:
        key = f"{leg.exchange}:{leg.symbol}:{self.interval}"
        self.bars.upsert_chain(d, expiry, leg.strike, leg.option_type, leg.symbol, frame)
        self.bars.upsert_bars(leg.exchange, leg.symbol, self.interval, frame)
        if d <= self.cache.settled_through():
            self.bars.add_coverage(leg.exchange, leg.symbol, self.interval, d, d)
        if len(frame) == 0:
            self._exhausted[leg.symbol] = d
        report.fetched[key] = int(len(frame))
        return int(len(frame))

    # backfill ------------------------------------------------------------

    def _recent_trading_days(self, count: int, through: date) -> list[date]:
        days: list[date] = []
        d = through
        while len(days) < count and d > through - timedelta(days=count * 3 + 30):
            if self.calendar.is_trading_day(d):
                days.append(d)
            d -= timedelta(days=1)
        return days

    def backfill_plan(
        self,
        weekly_days: int | None = None,
        monthly_days: int | None = None,
        only_expiry: date | None = None,
    ) -> list[tuple[date, list[date]]]:
        """``[(expiry, [trading days newest first])]``: monthlies (current, next) then weeklies.

        Weekly expiries get the last ``weekly_days`` trading days; the current and
        next monthly expiries get up to ``monthly_days`` trading days, both ending
        at the last settled session and never after the expiry itself.
        """
        weekly_days = self.weekly_backfill_days if weekly_days is None else int(weekly_days)
        monthly_days = self.monthly_backfill_days if monthly_days is None else int(monthly_days)
        settled = self.cache.settled_through()
        listed = self.resolver.expiries()
        all_monthlies = set(monthly_expiries(listed))
        monthlies = [e for e in sorted(all_monthlies) if e >= settled][:2]
        # Weeklies are the non-monthly expiries; far monthlies and quarterlies are not recorded.
        weeklies = [e for e in listed if e >= settled and e not in all_monthlies]
        plan: list[tuple[date, list[date]]] = []
        for expiry in monthlies + weeklies:
            if only_expiry is not None and expiry != only_expiry:
                continue
            count = monthly_days if expiry in monthlies else weekly_days
            days = [d for d in self._recent_trading_days(count, min(settled, expiry))]
            if days:
                plan.append((expiry, days))
        if only_expiry is not None and not plan and only_expiry in listed:
            count = monthly_days if only_expiry in monthly_expiries(listed) else weekly_days
            plan.append((only_expiry, self._recent_trading_days(count, min(settled, only_expiry))))
        return plan

    def backfill(
        self,
        weekly_days: int | None = None,
        monthly_days: int | None = None,
        only_expiry: date | None = None,
        progress: ProgressCallback | None = None,
        strikes_each_side: int | None = None,
    ) -> list[RecordReport]:
        """Record every planned (expiry, day), newest day first, stopping an expiry at its first unlisted day."""
        say = progress or (lambda text: None)
        plan = self.backfill_plan(weekly_days, monthly_days, only_expiry)
        total = sum(len(days) for _, days in plan)
        say(f"backfill: {len(plan)} expiries, {total} expiry-days planned")
        reports: list[RecordReport] = []
        number = 0
        for expiry, days in plan:
            code = f"{expiry:%d-%b-%y}".upper()
            for d in days:
                number += 1
                say(f"[{number}/{total}] {d} expiry {code}")
                report = self.record(d, strikes_each_side=strikes_each_side, expiries=[expiry], progress=say)
                reports.append(report)
                if report.declined:
                    say(f"backfill stopped: broker declined history ({report.declined}); rerun once the broker session is valid")
                    return reports
                if report.status_of(expiry) == "empty":
                    say(f"{code}: no bars on {d}; contract not listed earlier, moving on")
                    break
        return reports
