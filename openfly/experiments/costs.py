"""Cost helpers for backtests, targets and rewards.

Thin wrappers over ``openfly.market.costs.CostModel`` so the experiments give
the same rupees as the replay broker and the ledgers. The reference numbers
(a discount broker's options brokerage calculator: buy 100, sell 100,
quantity 400 costs INR 141.83) live in that module and in
``openfly.config.DEFAULT_SETTINGS["costs"]``.
"""

from __future__ import annotations

from openfly.config import DEFAULT_SETTINGS
from openfly.market.costs import CostBreakdown, CostModel

DEFAULT_COSTS: dict = dict(DEFAULT_SETTINGS["costs"])


def costs_from_settings(settings: dict | None) -> dict:
    """The ``costs`` section merged over the defaults (unknown keys ignored)."""
    out = dict(DEFAULT_COSTS)
    if isinstance(settings, dict):
        section = settings.get("costs", settings) if "costs" in settings else settings
        for key, value in (section or {}).items():
            if key in out:
                out[key] = bool(value) if isinstance(out[key], bool) else float(value)
    return out


def cost_model(costs: dict | None = None) -> CostModel:
    return CostModel.from_settings({"costs": costs_from_settings(costs)})


def round_trip_breakdown(
    sold_points: float,
    bought_points: float,
    lot_size: int = 65,
    lots: int = 1,
    costs: dict | None = None,
    orders: int = 4,
) -> CostBreakdown:
    """Sell ``sold_points`` of premium and buy back ``bought_points``.

    ``orders`` executed orders in total: half of them sells sharing the sold
    premium, half buys sharing the bought premium (4 for a two-leg straddle).
    """
    model = cost_model(costs)
    legs = max(1, int(orders) // 2)
    quantity = int(lot_size) * int(lots)
    total = CostBreakdown()
    sold = max(0.0, float(sold_points)) / legs
    bought = max(0.0, float(bought_points)) / legs
    for _ in range(legs):
        total = total + model.order_cost("SELL", sold, quantity)
    for _ in range(legs):
        total = total + model.order_cost("BUY", bought, quantity)
    return total


def round_trip_cost(
    sold_points: float,
    bought_points: float,
    lot_size: int = 65,
    lots: int = 1,
    costs: dict | None = None,
    orders: int = 4,
) -> float:
    """INR cost of selling ``sold_points`` of premium and buying back ``bought_points``."""
    return float(round_trip_breakdown(sold_points, bought_points, lot_size, lots, costs, orders).total)


def cost_fraction(premium_points: float, lot_size: int = 65, lots: int = 1, costs: dict | None = None) -> float:
    """Round-trip cost as a fraction of the premium sold (about 0.0095 at 204 points)."""
    if premium_points <= 0:
        return 0.0
    return cost_model(costs).cost_fraction(float(premium_points), int(lots), int(lot_size))


__all__ = [
    "DEFAULT_COSTS",
    "cost_fraction",
    "cost_model",
    "costs_from_settings",
    "round_trip_breakdown",
    "round_trip_cost",
]
