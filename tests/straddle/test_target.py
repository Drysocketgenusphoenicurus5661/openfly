"""Adaptive target and lock: sized from the combined stop percent, clipped, fixed per straddle."""

from __future__ import annotations

from datetime import date

import pytest

from openfly.straddle.engine import Action, State, StopSizer

from .helpers import ENTER, EXIT, at, make_engine, observation, observe, settings_with, sq, tick

CALL, PUT = 101.2, 100.1
MONTHLY = date(2026, 9, 29)


def adaptive_settings(**overrides):
    return settings_with(strategy__stop_mode="adaptive", strategy__target_mode="adaptive", **overrides)


def adaptive_engine(**overrides):
    return make_engine(strategy__stop_mode="adaptive", strategy__target_mode="adaptive", **overrides)


def test_adaptive_target_equals_the_combined_stop_percent_by_default():
    sizer = StopSizer(adaptive_settings())
    quote = sq(at(10, 20), CALL, PUT)
    basis = sizer.compute(quote, observation(at(10, 20), ret_std=0.00056), at(10, 20))
    assert basis.target_mode == "adaptive"
    assert basis.clipped["combined"] == "" and basis.combined_stop_pct > 3.0
    assert basis.target_pct == pytest.approx(basis.combined_stop_pct)
    assert basis.lock_after_pct == pytest.approx(0.5 * basis.target_pct)
    assert basis.clipped["target"] == "" and basis.bounds["target"] == [2.0, 40.0]
    # at the combined floor the target follows the floored percent (3 percent, above the 2 percent target floor)
    floored = sizer.compute(sq(at(10, 20), CALL, PUT, expiry=MONTHLY), observation(at(10, 20)), at(10, 20))
    assert floored.combined_stop_pct == 3.0 and floored.target_pct == 3.0 and floored.lock_after_pct == 1.5
    data = basis.to_dict()
    assert data["target_pct"] == round(basis.target_pct, 2) and data["lock_after_pct"] == round(basis.lock_after_pct, 2)


def test_adaptive_target_respects_min_and_max():
    quote = sq(at(10, 20), CALL, PUT)
    obs = observation(at(10, 20), ret_std=0.00056)
    low = StopSizer(adaptive_settings(strategy__target_ratio=0.1)).compute(quote, obs, at(10, 20))
    assert low.target_pct == 2.0 and low.clipped["target"] == "min" and low.lock_after_pct == 1.0
    high = StopSizer(adaptive_settings(strategy__target_ratio=10.0)).compute(quote, obs, at(10, 20))
    assert high.target_pct == 40.0 and high.clipped["target"] == "max" and high.lock_after_pct == 20.0
    custom = StopSizer(adaptive_settings(strategy__target_ratio=10.0, strategy__target_max_pct=25.0, strategy__lock_ratio=0.2)).compute(quote, obs, at(10, 20))
    assert custom.target_pct == 25.0 and custom.lock_after_pct == 5.0 and custom.bounds["target"] == [2.0, 25.0]


def test_fixed_target_mode_is_unchanged():
    basis = StopSizer(settings_with(strategy__stop_mode="adaptive")).compute(sq(at(10, 20), CALL, PUT), observation(at(10, 20), ret_std=0.00056), at(10, 20))
    assert basis.target_mode == "fixed" and basis.target_pct == 40.0 and basis.lock_after_pct == 15.0
    engine, broker, _ = make_engine()
    step = observe(engine, broker, at(10, 20), ENTER, CALL, PUT)
    assert engine.position.target_level == pytest.approx(120.78) and engine.position.lock_level == pytest.approx(171.105)
    assert "target 120.8, lock after 171.1, hard exit 15:15." in step.narrative
    assert step.straddle["stop_basis"]["target_mode"] == "fixed"


def test_entry_levels_snapshot_and_narrative_use_the_adaptive_target():
    engine, broker, _ = adaptive_engine()
    step = observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.00056)
    pos = engine.position
    basis = pos.basis
    assert pos.target_level == pytest.approx(201.3 * (1 - basis.target_pct / 100.0))
    assert pos.lock_level == pytest.approx(201.3 * (1 - basis.lock_after_pct / 100.0))
    snap = step.straddle
    assert snap["target_level"] == round(pos.target_level, 2)
    assert snap["stop_basis"]["target_pct"] == round(basis.target_pct, 2) and snap["stop_basis"]["lock_after_pct"] == round(basis.lock_after_pct, 2)
    text = step.narrative
    def pct(value):  # the narrative drops the decimal only for whole percentages
        return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"

    assert f"Target {pct(basis.target_pct)} percent at {pos.target_level:.1f}, lock after a {pct(basis.lock_after_pct)} percent fall (to {pos.lock_level:.1f}), hard exit 15:15." in text
    assert step.technical["thresholds"]["target_mode"] == "adaptive" and step.technical["thresholds"]["lock_ratio"] == 0.5


def test_lock_arms_at_half_the_target_and_target_can_hit():
    engine, broker, _ = adaptive_engine()
    observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.00056)
    pos = engine.position
    lock_level, target_level = pos.lock_level, pos.target_level
    above = round(lock_level / 2 + 0.05, 2)
    quiet, _ = tick(engine, broker, at(10, 25), above, above)
    assert quiet.action == Action.NONE.value and not pos.locked
    at_lock = round(lock_level / 2, 2)
    lock, _ = tick(engine, broker, at(10, 26), at_lock, at_lock)
    assert lock.action == Action.LOCK.value and pos.locked and pos.stop_level == pytest.approx(201.3)
    assert f"has fallen {pos.basis.lock_after_pct:.1f} percent below the 201.3 credit" in lock.narrative
    half = round(target_level / 2 - 0.05, 2)
    hit, _ = tick(engine, broker, at(10, 40), half, half)
    assert hit.action == Action.TARGET.value and engine.state == State.FLAT and engine.target_hits == 1
    assert engine.closed[0]["net"] > 0


def test_targets_differ_between_straddles_when_volatility_changed():
    engine, broker, _ = adaptive_engine()
    observe(engine, broker, at(10, 20), ENTER, CALL, PUT, ret_std=0.00056)
    first = engine.position.basis
    observe(engine, broker, at(10, 35), EXIT, CALL, PUT, roi=1.2, ret_std=0.0008)
    assert engine.state == State.FLAT
    second_step = observe(engine, broker, at(10, 45), ENTER, CALL, PUT, ret_std=0.0008)
    assert second_step.action == Action.REENTRY.value
    second = engine.position.basis
    assert second.combined_stop_pct > first.combined_stop_pct
    assert second.target_pct > first.target_pct and second.lock_after_pct > first.lock_after_pct
    assert second.target_pct == pytest.approx(second.combined_stop_pct)
    assert second_step.straddle["stop_basis"]["target_pct"] != first.to_dict()["target_pct"]
