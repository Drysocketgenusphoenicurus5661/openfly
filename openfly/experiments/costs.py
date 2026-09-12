"""One cost formula for backtests, targets and rewards.

Round trip of a short straddle: brokerage per order x 4, STT on the sold
premium, exchange charge on premium turnover both ways, SEBI fee and stamp
duty (both negligible), GST on brokerage plus exchange charges.
"""

from __future__ import annotations

DEFAULT_COSTS = {
    "brokerage_per_order": 20.0,
    "brokerage_pct": 0.03,
    "stt_sell_pct": 0.1,
    "exchange_pct": 0.03503,
    "sebi_pct": 0.0001,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,
}


def costs_from_settings(settings: dict | None) -> dict:
    out = dict(DEFAULT_COSTS)
    if isinstance(settings, dict):
        out.update({k: float(v) for k, v in settings.get("costs", {}).items() if k in out})
    return out


def round_trip_cost(
    sold_points: float,
    bought_points: float,
    lot_size: int = 65,
    lots: int = 1,
    costs: dict | None = None,
    orders: int = 4,
) -> float:
    """INR cost of selling `sold_points` of premium and buying back `bought_points`."""
    c = dict(DEFAULT_COSTS)
    if costs:
        c.update(costs)
    qty = float(lot_size) * float(lots)
    sold = max(0.0, float(sold_points)) * qty
    bought = max(0.0, float(bought_points)) * qty
    brokerage = c["brokerage_per_order"] * orders
    stt = c["stt_sell_pct"] / 100.0 * sold
    exchange = c["exchange_pct"] / 100.0 * (sold + bought)
    sebi = c["sebi_pct"] / 100.0 * (sold + bought)
    stamp = c["stamp_buy_pct"] / 100.0 * bought
    gst = c["gst_pct"] / 100.0 * (brokerage + exchange)
    return float(brokerage + stt + exchange + sebi + stamp + gst)


def cost_fraction(premium_points: float, lot_size: int = 65, lots: int = 1, costs: dict | None = None) -> float:
    """Round-trip cost as a fraction of the premium sold (about 0.01 at 200 points)."""
    if premium_points <= 0:
        return 0.0
    total = round_trip_cost(premium_points, premium_points, lot_size, lots, costs)
    return float(total / (premium_points * lot_size * lots))
