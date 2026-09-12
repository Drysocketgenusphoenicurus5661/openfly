from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from openfly.config import PATHS, history_path
from openfly.experiments.pricer import (
    StraddlePricer,
    bs_prices,
    calibrate,
    implied_move_points,
    realized_move_points,
    synthetic_minute_quotes,
)
from openfly.experiments.quotes import SourcedStraddleQuote
from openfly.experiments.sessions import IST, TradingCalendar
from openfly.interfaces import StraddleQuote
from tests.experiments.fakes import synthetic_market

HAVE_HISTORY = history_path("NSE_INDEX", "NIFTY", "1m").exists() and history_path("NFO", "NIFTY15SEP2623400CE", "1m").exists()


def test_put_call_symmetry_at_the_forward():
    call, put = bs_prices(23400.0, 23400.0, 3.0 / 252.0, 0.12)
    assert float(call) == pytest.approx(float(put), rel=1e-12)
    # parity with rate 0: call - put = S - K
    call, put = bs_prices(23500.0, 23400.0, 3.0 / 252.0, 0.12)
    assert float(call - put) == pytest.approx(100.0, abs=1e-9)


def test_straddle_grows_with_vix_and_time():
    p = StraddlePricer(factor=1.0)
    base = float(p.straddle(23400.0, 23400.0, 2.0, 12.0))
    assert float(p.straddle(23400.0, 23400.0, 2.0, 16.0)) > base
    assert float(p.straddle(23400.0, 23400.0, 4.0, 12.0)) > base
    assert float(p.straddle(23400.0, 23400.0, 0.0, 12.0)) < 10.0  # floored at one trading minute
    assert float(p.straddle(23500.0, 23400.0, 0.0, 12.0)) >= 100.0  # never below intrinsic
    assert p.atm_strike(23398.1) == 23400.0 and p.atm_strike(23426.0) == 23450.0


def test_implied_and_realized_moves():
    assert implied_move_points(204.0) == 204.0
    assert implied_move_points(204.0, 60, 1500) == pytest.approx(204.0 * np.sqrt(60 / 1500))
    assert implied_move_points(204.0, 5000, 1500) == pytest.approx(204.0)  # capped at the premium
    closes = np.array([100.0, 101.0, 99.0, 104.0, 90.0])
    assert realized_move_points(closes, 3) == 4.0
    assert realized_move_points(closes, 50) == 10.0  # clipped to the data


def test_trading_time_to_expiry():
    cal = TradingCalendar([date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11),
                           date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)])
    assert cal.next_expiry(date(2026, 9, 11)) == date(2026, 9, 15)
    assert cal.days_to_expiry(datetime(2026, 9, 11, 15, 30, tzinfo=IST)) == pytest.approx(2.0)
    assert cal.days_to_expiry(datetime(2026, 9, 11, 9, 15, tzinfo=IST)) == pytest.approx(3.0)
    assert cal.days_to_expiry(datetime(2026, 9, 15, 15, 30, tzinfo=IST)) == pytest.approx(0.0)
    # a holiday on the expiry weekday moves the expiry to the previous session
    cal2 = TradingCalendar([date(2026, 9, 14), date(2026, 9, 16)])
    assert cal2.next_expiry(date(2026, 9, 14)) == date(2026, 9, 14)


def test_synthetic_minute_quotes_shape():
    market = synthetic_market(days=2, seed=1)
    d = market.dates[1]
    pricer = StraddlePricer(factor=1.0, calendar=market.calendar)
    quotes = synthetic_minute_quotes(d, market.day_1m(d), market.vix_open_series(), pricer=pricer)
    ts = datetime.combine(d, datetime.min.time(), tzinfo=IST).replace(hour=10, minute=0)
    sq = quotes(ts)
    assert isinstance(sq, StraddleQuote) and isinstance(sq, SourcedStraddleQuote)
    assert sq.source == "synthetic" and sq.call.source == "synthetic"
    assert sq.call.ltp != sq.put.ltp  # distinct legs (index is off the strike by some points)
    assert sq.call.bid == pytest.approx(sq.call.ltp - 0.05) and sq.call.ask == pytest.approx(sq.call.ltp + 0.05)
    assert sq.combined_ltp > 0 and sq.strike % 50 == 0
    assert sq.call.symbol.endswith("CE") and sq.put.symbol.endswith("PE")
    pinned = quotes(ts, strike=sq.strike + 100)
    assert pinned.strike == sq.strike + 100
    quotes.pinned_strike = sq.strike + 50
    assert quotes(ts).strike == sq.strike + 50
    leg = quotes.leg(ts, sq.strike, "PE")
    assert leg.ltp == pytest.approx(sq.put.ltp)
    assert quotes.synthetic_fraction == 1.0


@pytest.mark.skipif(not HAVE_HISTORY, reason="NIFTY and option history not present")
def test_calibration_matches_recorded_straddle(tmp_path):
    result = calibrate(write=True, path=tmp_path / "calibration.json")
    assert 0.5 < result["factor"] < 2.0
    assert result["rows"] > 100 and (tmp_path / "calibration.json").exists()
    from openfly.experiments.data import MarketData

    market = MarketData(paths=PATHS)
    pricer = StraddlePricer(factor=result["factor"], calendar=market.calendar)
    # Friday 2026-09-11 close: NIFTY 23398.1, INDIAVIX 12.29, expiry Tuesday 2026-09-15 (2.0 sessions,
    # 3.4 calendar days) and a recorded 23400 straddle of 204 points.
    ts = datetime(2026, 9, 11, 15, 30, tzinfo=IST)
    days = market.calendar.days_to_expiry(ts, date(2026, 9, 15))
    assert days == pytest.approx(2.0)
    value = float(pricer.straddle(23398.1, 23400.0, days, 12.29))
    assert abs(value - 204.0) / 204.0 < 0.2
