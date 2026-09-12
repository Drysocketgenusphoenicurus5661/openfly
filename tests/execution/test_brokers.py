"""ReplayBroker fills and costs; OpenAlgoBroker against the fake exchange through the real engine."""

from __future__ import annotations

import pytest

from openfly.execution.brokers import OpenAlgoBroker, ReplayBroker, order_state, parse_broker_time
from openfly.execution.costs import SimpleCostModel
from openfly.execution.dispatch import apply_broker_events, execute_step
from openfly.execution.ledger import Ledger
from openfly.execution.types import KIND_STOPS, quote_lookup_for
from openfly.interfaces import (
    Bar,
    Contract,
    Decision,
    Intent,
    IntentStatus,
    Leg,
    MarketObservation,
    Prediction,
    Quote,
    SessionWindow,
    Side,
    StraddleQuote,
    UnresolvedOrder,
)
from openfly.straddle.engine import Action, State, StraddleEngine

from .fakes import CE, DAY, EXPIRY, PE, FakeClock, FakeExchange, at, settings_with, straddle_quote

# ---------------------------------------------------------------- helpers


def contract(symbol: str) -> Contract:
    return Contract(symbol, "NFO", "NIFTY", EXPIRY, 23350.0, symbol[-2:], 65, 0.05, 1800)


def window() -> SessionWindow:
    return SessionWindow(DAY, at(9, 15), at(15, 30), at(9, 20), at(14, 30), at(15, 15), False)


def observation(when, quote: StraddleQuote, engine: StraddleEngine) -> MarketObservation:
    bars = tuple(Bar(at(9, 15), 23350.0, 23350.0, 23350.0, 23350.0, 0.0) for _ in range(3))
    pos = engine.position if engine.in_position else None
    return MarketObservation(when, bars, 12.1, (), quote.combined_ltp, pos.entry_credit if pos else None, 3.6, 65, -pos.lots if pos else 0)


class Rig:
    """Engine + ledger + OpenAlgoBroker + fake exchange, wired the way the worker wires them."""

    def __init__(self, tmp_path, mode="paper", **overrides):
        self.settings = settings_with(**overrides)
        self.clock = FakeClock()
        self.exchange = FakeExchange(clock=self.clock)
        self.exchange.prices = {CE: 101.2, PE: 100.1}
        self.model = SimpleCostModel.from_settings(self.settings)
        self.ledger = Ledger(tmp_path, self.model)
        self.broker = OpenAlgoBroker(self.exchange, self.settings, self.ledger, mode=mode, clock=self.clock, sleep=self.clock.sleep)
        self.engine = StraddleEngine(self.settings, self.model)
        self.engine.start_day(window())

    def observe(self, when, decision, call=101.2, put=100.1, roi=0.82, strike=23350.0):
        quote = straddle_quote(call, put, when, strike=strike)
        step = self.engine.on_observation(observation(when, quote, self.engine), Prediction(roi, 0.6, decision), quote, window())
        if step.intents:
            execute_step(self.engine, step, self.broker, self.ledger, quote_lookup_for(quote))
        return step

    def tick(self, when, call, put):
        quote = straddle_quote(call, put, when)
        step = self.engine.on_tick(quote, when)
        if step.intents:
            execute_step(self.engine, step, self.broker, self.ledger, quote_lookup_for(quote))
        return step

    def reconcile(self, when):
        events = self.broker.reconcile()
        steps = apply_broker_events(self.engine, self.broker, events, when)
        for s in steps:
            if s.intents:
                execute_step(self.engine, s, self.broker, self.ledger, quote_lookup_for(straddle_quote(when=when)))
        return events, steps


# ------------------------------------------------------------ replay broker


