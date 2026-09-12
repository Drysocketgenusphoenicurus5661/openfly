"""Engine scenarios with synthetic minute quotes and the replay broker."""

from __future__ import annotations

import math
from datetime import date

import pytest

from openfly.execution.types import KIND_STOPS
from openfly.straddle.engine import Action, State
from openfly.straddle.guard import GuardContext

from .helpers import ENTER, EXIT, HOLD, at, make_engine, observe, tick

CALL, PUT = 101.2, 100.1  # credit 201.3: stop 251.6, target 120.8, lock 171.1


def enter(engine, broker, when=None, call=CALL, put=PUT, strike=23350.0):
    step = observe(engine, broker, when or at(10, 20), ENTER, call, put, strike=strike)
    assert step.action in (Action.ENTER.value, Action.REENTRY.value), step.narrative
    assert engine.state == State.IN_POSITION
    return step


def test_entry_then_combined_stop_hit():
    engine, broker, _ = make_engine()
    step = enter(engine, broker)
    pos = engine.position
    assert pos.entry_credit == pytest.approx(201.3)
    assert pos.stop_level == pytest.approx(251.625)
    assert pos.target_level == pytest.approx(120.78)
    assert pos.lock_level == pytest.approx(171.105)
    assert [i.kind for i in step.intents] == ["ENTRY", KIND_STOPS]
    snap = step.straddle
    assert snap["in_position"] and snap["lots"] == 1 and snap["strike"] == 23350.0
    assert [leg["stop_price"] for leg in snap["legs"]] == [131.6, 130.15]
    assert all(leg["stop_status"] == "pending" and leg["stop_order_id"] for leg in snap["legs"])

    quiet, _ = tick(engine, broker, at(10, 21), 105.0, 104.0)
    assert quiet.action == Action.NONE.value and engine.state == State.IN_POSITION
    assert "Holding: combined premium 209.0 against stop 251.6 and target 120.8" in quiet.narrative

    hit, _ = tick(engine, broker, at(10, 37, 12), 126.0, 126.0)
    assert hit.action == Action.STOP.value
    assert engine.state == State.FLAT and engine.stop_hits == 1
    assert engine.closed[0]["action"] == "STOP"
    assert engine.day_pnl() < 0
    assert set(broker.cancelled_stops) == {s["order_id"] for s in broker.stop_orders()}
    assert all(s["status"] == "cancelled" for s in broker.stop_orders())
    assert "10:37:12. Combined premium 252.0 reached the stop 251.6." in hit.narrative
    assert "Bought back 1 lot of the 23350 straddle for 252.0 points." in hit.narrative
    closed = engine.closed[0]
    assert closed["gross"] == pytest.approx((201.3 - 252.0) * 65)
    net_text = f"{int(math.floor(abs(closed['net']) + 0.5)):,}"
    cost_text = f"{int(math.floor(closed['costs'] + 0.5)):,}"
    assert f"Result -INR {net_text} after INR {cost_text} costs. Day P&L -INR {net_text}." in hit.narrative


def test_entry_then_target():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    step, _ = tick(engine, broker, at(11, 5), 60.0, 60.0)
    assert step.action == Action.TARGET.value
    assert engine.state == State.FLAT and engine.target_hits == 1
    assert engine.closed[0]["net"] > 0
    assert "reached the target 120.8" in step.narrative


def test_lock_then_stop_at_entry_credit():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    lock, _ = tick(engine, broker, at(11, 0), 85.0, 85.0)
    assert lock.action == Action.LOCK.value
    assert engine.position.locked and engine.position.stop_level == pytest.approx(201.3)
    assert "Stop moved to the entry credit 201.3" in lock.narrative
    quiet, _ = tick(engine, broker, at(11, 1), 90.0, 90.0)
    assert quiet.action == Action.NONE.value
    stop, _ = tick(engine, broker, at(11, 30), 101.0, 101.0)
    assert stop.action == Action.STOP.value and engine.state == State.FLAT
    assert engine.closed[0]["locked"] is True


def test_time_exit_at_square_off_minus_lead():
    engine, broker, _ = make_engine()
    enter(engine, broker, at(14, 20))
    early, _ = tick(engine, broker, at(15, 14, 0), 100.0, 100.0)
    assert early.action == Action.NONE.value and engine.in_position
    square, _ = tick(engine, broker, at(15, 14, 30), 100.0, 100.0)
    assert square.action == Action.SQUARE_OFF.value
    assert engine.state == State.FLAT and engine.time_exits == 1
    assert "Square-off time (15:14:30)" in square.narrative
    assert not engine.state_snapshot()["in_position"]


