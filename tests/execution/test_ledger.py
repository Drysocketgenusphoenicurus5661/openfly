"""Ledger lifecycle, idempotent settle, pending refusal, stop order records."""

from __future__ import annotations

from datetime import date

import pytest

from openfly.execution.costs import SimpleCostModel
from openfly.execution.ledger import STOP_PENDING, STOP_TRIGGERED, Ledger, intent_from_payload
from openfly.execution.types import PendingIntentError, StopLeg
from openfly.interfaces import Contract, Fill, Intent, IntentStatus, Leg, Side

from .fakes import CE, EXPIRY, PE, settings_with


def contract(symbol: str) -> Contract:
    return Contract(symbol, "NFO", "NIFTY", EXPIRY, 23350.0, symbol[-2:], 65, 0.05, 1800)


def entry_intent(intent_id: str = "entry-1") -> Intent:
    return Intent(intent_id, "ENTRY", (Leg(contract(CE), Side.SELL, 65, None), Leg(contract(PE), Side.SELL, 65, None)), "readout ENTER 0.82", 1_800_000_000.0)


def exit_intent(intent_id: str = "exit-1") -> Intent:
    return Intent(intent_id, "EXIT", (Leg(contract(CE), Side.BUY, 65, None), Leg(contract(PE), Side.BUY, 65, None)), "target", 1_800_000_100.0)


def test_lifecycle_and_idempotent_settle(tmp_path):
    ledger = Ledger(tmp_path)
    intent = entry_intent()
    ledger.reserve(intent)
    assert ledger.status("entry-1") == IntentStatus.PREPARED
    assert ledger.pending()[0]["intent_id"] == "entry-1"

    ledger.mark("entry-1", IntentStatus.UNKNOWN, "basket sent")
    assert ledger.status("entry-1") == IntentStatus.UNKNOWN
    ledger.record_leg_order("entry-1", CE, Side.SELL, "O1", "open")
    ledger.record_leg_order("entry-1", PE, Side.SELL, "O2", "open")

    fills = [Fill("entry-1", CE, Side.SELL, 65, 101.2, "O1", 1.0), Fill("entry-1", PE, Side.SELL, 65, 100.1, "O2", 1.0)]
    assert ledger.settle("entry-1", fills) == IntentStatus.SETTLED
    assert ledger.settle("entry-1", fills) == IntentStatus.SETTLED  # idempotent
    assert len(ledger.fills("entry-1")) == 2
    assert ledger.pending() == []
    row = ledger.get("entry-1")
    assert row["legs"][0] == {"symbol": CE, "side": "SELL", "quantity": 65, "limit_price": None, "order_id": "O1", "status": "complete", "filled_qty": 65, "average_price": 101.2}
    assert intent_from_payload(row["payload"]) == intent


