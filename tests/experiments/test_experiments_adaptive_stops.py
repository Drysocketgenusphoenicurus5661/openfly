from __future__ import annotations

import copy

import numpy as np
import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.experiments.data import MarketData
from openfly.experiments.pricer import StraddlePricer, bs_greeks
from openfly.experiments.quotes import minute_quotes_for
from openfly.experiments.simulator import Rules, run_trade, stop_basis
from tests.experiments.fakes import synthetic_frames


def _market(jump: bool, seed: int = 21) -> MarketData:
    """Two calm sessions; with `jump`, minutes 100 to 160 of the second day move 25 points a minute."""
    f, v = synthetic_frames(days=2, seed=seed, vol_per_minute=0.5)
    if jump:
        rng = np.random.default_rng(seed + 1)
        start = 375 + 100
        steps = rng.normal(0.0, 25.0, size=60)
        path = f.loc[start - 1, "close"] + np.cumsum(steps)
        idx = np.arange(start, start + 60)
        f.loc[idx, "close"] = path
        f.loc[idx, "open"] = np.r_[f.loc[start - 1, "close"], path[:-1]]
        f.loc[idx, "high"] = np.maximum(f.loc[idx, "open"], f.loc[idx, "close"]) + 1.0
        f.loc[idx, "low"] = np.minimum(f.loc[idx, "open"], f.loc[idx, "close"]) - 1.0
        shift = path[-1] - f.loc[start + 60, "open"]
        for col in ("open", "high", "low", "close"):
            f.loc[start + 60 :, col] = f.loc[start + 60 :, col] + shift
    return MarketData(store=False, frame_1m=f, vix_daily=v)


def _quotes(market: MarketData):
    d = market.dates[1]
    pricer = StraddlePricer(factor=1.0, calendar=market.calendar)
    return minute_quotes_for(d, market=market, pricer=pricer, settings=DEFAULT_SETTINGS, prefer_recorded=False)


def _basis(quotes, row: int, rules: Rules) -> dict:
    strike = float(quotes.atm_strikes[row])
    call, put, _ = quotes.straddle_path(strike)
    return stop_basis(quotes, row, strike, rules, float(call[row]), float(put[row]))


def test_greeks_are_sane():
    dc, dp, g = bs_greeks(23400.0, 23400.0, 2.0 / 252.0, 0.12)
    assert float(dc) == pytest.approx(0.5, abs=0.02) and float(dp) == pytest.approx(float(dc) - 1.0)
    assert 0.0005 < float(g) < 0.005  # per index point, matches the measured 0.00156 order of magnitude
    p = StraddlePricer(factor=1.0)
    greeks = p.greeks(23500.0, 23400.0, 2.0, 12.0)
    assert greeks["delta_ce"] > 0.6 and greeks["delta_pe"] < -0.3 and greeks["gamma"] > 0


def test_wider_stops_after_a_volatility_jump_and_narrower_when_calm():
    rules = Rules.from_settings(DEFAULT_SETTINGS)
    assert rules.stop_mode == "adaptive"
    calm = _basis(_quotes(_market(jump=False)), 160, rules)
    hot = _basis(_quotes(_market(jump=True)), 160, rules)
    assert hot["mode"] == "adaptive" and calm["mode"] == "adaptive"
    # calm hour: realized is below what the premium implies, so the implied move sets the stop
    assert calm["realized_move_points"] < calm["implied_move_points"]
    assert calm["expected_move_points"] == pytest.approx(calm["implied_move_points"])
    # after the jump: realized dominates and the stops widen on both legs
    assert hot["realized_move_points"] > hot["implied_move_points"]
    assert hot["expected_move_points"] == pytest.approx(hot["realized_move_points"])
    assert hot["leg_stop_pct"]["ce"] > calm["leg_stop_pct"]["ce"]
    assert hot["leg_stop_pct"]["pe"] > calm["leg_stop_pct"]["pe"]
    assert hot["combined_stop_pct"] >= calm["combined_stop_pct"]
    # the calm stop sits inside the bounds and follows the delta and gamma formula
    m = calm["expected_move_points"]
    rise = abs(calm["delta_ce"]) * m + 0.5 * calm["gamma"] * m * m
    q = _quotes(_market(jump=False))
    strike = float(q.atm_strikes[160])
    call, _, _ = q.straddle_path(strike)
    expected = np.clip(rules.stop_buffer * rise / float(call[160]) * 100.0, rules.leg_stop_min_pct, rules.leg_stop_max_pct)
    assert calm["leg_stop_pct"]["ce"] == pytest.approx(float(expected))
    assert rules.leg_stop_min_pct <= calm["leg_stop_pct"]["ce"] <= rules.leg_stop_max_pct


