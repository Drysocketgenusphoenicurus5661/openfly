"""Volatility-adaptive stops: widen with realized volatility, clip at the bounds, fixed mode unchanged."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pytest

from openfly.execution.types import ceil_to_tick
from openfly.interfaces import Quote, StraddleQuote
from openfly.straddle.engine import Action, State, StopSizer
from openfly.straddle.expiry import trading_minutes_to_expiry

from .helpers import (
    ENTER,
    EXIT,
    EXPIRY,
    at,
    make_engine,
    observation,
    observe,
    settings_with,
    sq,
    tick,
)

CALL, PUT = 101.2, 100.1
INDEX = 23350.0
HORIZON = 60
MONTHLY = date(2026, 9, 29)


def adaptive(**overrides):
    return make_engine(strategy__stop_mode="adaptive", **overrides)


def expected(ret_std: float, when=None, call=CALL, put=PUT, index=INDEX, buffer=1.25, deltas=(0.5, -0.5), gammas=None, expiry=EXPIRY):
    """Reference implementation of the spec formula for the test numbers (trading minutes to expiry)."""
    when = when or at(10, 20)
    combined = call + put
    minutes = trading_minutes_to_expiry(when, expiry)
    assert minutes == (1060 if expiry == EXPIRY else 4810)
    implied = combined * math.sqrt(HORIZON / minutes)
    realized = ret_std * math.sqrt(HORIZON) * index if ret_std > 0 else 0.0
    m = max(implied, realized)
    sigma_sqrt_t = combined / (0.8 * index)
    gamma = 0.4 / (index * sigma_sqrt_t)
    g_ce, g_pe = gammas or (gamma, gamma)
    rise_ce = abs(deltas[0]) * m + 0.5 * g_ce * m * m
    rise_pe = abs(deltas[1]) * m + 0.5 * g_pe * m * m
    leg_ce = min(80.0, max(15.0, buffer * rise_ce / call * 100.0))
    leg_pe = min(80.0, max(15.0, buffer * rise_pe / put * 100.0))
    net = abs(abs(deltas[0]) - abs(deltas[1]))
    rise_c = 0.5 * (g_ce + g_pe) * m * m + net * m
    combined_pct = min(50.0, max(3.0, buffer * rise_c / combined * 100.0))
    return {"m": m, "implied": implied, "realized": realized, "ce": leg_ce, "pe": leg_pe, "combined": combined_pct, "rise_ce": rise_ce}


def test_realized_move_scales_with_the_return_std():
    sizer = StopSizer(settings_with(strategy__stop_mode="adaptive"))
    obs = observation(at(10, 20), ret_std=0.0002)
    index, realized, used = sizer.realized_move(obs, HORIZON)
    assert used == 60 and index == pytest.approx(obs.index_bars[-1].close)
    assert realized == pytest.approx(0.0002 * math.sqrt(60) * index, rel=0.02)
    _, doubled, _ = sizer.realized_move(observation(at(10, 20), ret_std=0.0004), HORIZON)
    assert doubled == pytest.approx(2 * realized, rel=0.02)
    _, none, used = sizer.realized_move(observation(at(10, 20), ret_std=0.0004, n_bars=19), HORIZON)
    assert none is None and used == 19


def test_adaptive_stops_widen_when_realized_volatility_doubles_and_narrow_in_a_calm_hour():
    sizer = StopSizer(settings_with(strategy__stop_mode="adaptive"))
    quote = sq(at(10, 20), CALL, PUT)
    calm = sizer.compute(quote, observation(at(10, 20), ret_std=0.00028), at(10, 20))
    wild = sizer.compute(quote, observation(at(10, 20), ret_std=0.00056), at(10, 20))
    ref_calm, ref_wild = expected(0.00028), expected(0.00056)
    assert calm.realized_move_points > calm.implied_move_points  # realized rules in both cases
    assert calm.mode == "adaptive" and calm.bars_used == 60
    assert calm.expected_move_points == pytest.approx(ref_calm["m"], rel=0.02)
    assert calm.leg_stop_pct["ce"] == pytest.approx(ref_calm["ce"], rel=0.02)
    assert wild.leg_stop_pct["ce"] == pytest.approx(ref_wild["ce"], rel=0.02)
    assert 15.0 < calm.leg_stop_pct["ce"] < wild.leg_stop_pct["ce"] < 80.0
    assert wild.leg_stop_pct["pe"] > calm.leg_stop_pct["pe"]
    assert wild.expected_move_points == pytest.approx(2 * calm.expected_move_points, rel=0.02)
    assert {k: calm.clipped[k] for k in ("ce", "pe", "combined")} == {"ce": "", "pe": "", "combined": "min"}
    # a genuinely calm hour: realized below implied, so the implied move rules and the stops narrow
    quiet = sizer.compute(quote, observation(at(10, 20), ret_std=0.00001), at(10, 20))
    assert quiet.expected_move_points == pytest.approx(quiet.implied_move_points)
    assert quiet.leg_stop_pct["ce"] == pytest.approx(expected(0.0)["ce"], rel=1e-6)
    assert quiet.leg_stop_pct["ce"] < calm.leg_stop_pct["ce"] and quiet.combined_stop_pct == 3.0


def test_clipping_at_min_and_max():
    sizer = StopSizer(settings_with(strategy__stop_mode="adaptive"))
    quote = sq(at(10, 20), CALL, PUT)
    # a monthly expiry has 4,810 trading minutes left, so the one-hour implied move is small: the floors apply
    monthly = sq(at(10, 20), CALL, PUT, expiry=MONTHLY)
    floor = sizer.compute(monthly, observation(at(10, 20)), at(10, 20))
    assert floor.minutes_to_expiry == 4810 and floor.implied_move_points == pytest.approx(expected(0.0, expiry=MONTHLY)["implied"])
    assert floor.leg_stop_pct == {"ce": 15.0, "pe": 15.0} and floor.combined_stop_pct == 3.0
    assert {k: floor.clipped[k] for k in ("ce", "pe", "combined")} == {"ce": "min", "pe": "min", "combined": "min"}
    cap = sizer.compute(quote, observation(at(10, 20), ret_std=0.003), at(10, 20))
    assert cap.leg_stop_pct == {"ce": 80.0, "pe": 80.0} and cap.combined_stop_pct == 50.0
    assert {k: cap.clipped[k] for k in ("ce", "pe", "combined")} == {"ce": "max", "pe": "max", "combined": "max"}
    custom = StopSizer(settings_with(strategy__stop_mode="adaptive", strategy__leg_stop_min_pct=5, strategy__leg_stop_max_pct=40, strategy__combined_stop_min_pct=2, strategy__combined_stop_max_pct=20))
    tight = custom.compute(quote, observation(at(10, 20), ret_std=0.003), at(10, 20))
    assert tight.leg_stop_pct == {"ce": 40.0, "pe": 40.0} and tight.combined_stop_pct == 20.0
    assert tight.bounds["leg"] == [5.0, 40.0] and tight.bounds["combined"] == [2.0, 20.0]


def test_fewer_than_twenty_bars_falls_back_to_implied():
    sizer = StopSizer(settings_with(strategy__stop_mode="adaptive"))
    quote = sq(at(10, 20), CALL, PUT)
    basis = sizer.compute(quote, observation(at(10, 20), ret_std=0.004, n_bars=12), at(10, 20))
    assert basis.realized_move_points is None and basis.bars_used == 12
    assert basis.expected_move_points == pytest.approx(basis.implied_move_points)
    assert basis.implied_move_points == pytest.approx(expected(0.0)["implied"], rel=1e-6)


def test_fixed_mode_is_unchanged():
    sizer = StopSizer(settings_with())
    basis = sizer.compute(sq(at(10, 20), CALL, PUT), observation(at(10, 20), ret_std=0.004), at(10, 20))
    assert basis.mode == "fixed" and basis.leg_stop_pct == {"ce": 30.0, "pe": 30.0} and basis.combined_stop_pct == 25.0
    engine, broker, _ = make_engine()
    step = observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.004)
    pos = engine.position
    assert pos.stop_level == pytest.approx(251.625)
    assert [leg["stop_price"] for leg in step.straddle["legs"]] == [131.6, 130.15]
    assert step.straddle["stop_basis"]["mode"] == "fixed"
    assert "Stop 251.6, target 120.8, lock after 171.1, hard exit 15:15." in step.narrative


@dataclass(frozen=True)
class GreekQuote(Quote):
    delta: float = 0.0
    gamma: float = 0.0


def test_greeks_on_the_quote_are_used_and_asymmetric_deltas_widen_the_combined_stop():
    sizer = StopSizer(settings_with(strategy__stop_mode="adaptive"))
    ts = at(10, 20).timestamp()
    quote = StraddleQuote(
        call=GreekQuote("NIFTY15SEP2623350CE", "NFO", CALL, CALL, CALL, ts, delta=0.6, gamma=0.0016),
        put=GreekQuote("NIFTY15SEP2623350PE", "NFO", PUT, PUT, PUT, ts, delta=-0.4, gamma=0.0016),
        strike=23350.0,
        expiry=EXPIRY,
    )
    basis = sizer.compute(quote, observation(at(10, 20), ret_std=0.0004), at(10, 20))
    ref = expected(0.0004, deltas=(0.6, -0.4), gammas=(0.0016, 0.0016))
    assert basis.greeks == {"ce": {"delta": 0.6, "gamma": 0.0016}, "pe": {"delta": -0.4, "gamma": 0.0016}}
    assert basis.leg_stop_pct["ce"] == pytest.approx(ref["ce"], rel=0.02)
    assert basis.leg_stop_pct["pe"] == pytest.approx(ref["pe"], rel=0.02)
    assert basis.combined_stop_pct == pytest.approx(ref["combined"], rel=0.02)
    neutral = sizer.compute(sq(at(10, 20), CALL, PUT), observation(at(10, 20), ret_std=0.0004), at(10, 20))
    assert basis.combined_stop_pct > neutral.combined_stop_pct
    assert basis.leg_stop_pct["ce"] > basis.leg_stop_pct["pe"]


def test_entry_applies_adaptive_stops_and_the_narrative_explains_them():
    engine, broker, _ = adaptive()
    step = observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.0004)
    assert step.action == Action.ENTER.value
    pos = engine.position
    basis = pos.basis
    ref = expected(0.0004)
    assert basis.mode == "adaptive" and basis.leg_stop_pct["ce"] == pytest.approx(ref["ce"], rel=0.02)
    call_leg, put_leg = pos.legs.values()
    assert call_leg.stop_price == ceil_to_tick(CALL * (1 + basis.leg_stop_pct["ce"] / 100.0))
    assert put_leg.stop_price == ceil_to_tick(PUT * (1 + basis.leg_stop_pct["pe"] / 100.0))
    assert pos.stop_level == pytest.approx(201.3 * (1 + basis.combined_stop_pct / 100.0))
    assert pos.target_level == pytest.approx(120.78) and pos.lock_level == pytest.approx(171.105)
    snap = step.straddle
    assert snap["stop_basis"]["mode"] == "adaptive"
    assert set(snap["stop_basis"]) >= {"mode", "horizon_minutes", "expected_move_points", "implied_move_points", "realized_move_points", "leg_stop_pct", "combined_stop_pct"}
    assert snap["stop_basis"]["leg_stop_pct"] == {"ce": round(basis.leg_stop_pct["ce"], 2), "pe": round(basis.leg_stop_pct["pe"], 2)}
    assert snap["stop_level"] == round(pos.stop_level, 2)
    # the resting stop orders carry the adaptive prices
    stops = {s["symbol"][-2:]: s["trigger_price"] for s in broker.stop_orders()}
    assert stops == {"CE": call_leg.stop_price, "PE": put_leg.stop_price}
    text = step.narrative
    m, implied, realized = basis.expected_move_points, basis.implied_move_points, basis.realized_move_points
    assert f"Expected one-hour move {m:.0f} points (the straddle prices {implied:.0f}, the last hour realized {realized:.0f})." in text
    assert basis.leg_rise_points["ce"] == pytest.approx(ref["rise_ce"], rel=0.02)
    assert f"A move of that size lifts the call about {basis.leg_rise_points['ce']:.0f} points, so the call stop is {basis.leg_stop_pct['ce']:.0f} percent above its price at {call_leg.stop_price:.2f};" in text
    assert f"put stop {basis.leg_stop_pct['pe']:.0f} percent at {put_leg.stop_price:.2f};" in text
    assert basis.clipped["combined"] == "" and basis.combined_stop_pct > 3.0
    assert f"combined stop {basis.combined_stop_pct:.0f} percent at {pos.stop_level:.1f}." in text
    assert "Target 120.8, lock after 171.1, hard exit 15:15. Leg stops at the broker, held for the life of this straddle." in text
    assert step.technical["levels"]["stop_basis"]["mode"] == "adaptive"
    assert step.technical["thresholds"]["stop_mode"] == "adaptive"
    assert step.technical["lots"]["stop_pct"] == pytest.approx(basis.combined_stop_pct)


def test_adaptive_stops_are_held_for_the_straddle_and_recomputed_for_the_next_one():
    engine, broker, _ = adaptive()
    first = observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.0002)
    basis_1 = engine.position.basis
    stop_1 = engine.position.stop_level
    legs_1 = [leg.stop_price for leg in engine.position.legs.values()]
    # volatility doubles while the straddle is open: nothing moves
    quiet = observe(engine, broker, at(10, 40), EXIT if False else ENTER, CALL, PUT, ret_std=0.0004)
    assert quiet.action == Action.HOLD.value
    assert engine.position.basis is basis_1 and engine.position.stop_level == stop_1
    assert [leg.stop_price for leg in engine.position.legs.values()] == legs_1
    step = observe(engine, broker, at(11, 0), EXIT, CALL, PUT, roi=1.2, ret_std=0.0004)
    assert step.action == Action.EXIT.value and engine.state == State.FLAT
    second = observe(engine, broker, at(11, 10), ENTER, CALL, PUT, ret_std=0.0004)
    assert second.action == Action.REENTRY.value
    basis_2 = engine.position.basis
    assert basis_2.leg_stop_pct["ce"] > basis_1.leg_stop_pct["ce"]
    assert basis_2.expected_move_points > basis_1.expected_move_points
    assert [leg.stop_price for leg in engine.position.legs.values()] != legs_1
    assert engine.closed[0]["legs"] and first.straddle["stop_basis"] != second.straddle["stop_basis"]
    assert "Straddle 2 of the day" in second.narrative and "Expected one-hour move" in second.narrative


def test_adaptive_leg_stop_fires_at_the_adaptive_price():
    engine, broker, _ = adaptive()
    observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.0002)
    call_leg = next(leg for leg in engine.position.legs.values() if leg.option_type == "CE")
    below = round(call_leg.stop_price - 0.05, 2)
    quiet, stop_steps = tick(engine, broker, at(10, 30), below, 60.0)  # the put is low so the combined stop stays out of the way
    assert not stop_steps and quiet.action == Action.NONE.value
    _, stop_steps = tick(engine, broker, at(10, 31), call_leg.stop_price, 60.0)
    assert stop_steps and stop_steps[0].action == Action.STOP_LEG.value
    assert f"Call leg stop at {call_leg.stop_price:.2f} triggered at the broker." in stop_steps[0].narrative