def test_readout_exit_closes_early():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    hold = observe(engine, broker, at(10, 25), HOLD, 100.0, 100.0)
    assert hold.action == Action.HOLD.value and "Holding straddle 1 (23350) since 10:20" in hold.narrative
    step = observe(engine, broker, at(11, 0), EXIT, 100.0, 98.0, roi=1.12)
    assert step.action == Action.EXIT.value
    assert engine.state == State.FLAT and engine.early_exits == 1
    assert "The readout now expects 112 percent of the priced movement" in step.narrative
    assert step.guard is not None and step.guard.allowed


def test_no_second_entry_while_one_is_open():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    step = observe(engine, broker, at(10, 25), ENTER, 100.0, 100.0)
    assert step.action == Action.HOLD.value and not step.intents
    assert engine.entries_today == 1 and engine.position.number == 1
    assert len(broker.fills) == 2


def test_three_straddles_in_one_day_stop_target_square_off():
    engine, broker, _ = make_engine()
    first = enter(engine, broker)
    assert first.action == Action.ENTER.value and "Sold 1 lot of the 15-SEP-26 weekly straddle at 23350" in first.narrative
    stop, _ = tick(engine, broker, at(10, 37), 126.0, 126.0)
    assert stop.action == Action.STOP.value

    cooling = observe(engine, broker, at(10, 40), ENTER, 100.0, 100.0, strike=23400.0)
    assert cooling.action == Action.VETO.value
    assert [c.name for c in cooling.guard.failed] == ["reentry_cooldown"]
    assert "cooldown of 5 minutes runs until 10:42" in cooling.narrative

    second = observe(engine, broker, at(10, 45), ENTER, 100.0, 100.0, strike=23400.0)
    assert second.action == Action.REENTRY.value
    assert engine.position.number == 2 and engine.position.strike == 23400.0
    assert "Straddle 2 of the day, re-entry after the stop at 10:37: sold 1 lot of the 15-SEP-26 weekly straddle at 23400" in second.narrative
    target, _ = tick(engine, broker, at(11, 30), 55.0, 55.0, strike=23400.0)
    assert target.action == Action.TARGET.value

    third = observe(engine, broker, at(11, 40), ENTER, 99.0, 101.0, strike=23350.0)
    assert third.action == Action.REENTRY.value and engine.position.number == 3
    quiet, _ = tick(engine, broker, at(13, 0), 95.0, 95.0)
    assert quiet.action == Action.NONE.value
    square, _ = tick(engine, broker, at(15, 14, 30), 95.0, 95.0)
    assert square.action == Action.SQUARE_OFF.value
    assert [c["action"] for c in engine.closed] == ["STOP", "TARGET", "SQUARE_OFF"]
    assert [c["number"] for c in engine.closed] == [1, 2, 3]
    assert engine.entries_today == 3 and engine.state == State.FLAT
    assert engine.day_pnl() == pytest.approx(sum(c["net"] for c in engine.closed), abs=0.05)
    assert broker.positions() == {}


def test_no_entry_after_last_entry():
    engine, broker, _ = make_engine()
    step = observe(engine, broker, at(14, 35), ENTER, CALL, PUT)
    assert step.action == Action.VETO.value and engine.state == State.FLAT
    failed = [c.name for c in step.guard.failed]
    assert failed == ["trade_window", "last_entry"]
    assert "14:35 is past the last entry time 14:30" in step.narrative
    assert step.narrative.endswith("Still flat.")


def test_expiry_day_allowed_only_with_min_days_to_expiry_zero():
    expiry_day = date(2026, 9, 15)
    engine, broker, _ = make_engine(day=expiry_day, is_expiry_day=True, strategy__min_days_to_expiry=0)
    step = observe(engine, broker, at(10, 20, day=expiry_day), ENTER, CALL, PUT, expiry=expiry_day)
    assert step.action == Action.ENTER.value

    engine, broker, _ = make_engine(day=expiry_day, is_expiry_day=True, strategy__min_days_to_expiry=1)
    step = observe(engine, broker, at(10, 20, day=expiry_day), ENTER, CALL, PUT, expiry=expiry_day)
    assert step.action == Action.VETO.value
    assert [c.name for c in step.guard.failed] == ["expiry_min_dte"]


