"""In-memory fake exchange with the openfly.market.client response shapes.

Scenarios per symbol: "fill" (complete at once), "partial" (fills part and
stays open), "open" (never fills), "reject" (basket leg error). Stop orders
(SL-M) rest as "trigger pending" until the test triggers or cancels them.
`lose_response` makes basketorder place the orders and then raise, which is
how a lost HTTP response looks to the broker.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from openfly.config import DEFAULT_SETTINGS, deep_merge
from openfly.interfaces import Quote, StraddleQuote

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 9, 11)
EXPIRY = date(2026, 9, 15)
CE = "NIFTY15SEP2623350CE"
PE = "NIFTY15SEP2623350PE"


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, dtime(hh, mm, ss), IST)


class FakeClock:
    def __init__(self, start: float | None = None):
        self.t = start if start is not None else at(10, 20).timestamp()

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, IST)


def settings_with(**overrides) -> dict:
    """DEFAULT_SETTINGS with fixed stops (the adaptive tests opt in) plus section__key overrides."""
    update: dict = {"strategy": {"stop_mode": "fixed", "expiry_selection": "weekly"}}
    for key, value in overrides.items():
        section, _, name = key.partition("__")
        update.setdefault(section, {})[name] = value
    return deep_merge(DEFAULT_SETTINGS, update)


def straddle_quote(call: float = 101.2, put: float = 100.1, when: datetime | None = None, spread: float = 0.1, strike: float = 23350.0) -> StraddleQuote:
    ts = (when or at(10, 20)).timestamp()
    ce = CE if strike == 23350.0 else f"NIFTY15SEP26{int(strike)}CE"
    pe = PE if strike == 23350.0 else f"NIFTY15SEP26{int(strike)}PE"
    return StraddleQuote(
        call=Quote(ce, "NFO", call, round(call - spread / 2, 2), round(call + spread / 2, 2), ts),
        put=Quote(pe, "NFO", put, round(put - spread / 2, 2), round(put + spread / 2, 2), ts),
        strike=strike,
        expiry=EXPIRY,
    )


class FakeExchange:
    def __init__(self, *, analyzer: bool = True, cash: float = 10_000_000.0, symbols=(CE, PE), clock=None):
        self.analyzer = analyzer
        self.cash = cash
        self.known_symbols = set(symbols)
        self.orders: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.seq = 0
        self.scenarios: dict[str, str] = {}
        self.partial_qty: dict[str, int] = {}
        self.prices: dict[str, float] = {}
        self.lose_response = False
        self.positions: dict[str, int] = {}
        self.foreign_orders: list[dict[str, Any]] = []
        self.stop_behaviour: dict[str, str] = {}
        self.clock = clock or time.time

    # ----------------------------------------------------------- account

    def analyzer_status(self) -> dict:
        return {"analyze_mode": self.analyzer, "mode": "analyze" if self.analyzer else "live", "total_logs": 0}

    def funds(self) -> dict:
        return {"availablecash": self.cash, "collateral": 0.0, "m2mrealized": 0.0, "m2munrealized": 0.0, "utiliseddebits": 0.0}

    def symbol(self, symbol: str, exchange: str) -> dict:
        if symbol not in self.known_symbols:
            raise KeyError(f"{symbol} not in symbol master")
        return {"symbol": symbol, "exchange": exchange, "lotsize": 65, "tick_size": 0.05}

    def margin(self, positions: list[dict]) -> dict:
        lots = sum(int(p["quantity"]) for p in positions) // 65 // max(1, len(positions))
        return {"total_margin_required": 188700.31 * max(1, lots), "span_margin": 0.0, "exposure_margin": 0.0}

    # ------------------------------------------------------------ orders

    def _timestamp(self) -> str:
        return datetime.fromtimestamp(self.clock(), IST).strftime("%d-%b-%Y %H:%M:%S")

    def _apply_fill(self, order: dict[str, Any], filled: int, price: float) -> None:
        order["filled_quantity"] = str(filled)
        order["average_price"] = str(price)
        signed = filled if order["action"] == "BUY" else -filled
        self.positions[order["symbol"]] = self.positions.get(order["symbol"], 0) + signed

    def _place(self, o: dict[str, Any], strategy: str | None) -> dict[str, Any]:
        symbol = o["symbol"]
        pricetype = str(o.get("pricetype", "MARKET")).upper()
        scenario = self.scenarios.get(symbol, "fill")
        if scenario == "reject" and pricetype not in ("SL", "SL-M"):
            return {"symbol": symbol, "status": "error", "message": "margin shortfall"}
        self.seq += 1
        oid = f"F{self.seq:06d}"
        qty = int(o["quantity"])
        limit = float(o.get("price") or 0.0)
        price = self.prices.get(symbol, limit if limit > 0 else 100.0)
        order = {
            "orderid": oid,
            "symbol": symbol,
            "exchange": o.get("exchange", "NFO"),
            "action": str(o["action"]).upper(),
            "quantity": str(qty),
            "product": o.get("product", "NRML"),
            "pricetype": pricetype,
            "price": str(limit),
            "trigger_price": str(o.get("trigger_price", 0) or 0),
            "strategy": strategy or "",
            "order_status": "open",
            "filled_quantity": "0",
            "average_price": "0",
            "timestamp": self._timestamp(),
        }
        if pricetype in ("SL", "SL-M"):
            behaviour = self.stop_behaviour.get(symbol, "rest")
            order["order_status"] = "rejected" if behaviour == "reject" else "trigger pending"
        elif scenario == "fill":
            order["order_status"] = "complete"
            self._apply_fill(order, qty, price)
        elif scenario == "partial":
            self._apply_fill(order, self.partial_qty.get(symbol, qty // 2), price)
        self.orders[oid] = order
        return {"symbol": symbol, "status": "success", "orderid": oid}

    def basketorder(self, orders: list[dict], strategy: str | None = None) -> list[dict]:
        self.calls.append(("basketorder", [dict(o) for o in orders]))
        results = [self._place(o, strategy) for o in orders]
        if self.lose_response:
            raise ConnectionError("socket closed before the response arrived")
        return results

    def placeorder(self, symbol, exchange, action, quantity, product="NRML", pricetype="MARKET", price=0, trigger_price=0, disclosed_quantity=0, strategy=None) -> dict:
        order = {"symbol": symbol, "exchange": exchange, "action": action, "quantity": quantity, "product": product, "pricetype": pricetype, "price": price, "trigger_price": trigger_price}
        self.calls.append(("placeorder", dict(order)))
        result = self._place(order, strategy)
        if result["status"] != "success":
            raise RuntimeError(result["message"])
        return {"orderid": result["orderid"], "mode": "analyze" if self.analyzer else "live"}

    def orderstatus(self, orderid: str, strategy: str | None = None) -> dict:
        self.calls.append(("orderstatus", orderid))
        return dict(self.orders[orderid])

    def orderbook(self) -> dict:
        self.calls.append(("orderbook", None))
        return {"orders": [dict(o) for o in self.orders.values()] + [dict(o) for o in self.foreign_orders], "statistics": {}}

    def positionbook(self) -> list[dict]:
        return [
            {"symbol": s, "exchange": "NFO", "product": "NRML", "quantity": str(q), "average_price": "100.0", "ltp": "100.0", "pnl": "0"}
            for s, q in self.positions.items()
        ]

    def cancelorder(self, orderid: str, strategy: str | None = None) -> dict:
        self.calls.append(("cancelorder", orderid))
        order = self.orders[orderid]
        if order["order_status"] in ("open", "trigger pending"):
            order["order_status"] = "cancelled"
        return {"orderid": orderid}

    # ------------------------------------------------------ test controls

    def stop_orders(self) -> list[dict]:
        return [o for o in self.orders.values() if o["pricetype"] in ("SL", "SL-M")]

    def trigger_stop(self, orderid: str, price: float) -> None:
        order = self.orders[orderid]
        order["order_status"] = "complete"
        self._apply_fill(order, int(order["quantity"]), price)

    def cancel_externally(self, orderid: str) -> None:
        self.orders[orderid]["order_status"] = "cancelled"

    def complete_open(self, orderid: str, price: float | None = None) -> None:
        order = self.orders[orderid]
        remaining = int(order["quantity"]) - int(order["filled_quantity"])
        order["order_status"] = "complete"
        self._apply_fill(order, int(order["quantity"]), price or float(order["price"]) or 100.0)
        self.positions[order["symbol"]] -= (remaining if order["action"] == "BUY" else -remaining) * 0

    def calls_of(self, kind: str) -> list[Any]:
        return [payload for name, payload in self.calls if name == kind]
