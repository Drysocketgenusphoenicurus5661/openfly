from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from openfly.interfaces import SessionWindow
from openfly.market.client import IST
from openfly.market.session import SessionCalendar

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=IST)  # Saturday


class StubCalendarClient:
    def __init__(self):
        self.holiday_calls = 0
        self.timing_calls = 0

    def holidays(self, year):
        self.holiday_calls += 1
        return [
            {
                "date": f"{year}-10-02",
                "description": "Gandhi Jayanti",
                "holiday_type": "TRADING_HOLIDAY",
                "closed_exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX"],
                "open_exchanges": [],
            },
            {
                "date": f"{year}-09-22",
                "description": "Test Tuesday holiday",
                "holiday_type": "TRADING_HOLIDAY",
                "closed_exchanges": ["NSE", "NFO"],
                "open_exchanges": [],
            },
            {
                "date": f"{year}-02-19",
                "description": "Settlement only",
                "holiday_type": "SETTLEMENT_HOLIDAY",
                "closed_exchanges": [],
                "open_exchanges": [],
            },
        ]

    def timings(self, day):
        self.timing_calls += 1
        if day.weekday() >= 5:
            return []
        start = int(datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST).timestamp() * 1000)
        end = int(datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST).timestamp() * 1000)
        return [
            {"exchange": "NSE", "start_time": start, "end_time": end},
            {"exchange": "NFO", "start_time": start, "end_time": end},
        ]


def make(paths, settings, client=None, now=NOW, **kw) -> SessionCalendar:
    return SessionCalendar(client, settings, paths, now=lambda: now, **kw)


def test_window_from_settings_and_tuesday_fallback(paths, settings):
    settings["strategy"]["last_entry"] = "14:00"
    cal = make(paths, settings)
    window = cal.window_for(date(2026, 9, 15))
    assert isinstance(window, SessionWindow)
    assert window.market_open == datetime(2026, 9, 15, 9, 15, tzinfo=IST)
    assert window.market_close == datetime(2026, 9, 15, 15, 30, tzinfo=IST)
    assert window.trade_start == datetime(2026, 9, 15, 9, 20, tzinfo=IST)
    assert window.last_entry == datetime(2026, 9, 15, 14, 0, tzinfo=IST)
    assert window.square_off == datetime(2026, 9, 15, 15, 15, tzinfo=IST)
    assert window.is_expiry_day is True  # Tuesday
    assert cal.window_for(date(2026, 9, 16)).is_expiry_day is False
    assert cal.is_trading_day(date(2026, 9, 12)) is False  # Saturday
    assert cal.now_ist() == NOW and cal.today() == date(2026, 9, 12)


def test_expiry_list_overrides_weekday_rule(paths, settings):
    cal = make(paths, settings)
    cal.set_expiries([date(2026, 9, 15), date(2026, 9, 22), date(2026, 10, 1)])
    assert cal.is_expiry_day(date(2026, 9, 15))
    assert cal.is_expiry_day(date(2026, 10, 1))  # a Thursday, from the list
    assert not cal.is_expiry_day(date(2026, 9, 29))  # Tuesday but not in the list
    assert cal.is_expiry_day(date(2026, 9, 8))  # outside the list's range: weekday fallback
    # The list survives a restart through the JSON cache.
    again = make(paths, settings)
    assert again.expiries() == [date(2026, 9, 15), date(2026, 9, 22), date(2026, 10, 1)]


def test_holidays_and_timings_are_cached_for_offline_use(paths, settings):
    client = StubCalendarClient()
    cal = make(paths, settings, client)
    assert cal.is_trading_day(date(2026, 10, 2)) is False
    assert cal.is_trading_day(date(2026, 10, 1)) is True
    assert cal.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 5)
    assert cal.previous_trading_day(date(2026, 10, 5)) == date(2026, 10, 1)
    assert client.holiday_calls == 1
    window = cal.window_for(date(2026, 9, 11))
    assert window.market_open == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    assert client.timing_calls == 1
    cal.window_for(date(2026, 9, 11))
    assert client.timing_calls == 1
    raw = json.loads(cal.cache_file.read_text())
    assert "2026" in raw["holidays"] and "2026-09-11" in raw["timings"]

    offline = make(paths, settings, client=None)
    assert offline.is_trading_day(date(2026, 10, 2)) is False
    assert offline.window_for(date(2026, 9, 11)).market_close == datetime(2026, 9, 11, 15, 30, tzinfo=IST)
    assert offline.trading_days_between(date(2026, 9, 11), date(2026, 9, 15)) == 2


def test_tuesday_holiday_moves_expiry_to_previous_trading_day(paths, settings):
    cal = make(paths, settings, StubCalendarClient())
    assert cal.is_trading_day(date(2026, 9, 22)) is False
    assert cal.is_expiry_day(date(2026, 9, 21)) is True  # Monday before the Tuesday holiday
    assert cal.is_expiry_day(date(2026, 9, 22)) is False
    assert cal.is_expiry_day(date(2026, 9, 15)) is True


def test_timings_cache_marks_holidays_as_closed(paths, settings):
    cal = make(paths, settings, StubCalendarClient())
    cal._state["timings"]["2026-09-14"] = []  # what the API returns on a holiday
    assert cal.is_trading_day(date(2026, 9, 14)) is False


def test_session_info_and_trade_window(paths, settings):
    cal = make(paths, settings, now=datetime(2026, 9, 15, 10, 4, 12, tzinfo=IST))
    info = cal.session_info()
    assert info["trading_date"] == "2026-09-15"
    assert info["is_trading_day"] is True
    assert info["trade_start"] == "2026-09-15T09:20:00+05:30"
    assert info["last_entry"] == "2026-09-15T14:30:00+05:30"
    assert info["square_off"] == "2026-09-15T15:15:00+05:30"
    assert info["is_expiry_day"] is True
    assert info["now"] == "2026-09-15T10:04:12+05:30"
    assert cal.in_trade_window() and cal.can_enter()
    assert not cal.can_enter(datetime(2026, 9, 15, 14, 31, tzinfo=IST))
    assert not cal.in_trade_window(datetime(2026, 9, 15, 15, 15, tzinfo=IST))
    assert not cal.in_trade_window(datetime(2026, 9, 12, 10, 0, tzinfo=IST))
    assert cal.next_trading_day(date(2026, 9, 11)) == date(2026, 9, 14)
    assert cal.next_trading_day(date(2026, 9, 11) + timedelta(days=3)) == date(2026, 9, 15)