def test_refuses_a_second_pending_intent(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.reserve(entry_intent("a"))
    with pytest.raises(PendingIntentError, match="intent a is still PREPARED"):
        ledger.reserve(entry_intent("b"))
    ledger.mark("a", IntentStatus.ACCEPTED)
    with pytest.raises(PendingIntentError):
        ledger.reserve(entry_intent("b"))
    ledger.settle("a", [], IntentStatus.REJECTED)
    ledger.reserve(entry_intent("b"))  # allowed once the first is final
    with pytest.raises(PendingIntentError, match="already recorded"):
        ledger.reserve(entry_intent("b"))


def test_settle_classifies_partial_and_rejected(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.reserve(entry_intent("p"))
    assert ledger.settle("p", [Fill("p", CE, Side.SELL, 65, 101.2, "O1", 1.0)]) == IntentStatus.PARTIAL
    ledger.reserve(entry_intent("r"))
    assert ledger.settle("r", []) == IntentStatus.REJECTED


def test_day_pnl_from_fills_net_of_costs(tmp_path):
    model = SimpleCostModel.from_settings(settings_with())
    ledger = Ledger(tmp_path, cost_model=model)
    ledger.reserve(entry_intent())
    ledger.settle("entry-1", [Fill("entry-1", CE, Side.SELL, 65, 101.2, "O1", 1.0), Fill("entry-1", PE, Side.SELL, 65, 100.1, "O2", 1.0)])
    ledger.reserve(exit_intent())
    ledger.settle("exit-1", [Fill("exit-1", CE, Side.BUY, 65, 90.0, "O3", 2.0), Fill("exit-1", PE, Side.BUY, 65, 90.0, "O4", 2.0)])
    gross = (201.3 - 180.0) * 65
    costs = sum(f["cost"] for f in ledger.fills())
    assert costs > 100
    assert ledger.day_pnl() == pytest.approx(gross - costs, abs=0.01)
    plain = Ledger(tmp_path, filename="plain.db")
    plain.reserve(entry_intent())
    plain.settle("entry-1", [Fill("entry-1", CE, Side.SELL, 65, 101.2, "O1", 1.0)])
    assert plain.day_pnl() == pytest.approx(101.2 * 65)


def test_intents_api_shape_and_meta(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.reserve(entry_intent("first"))
    ledger.settle("first", [])
    ledger.reserve(entry_intent("second"))
    rows = ledger.intents(limit=10)
    assert [r["intent_id"] for r in rows] == ["second", "first"]
    assert set(rows[0]) >= {"intent_id", "kind", "status", "created_at", "reason", "legs"}
    assert rows[0]["created_at"].endswith("+05:30")
    assert "payload" not in rows[0]

    ledger.set_trading_date(date(2026, 9, 11))
    assert ledger.entries_today() == 0
    assert ledger.increment_entries() == 1
    ledger.set_trading_date(date(2026, 9, 11))
    assert ledger.entries_today() == 1
    ledger.set_trading_date(date(2026, 9, 14))
    assert ledger.entries_today() == 0

    assert ledger.halted() is None
    ledger.halt("unresolved order")
    assert ledger.halted()["reason"] == "unresolved order"
    ledger.clear_halt()
    assert ledger.halted() is None
    ledger.save_checkpoint({"state": "FLAT"})
    assert ledger.load_checkpoint() == {"state": "FLAT"}


def test_stop_order_records(tmp_path):
    ledger = Ledger(tmp_path)
    stops = Intent("stops-1", "STOPS", (StopLeg(contract(CE), Side.BUY, 65, None, 131.6),), "per-leg stops", 1.0)
    ledger.reserve(stops)
    payload = ledger.get("stops-1")["payload"]
    assert payload["legs"][0]["trigger_price"] == 131.6
    restored = intent_from_payload(payload)
    assert isinstance(restored.legs[0], StopLeg) and restored.legs[0].trigger_price == 131.6
    ledger.settle("stops-1", [], IntentStatus.SETTLED)
    ledger.record_stop_order("stops-1", CE, 65, 131.6, "S1")
    assert ledger.stop_orders(active_only=True)[0]["status"] == STOP_PENDING
    ledger.update_stop_order("S1", STOP_TRIGGERED, 65, 132.0)
    assert ledger.stop_orders(active_only=True) == []
    assert ledger.last_stop_for(CE)["average_price"] == 132.0
    ledger.add_fills("stops-1", [Fill("stops-1", CE, Side.BUY, 65, 132.0, "S1", 3.0)])
    ledger.add_fills("stops-1", [Fill("stops-1", CE, Side.BUY, 65, 132.0, "S1", 3.0)])
    assert len(ledger.fills("stops-1")) == 1
    assert ledger.status("stops-1") == IntentStatus.SETTLED


def test_open_legs_from_fills(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.reserve(entry_intent())
    ledger.settle("entry-1", [Fill("entry-1", CE, Side.SELL, 65, 101.2, "O1", 1.0), Fill("entry-1", PE, Side.SELL, 65, 100.1, "O2", 1.0)])
    assert ledger.open_legs() == {CE: -65, PE: -65}
    ledger.add_fills("stops-1", [Fill("stops-1", CE, Side.BUY, 65, 132.0, "S1", 2.0)])
    assert ledger.open_legs() == {PE: -65}
    ledger.reserve(exit_intent())
    ledger.settle("exit-1", [Fill("exit-1", PE, Side.BUY, 65, 90.0, "O4", 3.0)])
    assert ledger.open_legs() == {}
