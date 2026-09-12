from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.experiments.pricer import StraddlePricer
from openfly.experiments.quotes import MinuteQuotes, minute_quotes_for
from openfly.experiments.sessions import TradingCalendar
from openfly.experiments.simulator import (
    Rules,
    observation_rows,
    run_trade,
    simulate_day,
    simulate_window,
    square_off_row,
)
from tests.experiments.fakes import synthetic_market


def _quotes_with_paths(call: np.ndarray, put: np.ndarray, strike: float = 23400.0) -> MinuteQuotes:
    """A day whose recorded legs for `strike` follow the given minute paths."""
    market = synthetic_market(days=1, seed=0, vol_per_minute=0.0, level=strike)
    d = market.dates[0]
    n = market.day_view(d).n
    assert len(call) == n and len(put) == n
    pricer = StraddlePricer(factor=1.0, calendar=market.calendar)
    recorded = {
        (strike, "CE"): (np.asarray(call, dtype=np.float64), np.zeros(n, dtype=bool)),
        (strike, "PE"): (np.asarray(put, dtype=np.float64), np.zeros(n, dtype=bool)),
    }
    return MinuteQuotes(market.day_view(d), market.calendar.next_expiry(d), pricer, 12.0, market.calendar, recorded=recorded)


def _flat(n=375, c=100.0, p=100.0):
    return np.full(n, c), np.full(n, p)


def _fixed_rules() -> Rules:
    """The fixed percentage rules (30 percent per leg, 25 percent combined) these tests are written for."""
    rules = Rules.from_settings(DEFAULT_SETTINGS)
    rules.stop_mode = "fixed"
    return rules


def test_square_off_and_costs():
    call, put = _flat()
    q = _quotes_with_paths(call, put)
    rules = _fixed_rules()
    entry = 4  # bar completed at 09:20
    t = run_trade(q, entry, rules)
    assert t.exit_reason == "SQUARE_OFF" and t.exit_minute == 360 and t.entry_minute == 5
    assert t.pnl_points == pytest.approx(-0.2)  # spread only on a flat path
    assert t.cost_inr > 100 and t.net_inr == pytest.approx(t.gross_inr - t.cost_inr)
    assert t.source == "recorded" and t.synthetic_fraction == 0.0
    assert square_off_row(q, rules) == 359


def test_combined_stop_and_target():
    call, put = _flat()
    call[50:] = 130.0  # combined 230 = 15 percent up, no exit; then
    call[80:] = 160.0  # combined 260 = 30 percent up: combined stop (legs at +60 and 0 percent, no leg stop)
    q = _quotes_with_paths(call, put)
    rules = _fixed_rules()
    rules.leg_stop_pct = 100.0
    t = run_trade(q, 4, rules)
    assert t.exit_reason == "STOP" and t.exit_row == 80 and t.call_exit == 160.0
    call, put = _flat()
    call[40:] = 50.0
    put[40:] = 60.0  # combined 110 = 45 percent down: target
    t = run_trade(_quotes_with_paths(call, put), 4, rules)
    assert t.exit_reason == "TARGET" and t.exit_row == 40 and t.pnl_points == pytest.approx(90.0 - 0.2)


def test_lock_moves_stop_to_entry_credit():
    call, put = _flat()
    call[30:] = 80.0  # combined 180: 10 percent down
    call[40:] = 70.0  # combined 170: 15 percent down, lock armed
    call[60:] = 100.0  # back to the credit: lock exit
    rules = _fixed_rules()
    t = run_trade(_quotes_with_paths(call, put), 4, rules)
    assert t.exit_reason == "LOCK" and t.exit_row == 60


