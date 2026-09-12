"""Every guard check has a passing and a failing case with a readable detail."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from openfly.interfaces import Decision, Prediction, Quote, StraddleQuote
from openfly.straddle.guard import Guard, GuardContext

from .helpers import DAY, EXPIRY, at, settings_with, sq, window

ENTRY_CHECKS = [
    "trading_day",
    "trade_window",
    "last_entry",
    "expiry_min_dte",
    "vix_ceiling",
    "spread",
    "quote_age",
    "index_move",
    "daily_loss_limit",
    "entries_per_day",
    "reentry_cooldown",
    "position_flat",
    "lots_bounds",
    "margin_available",
    "stop_file",
    "halted",
    "pending_intent",
    "prediction_present",
]


def passing_ctx() -> GuardContext:
    return GuardContext(
        now=at(10, 20),
        window=window(),
        is_trading_day=True,
        quote=sq(at(10, 20), 101.2, 100.1, spread=0.10),
        quote_age_s=1.0,
        prediction=Prediction(0.82, 0.6, Decision.ENTER),
        vix=12.1,
        days_to_expiry=3.6,
        observation_index=23350.0,
        index_now=23355.0,
        day_pnl=-500.0,
        entries_today=1,
        last_exit_at=None,
        in_position=False,
        lots=1,
        margin_available=400000.0,
        margin_per_lot=188700.0,
        stop_file_present=False,
        halted=False,
        pending_intent=False,
    )


def test_all_entry_checks_pass_with_readable_details():
    guard = Guard(settings_with())
    result = guard.check_entry(passing_ctx())
    assert result.allowed
    assert [c.name for c in result.checks] == ENTRY_CHECKS
    details = {c.name: c.detail for c in result.checks}
    assert details["trade_window"] == "10:20 is inside the trade window 09:20 to 14:30"
    assert details["last_entry"].startswith("10:20 is before the last entry time 14:30")
    assert details["vix_ceiling"] == "INDIAVIX 12.1 is below the ceiling 20"
    assert "INR 400,000" in details["margin_available"]
    assert "1 lot is within 1 to 3" in details["lots_bounds"]
    assert result.summary() == "All 18 checks passed."


def wide_spread_quote() -> StraddleQuote:
    q = sq(at(10, 20), 101.2, 100.1)
    return StraddleQuote(call=Quote(q.call.symbol, "NFO", 101.2, 100.0, 103.0, q.call.timestamp), put=q.put, strike=q.strike, expiry=q.expiry)


@pytest.mark.parametrize(
    ("name", "changes", "settings", "expected"),
    [
        ("trading_day", {"is_trading_day": False}, {}, "not a trading day"),
        ("trading_day", {"window": None}, {}, "no session window"),
        ("trade_window", {"now": at(15, 0)}, {}, "15:00 is outside the trade window 09:20 to 14:30"),
        ("trade_window", {"now": at(9, 17)}, {}, "09:17 is outside the trade window"),
        ("last_entry", {"now": at(14, 35)}, {}, "14:35 is past the last entry time 14:30"),
        ("expiry_min_dte", {"quote": sq(at(10, 20), 101.2, 100.1, expiry=DAY)}, {"strategy__min_days_to_expiry": 1}, "expiry day"),
        ("vix_ceiling", {"vix": 21.3}, {}, "INDIAVIX 21.3 is above the ceiling 20"),
        ("vix_ceiling", {"vix": None}, {}, "unavailable"),
        ("spread", {"quote": wide_spread_quote()}, {}, "call spread 3.00 is 1.49 percent of the 201.3 premium"),
        ("quote_age", {"quote_age_s": 7.5}, {}, "7.5 seconds old, older than 5 seconds"),
        ("index_move", {"index_now": 23450.0}, {}, "more than 0.3 percent"),
        ("daily_loss_limit", {"day_pnl": -10000.0}, {}, "reached the daily loss limit INR 10,000"),
        ("entries_per_day", {"entries_today": 10}, {}, "all 10 entries for the day are used"),
        ("reentry_cooldown", {"last_exit_at": at(10, 17)}, {}, "cooldown of 5 minutes runs until 10:22"),
        ("position_flat", {"in_position": True}, {}, "already open"),
        ("lots_bounds", {"lots": 4}, {}, "4 lots is above the maximum of 3"),
        ("lots_bounds", {"lots": 0}, {}, "0 lots is below the minimum of 1"),
        ("margin_available", {"margin_available": 100000.0}, {}, "INR 100,000 is below INR 188,700"),
        ("stop_file", {"stop_file_present": True}, {}, "STOP file"),
        ("halted", {"halted": True, "halt_reason": "unresolved order"}, {}, "halted: unresolved order"),
        ("pending_intent", {"pending_intent": True}, {}, "still pending"),
        ("prediction_present", {"prediction": None}, {}, "no readout prediction"),
    ],
)
def test_each_entry_check_can_fail(name, changes, settings, expected):
    guard = Guard(settings_with(**settings))
    ctx = replace(passing_ctx(), **changes)
    result = guard.check_entry(ctx)
    assert not result.allowed
    failed = {c.name: c.detail for c in result.failed}
    assert name in failed, failed
    assert expected in failed[name]
    assert result.summary().startswith("The guard vetoed: ")


def test_expiry_day_allowed_with_min_dte_zero_and_refused_with_one():
    ctx = replace(passing_ctx(), quote=sq(at(10, 20), 101.2, 100.1, expiry=DAY), window=window(DAY, is_expiry_day=True))
    ok = Guard(settings_with(strategy__min_days_to_expiry=0)).expiry_min_dte(ctx)
    assert ok.ok and "expiry-day trading is allowed" in ok.detail
    no = Guard(settings_with(strategy__min_days_to_expiry=1)).expiry_min_dte(ctx)
    assert not no.ok and "at least 1 trading day to expiry is required" in no.detail
    later = Guard(settings_with(strategy__min_days_to_expiry=1)).expiry_min_dte(replace(ctx, quote=sq(at(10, 20), 101.2, 100.1, expiry=EXPIRY)))
    assert later.ok and later.detail == "the 15-SEP-26 weekly expiry is 2 trading days away, at least 1 required"
    monthly = Guard(settings_with(strategy__min_days_to_expiry=3, strategy__expiry_selection="monthly")).expiry_min_dte(
        replace(ctx, quote=sq(at(10, 20), 180.0, 175.0, expiry=date(2026, 9, 29)))
    )
    assert monthly.ok and monthly.detail == "the 29-SEP-26 monthly expiry is 12 trading days away, at least 3 required"
    holiday_aware = Guard(settings_with(strategy__min_days_to_expiry=3), is_trading_day=lambda d: d.weekday() < 5 and d != date(2026, 9, 14))
    assert "1 trading day away" in holiday_aware.expiry_min_dte(replace(ctx, quote=sq(at(10, 20), 101.2, 100.1, expiry=EXPIRY))).detail


def test_entries_per_day_zero_means_unlimited():
    guard = Guard(settings_with(strategy__max_entries_per_day=0))
    check = guard.entries_per_day(replace(passing_ctx(), entries_today=50))
    assert check.ok and "no daily limit" in check.detail


def test_reentry_cooldown_passes_after_the_cooldown():
    check = Guard(settings_with()).reentry_cooldown(replace(passing_ctx(), last_exit_at=at(10, 14)))
    assert check.ok and "cooldown of 5 minutes ended 10:19" in check.detail


def test_skipped_checks_say_so():
    guard = Guard(settings_with())
    ctx = replace(passing_ctx(), quote_age_s=None, margin_available=None, observation_index=None)
    result = guard.check_entry(ctx)
    assert result.allowed
    details = {c.name: c.detail for c in result.checks}
    assert "skipped" in details["quote_age"]
    assert "skipped" in details["margin_available"]
    assert "skipped" in details["index_move"]


def test_exit_checks():
    guard = Guard(settings_with())
    ok = guard.check_exit(passing_ctx())
    assert ok.allowed and [c.name for c in ok.checks] == ["trading_day", "halted", "pending_intent", "quote_present"]
    blocked = guard.check_exit(replace(passing_ctx(), pending_intent=True))
    assert not blocked.allowed and blocked.failed[0].name == "pending_intent"
    no_quote = guard.check_exit(replace(passing_ctx(), quote=None))
    assert not no_quote.allowed and "no straddle quote" in no_quote.failed[0].detail


def test_guard_result_serialises():
    result = Guard(settings_with()).check_entry(passing_ctx())
    data = result.to_dict()
    assert data["allowed"] is True
    assert data["checks"][0] == {"name": "trading_day", "ok": True, "detail": "11-Sep-2026 is a trading day"}