def test_replay_broker_fills_costs_and_positions():
    model = SimpleCostModel.from_settings(settings_with())
    broker = ReplayBroker(model, slippage_ticks=1)
    quote = straddle_quote(101.2, 100.1, spread=0.10)
    entry = Intent("e", "ENTRY", (Leg(contract(CE), Side.SELL, 65, None), Leg(contract(PE), Side.SELL, 65, None)), "test", 1.0)
    fills = broker.execute(entry, quote_lookup_for(quote))
    assert [f.average_price for f in fills] == [pytest.approx(101.1), pytest.approx(100.0)]  # bid minus one tick
    assert broker.positions() == {CE: -65, PE: -65}
    assert broker.costs > 40
    exit_ = Intent("x", "EXIT", (Leg(contract(CE), Side.BUY, 65, None), Leg(contract(PE), Side.BUY, 65, None)), "test", 2.0)
    fills = broker.execute(exit_, quote_lookup_for(straddle_quote(90.0, 90.0, spread=0.10)))
    assert [f.average_price for f in fills] == [pytest.approx(90.1), pytest.approx(90.1)]  # ask plus one tick
    assert broker.positions() == {}
    assert broker.gross_pnl() == pytest.approx((101.1 + 100.0 - 180.2) * 65)
    assert broker.net_pnl() == pytest.approx(broker.gross_pnl() - broker.costs, abs=0.01)
    no_depth = StraddleQuote(Quote(CE, "NFO", 50.0, 0.0, 0.0, 3.0), Quote(PE, "NFO", 40.0, 0.0, 0.0, 3.0), 23350.0, EXPIRY)
    fills = broker.execute(entry, quote_lookup_for(no_depth))
    assert [f.average_price for f in fills] == [pytest.approx(49.95), pytest.approx(39.95)]  # ltp minus one tick


def test_replay_broker_simulated_leg_stops():
    broker = ReplayBroker(None, slippage_ticks=0)
    from openfly.execution.types import StopLeg

    stops = Intent("s", KIND_STOPS, (StopLeg(contract(CE), Side.BUY, 65, None, 131.6), StopLeg(contract(PE), Side.BUY, 65, None, 130.15)), "stops", 1.0)
    assert broker.execute(stops, quote_lookup_for(straddle_quote())) == []
    assert [s["status"] for s in broker.stop_orders()] == ["pending", "pending"]
    assert broker.on_quote(straddle_quote(131.55, 90.0), None) == []
    events = broker.on_quote(straddle_quote(132.0, 90.0, spread=0.0), None)
    assert len(events) == 1 and events[0]["symbol"] == CE
    assert events[0]["fill"].average_price == pytest.approx(132.0)
    assert broker.positions() == {CE: 65}
    statuses = {s["symbol"]: s["status"] for s in broker.stop_orders()}
    assert statuses == {CE: "triggered", PE: "pending"}


# ----------------------------------------------------------- openalgo broker


def test_both_legs_complete_and_stops_are_placed(tmp_path):
    rig = Rig(tmp_path)
    step = rig.observe(at(10, 20), Decision.ENTER)
    assert step.action == Action.ENTER.value and rig.engine.state == State.IN_POSITION
    rows = {r["intent_id"]: r for r in rig.ledger.intents()}
    entry = next(r for r in rows.values() if r["kind"] == "ENTRY")
    assert entry["status"] == "SETTLED"
    assert all(leg["order_id"] and leg["status"] == "complete" and leg["average_price"] > 0 for leg in entry["legs"])
    basket = rig.exchange.calls_of("basketorder")[0]
    assert [o["action"] for o in basket] == ["SELL", "SELL"]
    assert basket[0]["pricetype"] == "LIMIT" and basket[0]["price"] == pytest.approx(101.1)  # ltp minus two ticks
    stops = rig.exchange.calls_of("placeorder")
    assert [(o["symbol"], o["pricetype"], o["trigger_price"]) for o in stops] == [(CE, "SL-M", 131.6), (PE, "SL-M", 130.15)]
    records = rig.ledger.stop_orders(active_only=True)
    assert len(records) == 2 and all(r["order_id"] for r in records)
    snap = rig.engine.state_snapshot()
    assert [leg["stop_status"] for leg in snap["legs"]] == ["pending", "pending"]
    assert snap["legs"][0]["stop_order_id"] == records[0]["order_id"]
    assert rig.broker.positions() == {CE: -65, PE: -65}
    assert rig.ledger.day_pnl() == pytest.approx(201.3 * 65 - sum(f["cost"] for f in rig.ledger.fills()))