def test_each_straddle_recomputes_and_holds_its_own_stops():
    q = _quotes(_market(jump=True))
    rules = Rules.from_settings(DEFAULT_SETTINGS)
    early = run_trade(q, 20, rules, last_row=90)  # before the jump
    late = run_trade(q, 165, rules, last_row=230)  # right after the jump
    assert late.leg_stop_pct_ce > early.leg_stop_pct_ce
    assert late.stop_basis["expected_move_points"] > early.stop_basis["expected_move_points"]
    for t in (early, late):
        assert t.stop_basis["leg_stop_pct"]["ce"] == t.leg_stop_pct_ce
        assert t.stop_basis["combined_stop_pct"] == t.combined_stop_pct
        assert t.to_dict()["stop_basis"]["mode"] == "adaptive"


def test_clipping_to_bounds():
    q = _quotes(_market(jump=True))
    rules = Rules.from_settings(DEFAULT_SETTINGS)
    rules.stop_buffer = 1000.0
    wide = _basis(q, 160, rules)
    assert wide["leg_stop_pct"] == {"ce": rules.leg_stop_max_pct, "pe": rules.leg_stop_max_pct}
    assert wide["combined_stop_pct"] == rules.combined_stop_max_pct
    rules.stop_buffer = 1e-6
    tight = _basis(q, 160, rules)
    assert tight["leg_stop_pct"] == {"ce": rules.leg_stop_min_pct, "pe": rules.leg_stop_min_pct}
    assert tight["combined_stop_pct"] == rules.combined_stop_min_pct


def test_fixed_mode_is_identical_to_the_fixed_percentages():
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["strategy"]["stop_mode"] = "fixed"
    rules = Rules.from_settings(settings)
    q = _quotes(_market(jump=True))
    basis = _basis(q, 160, rules)
    assert basis == {
        "mode": "fixed", "horizon_minutes": 60, "expected_move_points": None, "implied_move_points": None,
        "realized_move_points": None, "leg_stop_pct": {"ce": 30.0, "pe": 30.0}, "combined_stop_pct": 25.0,
    }
    t = run_trade(q, 160, rules)
    assert (t.leg_stop_pct_ce, t.leg_stop_pct_pe, t.combined_stop_pct) == (30.0, 30.0, 25.0)
    # a hand-built fixed rules object gives the same exits as the settings-driven one
    manual = Rules(stop_mode="fixed", leg_stop_pct=30.0, stop_pct=25.0, costs=rules.costs)
    t2 = run_trade(q, 160, manual)
    assert (t2.exit_row, t2.exit_reason, t2.pnl_points) == (t.exit_row, t.exit_reason, t.pnl_points)


def test_trailing_window_reaches_into_the_previous_session():
    q = _quotes(_market(jump=False))
    assert q.prior_closes is not None and len(q.prior_closes) == 60
    assert len(q.trailing_closes(4, 61)) == 61
    rules = Rules.from_settings(DEFAULT_SETTINGS)
    basis = _basis(q, 4, rules)
    assert basis["realized_move_points"] > 0.0
