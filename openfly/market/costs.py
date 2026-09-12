"""Transaction cost model for NSE index options: the one formula for backtests,
the replay broker, the paper and live ledgers and the expected-cost check.

Reference: a discount broker's NFO options brokerage calculator, verified on
2026-09-12. Buy 100, sell 100, quantity 400 (turnover 80,000) gives brokerage
40 (flat INR 20 per executed order), STT 60 (0.15 percent of the sell-side
premium value), exchange transaction charge 28.42 (0.03553 percent of turnover,
NSE options including IPFT), SEBI 0.08 (0.0001 percent of turnover), stamp
duty 1 (0.003 percent of the buy value, rounded to the rupee), GST 12.33 (18
percent of brokerage plus exchange plus SEBI), total 141.83, 0.35 points to
breakeven. ``CostModel().calculator_round_trip(100, 100, 400)`` reproduces it.

Per executed order on premium turnover (premium x quantity):

- brokerage: ``brokerage_per_order`` flat; when ``brokerage_pct`` is above 0
  the order pays the smaller of the flat fee and that percentage of turnover
  (0 means flat only, which is the rule for index options)
- STT: ``stt_sell_pct`` of premium on SELL orders
- exchange transaction charge: ``exchange_pct`` of turnover
- SEBI turnover fee: ``sebi_pct`` of turnover
- stamp duty: ``stamp_buy_pct`` of premium on BUY orders, rounded to the
  rupee per order when ``stamp_round_to_rupee`` is set
- GST: ``gst_pct`` of brokerage plus exchange charge (plus the SEBI fee when
  ``gst_on_sebi`` is set)

All percentages are in percent (0.15 means 0.15 percent).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from openfly.config import DEFAULT_SETTINGS, SettingsStore

COST_KEYS = tuple(DEFAULT_SETTINGS["costs"].keys())


def _round_half_up(value: float) -> float:
    return float(math.floor(value + 0.5))


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0
    orders: int = 0
    turnover: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def __float__(self) -> float:
        return float(self.total)

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            sebi=self.sebi + other.sebi,
            stamp=self.stamp + other.stamp,
            gst=self.gst + other.gst,
            orders=self.orders + other.orders,
            turnover=self.turnover + other.turnover,
        )

    def breakeven_points(self, quantity: int) -> float:
        """Premium points per unit that the costs eat (the calculator's points to breakeven)."""
        if quantity <= 0:
            return 0.0
        return self.total / float(quantity)

    def to_dict(self) -> dict[str, float]:
        data = {k: round(float(v), 4) for k, v in asdict(self).items()}
        data["orders"] = int(self.orders)
        data["total"] = round(self.total, 4)
        return data


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.0
    stt_sell_pct: float = 0.15
    exchange_pct: float = 0.03553
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    stamp_round_to_rupee: bool = True
    gst_pct: float = 18.0
    gst_on_sebi: bool = True

    @classmethod
    def from_settings(cls, settings: SettingsStore | dict[str, Any] | None = None) -> CostModel:
        """Build from the merged settings dict, a SettingsStore, or a bare ``costs`` dict."""
        if settings is None:
            merged: dict[str, Any] = DEFAULT_SETTINGS
        elif isinstance(settings, dict):
            merged = settings if "costs" in settings else {"costs": settings}
        else:
            merged = settings.get()
        costs = {**DEFAULT_SETTINGS["costs"], **(merged.get("costs") or {})}
        return cls(
            brokerage_per_order=float(costs["brokerage_per_order"]),
            brokerage_pct=float(costs["brokerage_pct"]),
            stt_sell_pct=float(costs["stt_sell_pct"]),
            exchange_pct=float(costs["exchange_pct"]),
            sebi_pct=float(costs["sebi_pct"]),
            stamp_buy_pct=float(costs["stamp_buy_pct"]),
            stamp_round_to_rupee=bool(costs["stamp_round_to_rupee"]),
            gst_pct=float(costs["gst_pct"]),
            gst_on_sebi=bool(costs["gst_on_sebi"]),
        )

    def brokerage(self, turnover: float | None = None) -> float:
        """Brokerage for one executed order (flat, or the smaller of flat and the percentage)."""
        flat = self.brokerage_per_order
        if turnover is None or turnover <= 0 or self.brokerage_pct <= 0:
            return flat
        return min(flat, self.brokerage_pct / 100.0 * turnover)

    def order_cost(
        self, side: str, premium: float, quantity: int, notional: float | None = None
    ) -> CostBreakdown:
        """Charges for one executed order of ``quantity`` units at ``premium`` per unit.

        ``notional``, when given, is the base for the brokerage percentage rule
        instead of the premium turnover (kept for callers that pass the contract
        notional; with ``brokerage_pct`` 0 it has no effect).
        """
        side_name = getattr(side, "value", side)
        side_u = str(side_name).upper()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        turnover = float(premium) * float(quantity)
        if turnover <= 0:
            return CostBreakdown(orders=1)
        brokerage = self.brokerage(turnover if notional is None else notional)
        stt = self.stt_sell_pct / 100.0 * turnover if side_u == "SELL" else 0.0
        exchange = self.exchange_pct / 100.0 * turnover
        sebi = self.sebi_pct / 100.0 * turnover
        stamp = self.stamp_buy_pct / 100.0 * turnover if side_u == "BUY" else 0.0
        if stamp and self.stamp_round_to_rupee:
            stamp = _round_half_up(stamp)
        gst_base = brokerage + exchange + (sebi if self.gst_on_sebi else 0.0)
        gst = self.gst_pct / 100.0 * gst_base
        return CostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange=exchange,
            sebi=sebi,
            stamp=stamp,
            gst=gst,
            orders=1,
            turnover=turnover,
        )

    def calculator_round_trip(self, buy_price: float, sell_price: float, quantity: int) -> CostBreakdown:
        """One buy order and one sell order of ``quantity`` units (the brokerage calculator's case)."""
        return self.order_cost("BUY", buy_price, quantity) + self.order_cost("SELL", sell_price, quantity)

    def straddle_round_trip(
        self,
        call_entry: float,
        put_entry: float,
        call_exit: float,
        put_exit: float,
        lots: int,
        lot_size: int = 65,
    ) -> CostBreakdown:
        """Four orders: sell both legs at entry, buy both legs at exit."""
        quantity = int(lots) * int(lot_size)
        total = CostBreakdown()
        for premium in (call_entry, put_entry):
            total = total + self.order_cost("SELL", premium, quantity)
        for premium in (call_exit, put_exit):
            total = total + self.order_cost("BUY", premium, quantity)
        return total

    def round_trip(
        self,
        entry_credit_points: float,
        lots: int,
        lot_size: int = 65,
        exit_debit_points: float | None = None,
        legs: int = 2,
    ) -> CostBreakdown:
        """Short straddle round trip: the credit is sold and bought back.

        With ``legs`` 2 (default) the credit is split evenly across the two legs,
        giving four executed orders. ``legs`` 1 is a single contract sold and
        bought back (two orders), which is how the brokerage calculator counts:
        ``round_trip(100, lots=1, lot_size=400, legs=1)`` gives INR 141.83.
        ``exit_debit_points`` defaults to the entry credit (exit at the same
        combined premium), the convention used in the cost facts.
        """
        exit_points = entry_credit_points if exit_debit_points is None else exit_debit_points
        n = max(1, int(legs))
        quantity = int(lots) * int(lot_size)
        per_leg_in = float(entry_credit_points) / n
        per_leg_out = float(exit_points) / n
        total = CostBreakdown()
        for _ in range(n):
            total = total + self.order_cost("SELL", per_leg_in, quantity)
        for _ in range(n):
            total = total + self.order_cost("BUY", per_leg_out, quantity)
        return total

    def round_trip_per_lot_points(self, entry_credit_points: float, lot_size: int = 65) -> float:
        """Round-trip cost expressed in index points per lot (for P&L in points)."""
        return self.round_trip(entry_credit_points, 1, lot_size).total / float(lot_size)

    def cost_fraction(self, premium_points: float, lots: int = 1, lot_size: int = 65) -> float:
        """Round-trip cost as a fraction of the premium sold (about 0.0095 at 204 points)."""
        if premium_points <= 0:
            return 0.0
        total = self.round_trip(premium_points, lots, lot_size).total
        return float(total / (float(premium_points) * int(lot_size) * int(lots)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["COST_KEYS", "CostBreakdown", "CostModel"]
