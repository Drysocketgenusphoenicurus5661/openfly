"""Transaction cost model for NSE index options (one formula for backtests,
the paper ledger and the expected-cost check).

Per executed order on premium turnover (premium x quantity):

- brokerage: the flat fee, capped at ``brokerage_pct`` of the contract
  notional (strike x quantity) when a notional is supplied. For index options
  the notional cap never binds, so the flat fee applies; premium turnover is
  not the base because no broker applies the percentage rule to option premium.
- STT: ``stt_sell_pct`` of premium on SELL orders
- exchange transaction charge: ``exchange_pct`` of premium turnover
- SEBI turnover fee: ``sebi_pct`` of premium turnover
- stamp duty: ``stamp_buy_pct`` of premium on BUY orders
- GST: ``gst_pct`` of brokerage plus exchange charges

All percentages are in percent (0.1 means 0.1 percent).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from openfly.config import DEFAULT_SETTINGS, SettingsStore


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

    def to_dict(self) -> dict[str, float]:
        data = {k: round(float(v), 4) for k, v in asdict(self).items()}
        data["total"] = round(self.total, 4)
        return data


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0
    brokerage_pct: float = 0.03
    stt_sell_pct: float = 0.1
    exchange_pct: float = 0.03503
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0

    @classmethod
    def from_settings(cls, settings: SettingsStore | dict[str, Any] | None = None) -> CostModel:
        if settings is None:
            merged = DEFAULT_SETTINGS
        elif isinstance(settings, dict):
            merged = settings
        else:
            merged = settings.get()
        costs = {**DEFAULT_SETTINGS["costs"], **merged.get("costs", {})}
        return cls(
            brokerage_per_order=float(costs["brokerage_per_order"]),
            brokerage_pct=float(costs["brokerage_pct"]),
            stt_sell_pct=float(costs["stt_sell_pct"]),
            exchange_pct=float(costs["exchange_pct"]),
            sebi_pct=float(costs["sebi_pct"]),
            stamp_buy_pct=float(costs["stamp_buy_pct"]),
            gst_pct=float(costs["gst_pct"]),
        )

    def brokerage(self, notional: float | None = None) -> float:
        flat = self.brokerage_per_order
        if notional is None or notional <= 0:
            return flat
        return min(flat, self.brokerage_pct / 100.0 * notional)

    def order_cost(
        self, side: str, premium: float, quantity: int, notional: float | None = None
    ) -> CostBreakdown:
        """Charges for one executed order of ``quantity`` units at ``premium`` per unit."""
        side_u = str(side).upper()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        turnover = float(premium) * float(quantity)
        if turnover <= 0:
            return CostBreakdown(orders=1)
        brokerage = self.brokerage(notional)
        stt = self.stt_sell_pct / 100.0 * turnover if side_u == "SELL" else 0.0
        exchange = self.exchange_pct / 100.0 * turnover
        sebi = self.sebi_pct / 100.0 * turnover
        stamp = self.stamp_buy_pct / 100.0 * turnover if side_u == "BUY" else 0.0
        gst = self.gst_pct / 100.0 * (brokerage + exchange)
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
    ) -> CostBreakdown:
        """Short straddle round trip: entry credit split evenly across the two legs.

        ``exit_debit_points`` defaults to the entry credit (exit at the same
        combined premium), which is the convention used in the cost facts.
        """
        exit_points = entry_credit_points if exit_debit_points is None else exit_debit_points
        half_in = float(entry_credit_points) / 2.0
        half_out = float(exit_points) / 2.0
        return self.straddle_round_trip(half_in, half_in, half_out, half_out, lots, lot_size)

    def round_trip_per_lot_points(self, entry_credit_points: float, lot_size: int = 65) -> float:
        """Round-trip cost expressed in index points per lot (for P&L in points)."""
        return self.round_trip(entry_credit_points, 1, lot_size).total / float(lot_size)

    def to_dict(self) -> dict[str, float]:
        return asdict(self)
