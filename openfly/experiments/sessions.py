"""Session and expiry arithmetic in trading time.

One NSE session runs 09:15 to 15:30 (375 minutes). Time to expiry is counted
in sessions: full sessions after today up to and including the expiry date,
plus the fraction of today's session still to run. NIFTY weekly options
expire on Tuesday from 2025-09-01 and expired on Thursday before that; when
the expiry weekday is a holiday the contract expires on the previous trading
day, which this module detects when it knows the trading dates.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
SESSION_MINUTES = 375
TRADING_DAYS_PER_YEAR = 252
TUESDAY_EXPIRY_FROM = date(2025, 9, 1)


def expiry_weekday(d: date) -> int:
    """0 Monday ... 6 Sunday. Tuesday from 2025-09-01, Thursday before."""
    return 1 if d >= TUESDAY_EXPIRY_FROM else 3


def session_open(d: date) -> datetime:
    return datetime.combine(d, SESSION_OPEN, tzinfo=IST)


def session_close(d: date) -> datetime:
    return datetime.combine(d, SESSION_CLOSE, tzinfo=IST)


def at_time(d: date, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(hh, mm), tzinfo=IST)


def _as_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def minutes_since_open(ts: datetime) -> int:
    ts = _as_ist(ts)
    return int((ts - session_open(ts.date())).total_seconds() // 60)


def remaining_session_fraction(ts: datetime) -> float:
    ts = _as_ist(ts)
    left = (session_close(ts.date()) - ts).total_seconds() / 60.0
    return float(min(1.0, max(0.0, left / SESSION_MINUTES)))


def weekdays_between(after: date, upto: date) -> int:
    """Weekdays strictly after `after` up to and including `upto`."""
    if upto <= after:
        return 0
    n = 0
    d = after + timedelta(days=1)
    while d <= upto:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


class TradingCalendar:
    """Known trading dates (from the bar history) with weekday fallback outside coverage."""

    def __init__(self, trading_dates: Iterable[date] = ()):
        self.dates: list[date] = sorted({d for d in trading_dates})
        self._set = set(self.dates)

    @property
    def first(self) -> date | None:
        return self.dates[0] if self.dates else None

    @property
    def last(self) -> date | None:
        return self.dates[-1] if self.dates else None

    def covers(self, d: date) -> bool:
        return bool(self.dates) and self.first <= d <= self.last

    def is_trading_day(self, d: date) -> bool:
        if self.covers(d):
            return d in self._set
        return d.weekday() < 5

    def previous_trading_day(self, d: date) -> date:
        d = d - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def next_trading_day(self, d: date) -> date:
        d = d + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def sessions_between(self, after: date, upto: date) -> int:
        """Sessions strictly after `after` up to and including `upto`."""
        if upto <= after:
            return 0
        if self.dates and self.covers(after + timedelta(days=1)) and self.covers(upto):
            lo = bisect_right(self.dates, after)
            hi = bisect_right(self.dates, upto)
            return hi - lo
        if not self.dates or upto < self.first or after >= self.last:
            return weekdays_between(after, upto)
        # Partially covered range: count the covered part exactly and the rest by weekdays.
        n = 0
        d = after + timedelta(days=1)
        while d <= upto:
            if self.is_trading_day(d):
                n += 1
            d += timedelta(days=1)
        return n

    def next_expiry(self, d: date) -> date:
        wd = expiry_weekday(d)
        exp = d + timedelta(days=(wd - d.weekday()) % 7)
        if not self.is_trading_day(exp):
            moved = self.previous_trading_day(exp)
            if moved >= d:
                return moved
            # The moved expiry already passed (today is after the holiday-adjusted date): next week.
            nxt = exp + timedelta(days=7)
            return nxt if self.is_trading_day(nxt) else self.previous_trading_day(nxt)
        return exp

    def days_to_expiry(self, ts: datetime, expiry: date | None = None) -> float:
        """Trading days to expiry including the fraction of the current session."""
        ts = _as_ist(ts)
        today = ts.date()
        if expiry is None:
            expiry = self.next_expiry(today)
        if today > expiry:
            return 0.0
        full = self.sessions_between(today, expiry)
        return float(full) + remaining_session_fraction(ts)

    def minutes_to_expiry(self, ts: datetime, expiry: date | None = None) -> float:
        return self.days_to_expiry(ts, expiry) * SESSION_MINUTES

    def dates_between(self, start: date, end: date) -> list[date]:
        lo = bisect_left(self.dates, start)
        hi = bisect_right(self.dates, end)
        return self.dates[lo:hi]


def next_expiry(d: date, calendar: TradingCalendar | None = None) -> date:
    return (calendar or TradingCalendar()).next_expiry(d)


def trading_days_to_expiry(ts: datetime, expiry: date | None = None, calendar: TradingCalendar | None = None) -> float:
    return (calendar or TradingCalendar()).days_to_expiry(ts, expiry)


def years_to_expiry(days: float, floor_minutes: float = 1.0) -> float:
    """Trading days to a year fraction, floored at `floor_minutes` of trading time."""
    minutes = max(float(days) * SESSION_MINUTES, floor_minutes)
    return minutes / (SESSION_MINUTES * TRADING_DAYS_PER_YEAR)