def test_leg_stop_hold_other_then_target():
    call, put = _flat()
    call[20:] = 131.0  # call up 31 percent: leg stop at row 20 (combined 231, below the 25 percent combined stop)
    put[70:] = 55.0  # the surviving put falls 45 percent: its own target
    rules = _fixed_rules()
    assert rules.on_leg_stop == "hold_other"
    t = run_trade(_quotes_with_paths(call, put), 4, rules)
    assert t.exit_reason == "LEG_STOP" and t.call_reason == "LEG_STOP" and t.call_exit_row == 20
    assert t.put_reason == "TARGET" and t.put_exit_row == 70 and t.put_exit == 55.0
    assert t.leg_stops == 1 and t.exit_row == 70
    assert t.pnl_points == pytest.approx((100 - 131) + (100 - 55) - 0.2)
    # exit_both closes both legs at the leg stop minute
    rules.on_leg_stop = "exit_both"
    t2 = run_trade(_quotes_with_paths(call, put), 4, rules)
    assert t2.exit_row == 20 and t2.put_reason == "LEG_STOP_OTHER" and t2.put_exit == 100.0


def test_readout_exit_and_dynamic_reentry():
    call, put = _flat()
    q = _quotes_with_paths(call, put)
    rules = _fixed_rules()
    obs = observation_rows(q, 1)
    entry = np.zeros(q.n, dtype=bool)
    exit_ = np.zeros(q.n, dtype=bool)
    entry[[4, 30, 31, 32, 100]] = True  # 09:20, 09:46 (inside cooldown of the 09:45 exit), 09:47, 09:48, 10:56
    exit_[[29, 150]] = True  # 09:45 and 11:46
    trades = simulate_day(q, rules, obs, entry_signal=entry, exit_signal=exit_)
    # The 09:45 exit starts a 5 minute cooldown: the signals at rows 30 to 32 are refused and the
    # next signal at row 100 opens the second straddle, closed by the EXIT signal at row 150.
    assert [t.entry_row for t in trades] == [4, 100]
    assert trades[0].exit_reason == "EXIT" and trades[0].exit_row == 29
    assert trades[1].entry_row == 100 and trades[1].exit_row == 150 and trades[1].exit_reason == "EXIT"
    assert trades[1].strike == trades[0].strike  # flat synthetic path keeps the ATM
    rules.max_entries_per_day = 1
    assert len(simulate_day(q, rules, obs, entry_signal=entry, exit_signal=exit_)) == 1


def test_fixed_entry_reenters_and_random_matches_rows():
    call, put = _flat()
    call[60:] = 50.0
    put[60:] = 60.0  # target at row 60 for the first straddle
    call[61:] = 100.0
    put[61:] = 100.0  # then flat again
    q = _quotes_with_paths(call, put)
    rules = _fixed_rules()
    obs = observation_rows(q, 5)
    trades = simulate_day(q, rules, obs, always_enter=True)
    assert trades[0].entry_row == 4 and trades[0].exit_reason == "TARGET" and trades[0].exit_row == 60
    assert trades[1].entry_row == 69  # first observation row (close minute multiple of 5) after the 5 minute cooldown
    assert trades[-1].exit_reason == "SQUARE_OFF"
    picked = simulate_day(q, rules, obs, entry_rows=np.array([9, 14, 199, 200]))
    assert [t.entry_row for t in picked] == [9, 199]  # 14 is inside the first straddle, 200 is off the 5 minute grid
    res = simulate_window([(date(2026, 7, 1), q)], rules, 5, always_enter=True)
    assert len(res.trades) == len(trades) and res.daily_pnl.iloc[0] == pytest.approx(sum(t.net_inr for t in trades))


def test_minute_quotes_for_synthetic_day():
    market = synthetic_market(days=1, seed=11)
    d = market.dates[0]
    q = minute_quotes_for(d, market=market, settings=DEFAULT_SETTINGS, prefer_recorded=False)
    assert q.source == "synthetic" and q.synthetic_fraction == 1.0 and q.n == 375
    rules = _fixed_rules()
    t = run_trade(q, 4, rules)
    assert t.credit > 0 and t.exit_minute <= 360
    assert isinstance(market.calendar, TradingCalendar)