def test_one_leg_rejected_produces_a_repair_that_unwinds(tmp_path):
    rig = Rig(tmp_path)
    rig.exchange.scenarios[PE] = "reject"
    step = rig.observe(at(10, 20), Decision.ENTER)
    assert [i.kind for i in step.intents] == ["ENTRY", "REPAIR"]
    assert rig.engine.state == State.FLAT and rig.engine.position is None
    assert step.entry_failed
    statuses = {r["kind"]: r["status"] for r in rig.ledger.intents()}
    assert statuses == {"ENTRY": "PARTIAL", "REPAIR": "SETTLED"}
    assert rig.exchange.positions == {CE: 0}
    assert rig.broker.positions() == {}
    assert "one-legged" in step.narrative.lower() or "unwound" in step.narrative
    assert not rig.engine.is_halted
    assert rig.ledger.pending() == []


def test_one_leg_rejected_with_complete_policy(tmp_path):
    rig = Rig(tmp_path, execution__repair_policy="complete")
    rig.exchange.scenarios[PE] = "reject"
    step = rig.observe(at(10, 20), Decision.ENTER)
    # the repair sells the missing put: the fake rejects it again, so the book stays one-legged and the engine halts
    assert [i.kind for i in step.intents] == ["ENTRY", "REPAIR"]
    assert rig.engine.is_halted and "unbalanced" in rig.engine.halt_reason


def test_lost_response_unknown_then_reconcile_adopts_unique_match(tmp_path):
    rig = Rig(tmp_path)
    rig.exchange.lose_response = True
    step = rig.observe(at(10, 20), Decision.ENTER)
    assert rig.engine.state == State.IN_POSITION
    entry = next(r for r in rig.ledger.intents() if r["kind"] == "ENTRY")
    assert entry["status"] == "SETTLED" and entry["detail"] == "adopted from the orderbook"
    assert all(leg["order_id"].startswith("F") for leg in entry["legs"])
    assert [i.kind for i in step.intents] == ["ENTRY", KIND_STOPS]
    assert rig.engine.position.entry_credit == pytest.approx(201.3)


def test_lost_response_with_ambiguous_orderbook_halts(tmp_path):
    rig = Rig(tmp_path)
    rig.exchange.lose_response = True
    rig.exchange.foreign_orders.append(
        {"orderid": "X1", "symbol": CE, "action": "SELL", "quantity": "65", "product": "NRML", "pricetype": "LIMIT", "order_status": "complete", "strategy": "openfly", "timestamp": rig.exchange._timestamp()}
    )
    with pytest.raises(UnresolvedOrder, match="2 orderbook matches"):
        rig.observe(at(10, 20), Decision.ENTER)
    assert rig.engine.is_halted and rig.ledger.halted()["reason"].startswith("unresolved order")
    assert rig.ledger.status(rig.ledger.intents()[0]["intent_id"]) == IntentStatus.UNKNOWN


def test_partial_fill_cancels_the_remainder_and_settles_partial(tmp_path):
    rig = Rig(tmp_path, strategy__lots=2, risk__max_lots=3)
    rig.exchange.scenarios = {CE: "partial", PE: "partial"}
    rig.exchange.partial_qty = {CE: 65, PE: 65}
    step = rig.observe(at(10, 20), Decision.ENTER)
    entry = next(r for r in rig.ledger.intents() if r["kind"] == "ENTRY")
    assert entry["status"] == "PARTIAL"
    assert [leg["filled_qty"] for leg in entry["legs"]] == [65, 65]
    cancelled = rig.exchange.calls_of("cancelorder")
    assert len(cancelled) == 2
    assert rig.clock.t >= at(10, 20).timestamp() + rig.broker.fill_timeout
    assert rig.engine.state == State.IN_POSITION and rig.engine.position.lots == 1
    assert step.straddle["lots"] == 1 and step.straddle["legs"][0]["qty"] == 65


