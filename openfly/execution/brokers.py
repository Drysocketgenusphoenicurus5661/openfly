"""Brokers: deterministic replay fills and the OpenAlgo broker with reconciliation.

Both implement BrokerProtocol (openfly.execution.types). The OpenAlgo broker
talks to a ClientProtocol whose shapes follow openfly.market.client:
basketorder returns the per-leg results list, orderstatus the order dict,
orderbook {"orders": [...]} and positionbook a list of dicts with string
numbers. Tests use an in-memory fake exchange with the same shapes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openfly.execution.costs import order_cost_inr
from openfly.execution.ledger import (
    IST,
    STOP_CANCELLED,
    STOP_PENDING,
    STOP_REJECTED,
    STOP_TRIGGERED,
    Ledger,
    intent_from_payload,
)
from openfly.execution.types import (
    EXIT_KINDS,
    KIND_REPAIR,
    KIND_STOPS,
    TICK,
    BrokerError,
    LostResponse,
    OneLegged,
    QuoteLookup,
    StopLeg,
    ceil_to_tick,
    classify_fills,
    execution_settings,
    floor_to_tick,
    is_balanced,
    round_to_tick,
)
from openfly.interfaces import (
    Fill,
    Intent,
    IntentStatus,
    Quote,
    Side,
    StraddleQuote,
    UnresolvedOrder,
)

logger = logging.getLogger("openfly.execution")

FINAL_ORDER_STATUSES = ("complete", "rejected", "cancelled")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Replay broker
# ---------------------------------------------------------------------------


class ReplayBroker:
    """Immediate deterministic fills at the quote, with slippage, costs and simulated stop orders.

    SELL fills at bid minus slippage, BUY at ask plus slippage; when bid or ask is
    missing the LTP is used. Resting per-leg stop orders (kind STOPS) are held in
    memory and triggered by `on_quote` on the minute path: when a leg's LTP reaches
    its trigger the leg is bought back at max(ask, trigger) plus slippage.
    """

    def __init__(self, cost_model: Any = None, slippage_ticks: int = 1, tick: float = TICK):
        self.cost_model = cost_model
        self.tick = tick
        self.slippage = slippage_ticks * tick
        self.fills: list[Fill] = []
        self.costs = 0.0
        self.events: list[dict] = []
        self.cancelled_stops: list[str] = []
        self._positions: dict[str, int] = {}
        self._stops: list[dict[str, Any]] = []
        self._seq = 0

    # ----------------------------------------------------------- protocol

    def preflight(self, symbols: Sequence[str] = (), lots: int = 1) -> dict:
        return {"ok": True, "broker": "replay", "mode": "replay", "checks": []}

    def execute(self, intent: Intent, quote_lookup: QuoteLookup) -> list[Fill]:
        if intent.kind == KIND_STOPS:
            return self._place_stops(intent)
        if intent.kind in EXIT_KINDS or intent.kind == KIND_REPAIR:
            self._cancel_stops([leg.contract.symbol for leg in intent.legs])
        fills: list[Fill] = []
        for leg in intent.legs:
            quote = quote_lookup(leg.contract.symbol)
            if quote is None:
                raise BrokerError(f"replay broker has no quote for {leg.contract.symbol}")
            price = self._price(leg.side, quote)
            fill = Fill(
                intent_id=intent.intent_id,
                symbol=leg.contract.symbol,
                side=leg.side,
                quantity=leg.quantity,
                average_price=price,
                order_id=self._next_id(),
                timestamp=quote.timestamp,
            )
            self._book(fill)
            fills.append(fill)
        return fills

    def reconcile(self) -> list[dict]:
        return []

    def positions(self) -> dict[str, int]:
        return {s: q for s, q in self._positions.items() if q != 0}

    def stop_orders(self) -> list[dict]:
        return [dict(s) for s in self._stops]

    # ------------------------------------------------------------- stops

    def on_quote(self, quote: StraddleQuote, now: datetime | float | None = None) -> list[dict]:
        """Trigger resting stop orders against the leg LTPs of this quote."""
        events: list[dict] = []
        for stop in self._stops:
            if stop["status"] != STOP_PENDING:
                continue
            leg_quote = quote.call if quote.call.symbol == stop["symbol"] else quote.put if quote.put.symbol == stop["symbol"] else None
            if leg_quote is None or leg_quote.ltp < stop["trigger_price"]:
                continue
            base = leg_quote.ask if leg_quote.ask > 0 else leg_quote.ltp
            price = round_to_tick(max(base, stop["trigger_price"]) + self.slippage, self.tick)
            fill = Fill(
                intent_id=stop["intent_id"],
                symbol=stop["symbol"],
                side=Side.BUY,
                quantity=stop["quantity"],
                average_price=price,
                order_id=stop["order_id"],
                timestamp=leg_quote.timestamp,
            )
            stop["status"] = STOP_TRIGGERED
            stop["filled_qty"] = fill.quantity
            stop["average_price"] = price
            self._book(fill)
            event = {
                "type": "stop_triggered",
                "symbol": stop["symbol"],
                "order_id": stop["order_id"],
                "trigger_price": stop["trigger_price"],
                "fill": fill,
            }
            self.events.append(event)
            events.append(event)
        return events

    def _place_stops(self, intent: Intent) -> list[Fill]:
        for leg in intent.legs:
            trigger = leg.trigger_price if isinstance(leg, StopLeg) else (leg.limit_price or 0.0)
            self._stops.append(
                {
                    "intent_id": intent.intent_id,
                    "symbol": leg.contract.symbol,
                    "exchange": leg.contract.exchange,
                    "side": Side.BUY.value,
                    "quantity": leg.quantity,
                    "trigger_price": float(trigger),
                    "order_id": self._next_id("stop"),
                    "status": STOP_PENDING,
                    "filled_qty": 0,
                    "average_price": 0.0,
                }
            )
        return []

    def _cancel_stops(self, symbols: Sequence[str]) -> None:
        for stop in self._stops:
            if stop["status"] == STOP_PENDING and stop["symbol"] in symbols:
                stop["status"] = STOP_CANCELLED
                self.cancelled_stops.append(stop["order_id"])

    # ----------------------------------------------------------- helpers

    def _next_id(self, prefix: str = "replay") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _price(self, side: Side, quote: Quote) -> float:
        if side == Side.SELL:
            base = quote.bid if quote.bid > 0 else quote.ltp
            return max(self.tick, round_to_tick(base - self.slippage, self.tick))
        base = quote.ask if quote.ask > 0 else quote.ltp
        return round_to_tick(base + self.slippage, self.tick)

    def _book(self, fill: Fill) -> None:
        signed = fill.quantity if fill.side == Side.BUY else -fill.quantity
        self._positions[fill.symbol] = self._positions.get(fill.symbol, 0) + signed
        self.costs += order_cost_inr(self.cost_model, fill.side, fill.average_price, fill.quantity)
        self.fills.append(fill)

    def gross_pnl(self) -> float:
        return round(sum((f.quantity * f.average_price) * (1 if f.side == Side.SELL else -1) for f in self.fills), 2)

    def net_pnl(self) -> float:
        return round(self.gross_pnl() - self.costs, 2)


# ---------------------------------------------------------------------------
# OpenAlgo broker
# ---------------------------------------------------------------------------


@dataclass
class OrderState:
    status: str
    filled: int
    average_price: float


def _analyze_mode(status: Any) -> bool | None:
    if not isinstance(status, dict):
        return None
    data = status.get("data", status) if isinstance(status.get("data"), dict) else status
    if "analyze_mode" in data:
        return bool(data["analyze_mode"])
    mode = data.get("mode")
    if mode is None:
        return None
    return str(mode).lower() in ("analyze", "analyzer", "paper", "true")


def _orders_of(orderbook: Any) -> list[dict]:
    if isinstance(orderbook, list):
        return [o for o in orderbook if isinstance(o, dict)]
    if isinstance(orderbook, dict):
        data = orderbook.get("data", orderbook)
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict)]
        if isinstance(data, dict):
            return [o for o in (data.get("orders") or []) if isinstance(o, dict)]
    return []


def _positions_of(positionbook: Any) -> list[dict]:
    if isinstance(positionbook, list):
        return [p for p in positionbook if isinstance(p, dict)]
    if isinstance(positionbook, dict):
        data = positionbook.get("data", positionbook)
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
    return []


def _results_of(response: Any) -> list[dict]:
    if isinstance(response, list):
        return [r for r in response if isinstance(r, dict)]
    if isinstance(response, dict):
        return [r for r in (response.get("results") or []) if isinstance(r, dict)]
    return []


def parse_broker_time(value: Any) -> float | None:
    """Epoch seconds from the timestamp formats OpenAlgo uses, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value) / 1000.0 if float(value) > 1e11 else float(value)
    text = str(value).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%H:%M:%S %d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST).timestamp()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.timestamp()
    except ValueError:
        return None


