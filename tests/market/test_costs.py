"""The one cost formula: the brokerage calculator numbers and the three call sites agreeing."""

from __future__ import annotations

import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.execution.costs import SimpleCostModel, load_cost_model, order_cost_inr, round_trip_inr
from openfly.experiments.costs import cost_fraction, costs_from_settings, round_trip_cost
from openfly.market.costs import CostBreakdown, CostModel

# A discount broker's NFO options brokerage calculator, verified 2026-09-12:
# buy 100, sell 100, quantity 400.
CALCULATOR = {
    "brokerage": 40.0,
    "stt": 60.0,
    "exchange": 28.42,
    "gst": 12.33,
    "sebi": 0.08,
    "stamp": 1.0,
    "total": 141.83,
    "breakeven_points": 0.35,
}


def test_calculator_reference_numbers(settings):
    model = CostModel.from_settings(settings)
    cost = model.calculator_round_trip(buy_price=100.0, sell_price=100.0, quantity=400)
    assert cost.orders == 2
    assert cost.turnover == pytest.approx(80_000.0)
    assert cost.brokerage == pytest.approx(CALCULATOR["brokerage"])
    assert cost.stt == pytest.approx(CALCULATOR["stt"])
    assert cost.exchange == pytest.approx(CALCULATOR["exchange"], abs=0.005)
    assert cost.gst == pytest.approx(CALCULATOR["gst"], abs=0.005)
    assert cost.sebi == pytest.approx(CALCULATOR["sebi"])
    assert cost.stamp == pytest.approx(CALCULATOR["stamp"])
    assert round(cost.total, 2) == CALCULATOR["total"]
    assert round(cost.breakeven_points(400), 2) == CALCULATOR["breakeven_points"]


def test_round_trip_single_leg_matches_the_calculator():
    model = CostModel()
    cost = model.round_trip(100.0, lots=1, lot_size=400, legs=1)
    assert cost.orders == 2
    assert round(cost.total, 2) == 141.83
    assert round(cost.breakeven_points(400), 2) == 0.35


def test_round_trip_of_the_204_point_straddle(settings):
    """One lot (65) of a 204 point straddle sold and bought back at the same premium."""
    model = CostModel.from_settings(settings)
    cost = model.round_trip(204.0, lots=1, lot_size=65)
    assert cost.orders == 4
    assert cost.turnover == pytest.approx(26_520.0)
    assert cost.brokerage == pytest.approx(80.0)
    assert cost.stt == pytest.approx(0.0015 * 13_260.0)
    assert cost.exchange == pytest.approx(0.0003553 * 26_520.0)
    assert cost.sebi == pytest.approx(0.000001 * 26_520.0)
    assert cost.stamp == 0.0  # 0.003 percent of 6,630 per buy order is 0.20, rounded to the rupee
    assert cost.gst == pytest.approx(0.18 * (80.0 + cost.exchange + cost.sebi))
    assert cost.total == pytest.approx(125.44, abs=0.01)
    assert 0.9 < 100 * cost.total / 13_260 < 1.0


def test_order_cost_sides_and_rounding():
    model = CostModel()
    sell = model.order_cost("SELL", 133.6, 65)
    buy = model.order_cost("buy", 133.6, 65)
    turnover = 133.6 * 65
    assert sell.stt == pytest.approx(0.0015 * turnover) and buy.stt == 0.0
    assert buy.stamp == 0.0 and sell.stamp == 0.0  # 0.26 rounds to zero rupees
    assert sell.brokerage == 20.0 and buy.brokerage == 20.0
    assert sell.gst == pytest.approx(0.18 * (20.0 + 0.0003553 * turnover + 0.000001 * turnover))
    assert (sell + buy).orders == 2
    unrounded = CostModel(stamp_round_to_rupee=False).order_cost("BUY", 133.6, 65)
    assert unrounded.stamp == pytest.approx(0.00003 * turnover)
    no_sebi_gst = CostModel(gst_on_sebi=False).order_cost("SELL", 133.6, 65)
    assert no_sebi_gst.gst == pytest.approx(0.18 * (20.0 + 0.0003553 * turnover))
    with pytest.raises(ValueError):
        model.order_cost("HOLD", 1, 1)
    assert model.order_cost("SELL", 0, 65).total == 0.0
    assert float(sell) == pytest.approx(sell.total)


def test_brokerage_percentage_rule_only_when_enabled():
    flat_only = CostModel(brokerage_per_order=20.0, brokerage_pct=0.0)
    assert flat_only.brokerage(None) == 20.0
    assert flat_only.brokerage(1_000.0) == 20.0
    capped = CostModel(brokerage_per_order=20.0, brokerage_pct=0.03)
    assert capped.brokerage(23400 * 65) == 20.0  # large turnover: flat fee wins
    assert capped.brokerage(10_000.0) == pytest.approx(3.0)  # small turnover: percentage wins
    assert capped.order_cost("BUY", 10.0, 65, notional=10_000.0).brokerage == pytest.approx(3.0)


def test_scaling_with_lots_and_custom_exit():
    model = CostModel()
    one = model.round_trip(204.0, 1)
    three = model.round_trip(204.0, 3)
    assert three.brokerage == one.brokerage  # still four orders
    assert three.stt == pytest.approx(3 * one.stt)
    cheaper_exit = model.round_trip(204.0, 1, exit_debit_points=120.0)
    assert cheaper_exit.total < one.total
    assert model.round_trip_per_lot_points(204.0) == pytest.approx(one.total / 65)
    assert model.cost_fraction(204.0) == pytest.approx(one.total / (204.0 * 65))


def test_from_settings_overrides_and_dicts():
    model = CostModel.from_settings({"costs": {"brokerage_per_order": 0.0}})
    assert model.brokerage_per_order == 0.0 and model.stt_sell_pct == 0.15
    assert CostModel.from_settings({"brokerage_per_order": 5.0}).brokerage_per_order == 5.0
    assert CostModel.from_settings(None) == CostModel.from_settings(DEFAULT_SETTINGS)
    cost = model.round_trip(204.0, 1)
    assert cost.brokerage == 0.0
    payload = cost.to_dict()
    assert set(payload) == {"brokerage", "stt", "exchange", "sebi", "stamp", "gst", "orders", "turnover", "total"}
    assert isinstance(CostBreakdown().total, float)


def test_the_three_packages_give_identical_numbers(settings):
    market = CostModel.from_settings(settings).round_trip(204.0, 1, 65).total
    experiments = round_trip_cost(204.0, 204.0, lot_size=65, lots=1, costs=costs_from_settings(settings))
    execution = round_trip_inr(load_cost_model(settings), 204.0, 1, 65)
    assert experiments == pytest.approx(market)
    assert execution == pytest.approx(round(market, 2))
    assert SimpleCostModel is CostModel
    assert order_cost_inr(load_cost_model(settings), "SELL", 102.0, 65) == pytest.approx(
        CostModel.from_settings(settings).order_cost("SELL", 102.0, 65).total, abs=1e-4
    )
    assert cost_fraction(204.0) == pytest.approx(market / (204.0 * 65))
    assert costs_from_settings(None) == DEFAULT_SETTINGS["costs"]
    assert costs_from_settings({"costs": {"gst_on_sebi": 0, "unknown": 1}})["gst_on_sebi"] is False
