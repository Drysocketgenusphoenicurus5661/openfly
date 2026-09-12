from __future__ import annotations

import pytest

from openfly.market.costs import CostBreakdown, CostModel


def test_round_trip_matches_measured_facts(settings):
    model = CostModel.from_settings(settings)
    cost = model.round_trip(204.0, lots=1, lot_size=65)
    assert cost.orders == 4
    assert cost.brokerage == pytest.approx(80.0)
    assert cost.stt == pytest.approx(13.26, abs=0.01)
    assert cost.exchange == pytest.approx(9.29, abs=0.01)
    assert cost.sebi == pytest.approx(0.0265, abs=0.001)
    assert cost.stamp == pytest.approx(0.398, abs=0.001)
    assert cost.gst == pytest.approx(16.07, abs=0.01)
    assert cost.total == pytest.approx(119.0, abs=0.5)
    assert cost.turnover == pytest.approx(26520.0)
    assert 0.85 < 100 * cost.total / 13260 < 0.95


def test_order_cost_sides():
    model = CostModel()
    sell = model.order_cost("SELL", 133.6, 65)
    buy = model.order_cost("buy", 133.6, 65)
    turnover = 133.6 * 65
    assert sell.stt == pytest.approx(0.001 * turnover) and buy.stt == 0.0
    assert buy.stamp == pytest.approx(0.00003 * turnover) and sell.stamp == 0.0
    assert sell.brokerage == 20.0 and buy.brokerage == 20.0
    assert sell.gst == pytest.approx(0.18 * (20.0 + 0.0003503 * turnover))
    assert (sell + buy).orders == 2
    with pytest.raises(ValueError):
        model.order_cost("HOLD", 1, 1)
    assert model.order_cost("SELL", 0, 65).total == 0.0


def test_brokerage_percentage_cap_on_notional():
    model = CostModel(brokerage_per_order=20.0, brokerage_pct=0.03)
    assert model.brokerage(None) == 20.0
    assert model.brokerage(23400 * 65) == 20.0  # index option notional: flat fee wins
    assert model.brokerage(10000.0) == pytest.approx(3.0)  # small notional: percentage wins
    cost = model.order_cost("BUY", 10.0, 65, notional=10000.0)
    assert cost.brokerage == pytest.approx(3.0)


def test_scaling_with_lots_and_custom_exit():
    model = CostModel()
    one = model.round_trip(204.0, 1)
    three = model.round_trip(204.0, 3)
    assert three.brokerage == one.brokerage  # still four orders
    assert three.stt == pytest.approx(3 * one.stt)
    cheaper_exit = model.round_trip(204.0, 1, exit_debit_points=120.0)
    assert cheaper_exit.total < one.total
    assert model.round_trip_per_lot_points(204.0) == pytest.approx(one.total / 65)


def test_from_settings_overrides_and_dicts():
    model = CostModel.from_settings({"costs": {"brokerage_per_order": 0.0}})
    assert model.brokerage_per_order == 0.0 and model.stt_sell_pct == 0.1
    cost = model.round_trip(204.0, 1)
    assert cost.brokerage == 0.0
    payload = cost.to_dict()
    assert set(payload) == {"brokerage", "stt", "exchange", "sebi", "stamp", "gst", "orders", "turnover", "total"}
    assert isinstance(CostBreakdown().total, float)