def test_sizing_math_from_the_market_facts():
    engine, _, _ = make_engine(strategy__lots=0)
    lots, math_ = engine.size_lots(204.0)
    assert lots == 3
    assert math_["risk_budget"] == 10000.0 and math_["risk_per_lot"] == pytest.approx(3315.0)
    assert math_["risk_lots"] == 3
    lots, math_ = engine.size_lots(204.0, margin_available=400000.0, margin_per_lot=188700.0)
    assert lots == 2 and math_["caps"]["margin"] == 2
    engine, _, _ = make_engine(strategy__lots=1)
    assert engine.size_lots(204.0)[0] == 1
    engine, _, _ = make_engine(strategy__lots=0, risk__capital=200000.0)
    lots, math_ = engine.size_lots(204.0)
    assert lots == 1 and math_["risk_lots"] == 0
    engine, _, _ = make_engine(strategy__lots=0, risk__max_lots=2)
    assert engine.size_lots(150.0)[0] == 2


def test_sizing_feeds_the_guard_and_the_trade():
    engine, broker, _ = make_engine(strategy__lots=0)
    step = observe(engine, broker, at(10, 20), ENTER, 104.0, 100.0)
    assert engine.position.lots == 3
    assert step.straddle["legs"][0]["qty"] == 195
    assert "Sold 3 lots of the 15-SEP-26 weekly straddle at 23350 for 204.0 points credit (INR 39,780)" in step.narrative
    ctx = GuardContext(margin_available=200000.0, margin_per_lot=188700.0)
    engine2, broker2, _ = make_engine(strategy__lots=0)
    step2 = observe(engine2, broker2, at(10, 20), ENTER, 104.0, 100.0, ctx=ctx)
    assert engine2.position.lots == 1 and step2.technical["lots"]["caps"]["margin"] == 1


def test_narrative_contains_the_key_numbers():
    engine, broker, _ = make_engine()
    step = enter(engine, broker)
    text = step.narrative
    assert text.startswith("10:20. NIFTY 23,350, INDIAVIX 12.1. The readout expects 82 percent of the movement the 23350 straddle is pricing. All 18 checks passed.")
    assert "Sold 1 lot of the 15-SEP-26 weekly straddle at 23350 for 201.3 points credit (INR 13,085)." in text
    assert "Stop 251.6, target 120.8, lock after 171.1, hard exit 15:15." in text
    assert "Leg stops: call 131.60, put 130.15 (30 percent, at the broker)." in text
    tech = step.technical
    assert tech["levels"]["stop_level"] == pytest.approx(251.625, abs=0.01) and tech["levels"]["target_level"] == 120.78
    assert tech["lots"]["lots"] == 1 and tech["thresholds"]["stop_pct"] == 25.0
    assert tech["costs"]["estimated_round_trip"] > 100
    assert tech["quote"]["implied_move_points"] == 201.3


def test_leg_stop_on_the_call_while_the_put_keeps_running():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    call_stop = engine.position.legs[engine.last_quote.call.symbol].stop_price
    assert call_stop == 131.6
    quiet, stop_steps = tick(engine, broker, at(11, 4), 131.55, 90.0)
    assert not stop_steps and quiet.action == Action.NONE.value
    step, stop_steps = tick(engine, broker, at(11, 5), 132.0, 90.0)
    assert len(stop_steps) == 1
    leg_step = stop_steps[0]
    assert leg_step.action == Action.STOP_LEG.value
    assert engine.state == State.IN_POSITION and engine.in_position
    legs = {leg["symbol"][-2:]: leg for leg in leg_step.straddle["legs"]}
    assert legs["CE"]["status"] == "closed" and legs["CE"]["stop_status"] == "triggered"
    assert legs["PE"]["status"] == "open" and legs["PE"]["stop_status"] == "pending"
    assert leg_step.straddle["combined_ltp"] == pytest.approx(132.0 + 90.0)
    assert "11:05:00. Call leg stop at 131.60 triggered at the broker." in leg_step.narrative
    assert "The put stays open with its own fixed stop at 130.15." in leg_step.narrative
    assert engine.leg_stops_today == 1
    # the combined stop no longer applies; the put runs to its own stop
    quiet, _ = tick(engine, broker, at(11, 30), 132.0, 125.0)
    assert quiet.action == Action.NONE.value and engine.in_position
    hold = observe(engine, broker, at(11, 35), HOLD, 132.0, 125.0)
    assert "The call was stopped at 132.00. The put runs with its own stop at 130.15." in hold.narrative
    _, stop_steps = tick(engine, broker, at(12, 0), 132.0, 131.0)
    assert stop_steps and stop_steps[0].action == Action.STOP_LEG.value
    assert engine.state == State.FLAT and engine.closed[0]["action"] == "STOP_LEG"
    assert engine.closed[0]["leg_stops"] == 2 and engine.leg_stops_today == 2
    assert broker.positions() == {}


