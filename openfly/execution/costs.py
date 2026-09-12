"""Cost adapters for the execution layer.

The model is ``openfly.market.costs.CostModel`` (one formula everywhere; the
reference numbers are in that module). ``order_cost_inr`` and
``round_trip_inr`` normalise any model with the same signature to INR floats,
whether it returns a ``CostBreakdown`` or a plain number, so brokers, the
ledger and the engine never depend on the return type.

``SimpleCostModel`` is kept as a name for older callers; it is the same class.
"""

from __future__ import annotations

from typing import Any

from openfly.interfaces import Side
from openfly.market.costs import CostBreakdown, CostModel

SimpleCostModel = CostModel


def _total(value: Any) -> float:
    total = getattr(value, "total", None)
    if total is None:
        return float(value)
    return float(total() if callable(total) else total)


def order_cost_inr(model: Any, side: Side | str, premium: float, quantity: int) -> float:
    """INR charges for one executed order of `quantity` units at `premium` per unit."""
    if model is None or quantity <= 0:
        return 0.0
    side_name = side.value if isinstance(side, Side) else str(side).upper()
    return round(_total(model.order_cost(side_name, float(premium), int(quantity))), 4)


def round_trip_inr(model: Any, entry_credit_points: float, lots: int, lot_size: int = 65) -> float:
    if model is None or lots <= 0:
        return 0.0
    try:
        return round(_total(model.round_trip(float(entry_credit_points), int(lots), int(lot_size))), 2)
    except TypeError:
        return round(_total(model.round_trip(float(entry_credit_points), int(lots))), 2)


def load_cost_model(settings: dict | None) -> CostModel:
    """The shared CostModel built from the merged settings."""
    return CostModel.from_settings(settings)


__all__ = [
    "CostBreakdown",
    "CostModel",
    "SimpleCostModel",
    "load_cost_model",
    "order_cost_inr",
    "round_trip_inr",
]