def test_preflight_refuses_wrong_analyzer_mode(tmp_path):
    rig = Rig(tmp_path, mode="paper")
    rig.exchange.analyzer = False
    result = rig.broker.preflight(symbols=[CE, PE], lots=1)
    assert not result["ok"]
    checks = {c["name"]: c for c in result["checks"]}
    assert not checks["analyzer"]["ok"] and checks["analyzer"]["detail"] == "analyzer is off; paper mode requires it on"
    assert checks["funds"]["ok"] and "INR 188,700" in checks["funds"]["detail"]
    assert checks["foreign_orders"]["ok"] and checks["ledger_pending"]["ok"]
    rig.exchange.analyzer = True
    assert rig.broker.preflight(symbols=[CE, PE], lots=1)["ok"]
    live = Rig(tmp_path / "live", mode="live")
    live.exchange.analyzer = True
    assert not live.broker.preflight(symbols=[CE, PE])["ok"]
    live.exchange.analyzer = False
    assert live.broker.preflight(symbols=[CE, PE])["ok"]


def test_preflight_flags_unknown_symbols_and_foreign_orders(tmp_path):
    rig = Rig(tmp_path)
    rig.exchange.known_symbols = {CE}
    rig.exchange.foreign_orders.append({"orderid": "Z9", "symbol": PE, "action": "BUY", "quantity": "65", "order_status": "open", "strategy": "someone_else", "timestamp": ""})
    result = rig.broker.preflight(symbols=[CE, PE])
    failed = {c["name"]: c["detail"] for c in result["checks"] if not c["ok"]}
    assert set(failed) == {"symbol_master", "foreign_orders"}
    assert failed["symbol_master"].startswith(f"{PE} lookup failed")
    assert failed["foreign_orders"] == "1 open order(s) on the legs from other strategies"


