"""Cost helpers: a fallback cost model and adapters over any cost model.

The signature follows openfly.market.costs.CostModel: `order_cost(side, premium,
quantity)` for one executed order and `round_trip(entry_credit_points, lots,
lot_size)` for a straddle sold and bought back. Both may return a float or an
object with a `total` attribute (the market package's CostBreakdown); the
`order_cost_inr` and `round_trip_inr` adapters normalise that to INR floats.

The fallback formula is the one in docs/openalgo-notes.md: flat brokerage per
executed options order, STT on sold premium, exchange charge on turnover, SEBI
fee, stamp duty on bought premium and GST on brokerage plus exchange charges.
One lot of a 204 point straddle round trip costs about INR 119.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openfly.interfaces import Side


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


@dataclass(frozen=True)
class SimpleCostModel:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.1
    exchange_pct: float = 0.03503
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0

    @classmethod
    def from_settings(cls, settings: dict) -> SimpleCostModel:
        costs = settings.get("costs", {})
        return cls(
            brokerage_per_order=float(costs.get("brokerage_per_order", 20.0)),
            stt_sell_pct=float(costs.get("stt_sell_pct", 0.1)),
            exchange_pct=float(costs.get("exchange_pct", 0.03503)),
            sebi_pct=float(costs.get("sebi_pct", 0.0001)),
            stamp_buy_pct=float(costs.get("stamp_buy_pct", 0.003)),
            gst_pct=float(costs.get("gst_pct", 18.0)),
        )

    def order_cost(self, side: Side | str, premium: float, quantity: int, notional: float | None = None) -> float:
        if quantity <= 0:
            return 0.0
        turnover = abs(int(quantity)) * float(premium)
        side_name = side.value if isinstance(side, Side) else str(side).upper()
        brokerage = self.brokerage_per_order
        exchange = turnover * self.exchange_pct / 100.0
        sebi = turnover * self.sebi_pct / 100.0
        stt = turnover * self.stt_sell_pct / 100.0 if side_name == "SELL" else 0.0
        stamp = turnover * self.stamp_buy_pct / 100.0 if side_name == "BUY" else 0.0
        gst = (brokerage + exchange) * self.gst_pct / 100.0
        return round(brokerage + exchange + sebi + stt + stamp + gst, 4)

    def round_trip(
        self, entry_credit_points: float, lots: int, lot_size: int = 65, exit_debit_points: float | None = None
    ) -> float:
        """Sell both legs and buy them back, the credit split evenly across the legs."""
        qty = max(1, int(lots)) * int(lot_size)
        half_in = float(entry_credit_points) / 2.0
        half_out = half_in if exit_debit_points is None else float(exit_debit_points) / 2.0
        sells = 2 * self.order_cost(Side.SELL, half_in, qty)
        buys = 2 * self.order_cost(Side.BUY, half_out, qty)
        return round(sells + buys, 2)


def load_cost_model(settings: dict) -> Any:
    """The market package's CostModel when importable, else SimpleCostModel."""
    try:
        from openfly.market.costs import CostModel  # lazy: another agent owns openfly.market
    except ImportError:
        return SimpleCostModel.from_settings(settings)
    try:
        return CostModel.from_settings(settings)
    except Exception:
        return SimpleCostModel.from_settings(settings)
