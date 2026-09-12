"""Monthly expiry selection, trading minutes to expiry, and a monthly-expiry entry."""

from __future__ import annotations

import math
from datetime import date

import pytest

from openfly.straddle.engine import Action, StopSizer
from openfly.straddle.expiry import (
    last_tuesday_of_month,
    monthly_expiry_for,
    rule_expiry_for,
    select_expiry_for,
    trading_days_between,
    trading_minutes_to_expiry,
    weekly_expiry_for,
)

from .helpers import ENTER, at, make_engine, observation, observe, settings_with, sq

MONTHLY = date(2026, 9, 29)
WEEKLY = date(2026, 9, 15)


def test_monthly_rule_matches_the_listed_expiries():
    assert last_tuesday_of_month(2026, 9) == MONTHLY
    assert last_tuesday_of_month(2026, 10) == date(2026, 10, 27)
    assert monthly_expiry_for(date(2026, 9, 11)) == MONTHLY
    assert monthly_expiry_for(MONTHLY) == MONTHLY  # expiry day trades the expiring contract
    assert monthly_expiry_for(date(2026, 9, 30)) == date(2026, 10, 27)
    assert monthly_expiry_for(date(2026, 12, 31)) == date(2027, 1, 26)
    assert weekly_expiry_for(date(2026, 9, 11)) == WEEKLY
    assert weekly_expiry_for(WEEKLY) == WEEKLY
    assert weekly_expiry_for(date(2026, 9, 16)) == date(2026, 9, 22)
    # a Tuesday holiday moves the expiry to the previous trading day
    holiday = date(2026, 9, 29)
    assert monthly_expiry_for(date(2026, 9, 11), lambda d: d.weekday() < 5 and d != holiday) == date(2026, 9, 28)
    assert rule_expiry_for(date(2026, 9, 11), "weekly") == WEEKLY and rule_expiry_for(date(2026, 9, 11), "monthly") == MONTHLY


def test_select_expiry_prefers_the_resolver_and_falls_back_to_the_rule():
    class Resolver:
        def __init__(self):
            self.calls = []

        def expiry_for_date(self, day, selection):
            self.calls.append((day, selection))
            return date(2026, 9, 28)

    resolver = Resolver()
    expiry, source = select_expiry_for(date(2026, 9, 11), settings_with(strategy__expiry_selection="monthly"), resolver=resolver)
    assert (expiry, source) == (date(2026, 9, 28), "ChainResolver.expiry_for_date")
    assert resolver.calls == [(date(2026, 9, 11), "monthly")]

    class Selecting:
        def select_expiry(self, settings):
            return date(2026, 9, 29)

    assert select_expiry_for(date(2026, 9, 11), settings_with(), resolver=Selecting()) == (MONTHLY, "ChainResolver.select_expiry")

    class Broken:
        def expiry_for_date(self, day, selection):
            raise RuntimeError("no list")

    expiry, source = select_expiry_for(date(2026, 9, 11), settings_with(strategy__expiry_selection="monthly"), resolver=Broken())
    assert expiry == MONTHLY and source.startswith("rule")
    expiry, source = select_expiry_for(date(2026, 9, 11), settings_with(strategy__expiry_selection="weekly"))
    assert expiry == WEEKLY and source == "rule: next Tuesday"


def test_trading_minutes_to_expiry_counts_sessions_not_calendar_time():
    now = at(10, 20)  # Friday 11-SEP-26
    assert trading_days_between(date(2026, 9, 11), MONTHLY) == 12
    assert trading_minutes_to_expiry(now, MONTHLY) == 310 + 12 * 375  # 4810
    assert trading_minutes_to_expiry(now, WEEKLY) == 310 + 2 * 375  # 1060
    assert trading_minutes_to_expiry(at(10, 20, day=WEEKLY), WEEKLY) == 310
    assert trading_minutes_to_expiry(at(15, 45), date(2026, 9, 11)) == 1.0
    assert trading_minutes_to_expiry(at(9, 0), date(2026, 9, 11)) == 375
    holiday = date(2026, 9, 21)
    assert trading_minutes_to_expiry(now, MONTHLY, lambda d: d.weekday() < 5 and d != holiday) == 310 + 11 * 375
    calendar_minutes = (at(15, 30, day=MONTHLY) - now).total_seconds() / 60
    assert calendar_minutes > 25000 > trading_minutes_to_expiry(now, MONTHLY)


def test_adaptive_stops_use_trading_minutes_to_the_selected_expiry():
    sizer = StopSizer(settings_with(strategy__stop_mode="adaptive", strategy__expiry_selection="monthly"))
    quote = sq(at(10, 20), 180.0, 175.0, expiry=MONTHLY)
    basis = sizer.compute(quote, observation(at(10, 20), n_bars=10), at(10, 20))
    assert basis.minutes_to_expiry == 4810
    assert basis.implied_move_points == pytest.approx(355.0 * math.sqrt(60 / 4810))
    weekly = sizer.compute(sq(at(10, 20), 101.2, 100.1, expiry=WEEKLY), observation(at(10, 20), n_bars=10), at(10, 20))
    assert weekly.minutes_to_expiry == 1060
    assert weekly.implied_move_points == pytest.approx(201.3 * math.sqrt(60 / 1060))
    # a bigger premium with far more sessions left still implies a modest one-hour move
    assert basis.implied_move_points < weekly.implied_move_points * 2
    # the monthly straddle's gamma is smaller (derived from the larger premium)
    assert basis.greeks["ce"]["gamma"] < weekly.greeks["ce"]["gamma"]


def test_monthly_expiry_entry_snapshot_trace_and_narrative():
    engine, broker, settings = make_engine(strategy__stop_mode="adaptive", strategy__expiry_selection="monthly")
    assert engine.expiry_selection == "monthly"
    step = observe(engine, broker, at(10, 20), ENTER, 180.0, 175.0, expiry=MONTHLY, ret_std=0.0003)
    assert step.action == Action.ENTER.value
    pos = engine.position
    assert pos.expiry == MONTHLY and pos.basis.minutes_to_expiry == 4810
    assert all(leg.contract.expiry == MONTHLY and "29SEP26" in leg.symbol for leg in pos.legs.values())
    snap = step.straddle
    assert snap["expiry"] == "2026-09-29" and snap["expiry_selection"] == "monthly"
    assert snap["stop_basis"]["minutes_to_expiry"] == 4810
    trace = engine.to_trace_step(step, 0)
    assert trace["expiry"] == "2026-09-29" and trace["expiry_selection"] == "monthly"
    assert trace["straddle"]["expiry_selection"] == "monthly"
    text = step.narrative
    assert "Sold 1 lot of the 29-SEP-26 monthly straddle at 23350 for 355.0 points credit (INR 23,075)." in text
    assert "Expected one-hour move" in text
    flat = make_engine(strategy__expiry_selection="monthly")[0].state_snapshot()
    assert flat["expiry"] is None and flat["expiry_selection"] == "monthly"
    weekly_engine, weekly_broker, _ = make_engine()
    weekly_step = observe(weekly_engine, weekly_broker, at(10, 20), ENTER, 101.2, 100.1)
    assert "Sold 1 lot of the 15-SEP-26 weekly straddle at 23350" in weekly_step.narrative