def test_stop_orders_cancelled_before_a_target_exit(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    stop_ids = [r["order_id"] for r in rig.ledger.stop_orders(active_only=True)]
    rig.exchange.prices = {CE: 60.0, PE: 60.0}
    step = rig.tick(at(11, 5), 60.0, 60.0)
    assert step.action == Action.TARGET.value and rig.engine.state == State.FLAT
    names = [name for name, _ in rig.exchange.calls if name in ("cancelorder", "basketorder")]
    assert names[:3] == ["basketorder", "cancelorder", "cancelorder"]
    assert names[3] == "basketorder"
    assert [p for n, p in rig.exchange.calls if n == "cancelorder"] == stop_ids
    assert all(r["status"] == "cancelled" for r in rig.ledger.stop_orders())
    assert rig.exchange.positions == {CE: 0, PE: 0}
    assert rig.ledger.pending() == []


def test_missing_stop_order_is_replaced_on_reconcile(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    old = rig.ledger.stop_orders(active_only=True)
    rig.exchange.cancel_externally(old[0]["order_id"])
    events, steps = rig.reconcile(at(10, 30))
    assert [e["type"] for e in events] == ["stop_replaced"]
    assert events[0]["symbol"] == CE and events[0]["old_order_id"] == old[0]["order_id"]
    active = {r["symbol"]: r for r in rig.ledger.stop_orders(active_only=True)}
    assert set(active) == {CE, PE}
    assert active[CE]["order_id"] != old[0]["order_id"] and active[CE]["trigger_price"] == 131.6
    snap = rig.engine.state_snapshot()
    call_leg = next(leg for leg in snap["legs"] if leg["symbol"] == CE)
    assert call_leg["stop_order_id"] == active[CE]["order_id"] and call_leg["stop_status"] == "pending"
    assert steps == []


def test_leg_stop_executed_at_the_broker_is_detected_on_reconcile(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    call_stop = next(r for r in rig.ledger.stop_orders(active_only=True) if r["symbol"] == CE)
    rig.exchange.trigger_stop(call_stop["order_id"], 132.0)
    events, steps = rig.reconcile(at(13, 5))
    assert [e["type"] for e in events] == ["stop_triggered"]
    assert len(steps) == 1 and steps[0].action == Action.STOP_LEG.value
    assert "13:05:00. Call leg stop at 131.60 triggered at the broker." in steps[0].narrative
    assert "The put stays open with its own fixed stop at 130.15." in steps[0].narrative
    assert rig.engine.state == State.IN_POSITION
    legs = {leg["symbol"]: leg for leg in rig.engine.state_snapshot()["legs"]}
    assert legs[CE]["status"] == "closed" and legs[CE]["stop_status"] == "triggered"
    assert legs[PE]["status"] == "open" and legs[PE]["stop_status"] == "pending"
    stops_intent = next(r for r in rig.ledger.intents() if r["kind"] == KIND_STOPS)
    assert [f["symbol"] for f in rig.ledger.fills(stops_intent["intent_id"])] == [CE]
    assert rig.broker.positions() == {PE: -65}
    # a second reconcile does not double count
    events, _ = rig.reconcile(at(13, 6))
    assert events == []


def test_exit_when_a_stop_fired_during_cancellation(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    call_stop = next(r for r in rig.ledger.stop_orders(active_only=True) if r["symbol"] == CE)
    rig.exchange.trigger_stop(call_stop["order_id"], 132.0)
    rig.exchange.prices = {CE: 132.0, PE: 60.0}
    step = rig.tick(at(11, 5), 132.0, 60.0)  # combined 192 is not a level; the readout exits instead
    assert step.action == Action.NONE.value
    step = rig.observe(at(11, 10), Decision.EXIT, 132.0, 60.0, roi=1.2)
    assert step.action == Action.EXIT.value and rig.engine.state == State.FLAT
    basket = rig.exchange.calls_of("basketorder")[-1]
    assert [o["symbol"] for o in basket] == [PE]  # the call was already closed by its stop
    assert rig.exchange.positions == {CE: 0, PE: 0}
    assert rig.engine.closed[0]["leg_stops"] == 1 and rig.engine.closed[0]["legs"][CE]["stop_status"] == "triggered"
    assert rig.engine.closed[0]["legs"][PE]["stop_status"] == "cancelled"


def test_stop_order_rejected_is_reported_and_replaced_later(tmp_path):
    rig = Rig(tmp_path)
    rig.exchange.stop_behaviour[PE] = "reject"
    rig.observe(at(10, 20), Decision.ENTER)
    legs = {leg["symbol"]: leg for leg in rig.engine.state_snapshot()["legs"]}
    assert legs[CE]["stop_status"] == "pending" and legs[PE]["stop_status"] == "rejected"
    stops_intent = next(r for r in rig.ledger.intents() if r["kind"] == KIND_STOPS)
    assert stops_intent["status"] == "PARTIAL"
    rig.exchange.stop_behaviour.pop(PE)
    events, _ = rig.reconcile(at(10, 25))
    assert [e["type"] for e in events] == ["stop_replaced"] and events[0]["symbol"] == PE
    legs = {leg["symbol"]: leg for leg in rig.engine.state_snapshot()["legs"]}
    assert legs[PE]["stop_status"] == "pending" and legs[PE]["stop_order_id"]


def test_exit_rejected_is_retried_and_halts_after_three_failures(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    rig.exchange.scenarios = {CE: "reject", PE: "reject"}
    first = rig.tick(at(11, 0), 126.0, 126.0)
    assert first.action == Action.STOP.value and rig.engine.state == State.IN_POSITION
    assert "rejected" in first.narrative
    rig.tick(at(11, 1), 126.0, 126.0)
    third = rig.tick(at(11, 2), 126.0, 126.0)
    assert rig.engine.is_halted and "exit failed 3 times" in rig.engine.halt_reason
    assert third.action == Action.STOP.value


def test_prepared_but_never_sent_intent_is_rejected_on_reconcile(tmp_path):
    rig = Rig(tmp_path)
    intent = Intent("ghost", "ENTRY", (Leg(contract(CE), Side.SELL, 65, None),), "crash before send", at(10, 0).timestamp())
    rig.ledger.reserve(intent)
    events = rig.broker.reconcile()
    assert events[0]["status"] == "REJECTED" and rig.ledger.status("ghost") == IntentStatus.REJECTED


def test_order_state_and_time_parsing():
    assert order_state({"order_status": "complete", "quantity": "65", "average_price": "101.2"}) .filled == 65
    partial = order_state({"data": {"order_status": "open", "quantity": "130", "filled_quantity": "65", "average_price": "100"}})
    assert partial.status == "open" and partial.filled == 65 and partial.average_price == 100.0
    assert order_state({"order_status": "rejected", "quantity": "65"}).filled == 0
    assert parse_broker_time("11-Sep-2026 10:20:04") == pytest.approx(at(10, 20, 4).timestamp())
    assert parse_broker_time(at(10, 20).timestamp() * 1000) == pytest.approx(at(10, 20).timestamp())
    assert parse_broker_time("") is None and parse_broker_time("garbage") is None


def test_exit_basket_matches_the_entry_legs_from_the_ledger(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    entry_basket = rig.exchange.calls_of("basketorder")[0]
    assert rig.ledger.open_legs() == {CE: -65, PE: -65}
    # the index moves 150 points; the chain now says the ATM is 23500 and a quote for it arrives
    moved = rig.observe(at(11, 0), Decision.HOLD, 95.0, 105.0, strike=23500.0)
    assert moved.action == Action.HOLD.value and rig.engine.position.strike == 23350.0
    rig.exchange.prices = {CE: 100.0, PE: 100.0}
    step = rig.observe(at(11, 5), Decision.EXIT, 100.0, 100.0, roi=1.2)
    assert step.action == Action.EXIT.value and rig.engine.state == State.FLAT
    exit_basket = rig.exchange.calls_of("basketorder")[-1]
    assert [(o["symbol"], o["quantity"], o["action"]) for o in exit_basket] == [(o["symbol"], o["quantity"], "BUY") for o in entry_basket]
    assert rig.ledger.open_legs() == {} and rig.broker.positions() == {} and rig.exchange.positions == {CE: 0, PE: 0}


def test_exit_after_a_broker_leg_stop_closes_only_the_remaining_leg(tmp_path):
    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    call_stop = next(r for r in rig.ledger.stop_orders(active_only=True) if r["symbol"] == CE)
    rig.exchange.trigger_stop(call_stop["order_id"], 132.0)
    rig.reconcile(at(11, 0))
    assert rig.ledger.open_legs() == {PE: -65}
    rig.exchange.prices = {PE: 90.0}
    step = rig.observe(at(11, 5), Decision.EXIT, 132.0, 90.0, roi=1.2)
    exit_basket = rig.exchange.calls_of("basketorder")[-1]
    assert [(o["symbol"], o["quantity"], o["action"]) for o in exit_basket] == [(PE, 65, "BUY")]
    assert step.action == Action.EXIT.value and rig.ledger.open_legs() == {} and rig.engine.state == State.FLAT


def test_exit_that_disagrees_with_the_ledger_halts_instead_of_sending(tmp_path):
    from openfly.execution.dispatch import execute_intent, ledger_disagreement
    from openfly.straddle.engine import EngineStep

    rig = Rig(tmp_path)
    rig.observe(at(10, 20), Decision.ENTER)
    wrong = Intent("bad-exit", "EXIT", (Leg(contract("NIFTY15SEP2623500PE"), Side.BUY, 65, None), Leg(contract(CE), Side.BUY, 130, None)), "wrong strike", at(11, 0).timestamp())
    assert ledger_disagreement(rig.ledger, wrong) == "BUY 65 NIFTY15SEP2623500PE but the ledger shows 0 short; BUY 130 NIFTY15SEP2623350CE but the ledger shows 65 short"
    sent_before = len(rig.exchange.calls_of("basketorder"))
    step = EngineStep(at=at(11, 0), trigger="manual")
    rig.engine._current_step = step
    execute_intent(rig.engine, step, rig.broker, rig.ledger, wrong, quote_lookup_for(straddle_quote()))
    assert len(rig.exchange.calls_of("basketorder")) == sent_before
    assert rig.engine.is_halted and "do not match the ledger" in rig.engine.halt_reason
    assert rig.ledger.halted()["reason"].startswith("exit legs do not match")
    assert rig.ledger.open_legs() == {CE: -65, PE: -65}


def test_adaptive_leg_stop_orders_carry_the_adaptive_prices(tmp_path):
    from openfly.execution.types import ceil_to_tick

    rig = Rig(tmp_path, strategy__stop_mode="adaptive")
    step = rig.observe(at(10, 20), Decision.ENTER)
    assert step.action == Action.ENTER.value
    import math

    basis = rig.engine.position.basis
    assert basis.mode == "adaptive" and basis.realized_move_points is None  # the rig observation has 3 bars
    assert basis.minutes_to_expiry == 1060  # 310 minutes left today plus two sessions to the 15-SEP-26 weekly expiry
    implied = 201.3 * math.sqrt(60 / 1060)
    gamma = 0.32 / 201.3
    rise = 0.5 * implied + 0.5 * gamma * implied * implied
    assert basis.leg_stop_pct["ce"] == pytest.approx(1.25 * rise / 101.2 * 100, rel=1e-6)
    assert basis.leg_stop_pct["pe"] == pytest.approx(1.25 * rise / 100.1 * 100, rel=1e-6)
    assert {k: basis.clipped[k] for k in ("ce", "pe", "combined")} == {"ce": "", "pe": "", "combined": "min"}
    legs = {leg.option_type: leg for leg in rig.engine.position.legs.values()}
    assert legs["CE"].stop_price == ceil_to_tick(101.2 * (1 + basis.leg_stop_pct["ce"] / 100))
    assert legs["PE"].stop_price == ceil_to_tick(100.1 * (1 + basis.leg_stop_pct["pe"] / 100))
    placed = [(o["symbol"], o["pricetype"], o["trigger_price"]) for o in rig.exchange.calls_of("placeorder")]
    assert placed == [(CE, "SL-M", legs["CE"].stop_price), (PE, "SL-M", legs["PE"].stop_price)]
    records = {r["symbol"]: r["trigger_price"] for r in rig.ledger.stop_orders(active_only=True)}
    assert records == {CE: legs["CE"].stop_price, PE: legs["PE"].stop_price}
    snap = rig.engine.state_snapshot()
    assert snap["stop_basis"]["leg_stop_pct"] == {"ce": round(basis.leg_stop_pct["ce"], 2), "pe": round(basis.leg_stop_pct["pe"], 2)}
    assert snap["stop_basis"]["combined_stop_pct"] == 3.0 and snap["stop_level"] == round(201.3 * 1.03, 2)
    assert snap["stop_basis"]["target_mode"] == "fixed" and snap["stop_basis"]["target_pct"] == 40.0
    intents = {r["kind"]: r for r in rig.ledger.intents()}
    assert intents["STOPS"]["reason"] == f"per-leg adaptive stops: ce {basis.leg_stop_pct['ce']:.1f} percent, pe {basis.leg_stop_pct['pe']:.1f} percent"

