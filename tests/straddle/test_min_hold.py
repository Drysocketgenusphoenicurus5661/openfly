"""Minimum hold: a readout EXIT waits until the straddle is old enough; mechanical exits do not."""

from __future__ import annotations

from openfly.interfaces import SessionWindow
from openfly.straddle.engine import Action, State

from .helpers import ENTER, EXIT, HOLD, at, make_engine, observe, settings_with, tick

CALL, PUT = 101.2, 100.1


def enter(engine, broker, when=None):
    step = observe(engine, broker, when or at(10, 20), ENTER, CALL, PUT)
    assert step.action == Action.ENTER.value and engine.state == State.IN_POSITION
    return step


def test_default_minimum_hold_is_ten_minutes():
    assert settings_with()["strategy"]["min_hold_minutes"] == 10
    engine, _, _ = make_engine()
    assert engine.min_hold_minutes == 10.0


def test_readout_exit_at_minute_three_is_deferred_and_taken_at_minute_ten():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    early = observe(engine, broker, at(10, 23), EXIT, 100.0, 100.0, roi=1.2)
    assert early.action == Action.HOLD.value and not early.intents and engine.in_position
    assert "The readout wants out but the straddle is only 3 minutes old; holding until 10:30 (minimum hold 10 minutes)." in early.narrative
    assert "Holding straddle 1 (23350) since 10:20" in early.narrative
    assert early.technical["thresholds"]["min_hold_minutes"] == 10.0
    assert early.technical["deferred_exit"] == {
        "age_minutes": 3.0,
        "min_hold_minutes": 10.0,
        "entered_at": "2026-09-11T10:20:00+05:30",
        "hold_until": "2026-09-11T10:30:00+05:30",
    }
    assert early.technical["levels"]["age_minutes"] == 3.0 and early.technical["levels"]["hold_until"].endswith("T10:30:00+05:30")
    assert engine.status()["deferred_exits"] == 1 and engine.early_exits == 0

    again = observe(engine, broker, at(10, 29), EXIT, 100.0, 100.0, roi=1.2)
    assert again.action == Action.HOLD.value and "only 9 minutes old" in again.narrative
    assert engine.status()["deferred_exits"] == 2

    done = observe(engine, broker, at(10, 30), EXIT, 100.0, 100.0, roi=1.2)
    assert done.action == Action.EXIT.value and engine.state == State.FLAT
    assert engine.early_exits == 1 and done.closed is not None
    assert "The readout now expects 120 percent of the priced movement, so the straddle is closed early." in done.narrative


def test_a_deferred_exit_is_not_taken_if_the_readout_changes_its_mind():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    observe(engine, broker, at(10, 23), EXIT, 100.0, 100.0, roi=1.2)
    later = observe(engine, broker, at(10, 30), HOLD, 100.0, 100.0)
    assert later.action == Action.HOLD.value and engine.in_position and later.deferred_exit is None
    assert "wants out" not in later.narrative


def test_min_hold_zero_exits_at_once():
    engine, broker, _ = make_engine(strategy__min_hold_minutes=0)
    enter(engine, broker)
    step = observe(engine, broker, at(10, 21), EXIT, 100.0, 100.0, roi=1.2)
    assert step.action == Action.EXIT.value and engine.state == State.FLAT


def test_mechanical_exits_fire_during_the_hold():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    stop, _ = tick(engine, broker, at(10, 23), 126.0, 126.0)
    assert stop.action == Action.STOP.value and engine.state == State.FLAT and engine.stop_hits == 1

    engine, broker, _ = make_engine()
    enter(engine, broker)
    _, stop_steps = tick(engine, broker, at(10, 24), 132.0, 90.0)
    assert stop_steps and stop_steps[0].action == Action.STOP_LEG.value and engine.leg_stops_today == 1

    engine, broker, _ = make_engine()
    enter(engine, broker)
    target, _ = tick(engine, broker, at(10, 25), 60.0, 60.0)
    assert target.action == Action.TARGET.value and engine.target_hits == 1

    engine, broker, _ = make_engine()
    enter(engine, broker)
    lock, _ = tick(engine, broker, at(10, 22), 85.0, 85.0)
    assert lock.action == Action.LOCK.value and engine.position.locked

    engine, broker, _ = make_engine()
    w = engine.window
    engine.start_day(SessionWindow(w.trading_date, w.market_open, w.market_close, w.trade_start, at(15, 12), w.square_off, w.is_expiry_day))
    enter(engine, broker, at(15, 10))
    square, _ = tick(engine, broker, at(15, 14, 30), 100.0, 100.0)
    assert square.action == Action.SQUARE_OFF.value and engine.time_exits == 1  # 4.5 minutes old, inside the hold

    engine, broker, _ = make_engine(strategy__leg_stop_mode="software")
    enter(engine, broker)
    soft, _ = tick(engine, broker, at(10, 22), 132.0, 90.0)
    assert soft.action == Action.STOP_LEG.value and engine.leg_stops_today == 1
