"""Types shared by the straddle engine, the ledger and the brokers.

This module has no dependency on the rest of the execution package so the
engine (openfly.straddle) and the brokers (openfly.execution) can both import
it without a cycle.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from openfly.interfaces import Fill, Intent, IntentStatus, Leg, Quote, Side, StraddleQuote

TICK = 0.05

# Intent kinds. ENTRY and EXIT carry two legs, the others one or two.
KIND_ENTRY = "ENTRY"
KIND_EXIT = "EXIT"
KIND_SQUARE_OFF = "SQUARE_OFF"
KIND_REPAIR = "REPAIR"
KIND_STOPS = "STOPS"  # resting per-leg BUY SL-M stop orders placed after an entry
KIND_EXIT_LEG = "EXIT_LEG"  # one leg bought back (software leg stop or exit_both)

EXIT_KINDS = (KIND_EXIT, KIND_SQUARE_OFF, KIND_EXIT_LEG)

# Settings the execution layer reads from settings["execution"], with defaults.
EXECUTION_DEFAULTS: dict[str, Any] = {
    "order_type": "LIMIT",  # LIMIT (marketable, ltp plus or minus offset) or MARKET
    "limit_offset_ticks": 2,
    "fill_timeout_s": 10.0,
    "poll_interval_s": 0.5,
    "reconcile_window_s": 900.0,  # orderbook time window searched around intent creation
    "reconcile_interval_s": 5.0,  # worker cadence for broker.reconcile()
    "repair_policy": "unwind",  # unwind (buy back the filled leg) or complete (sell the missing leg)
    "margin_per_lot": 190000.0,  # used when the margin endpoint is unavailable
    "tick_interval_s": 0.5,  # worker polling cadence for the feed
}


def execution_settings(settings: dict) -> dict[str, Any]:
    merged = dict(EXECUTION_DEFAULTS)
    merged.update(settings.get("execution", {}) or {})
    return merged


def round_to_tick(price: float, tick: float = TICK) -> float:
    return round(round(price / tick) * tick, 2)


def ceil_to_tick(price: float, tick: float = TICK) -> float:
    return round(math.ceil(round(price / tick, 6)) * tick, 2)


def floor_to_tick(price: float, tick: float = TICK) -> float:
    return round(math.floor(round(price / tick, 6)) * tick, 2)


@dataclass(frozen=True)
class StopLeg(Leg):
    """A resting stop order leg. `limit_price` is None (SL-M); `trigger_price` is the stop."""

    trigger_price: float = 0.0


class BrokerError(Exception):
    """Base class for broker failures that the dispatcher understands."""


class LostResponse(BrokerError):
    """The order call did not return. The intent is UNKNOWN until reconciled."""

    def __init__(self, intent_id: str, detail: str = ""):
        super().__init__(f"lost response for intent {intent_id}: {detail}")
        self.intent_id = intent_id
        self.detail = detail


class OneLegged(BrokerError):
    """A two-leg intent filled unevenly. The engine must repair (complete or unwind)."""

    def __init__(self, intent: Intent, fills: Sequence[Fill], detail: str = ""):
        super().__init__(f"one-legged outcome for intent {intent.intent_id}: {detail}")
        self.intent = intent
        self.fills = list(fills)
        self.detail = detail


class PendingIntentError(Exception):
    """The ledger refused a new intent because another one is still pending."""


@runtime_checkable
class CostModelProtocol(Protocol):
    """What the engine and brokers need from a cost model (openfly.market.costs.CostModel shape).

    `order_cost(side, premium, quantity)` prices one executed order of `quantity` units
    at `premium` per unit; `round_trip(entry_credit_points, lots, lot_size)` prices a
    straddle sold and bought back. Both return a float or an object with `.total`.
    Use openfly.execution.costs.order_cost_inr and round_trip_inr to normalise.
    """

    def order_cost(self, side: Side | str, premium: float, quantity: int) -> Any: ...

    def round_trip(self, entry_credit_points: float, lots: int, lot_size: int = 65) -> Any: ...


QuoteLookup = Callable[[str], Quote | None]


@runtime_checkable
class BrokerProtocol(Protocol):
    def preflight(self, symbols: Sequence[str] = (), lots: int = 1) -> dict: ...

    def execute(self, intent: Intent, quote_lookup: QuoteLookup) -> list[Fill]: ...

    def reconcile(self) -> list[dict]: ...

    def positions(self) -> dict[str, int]: ...

    def stop_orders(self) -> list[dict]: ...


@runtime_checkable
class ClientProtocol(Protocol):
    """The subset of the OpenAlgo client the broker uses. Shapes follow openfly.market.client."""

    def basketorder(self, orders: list[dict], strategy: str | None = None) -> list[dict] | dict: ...

    def placeorder(
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        product: str = "NRML",
        pricetype: str = "MARKET",
        price: float = 0,
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        strategy: str | None = None,
    ) -> dict: ...

    def orderstatus(self, orderid: str, strategy: str | None = None) -> dict: ...

    def orderbook(self) -> dict | list: ...

    def positionbook(self) -> list[dict] | dict: ...

    def cancelorder(self, orderid: str, strategy: str | None = None) -> dict: ...

    def analyzer_status(self) -> dict: ...


def quote_lookup_for(quote: StraddleQuote | None) -> QuoteLookup:
    """A quote lookup over the two legs of one straddle quote."""

    def lookup(symbol: str) -> Quote | None:
        if quote is None:
            return None
        if quote.call.symbol == symbol:
            return quote.call
        if quote.put.symbol == symbol:
            return quote.put
        return None

    return lookup


def classify_fills(intent: Intent, fills: Sequence[Fill]) -> IntentStatus:
    """SETTLED when every leg filled in full, REJECTED when nothing filled, else PARTIAL."""
    wanted = {leg.contract.symbol: leg.quantity for leg in intent.legs}
    got: dict[str, int] = dict.fromkeys(wanted, 0)
    for fill in fills:
        got[fill.symbol] = got.get(fill.symbol, 0) + fill.quantity
    if all(got[s] >= q for s, q in wanted.items()):
        return IntentStatus.SETTLED
    if all(got[s] == 0 for s in wanted):
        return IntentStatus.REJECTED
    return IntentStatus.PARTIAL


def is_balanced(intent: Intent, fills: Sequence[Fill]) -> bool:
    """True when every leg of a multi-leg intent filled the same fraction (possibly zero)."""
    filled: dict[str, int] = {leg.contract.symbol: 0 for leg in intent.legs}
    for fill in fills:
        filled[fill.symbol] = filled.get(fill.symbol, 0) + fill.quantity
    values = list(filled.values())
    return len(values) <= 1 or all(v == values[0] for v in values)