def order_state(data: Any) -> OrderState | None:
    """Normalise an orderstatus or orderbook entry."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("data"), dict):
        data = data["data"]
    status = str(data.get("order_status") or data.get("status") or "").lower()
    quantity = _as_int(data.get("quantity"))
    filled = None
    for key in ("filled_quantity", "filledshares", "filled_qty", "filledqty", "filled"):
        if key in data and data[key] not in (None, ""):
            filled = _as_int(data[key])
            break
    if filled is None:
        filled = quantity if status == "complete" else 0
    if status == "complete" and filled == 0:
        filled = quantity
    avg = 0.0
    for key in ("average_price", "averageprice", "avgprice", "price"):
        if key in data and data[key] not in (None, ""):
            avg = _as_float(data[key])
            if avg > 0:
                break
    return OrderState(status=status, filled=filled, average_price=avg)


class OpenAlgoBroker:
    """Basket entries and exits through OpenAlgo with an intent-before-send ledger."""

    def __init__(
        self,
        client: Any,
        settings: dict,
        ledger: Ledger,
        mode: str = "paper",
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.settings = settings
        self.ledger = ledger
        self.mode = mode
        strategy = settings.get("strategy", {})
        self.tag = str(strategy.get("strategy_tag", "openfly"))
        self.product = str(strategy.get("product", "NRML"))
        self.exchange = str(strategy.get("options_exchange", "NFO"))
        self.lot_size = int(strategy.get("lot_size", 65))
        ex = execution_settings(settings)
        self.order_type = str(ex["order_type"]).upper()
        self.offset_ticks = int(ex["limit_offset_ticks"])
        self.fill_timeout = float(ex["fill_timeout_s"])
        self.poll_interval = float(ex["poll_interval_s"])
        self.reconcile_window = float(ex["reconcile_window_s"])
        self.margin_per_lot = float(ex["margin_per_lot"])
        self._clock = clock
        self._sleep = sleep

    # --------------------------------------------------------- preflight

    def preflight(self, symbols: Sequence[str] = (), lots: int = 1) -> dict:
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        try:
            analyze = _analyze_mode(self.client.analyzer_status())
        except Exception as exc:  # network or auth failure
            add("analyzer", False, f"analyzer status unavailable: {exc}")
        else:
            if analyze is None:
                add("analyzer", False, "analyzer status has no analyze_mode field")
            elif self.mode == "paper":
                add("analyzer", analyze, "analyzer is on, orders are simulated" if analyze else "analyzer is off; paper mode requires it on")
            else:
                add("analyzer", not analyze, "analyzer is off, orders are real" if not analyze else "analyzer is on; live mode requires it off")

        required = self._margin_required(symbols, lots)
        funds_fn = getattr(self.client, "funds", None)
        if funds_fn is None:
            add("funds", True, "funds endpoint not available on this client, check skipped")
        else:
            try:
                funds = funds_fn() or {}
                data = funds.get("data", funds) if isinstance(funds, dict) else {}
                available = _as_float(data.get("availablecash", data.get("available_cash", 0.0)))
                add(
                    "funds",
                    available >= required,
                    f"available cash INR {available:,.0f} against INR {required:,.0f} needed for {lots} lot{'s' if lots != 1 else ''}",
                )
            except Exception as exc:
                add("funds", False, f"funds unavailable: {exc}")

        symbol_fn = getattr(self.client, "symbol", None)
        for symbol in symbols:
            if symbol_fn is None:
                add("symbol_master", True, f"{symbol}: symbol endpoint not available, check skipped")
                continue
            try:
                info = symbol_fn(symbol, self.exchange)
                ok = info is not None and not (isinstance(info, dict) and info.get("status") == "error")
                add("symbol_master", ok, f"{symbol} resolves in the symbol master" if ok else f"{symbol} not found in the symbol master")
            except Exception as exc:
                add("symbol_master", False, f"{symbol} lookup failed: {exc}")

        try:
            orders = _orders_of(self.client.orderbook())
        except Exception as exc:
            add("foreign_orders", False, f"orderbook unavailable: {exc}")
        else:
            foreign = [
                o
                for o in orders
                if o.get("symbol") in set(symbols)
                and str(o.get("order_status", "")).lower() in ("open", "pending", "trigger pending")
                and str(o.get("strategy") or "") != self.tag
            ]
            add(
                "foreign_orders",
                not foreign,
                "no open orders from other strategies on the legs" if not foreign else f"{len(foreign)} open order(s) on the legs from other strategies",
            )

        pending = self.ledger.pending()
        add("ledger_pending", not pending, "no pending intent in the ledger" if not pending else f"intent {pending[0]['intent_id']} is {pending[0]['status']}")
        halted = self.ledger.halted()
        add("ledger_halted", halted is None, "ledger is not halted" if halted is None else f"ledger halted: {halted.get('reason')}")

        return {"ok": all(c["ok"] for c in checks), "mode": self.mode, "checks": checks, "margin_required": required}

    def _margin_required(self, symbols: Sequence[str], lots: int) -> float:
        margin_fn = getattr(self.client, "margin", None)
        if margin_fn is not None and symbols:
            positions = [
                {"symbol": s, "exchange": self.exchange, "action": "SELL", "product": self.product, "quantity": lots * self.lot_size}
                for s in symbols
            ]
            try:
                data = margin_fn(positions) or {}
                data = data.get("data", data) if isinstance(data, dict) else {}
                total = _as_float(data.get("total_margin_required", 0.0))
                if total > 0:
                    return total
            except Exception as exc:
                logger.warning("margin endpoint failed, using margin_per_lot: %s", exc)
        return float(lots) * self.margin_per_lot

    # ----------------------------------------------------------- execute

    def execute(self, intent: Intent, quote_lookup: QuoteLookup) -> list[Fill]:
        if intent.kind == KIND_STOPS:
            return self._place_stops(intent)
        stop_fills: list[Fill] = []
        closed: set[str] = set()
        if intent.kind in EXIT_KINDS or intent.kind == KIND_REPAIR:
            stop_fills, closed = self._cancel_stops_for([leg.contract.symbol for leg in intent.legs])
        legs = [leg for leg in intent.legs if leg.contract.symbol not in closed]
        if not legs:
            self.ledger.settle(intent.intent_id, [], IntentStatus.SETTLED)
            self.ledger.mark(intent.intent_id, IntentStatus.SETTLED, "legs already closed by their stop orders")
            return stop_fills

        self.ledger.mark(intent.intent_id, IntentStatus.UNKNOWN, "basket sent")
        orders = [self._order_payload(leg, quote_lookup) for leg in legs]
        try:
            response = self.client.basketorder(orders, self.tag)
        except Exception as exc:
            raise LostResponse(intent.intent_id, str(exc)) from exc

        leg_orders = self._map_results(intent, legs, _results_of(response))
        if not any(leg_orders.values()):
            self.ledger.settle(intent.intent_id, [], IntentStatus.REJECTED)
            self.ledger.mark(intent.intent_id, IntentStatus.REJECTED, "every leg was rejected by the broker")
            return stop_fills
        self.ledger.mark(intent.intent_id, IntentStatus.ACCEPTED, "basket accepted")
        fills = self._await_fills(intent, legs, leg_orders)
        return stop_fills + self._finish(intent, legs, fills)

    def _order_payload(self, leg: Any, quote_lookup: QuoteLookup) -> dict[str, Any]:
        order: dict[str, Any] = {
            "symbol": leg.contract.symbol,
            "exchange": leg.contract.exchange,
            "action": leg.side.value,
            "quantity": int(leg.quantity),
            "product": self.product,
            "pricetype": "MARKET",
        }
        tick = leg.contract.tick_size or TICK
        price: float | None = None
        if leg.limit_price is not None:
            price = float(leg.limit_price)
        elif self.order_type == "LIMIT":
            quote = quote_lookup(leg.contract.symbol)
            if quote is not None and quote.ltp > 0:
                offset = self.offset_ticks * tick
                if leg.side == Side.SELL:
                    price = max(tick, floor_to_tick(quote.ltp - offset, tick))
                else:
                    price = ceil_to_tick(quote.ltp + offset, tick)
        if price is not None:
            order["pricetype"] = "LIMIT"
            order["price"] = price
        return order

    def _map_results(self, intent: Intent, legs: Sequence[Any], results: list[dict]) -> dict[str, str | None]:
        leg_orders: dict[str, str | None] = {}
        used: set[int] = set()
        for leg in legs:
            symbol = leg.contract.symbol
            match = None
            for idx, r in enumerate(results):
                if idx in used:
                    continue
                if r.get("symbol") == symbol and str(r.get("action") or leg.side.value).upper() == leg.side.value:
                    match = r
                    used.add(idx)
                    break
            if match is not None and str(match.get("status", "")).lower() == "success" and match.get("orderid"):
                order_id = str(match["orderid"])
                leg_orders[symbol] = order_id
                self.ledger.record_leg_order(intent.intent_id, symbol, leg.side, order_id, "open")
            else:
                message = (match or {}).get("message", "no result for this leg")
                leg_orders[symbol] = None
                self.ledger.record_leg_order(intent.intent_id, symbol, leg.side, "", f"rejected: {message}")
        return leg_orders

    def _poll(self, order_id: str) -> OrderState | None:
        try:
            return order_state(self.client.orderstatus(order_id, self.tag))
        except Exception as exc:
            logger.warning("orderstatus %s failed: %s", order_id, exc)
            return None

    def _await_fills(self, intent: Intent, legs: Sequence[Any], leg_orders: dict[str, str | None]) -> list[Fill]:
        sides = {leg.contract.symbol: leg.side for leg in legs}
        final: dict[str, OrderState] = {}
        open_ids = {s: oid for s, oid in leg_orders.items() if oid}
        deadline = self._clock() + self.fill_timeout
        while open_ids:
            for symbol, oid in list(open_ids.items()):
                state = self._poll(oid)
                if state is not None and state.status in FINAL_ORDER_STATUSES:
                    final[symbol] = state
                    del open_ids[symbol]
            if not open_ids or self._clock() >= deadline:
                break
            self._sleep(self.poll_interval)
        for symbol, oid in open_ids.items():
            try:
                self.client.cancelorder(oid, self.tag)
            except Exception as exc:
                logger.warning("cancelorder %s failed: %s", oid, exc)
            state = self._poll(oid) or OrderState("cancelled", 0, 0.0)
            if state.status not in FINAL_ORDER_STATUSES:
                state = OrderState("cancelled", state.filled, state.average_price)
            final[symbol] = state
        fills: list[Fill] = []
        now = self._clock()
        for symbol, oid in leg_orders.items():
            if not oid or symbol not in final:
                continue
            state = final[symbol]
            self.ledger.record_leg_order(intent.intent_id, symbol, sides[symbol], oid, state.status, state.filled, state.average_price)
            if state.filled > 0:
                fills.append(Fill(intent.intent_id, symbol, sides[symbol], state.filled, state.average_price, oid, now))
        return fills

    def _finish(self, intent: Intent, legs: Sequence[Any], fills: list[Fill]) -> list[Fill]:
        sub = Intent(intent.intent_id, intent.kind, tuple(legs), intent.reason, intent.created_at, intent.observation_hash)
        status = classify_fills(sub, fills)
        if status == IntentStatus.REJECTED:
            self.ledger.settle(intent.intent_id, fills, IntentStatus.REJECTED)
            return []
        if status == IntentStatus.SETTLED:
            self.ledger.settle(intent.intent_id, fills, IntentStatus.SETTLED)
            return fills
        self.ledger.settle(intent.intent_id, fills, IntentStatus.PARTIAL)
        if len(legs) == 1 or is_balanced(sub, fills):
            return fills
        filled = {f.symbol: f.quantity for f in fills}
        detail = ", ".join(f"{leg.contract.symbol} {filled.get(leg.contract.symbol, 0)} of {leg.quantity}" for leg in legs)
        raise OneLegged(intent, fills, detail)

    # ------------------------------------------------------------- stops

    def _place_stop(self, intent_id: str, symbol: str, exchange: str, quantity: int, trigger: float) -> str:
        try:
            response = self.client.placeorder(
                symbol=symbol,
                exchange=exchange,
                action="BUY",
                quantity=int(quantity),
                product=self.product,
                pricetype="SL-M",
                price=0,
                trigger_price=float(trigger),
                strategy=self.tag,
            )
        except Exception as exc:
            logger.warning("stop order for %s failed: %s", symbol, exc)
            self.ledger.record_stop_order(intent_id, symbol, quantity, trigger, "", STOP_REJECTED, exchange)
            return ""
        order_id = str((response or {}).get("orderid") or "") if isinstance(response, dict) else ""
        if order_id:
            state = self._poll(order_id)
            if state is not None and state.status in ("rejected", "cancelled"):
                logger.warning("stop order %s for %s was %s at once", order_id, symbol, state.status)
                self.ledger.record_stop_order(intent_id, symbol, quantity, trigger, order_id, STOP_REJECTED, exchange)
                return ""
        self.ledger.record_stop_order(intent_id, symbol, quantity, trigger, order_id, STOP_PENDING if order_id else STOP_REJECTED, exchange)
        return order_id

    def _place_stops(self, intent: Intent) -> list[Fill]:
        placed = 0
        for leg in intent.legs:
            trigger = leg.trigger_price if isinstance(leg, StopLeg) else float(leg.limit_price or 0.0)
            order_id = self._place_stop(intent.intent_id, leg.contract.symbol, leg.contract.exchange, leg.quantity, trigger)
            self.ledger.record_leg_order(intent.intent_id, leg.contract.symbol, leg.side, order_id, "pending" if order_id else "rejected")
            placed += 1 if order_id else 0
        if placed == len(intent.legs):
            status = IntentStatus.SETTLED
        elif placed == 0:
            status = IntentStatus.REJECTED
        else:
            status = IntentStatus.PARTIAL
        self.ledger.settle(intent.intent_id, [], status)
        return []

    def _cancel_stops_for(self, symbols: Sequence[str]) -> tuple[list[Fill], set[str]]:
        """Cancel resting stops on these legs. Stops that turn out to have executed become fills."""
        fills: list[Fill] = []
        closed: set[str] = set()
        for rec in self.ledger.stop_orders(active_only=True):
            if rec["symbol"] not in symbols:
                continue
            oid = rec["order_id"]
            try:
                self.client.cancelorder(oid, self.tag)
            except Exception as exc:
                logger.warning("cancel stop %s failed: %s", oid, exc)
            state = self._poll(oid)
            if state is not None and (state.status == "complete" or state.filled >= rec["quantity"] > 0):
                fill = Fill(rec["intent_id"], rec["symbol"], Side.BUY, state.filled or rec["quantity"], state.average_price, oid, self._clock())
                self.ledger.update_stop_order(oid, STOP_TRIGGERED, fill.quantity, fill.average_price)
                self.ledger.add_fills(rec["intent_id"], [fill])
                fills.append(fill)
                if fill.quantity >= rec["quantity"]:
                    closed.add(rec["symbol"])
            else:
                self.ledger.update_stop_order(oid, STOP_CANCELLED)
        return fills, closed

    def _maintain_stops(self) -> list[dict]:
        events: list[dict] = []
        positions: dict[str, int] | None = None
        for rec in self.ledger.stop_orders(active_only=True):
            state = self._poll(rec["order_id"])
            if state is None:
                continue
            if state.status == "complete" or (state.filled >= rec["quantity"] > 0):
                fill = Fill(rec["intent_id"], rec["symbol"], Side.BUY, state.filled or rec["quantity"], state.average_price, rec["order_id"], self._clock())
                self.ledger.update_stop_order(rec["order_id"], STOP_TRIGGERED, fill.quantity, fill.average_price)
                self.ledger.add_fills(rec["intent_id"], [fill])
                events.append({"type": "stop_triggered", "symbol": rec["symbol"], "order_id": rec["order_id"], "trigger_price": rec["trigger_price"], "fill": fill})
            elif state.status in ("cancelled", "rejected"):
                self.ledger.update_stop_order(rec["order_id"], state.status)
                if positions is None:
                    positions = self.positions()
                if positions.get(rec["symbol"], 0) < 0:
                    new_id = self._place_stop(rec["intent_id"], rec["symbol"], rec["exchange"], rec["quantity"], rec["trigger_price"])
                    events.append({"type": "stop_replaced", "symbol": rec["symbol"], "old_order_id": rec["order_id"], "order_id": new_id, "trigger_price": rec["trigger_price"]})
        if positions is None:
            positions = self.positions()
        active = {r["symbol"] for r in self.ledger.stop_orders(active_only=True)}
        for symbol, qty in positions.items():
            if qty >= 0 or symbol in active:
                continue
            last = self.ledger.last_stop_for(symbol)
            if last is None:
                events.append({"type": "stop_missing", "symbol": symbol, "quantity": -qty})
                continue
            new_id = self._place_stop(last["intent_id"], symbol, last["exchange"], -qty, last["trigger_price"])
            events.append({"type": "stop_replaced", "symbol": symbol, "old_order_id": last["order_id"], "order_id": new_id, "trigger_price": last["trigger_price"]})
        return events

    def stop_orders(self) -> list[dict]:
        return self.ledger.stop_orders()

    # --------------------------------------------------------- reconcile

    def reconcile(self) -> list[dict]:
        """Resolve pending intents from the orderbook and maintain the resting stop orders."""
        events: list[dict] = []
        for row in self.ledger.pending():
            intent = intent_from_payload(row["payload"])
            status = row["status"]
            if status == IntentStatus.PREPARED.value:
                self.ledger.settle(intent.intent_id, [], IntentStatus.REJECTED)
                self.ledger.mark(intent.intent_id, IntentStatus.REJECTED, "prepared but never sent")
                events.append({"type": "intent", "intent_id": intent.intent_id, "status": IntentStatus.REJECTED.value, "fills": []})
                continue
            if status == IntentStatus.UNKNOWN.value:
                leg_orders = self._adopt(intent, row)
            else:
                leg_orders = {leg["symbol"]: leg["order_id"] for leg in row["legs"] if leg["order_id"]}
            legs = list(intent.legs)
            fills = self._await_fills(intent, legs, leg_orders)
            try:
                fills = self._finish(intent, legs, fills)
            except OneLegged as exc:
                events.append({"type": "intent", "intent_id": intent.intent_id, "status": IntentStatus.PARTIAL.value, "fills": exc.fills, "one_legged": True, "detail": exc.detail})
                continue
            final = self.ledger.status(intent.intent_id)
            events.append({"type": "intent", "intent_id": intent.intent_id, "status": final.value if final else None, "fills": fills})
        events.extend(self._maintain_stops())
        return events

    def _adopt(self, intent: Intent, row: dict) -> dict[str, str | None]:
        try:
            orders = _orders_of(self.client.orderbook())
        except Exception as exc:
            raise UnresolvedOrder(f"intent {intent.intent_id}: orderbook unavailable while reconciling: {exc}") from exc
        created = float(row.get("created_at_epoch") or intent.created_at)
        leg_orders: dict[str, str | None] = {}
        for leg in intent.legs:
            matches = [o for o in orders if self._matches(o, leg, created)]
            if len(matches) != 1:
                raise UnresolvedOrder(
                    f"intent {intent.intent_id}: {len(matches)} orderbook matches for {leg.side.value} {leg.quantity} {leg.contract.symbol}"
                )
            order_id = str(matches[0].get("orderid"))
            leg_orders[leg.contract.symbol] = order_id
            self.ledger.record_leg_order(intent.intent_id, leg.contract.symbol, leg.side, order_id, "open")
        self.ledger.mark(intent.intent_id, IntentStatus.ACCEPTED, "adopted from the orderbook")
        return leg_orders

    def _matches(self, order: dict, leg: Any, created: float) -> bool:
        if order.get("symbol") != leg.contract.symbol:
            return False
        if str(order.get("action", "")).upper() != leg.side.value:
            return False
        if _as_int(order.get("quantity")) != leg.quantity:
            return False
        if order.get("product") and str(order["product"]).upper() != self.product.upper():
            return False
        if order.get("strategy") and str(order["strategy"]) != self.tag:
            return False
        if str(order.get("pricetype", "")).upper() in ("SL", "SL-M"):
            return False
        stamp = parse_broker_time(order.get("timestamp"))
        if stamp is not None and not (created - 60.0 <= stamp <= created + self.reconcile_window):
            return False
        return True

    # --------------------------------------------------------- positions

    def positions(self, symbols: Sequence[str] | None = None) -> dict[str, int]:
        try:
            book = _positions_of(self.client.positionbook())
        except Exception as exc:
            raise UnresolvedOrder(f"positionbook unavailable: {exc}") from exc
        out: dict[str, int] = {}
        wanted = set(symbols) if symbols else None
        for pos in book:
            if str(pos.get("product", self.product)).upper() != self.product.upper():
                continue
            symbol = str(pos.get("symbol", ""))
            if wanted is not None and symbol not in wanted:
                continue
            qty = _as_int(pos.get("quantity", pos.get("netqty", 0)))
            if qty != 0:
                out[symbol] = out.get(symbol, 0) + qty
        return out
