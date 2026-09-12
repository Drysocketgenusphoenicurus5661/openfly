"""Expiry selection and trading-time arithmetic for the straddle.

The straddle trades the CURRENT MONTH expiry by default (the last expiry of the
calendar month, for example 29-SEP-26 in September); `strategy.expiry_selection`
"weekly" selects the nearest weekly expiry instead. The market package's
ChainResolver owns the real expiry list (`select_expiry(settings)`,
`expiry_for_date(trading_date, selection)`); this module asks it when it is
available and otherwise applies the exchange rule: NIFTY expiries fall on
Tuesdays, the monthly one is the last Tuesday of the month, and an expiry that
lands on a holiday moves to the previous trading day.
"""

from __future__ import annotations

import calendar as _calendar
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_WEEKDAY = 1  # Tuesday
SESSION_MINUTES = 375  # 09:15 to 15:30
SESSION_CLOSE = time(15, 30)

TradingDayFn = Callable[[date], bool]


def weekday_rule(day: date) -> bool:
    return day.weekday() < 5


def expiry_label(expiry: date) -> str:
    return expiry.strftime("%d-%b-%y").upper()


def selection_of(settings: dict) -> str:
    value = str(settings.get("strategy", {}).get("expiry_selection", "monthly")).lower()
    return value if value in ("monthly", "weekly") else "monthly"


def _previous_trading_day(day: date, is_trading_day: TradingDayFn) -> date:
    probe = day
    for _ in range(14):
        if is_trading_day(probe):
            return probe
        probe -= timedelta(days=1)
    return day


def last_tuesday_of_month(year: int, month: int) -> date:
    last = date(year, month, _calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - EXPIRY_WEEKDAY) % 7)


def monthly_expiry_for(day: date, is_trading_day: TradingDayFn | None = None) -> date:
    """The current month's last expiry on or after `day`, else next month's."""
    is_td = is_trading_day or weekday_rule
    candidate = _previous_trading_day(last_tuesday_of_month(day.year, day.month), is_td)
    if candidate >= day:
        return candidate
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return _previous_trading_day(last_tuesday_of_month(year, month), is_td)


def weekly_expiry_for(day: date, is_trading_day: TradingDayFn | None = None) -> date:
    """The nearest weekly expiry on or after `day`."""
    is_td = is_trading_day or weekday_rule
    tuesday = day + timedelta(days=(EXPIRY_WEEKDAY - day.weekday()) % 7)
    candidate = _previous_trading_day(tuesday, is_td)
    if candidate >= day:
        return candidate
    return _previous_trading_day(tuesday + timedelta(days=7), is_td)


def rule_expiry_for(day: date, selection: str, is_trading_day: TradingDayFn | None = None) -> date:
    if selection == "weekly":
        return weekly_expiry_for(day, is_trading_day)
    return monthly_expiry_for(day, is_trading_day)


def select_expiry_for(
    day: date,
    settings: dict,
    resolver: Any = None,
    is_trading_day: TradingDayFn | None = None,
) -> tuple[date, str]:
    """(expiry, source). Asks the resolver first, then applies the rule."""
    selection = selection_of(settings)
    if resolver is not None:
        for name, call in (
            ("ChainResolver.expiry_for_date", lambda: resolver.expiry_for_date(day, selection)),
            ("ChainResolver.select_expiry", lambda: resolver.select_expiry(settings)),
        ):
            fn_name = name.split(".")[-1]
            if getattr(resolver, fn_name, None) is None:
                continue
            try:
                value = call()
            except Exception:
                continue
            if isinstance(value, datetime):
                value = value.date()
            if isinstance(value, date):
                return value, name
    return rule_expiry_for(day, selection, is_trading_day), f"rule: {'last Tuesday of the month' if selection == 'monthly' else 'next Tuesday'}"


def trading_days_between(start: date, end: date, is_trading_day: TradingDayFn | None = None) -> int:
    """Trading days in (start, end]: strictly after start, up to and including end."""
    is_td = is_trading_day or weekday_rule
    if end <= start:
        return 0
    count = 0
    probe = start + timedelta(days=1)
    while probe <= end:
        if is_td(probe):
            count += 1
        probe += timedelta(days=1)
    return count


def trading_minutes_to_expiry(now: datetime, expiry: date, is_trading_day: TradingDayFn | None = None) -> float:
    """Trading minutes from `now` to the expiry's 15:30: the rest of today plus 375 per trading day.

    Never below one minute. A past expiry counts as one minute.
    """
    is_td = is_trading_day or weekday_rule
    today = now.date()
    tz = now.tzinfo or IST
    rest = 0.0
    if is_td(today) and expiry >= today:
        close = datetime.combine(today, SESSION_CLOSE, tz)
        rest = min(float(SESSION_MINUTES), max(0.0, (close - now).total_seconds() / 60.0))
    if expiry <= today:
        return max(1.0, rest)
    days = trading_days_between(today, expiry, is_td)
    return max(1.0, rest + days * SESSION_MINUTES)