def test_leg_stop_exit_both_variant():
    engine, broker, _ = make_engine(strategy__on_leg_stop="exit_both")
    enter(engine, broker)
    _, stop_steps = tick(engine, broker, at(11, 5), 132.0, 90.0)
    assert len(stop_steps) == 1 and stop_steps[0].action == Action.STOP_LEG.value
    assert [i.kind for i in stop_steps[0].intents] == ["EXIT"]
    assert engine.state == State.FLAT
    assert engine.closed[0]["action"] == "STOP_LEG" and engine.closed[0]["leg_stops"] == 1
    assert "Exiting the remaining leg as well (on_leg_stop exit_both)." in stop_steps[0].narrative
    assert "Result" in stop_steps[0].narrative
    assert broker.positions() == {}


def test_stop_orders_cancelled_before_a_target_exit():
    engine, broker, _ = make_engine()
    enter(engine, broker)
    ids = [s["order_id"] for s in broker.stop_orders()]
    assert len(ids) == 2 and all(s["status"] == "pending" for s in broker.stop_orders())
    step, _ = tick(engine, broker, at(11, 5), 60.0, 60.0)
    assert step.action == Action.TARGET.value
    assert broker.cancelled_stops == ids
    assert all(s["status"] == "cancelled" for s in broker.stop_orders())
    assert engine.closed[0]["legs"][engine.last_quote.call.symbol]["stop_status"] == "cancelled"


def test_software_leg_stops():
    engine, broker, _ = make_engine(strategy__leg_stop_mode="software")
    step = enter(engine, broker)
    assert [i.kind for i in step.intents] == ["ENTRY"]
    assert broker.stop_orders() == []
    assert all(leg["stop_status"] == "software" for leg in step.straddle["legs"])
    assert "Leg stops: call 131.60, put 130.15 (30 percent, software)." in step.narrative
    hit, stop_steps = tick(engine, broker, at(11, 5), 132.0, 90.0)
    assert not stop_steps
    assert hit.action == Action.STOP_LEG.value and [i.kind for i in hit.intents] == ["EXIT_LEG"]
    assert engine.state == State.IN_POSITION
    legs = {leg["symbol"][-2:]: leg for leg in hit.straddle["legs"]}
    assert legs["CE"]["status"] == "closed" and legs["PE"]["status"] == "open"
    assert "Call price 132.00 reached its stop 131.60." in hit.narrative
    assert "The put stays open with its own fixed stop at 130.15." in hit.narrative
    second, _ = tick(engine, broker, at(12, 0), 132.0, 131.0)
    assert second.action == Action.STOP_LEG.value and engine.state == State.FLAT
    assert engine.leg_stops_today == 2


def test_software_leg_stop_exit_both():
    engine, broker, _ = make_engine(strategy__leg_stop_mode="software", strategy__on_leg_stop="exit_both")
    enter(engine, broker)
    hit, _ = tick(engine, broker, at(11, 5), 132.0, 90.0)
    assert hit.action == Action.STOP_LEG.value
    assert [i.kind for i in hit.intents] == ["EXIT_LEG", "EXIT"]
    assert engine.state == State.FLAT and engine.closed[0]["action"] == "STOP_LEG"


def test_combined_stop_disabled_keeps_target_and_leg_stops():
    engine, broker, _ = make_engine(strategy__combined_stop_enabled=False)
    step = enter(engine, broker)
    assert engine.position.stop_level is None and engine.position.lock_level is None
    assert step.straddle["stop_level"] is None
    assert "No combined stop, target 120.8" in step.narrative
    quiet, _ = tick(engine, broker, at(11, 0), 126.0, 126.0)
    assert quiet.action == Action.NONE.value and engine.in_position
    _, stop_steps = tick(engine, broker, at(11, 10), 132.0, 126.0)
    assert stop_steps and stop_steps[0].action == Action.STOP_LEG.value


