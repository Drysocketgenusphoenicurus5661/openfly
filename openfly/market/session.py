"""Trading session calendar for NSE and NFO.

Holidays per year and timings per date are fetched once from OpenAlgo and
kept in ``PATHS.data/session/calendar.json`` so that everything works offline
afterwards. Session windows come from the strategy settings: trade_start
(09:20), last_entry (14:30), square_off (15:15), all Asia/Kolkata.

Expiry-day detection uses the NIFTY option expiry list when one has been
supplied (by ``ChainResolver`` or an ``expiry_source`` callable) and falls back
to the weekly rule: Tuesday, or the previous trading day when Tuesday is a
holiday.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock

from openfly.config import DEFAULT_SETTINGS, PATHS, Paths, SettingsStore
from openfly.interfaces import SessionWindow
from openfly.market.client import IST, OpenAlgoClient, OpenAlgoError

logger = logging.getLogger("openfly.market.session")

DEFAULT_OPEN = time(9, 15)
DEFAULT_CLOSE = time(15, 30)
WEEKLY_EXPIRY_WEEKDAY = 1  # Tuesday
CALENDAR_FILE = "calendar.json"


def _parse_hhmm(text: str) -> time:
    hours, minutes = str(text).split(":")[:2]
    return time(int(hours), int(minutes))


def _to_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class SessionCalendar:
    """Trading-day, window and expiry-day answers, cached to JSON for offline use."""

    def __init__(
        self,
        client: OpenAlgoClient | None = None,
        store: SettingsStore | dict[str, Any] | None = None,
        paths: Paths = PATHS,
        expiry_source: Callable[[], list[date]] | None = None,
        exchange: str | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.client = client
        self.paths = paths
        self._now = now
        if isinstance(store, dict):
            self.settings = store
        else:
            self.settings = (store or SettingsStore()).get() if store is not None else DEFAULT_SETTINGS
        strategy = self.settings.get("strategy", {})
        self.exchange = exchange or strategy.get("options_exchange", "NFO")
        self.trade_start = _parse_hhmm(strategy.get("trade_start", "09:20"))
        self.last_entry = _parse_hhmm(strategy.get("last_entry", "14:30"))
        self.square_off = _parse_hhmm(strategy.get("square_off", "15:15"))
        self.expiry_source = expiry_source
        self._lock = threading.RLock()
        self._file = self.paths.data / "session" / CALENDAR_FILE
        self._state: dict[str, Any] = {"holidays": {}, "timings": {}, "expiries": []}
        self._load()

    # persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("session calendar cache unreadable; starting empty")
            return
        self._state["holidays"] = {str(k): v for k, v in raw.get("holidays", {}).items()}
        self._state["timings"] = {str(k): v for k, v in raw.get("timings", {}).items()}
        self._state["expiries"] = list(raw.get("expiries", []))
        self._state["expiries_fetched_at"] = raw.get("expiries_fetched_at")

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._file) + ".lock"):
            self._file.write_text(json.dumps(self._state, indent=1, sort_keys=True), encoding="utf-8")

    @property
    def cache_file(self) -> Path:
        return self._file

    # time ----------------------------------------------------------------

    def now_ist(self) -> datetime:
        if self._now is not None:
            value = self._now()
            return value if value.tzinfo else value.replace(tzinfo=IST)
        return datetime.now(IST)

    def today(self) -> date:
        return self.now_ist().date()

    # holidays ------------------------------------------------------------

    def holidays(self, year: int, refresh: bool = False) -> dict[date, dict[str, Any]]:
        """Trading holidays for ``self.exchange`` in ``year`` (date -> raw entry)."""
        key = str(year)
        with self._lock:
            cached = self._state["holidays"].get(key)
        if cached is None or refresh:
            if self.client is not None:
                try:
                    cached = self.client.holidays(year)
                    with self._lock:
                        self._state["holidays"][key] = cached
                        self._save()
                except OpenAlgoError as exc:
                    logger.warning("holidays %s unavailable: %s", year, exc)
                    cached = cached or []
            else:
                cached = cached or []
        result: dict[date, dict[str, Any]] = {}
        for entry in cached:
            closed = entry.get("closed_exchanges") or []
            if self.exchange in closed or ("NSE" in closed and not closed):
                result[date.fromisoformat(entry["date"])] = entry
            elif not closed and entry.get("holiday_type") == "TRADING_HOLIDAY":
                result[date.fromisoformat(entry["date"])] = entry
        return result

    def has_holidays(self, year: int) -> bool:
        with self._lock:
            return str(year) in self._state["holidays"]

    # timings -------------------------------------------------------------

    def timings(self, day: date | str, refresh: bool = False) -> list[dict[str, Any]] | None:
        """Raw timings rows for the date, or None when unknown and offline."""
        d = _to_date(day)
        key = d.isoformat()
        with self._lock:
            cached = self._state["timings"].get(key)
        if cached is not None and not refresh:
            return cached
        if self.client is None:
            return None
        try:
            rows = self.client.timings(d)
        except OpenAlgoError as exc:
            logger.warning("timings %s unavailable: %s", key, exc)
            return cached
        with self._lock:
            self._state["timings"][key] = rows
            self._save()
        return rows

    def _session_times(self, d: date) -> tuple[datetime, datetime]:
        rows = self.timings(d) or []
        for row in rows:
            if row.get("exchange") == self.exchange:
                start = datetime.fromtimestamp(float(row["start_time"]) / 1000.0, IST)
                end = datetime.fromtimestamp(float(row["end_time"]) / 1000.0, IST)
                return start, end
        return datetime.combine(d, DEFAULT_OPEN, IST), datetime.combine(d, DEFAULT_CLOSE, IST)

    # trading days --------------------------------------------------------

    def is_trading_day(self, day: date | str) -> bool:
        """Weekends are closed; then the cached timings for the date, then the holiday list."""
        d = _to_date(day)
        if d.weekday() >= 5:
            return False
        with self._lock:
            rows = self._state["timings"].get(d.isoformat())
        if rows is not None:
            return any(row.get("exchange") == self.exchange for row in rows)
        holidays = self.holidays(d.year)
        return d not in holidays

    def next_trading_day(self, day: date | str) -> date:
        d = _to_date(day) + timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(d):
                return d
            d += timedelta(days=1)
        raise RuntimeError("no trading day found within 60 days")

    def previous_trading_day(self, day: date | str) -> date:
        d = _to_date(day) - timedelta(days=1)
        for _ in range(60):
            if self.is_trading_day(d):
                return d
            d -= timedelta(days=1)
        raise RuntimeError("no trading day found within 60 days")

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days in (start, end], i.e. excluding start and including end."""
        if end <= start:
            return 0
        count = 0
        d = start + timedelta(days=1)
        while d <= end:
            if self.is_trading_day(d):
                count += 1
            d += timedelta(days=1)
        return count

    # expiries ------------------------------------------------------------

    def set_expiries(self, expiries: list[date]) -> None:
        with self._lock:
            self._state["expiries"] = sorted({e.isoformat() for e in expiries})
            self._state["expiries_fetched_at"] = self.now_ist().isoformat()
            self._save()

    def expiries(self) -> list[date]:
        """Known option expiries: from ``expiry_source`` when set, else the cached list."""
        if self.expiry_source is not None:
            try:
                fresh = list(self.expiry_source())
                if fresh:
                    self.set_expiries(fresh)
                    return sorted(fresh)
            except OpenAlgoError as exc:
                logger.warning("expiry list unavailable: %s", exc)
        with self._lock:
            return [date.fromisoformat(text) for text in self._state["expiries"]]

    def is_expiry_day(self, day: date | str) -> bool:
        d = _to_date(day)
        known = self.expiries()
        if known and min(known) <= d <= max(known):
            return d in set(known)
        return self._fallback_expiry(d)

    def _fallback_expiry(self, d: date) -> bool:
        if not self.is_trading_day(d):
            return False
        if d.weekday() == WEEKLY_EXPIRY_WEEKDAY:
            return True
        # Tuesday holiday: the expiry moves to the previous trading day.
        probe = d + timedelta(days=1)
        while probe.weekday() != WEEKLY_EXPIRY_WEEKDAY:
            if self.is_trading_day(probe):
                return False
            probe += timedelta(days=1)
        return not self.is_trading_day(probe)

    # windows -------------------------------------------------------------

    def window_for(self, day: date | str | None = None) -> SessionWindow:
        """Session window for a date (today by default). Built even on non-trading days."""
        d = _to_date(day) if day is not None else self.today()
        market_open, market_close = self._session_times(d)
        return SessionWindow(
            trading_date=d,
            market_open=market_open,
            market_close=market_close,
            trade_start=datetime.combine(d, self.trade_start, IST),
            last_entry=datetime.combine(d, self.last_entry, IST),
            square_off=datetime.combine(d, self.square_off, IST),
            is_expiry_day=self.is_expiry_day(d),
        )

    def session_info(self, day: date | str | None = None) -> dict[str, Any]:
        """The ``session`` object of GET /api/status and /api/market/session."""
        window = self.window_for(day)
        return {
            "trading_date": window.trading_date.isoformat(),
            "is_trading_day": self.is_trading_day(window.trading_date),
            "market_open": window.market_open.isoformat(),
            "market_close": window.market_close.isoformat(),
            "trade_start": window.trade_start.isoformat(),
            "last_entry": window.last_entry.isoformat(),
            "square_off": window.square_off.isoformat(),
            "is_expiry_day": window.is_expiry_day,
            "now": self.now_ist().isoformat(),
        }

    def in_trade_window(self, when: datetime | None = None) -> bool:
        moment = when or self.now_ist()
        if not self.is_trading_day(moment.date()):
            return False
        window = self.window_for(moment.date())
        return window.trade_start <= moment < window.square_off

    def can_enter(self, when: datetime | None = None) -> bool:
        moment = when or self.now_ist()
        if not self.is_trading_day(moment.date()):
            return False
        window = self.window_for(moment.date())
        return window.trade_start <= moment <= window.last_entry

    def warm(self, years: list[int] | None = None, days: list[date] | None = None) -> None:
        """Populate the cache for offline use (holiday years and timings for specific dates)."""
        for year in years or [self.today().year]:
            self.holidays(year)
        for d in days or []:
            self.timings(d)