def test_halted_engine_does_nothing():
    engine, broker, _ = make_engine()
    engine.halt("unresolved order")
    step = observe(engine, broker, at(10, 20), ENTER, CALL, PUT)
    assert step.action == Action.NONE.value and not step.intents
    assert "Halted: unresolved order." in step.narrative
    assert engine.status()["state"] == "HALTED"


def test_manual_square_off_and_state_snapshot_shape():
    engine, broker, _ = make_engine()
    flat = engine.state_snapshot()
    assert flat == {
        "in_position": False,
        "expiry": None,
        "strike": None,
        "lots": None,
        "legs": [],
        "entry_credit": None,
        "combined_ltp": None,
        "stop_level": None,
        "target_level": None,
        "pnl": None,
        "entered_at": None,
        "square_off_at": None,
        "stop_basis": None,
        "expiry_selection": "weekly",
    }
    enter(engine, broker)
    snap = engine.state_snapshot()
    assert snap["expiry"] == "2026-09-15" and snap["entered_at"].startswith("2026-09-11T10:20:00")
    assert snap["square_off_at"] == "2026-09-11T15:15:00+05:30"
    assert set(snap["legs"][0]) == {"symbol", "side", "qty", "entry_price", "ltp", "stop_price", "stop_order_id", "stop_status", "status"}
    step = engine.square_off_now(at(12, 0), "STOP file present")
    assert step.action == Action.SQUARE_OFF.value and step.intents


def legs_of(intent):
    return [(leg.contract.symbol, leg.contract.strike, leg.contract.expiry, leg.quantity, leg.side) for leg in intent.legs]


def test_exits_close_exactly_the_entered_contracts_after_the_atm_moves():
    from openfly.interfaces import Side

    engine, broker, _ = make_engine()
    entry = enter(engine, broker)
    entry_intent = entry.intents[0]
    entered = legs_of(entry_intent)
    assert [leg[0][-2:] for leg in entered] == ["CE", "PE"] and all(leg[1] == 23350.0 for leg in entered)

    # the index moves 150 points: the current ATM is 23500 and a quote for that straddle arrives
    moved = observe(engine, broker, at(11, 0), HOLD, 95.0, 105.0, strike=23500.0, index=23500.0)
    assert moved.action == Action.HOLD.value and engine.position.strike == 23350.0
    assert engine.state_snapshot()["legs"][0]["symbol"].endswith("23350CE")

    # the readout exits; the quote for the open legs is what the worker feeds while in position
    step = observe(engine, broker, at(11, 5), EXIT, 100.0, 100.0, strike=23350.0, index=23500.0, roi=1.2)
    exit_intent = next(i for i in step.intents if i.kind == "EXIT")
    assert legs_of(exit_intent) == [(s, k, e, q, Side.BUY) for (s, k, e, q, _) in entered]
    assert engine.state == State.FLAT and broker.positions() == {}

    # the next entry re-strikes at the current ATM from the latest quote
    fresh = observe(engine, broker, at(11, 15), ENTER, 95.0, 105.0, strike=23500.0, index=23500.0)
    assert fresh.action == Action.REENTRY.value and engine.position.strike == 23500.0
    new_legs = legs_of(fresh.intents[0])
    assert [leg[0] for leg in new_legs] == ["NIFTY15SEP2623500CE", "NIFTY15SEP2623500PE"]
    assert all(leg[3] == 65 and leg[4] == Side.SELL for leg in new_legs)


def test_exit_after_a_leg_stop_closes_only_the_remaining_leg():
    from openfly.interfaces import Side

    engine, broker, _ = make_engine()
    entry = enter(engine, broker)
    put_symbol = entry.intents[0].legs[1].contract.symbol
    _, stop_steps = tick(engine, broker, at(11, 5), 132.0, 90.0)
    assert stop_steps and stop_steps[0].action == Action.STOP_LEG.value
    assert broker.positions() == {put_symbol: -65}
    step = observe(engine, broker, at(11, 10), EXIT, 132.0, 90.0, strike=23350.0, roi=1.2)
    exit_intent = next(i for i in step.intents if i.kind == "EXIT")
    assert legs_of(exit_intent) == [(put_symbol, 23350.0, entry.intents[0].legs[1].contract.expiry, 65, Side.BUY)]
    assert engine.state == State.FLAT and broker.positions() == {}
    assert engine.exit_legs() == ()
