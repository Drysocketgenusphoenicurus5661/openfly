"""Straddle engine: the state machine that turns readout predictions into intents.

States: FLAT, ENTERING, IN_POSITION, EXITING, HALTED. Inputs: one observation
per completed bar (`on_observation`) and one call per leg tick (`on_tick`).
Outputs: Intents for the broker (ENTRY with two SELL legs, EXIT with two BUY
legs, SQUARE_OFF, EXIT_LEG, STOPS, REPAIR) plus an EngineStep with the action,
the guard result, the straddle snapshot, day P&L, a plain-language narrative
and a technical dict. The caller executes the intents and reports fills back
through `on_execution`; broker-side leg stops arrive through `on_leg_stop`.

Money rules (docs/PLAN.md section 2):
- short N lots at the ATM strike of the StraddleQuote, one straddle at a time,
  fresh straddles allowed after any exit once the re-entry cooldown has passed;
- sizing: floor(risk budget / (credit x stop percent x lot size)), clipped to
  [1, max_lots], to the requested lot count and to a margin cap when given;
- combined stop at credit x (1 + stop_pct/100), combined target at
  credit x (1 - target_pct/100), lock: once the premium has fallen
  lock_after_pct the stop moves to the entry credit;
- per-leg fixed stops at entry_price x (1 + leg_stop_pct/100) rounded up to
  the tick, placed at the broker (STOPS intent) or checked in software;
- early exit when the readout says EXIT, time exit at square_off minus the lead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from openfly.execution.costs import order_cost_inr, round_trip_inr
from openfly.execution.types import (
    EXIT_KINDS,
    KIND_ENTRY,
    KIND_EXIT,
    KIND_EXIT_LEG,
    KIND_REPAIR,
    KIND_SQUARE_OFF,
    KIND_STOPS,
    TICK,
    StopLeg,
    ceil_to_tick,
    execution_settings,
)
from openfly.interfaces import (
    Contract,
    Decision,
    Fill,
    Intent,
    IntentStatus,
    Leg,
    MarketObservation,
    Prediction,
    Quote,
    SessionWindow,
    Side,
    StraddleQuote,
)
from openfly.straddle.guard import Guard, GuardContext, GuardResult, hhmm

IST = ZoneInfo("Asia/Kolkata")


class State(StrEnum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    IN_POSITION = "IN_POSITION"
    EXITING = "EXITING"
    HALTED = "HALTED"


class Action(StrEnum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    STOP = "STOP"
    STOP_LEG = "STOP_LEG"
    TARGET = "TARGET"
    LOCK = "LOCK"
    SQUARE_OFF = "SQUARE_OFF"
    REENTRY = "REENTRY"
    HOLD = "HOLD"
    VETO = "VETO"
    NONE = "NONE"


def _hms(value: datetime | None) -> str:
    return value.strftime("%H:%M:%S") if value else "?"


def _inr(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}INR {int(math.floor(abs(value) + 0.5)):,}"


def _expiry_label(value: date) -> str:
    return value.strftime("%d-%b-%y").upper()


def _strike_label(value: float) -> str:
    return f"{value:g}"


def _lots_text(lots: int) -> str:
    return f"{lots} lot" if lots == 1 else f"{lots} lots"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_dt(epoch: float, fallback: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(float(epoch), IST) if epoch and epoch > 0 else fallback
    except (OverflowError, OSError, ValueError):
        return fallback


@dataclass
class LegState:
    contract: Contract
    quantity: int = 0  # open short quantity (positive)
    entry_price: float = 0.0
    entry_qty: int = 0
    exit_qty: int = 0
    exit_value: float = 0.0
    ltp: float = 0.0
    stop_price: float | None = None
    stop_order_id: str | None = None
    stop_status: str = "none"  # none, pending, triggered, cancelled, rejected, software
    status: str = "pending"  # pending (not filled yet), open, closed

    @property
    def symbol(self) -> str:
        return self.contract.symbol

    @property
    def option_type(self) -> str:
        return self.contract.option_type

    @property
    def word(self) -> str:
        return "call" if self.option_type == "CE" else "put"

    @property
    def exit_price(self) -> float | None:
        return self.exit_value / self.exit_qty if self.exit_qty else None


@dataclass
class Position:
    number: int
    strike: float
    expiry: date
    lots: int
    legs: dict[str, LegState]
    sizing: dict[str, Any] = field(default_factory=dict)
    quoted_credit: float = 0.0
    entry_credit: float = 0.0
    entered_at: datetime | None = None
    stop_level: float | None = None
    target_level: float | None = None
    lock_level: float | None = None
    locked: bool = False
    exit_action: str | None = None
    gross: float = 0.0  # sold value minus bought value over every fill of this straddle
    costs: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    leg_stops_hit: int = 0
    repairs: int = 0

    def open_legs(self) -> list[LegState]:
        return [leg for leg in self.legs.values() if leg.status == "open"]

    def closed_legs(self) -> list[LegState]:
        return [leg for leg in self.legs.values() if leg.status == "closed"]

    def is_open(self) -> bool:
        return any(leg.status == "open" for leg in self.legs.values())

    def combined_premium(self) -> float:
        """Points: open legs at LTP plus closed legs at their exit price."""
        total = 0.0
        for leg in self.legs.values():
            if leg.status == "open":
                total += leg.ltp
            elif leg.status == "closed" and leg.exit_price is not None:
                total += leg.exit_price
        return total

    def liquidation(self) -> float:
        return sum(leg.quantity * leg.ltp for leg in self.open_legs())

    def pnl_gross(self) -> float:
        return self.gross - self.liquidation()

    def exit_debit(self) -> float:
        return sum(leg.exit_price or 0.0 for leg in self.legs.values() if leg.exit_price is not None)

    def quantity(self) -> int:
        return max((leg.entry_qty for leg in self.legs.values()), default=0)


@dataclass
class EngineStep:
    at: datetime
    trigger: str  # observation, tick, broker, manual
    action: str = Action.NONE.value
    guard: GuardResult | None = None
    intents: list[Intent] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    straddle: dict[str, Any] = field(default_factory=dict)
    pnl_day: float = 0.0
    narrative: str = ""
    technical: dict[str, Any] = field(default_factory=dict)
    prediction: Prediction | None = None
    quote: StraddleQuote | None = None
    observation: MarketObservation | None = None
    detail: str = ""
    sizing: dict[str, Any] | None = None
    closed: dict[str, Any] | None = None
    entry_failed: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": self.at.isoformat(),
            "trigger": self.trigger,
            "action": self.action,
            "guard": self.guard.to_dict() if self.guard else None,
            "intents": [intent_summary(i) for i in self.intents],
            "fills": [fill_summary(f) for f in self.fills],
            "straddle": self.straddle,
            "pnl_day": self.pnl_day,
            "narrative": self.narrative,
            "technical": self.technical,
        }


def intent_summary(intent: Intent) -> dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "kind": intent.kind,
        "reason": intent.reason,
        "legs": [
            {
                "symbol": leg.contract.symbol,
                "side": leg.side.value,
                "quantity": leg.quantity,
                "limit_price": leg.limit_price,
                **({"trigger_price": leg.trigger_price} if isinstance(leg, StopLeg) else {}),
            }
            for leg in intent.legs
        ],
    }


def fill_summary(fill: Fill) -> dict[str, Any]:
    return {
        "symbol": fill.symbol,
        "side": fill.side.value,
        "qty": fill.quantity,
        "price": fill.average_price,
        "order_id": fill.order_id,
        "intent_id": fill.intent_id,
        "t": _iso(_to_dt(fill.timestamp, datetime.fromtimestamp(0, IST))),
    }


class StraddleEngine:
    """See the module docstring. One instance per run; call `start_day` (or pass a window) per day."""

    def __init__(self, settings: dict, cost_model: Any = None, guard: Guard | None = None):
        self.settings = settings
        self.cost_model = cost_model
        self.guard = guard or Guard(settings)
        self.state = State.FLAT
        self.window: SessionWindow | None = None
        self.trading_date: date | None = None
        self.position: Position | None = None
        self.halt_reason = ""
        self.last_quote: StraddleQuote | None = None
        self.last_step: EngineStep | None = None
        self.last_closed: dict[str, Any] | None = None
        self.last_exit_at: datetime | None = None
        self.entries_today = 0
        self.stop_hits = 0
        self.target_hits = 0
        self.time_exits = 0
        self.early_exits = 0
        self.leg_stops_today = 0
        self.vetoes = 0
        self.realized_day = 0.0
        self.closed: list[dict[str, Any]] = []
        self._intents: dict[str, Intent] = {}
        self._pending: Intent | None = None
        self._seq = 0
        self._current_step: EngineStep | None = None
        self._exit_failures = 0

    # ------------------------------------------------------------ settings

    @property
    def strategy(self) -> dict:
        return self.settings.get("strategy", {})

    @property
    def risk(self) -> dict:
        return self.settings.get("risk", {})

    @property
    def lot_size(self) -> int:
        return int(self.strategy.get("lot_size", 65))

    @property
    def requested_lots(self) -> int:
        return int(self.strategy.get("lots", 1) or 0)

    @property
    def stop_pct(self) -> float:
        return float(self.strategy.get("stop_pct", 25.0))

    @property
    def target_pct(self) -> float:
        return float(self.strategy.get("target_pct", 40.0))

    @property
    def lock_after_pct(self) -> float:
        return float(self.strategy.get("lock_after_pct", 15.0) or 0.0)

    @property
    def leg_stop_pct(self) -> float:
        return float(self.strategy.get("leg_stop_pct", 30.0) or 0.0)

    @property
    def leg_stop_mode(self) -> str:
        return str(self.strategy.get("leg_stop_mode", "broker")).lower()

    @property
    def on_leg_stop_policy(self) -> str:
        return str(self.strategy.get("on_leg_stop", "hold_other")).lower()

    @property
    def combined_stop_enabled(self) -> bool:
        return bool(self.strategy.get("combined_stop_enabled", True))

    @property
    def square_off_lead(self) -> timedelta:
        return timedelta(seconds=float(self.strategy.get("square_off_lead_seconds", 30)))

    @property
    def max_lots(self) -> int:
        return int(self.risk.get("max_lots", 3))

    @property
    def capital(self) -> float:
        return float(self.risk.get("capital", 1000000.0))

    @property
    def risk_budget_pct(self) -> float:
        return float(self.risk.get("risk_budget_pct", 1.0))

    @property
    def tau(self) -> float | None:
        value = self.settings.get("neural", {}).get("tau")
        return float(value) if value is not None else None

    @property
    def deadline(self) -> datetime | None:
        return self.window.square_off - self.square_off_lead if self.window else None

    # ------------------------------------------------------------ status

    @property
    def is_halted(self) -> bool:
        return self.state == State.HALTED

    @property
    def in_position(self) -> bool:
        return self.position is not None and self.position.is_open()

    @property
    def is_flat_book(self) -> bool:
        return self.position is None or not self.position.is_open()

    @property
    def pending_intent(self) -> Intent | None:
        return self._pending

    def start_day(self, window: SessionWindow, entries_today: int = 0, realized_day: float = 0.0) -> None:
        """Reset the per-day counters when a new trading date begins."""
        if self.window is not None and window.trading_date == self.trading_date:
            self.window = window
            return
        self.window = window
        self.trading_date = window.trading_date
        self.entries_today = entries_today
        self.realized_day = realized_day
        self.stop_hits = self.target_hits = self.time_exits = self.early_exits = self.leg_stops_today = self.vetoes = 0
        self.closed = []
        self.last_exit_at = None
        self.last_closed = None
        self._exit_failures = 0

    def halt(self, reason: str) -> None:
        self.state = State.HALTED
        self.halt_reason = reason

    def day_pnl(self) -> float:
        """Realized net P&L of the closed straddles plus the open one's mark-to-market after its costs."""
        total = self.realized_day
        if self.position is not None and self.position.is_open():
            total += self.position.pnl_gross() - self.position.costs
        return round(total, 2)

    # ------------------------------------------------------------ sizing

    def size_lots(
        self, entry_credit: float, margin_available: float | None = None, margin_per_lot: float | None = None
    ) -> tuple[int, dict[str, Any]]:
        """lots = floor(risk budget / (credit x stop percent x lot size)), then the caps."""
        budget = self.capital * self.risk_budget_pct / 100.0
        stop_pct = self.stop_pct if self.combined_stop_enabled and self.stop_pct > 0 else self.leg_stop_pct
        risk_per_lot = entry_credit * stop_pct / 100.0 * self.lot_size
        risk_lots = math.floor(budget / risk_per_lot) if risk_per_lot > 0 else 0
        lots = max(1, min(risk_lots, self.max_lots))
        caps: dict[str, Any] = {"risk": risk_lots, "max_lots": self.max_lots}
        if self.requested_lots > 0:
            caps["requested"] = self.requested_lots
            lots = min(lots, self.requested_lots)
        if margin_available is not None and margin_per_lot:
            margin_lots = math.floor(margin_available / margin_per_lot)
            caps["margin"] = margin_lots
            lots = min(lots, margin_lots)
        detail = (
            f"risk budget {_inr(budget)} over {_inr(risk_per_lot)} per lot allows {risk_lots}, "
            + ", ".join(f"{k} cap {v}" for k, v in caps.items() if k != "risk")
        )
        return lots, {
            "capital": self.capital,
            "risk_budget_pct": self.risk_budget_pct,
            "risk_budget": round(budget, 2),
            "entry_credit": entry_credit,
            "stop_pct": stop_pct,
            "lot_size": self.lot_size,
            "risk_per_lot": round(risk_per_lot, 2),
            "risk_lots": risk_lots,
            "caps": caps,
            "lots": lots,
            "detail": detail,
        }

    # ------------------------------------------------------------ inputs

    def on_observation(
        self,
        observation: MarketObservation,
        prediction: Prediction | None,
        quote: StraddleQuote | None,
        window: SessionWindow | None,
        guard_ctx: GuardContext | None = None,
    ) -> EngineStep:
        now = observation.timestamp
        if window is not None:
            self.start_day(window)
        self._update_ltp(quote)
        step = EngineStep(at=now, trigger="observation", prediction=prediction, quote=quote, observation=observation)
        self._current_step = step
        ctx = guard_ctx if guard_ctx is not None else GuardContext()

        if self.state == State.HALTED:
            step.action = Action.NONE.value
            step.detail = f"halted: {self.halt_reason}"
        elif self.state in (State.ENTERING, State.EXITING):
            step.action = Action.HOLD.value
            step.detail = "waiting for the broker to finish the previous intent"
        elif self.state == State.IN_POSITION:
            handled = self._check_levels(step, now)
            if not handled and prediction is not None and prediction.decision == Decision.EXIT:
                merged = self._merge_ctx(ctx, now, quote, prediction, observation, self.position.lots if self.position else 0)
                result = self.guard.check_exit(merged)
                step.guard = result
                if result.allowed:
                    self._emit_exit(step, Action.EXIT, f"readout EXIT, expects {prediction.realized_over_implied:.2f} of implied")
                else:
                    step.action = Action.VETO.value
                    step.detail = "exit vetoed"
                    self.vetoes += 1
            elif not handled and step.action == Action.NONE.value:
                step.action = Action.HOLD.value
        else:  # FLAT
            if prediction is not None and prediction.decision == Decision.ENTER and quote is not None:
                lots, sizing = self.size_lots(quote.combined_ltp, ctx.margin_available, ctx.margin_per_lot)
                step.sizing = sizing
                merged = self._merge_ctx(ctx, now, quote, prediction, observation, lots, sizing["detail"])
                result = self.guard.check_entry(merged)
                step.guard = result
                if result.allowed:
                    self._emit_entry(step, quote, lots, prediction, now, sizing)
                else:
                    step.action = Action.VETO.value
                    self.vetoes += 1
            else:
                step.action = Action.HOLD.value
        self._finish_step(step)
        return step

    def on_tick(self, quote: StraddleQuote, now: datetime) -> EngineStep:
        self._update_ltp(quote)
        step = EngineStep(at=now, trigger="tick", quote=quote)
        self._current_step = step
        if self.state == State.IN_POSITION:
            self._check_levels(step, now)
        elif self.state == State.HALTED:
            step.detail = f"halted: {self.halt_reason}"
        self._finish_step(step)
        return step

    def square_off_now(self, now: datetime, reason: str = "manual square-off") -> EngineStep:
        step = EngineStep(at=now, trigger="manual", quote=self.last_quote)
        self._current_step = step
        if self.state == State.IN_POSITION:
            self._emit_exit(step, Action.SQUARE_OFF, reason)
        else:
            step.detail = f"{reason}: nothing to square off"
        self._finish_step(step)
        return step

    # ---------------------------------------------------------- execution

    def on_execution(
        self,
        intent_id: str,
        fills: list[Fill],
        status: IntentStatus | None = None,
        detail: str = "",
        stop_orders: list[dict] | None = None,
    ) -> EngineStep:
        """Report the outcome of an intent. Updates the current step in place and returns it."""
        intent = self._intents.get(intent_id)
        was_halted = self.state == State.HALTED
        step = self._current_step
        if step is None:
            step = EngineStep(at=datetime.now(IST), trigger="execution")
            self._current_step = step
        if self._pending is not None and self._pending.intent_id == intent_id:
            self._pending = None
        if intent is None:
            step.detail = f"execution report for unknown intent {intent_id}"
            self._finish_step(step)
            return step
        new_fills = [f for f in fills if f not in step.fills]
        step.fills.extend(new_fills)
        for fill in new_fills:
            self._apply_fill(fill)
        if detail:
            step.detail = detail
        now = step.at
        pos = self.position

        if intent.kind == KIND_STOPS:
            if stop_orders:
                self.on_stops_placed(stop_orders)
            elif pos is not None and status == IntentStatus.REJECTED:
                for leg in pos.open_legs():
                    leg.stop_status = "rejected"
            self._finish_step(step)
            return step
        if pos is None:
            self._finish_step(step)
            return step

        open_legs = pos.open_legs()
        entering = pos.entered_at is None
        if intent.kind == KIND_ENTRY or (intent.kind == KIND_REPAIR and entering):
            if self._balanced_open(pos):
                if intent.kind == KIND_REPAIR:
                    step.notes.append("the pair was completed by a repair order")
                self._open_position(step, now)
            elif not open_legs:
                if intent.kind == KIND_REPAIR:
                    step.notes.append("the filled leg was unwound; no position")
                self.position = None
                self.state = State.FLAT
                step.entry_failed = True
                if not step.detail:
                    step.detail = "the entry basket was rejected"
            elif intent.kind == KIND_REPAIR:
                self.halt(f"repair left the book unbalanced: {detail or 'see fills'}")
            else:
                step.detail = detail or "one-legged entry, repairing"
        elif intent.kind in EXIT_KINDS or intent.kind == KIND_REPAIR:
            leg_closed = intent.kind == KIND_EXIT_LEG and all(pos.legs[leg.contract.symbol].status == "closed" for leg in intent.legs)
            if leg_closed and pos.exit_action == Action.STOP_LEG.value:
                for leg in intent.legs:
                    pos.legs[leg.contract.symbol].stop_status = "triggered"
                pos.leg_stops_hit += 1
                self.leg_stops_today += 1
            if not open_legs:
                if intent.kind == KIND_REPAIR:
                    step.notes.append("the remaining leg was closed by a repair order")
                self._close_position(step, now, pos.exit_action or Action.EXIT.value)
            elif intent.kind == KIND_REPAIR:
                self.halt(f"repair left the book unbalanced: {detail or 'see fills'}")
            elif not new_fills:
                self.state = State.IN_POSITION
                pos.exit_action = None
                self._exit_failures += 1
                if not step.detail:
                    step.detail = "the exit order was rejected"
                if self._exit_failures >= 3:
                    self.halt(f"exit failed {self._exit_failures} times: {step.detail}")
            elif leg_closed:
                self._after_leg_exit(step, intent)
            else:
                step.detail = detail or "one-legged exit, repairing"
        if was_halted:
            self.state = State.HALTED
        self._finish_step(step)
        return step

    def exit_legs(self, legs: list[LegState] | None = None) -> tuple[Leg, ...]:
        """BUY legs that close exactly the contracts of the open straddle.

        Symbols, strike, expiry and quantities come from the position's own leg
        states (filled from the broker's reports, the same fills the ledger holds),
        never from the latest quote: after a leg stop only the remaining leg is
        included, with its exact open quantity.
        """
        pos = self.position
        if pos is None:
            return ()
        targets = legs if legs is not None else pos.open_legs()
        return tuple(Leg(leg.contract, Side.BUY, leg.quantity, None) for leg in targets if leg.quantity > 0)

    def on_leg_stop(self, symbol: str, fills: list[Fill], now: datetime) -> EngineStep:
        """A broker-side per-leg stop executed."""
        step = EngineStep(at=now, trigger="broker", action=Action.STOP_LEG.value, quote=self.last_quote)
        self._current_step = step
        pos = self.position
        if pos is None or symbol not in pos.legs:
            step.action = Action.NONE.value
            step.detail = f"stop fill reported for {symbol}, which is not a leg of the open straddle"
            self._finish_step(step)
            return step
        leg = pos.legs[symbol]
        step.fills.extend(fills)
        for fill in fills:
            self._apply_fill(fill)
        if leg.stop_status != "triggered":
            leg.stop_status = "triggered"
            pos.leg_stops_hit += 1
            self.leg_stops_today += 1
        step.detail = f"{leg.word} leg stop at {leg.stop_price:.2f} triggered at the broker" if leg.stop_price else f"{leg.word} leg stop triggered at the broker"
        if not pos.is_open():
            self._close_position(step, now, Action.STOP_LEG.value)
        elif self.on_leg_stop_policy == "exit_both" and self.state in (State.IN_POSITION, State.ENTERING):
            self._emit_exit(step, Action.EXIT, "on_leg_stop exit_both: closing the remaining leg", keep_action=True)
            pos.exit_action = Action.STOP_LEG.value
        self._finish_step(step)
        return step

    def on_stops_placed(self, records: list[dict]) -> None:
        pos = self.position
        if pos is None:
            return
        for rec in records:
            leg = pos.legs.get(rec.get("symbol", ""))
            if leg is None or leg.status != "open":
                continue
            status = str(rec.get("status", ""))
            if status == "pending":
                leg.stop_order_id = rec.get("order_id") or None
                leg.stop_status = "pending"
                if rec.get("trigger_price"):
                    leg.stop_price = float(rec["trigger_price"])
            elif rec.get("order_id") and rec.get("order_id") == leg.stop_order_id:
                leg.stop_status = status
            elif status == "rejected" and leg.stop_status in ("none", "rejected"):
                leg.stop_status = "rejected"

    def repair_intent(self, detail: str = "") -> Intent | None:
        """A REPAIR intent after a one-legged fill: unwind (default) or complete the pair."""
        pos = self.position
        if pos is None:
            return None
        open_legs = pos.open_legs()
        entering = pos.entered_at is None
        policy = str(execution_settings(self.settings)["repair_policy"]).lower()
        legs: list[Leg] = []
        if entering and policy == "complete" and open_legs:
            target = max(leg.quantity for leg in open_legs)
            for leg in pos.legs.values():
                if leg.quantity < target:
                    legs.append(Leg(leg.contract, Side.SELL, target - leg.quantity, None))
            word = "complete"
        else:
            for leg in open_legs:
                legs.append(Leg(leg.contract, Side.BUY, leg.quantity, None))
            word = "unwind" if entering else "complete the exit of"
        if not legs:
            return None
        pos.repairs += 1
        now = self._current_step.at if self._current_step else datetime.now(IST)
        return self._new_intent(KIND_REPAIR, tuple(legs), f"{word} the pair after a one-legged fill: {detail}", now)

    # ----------------------------------------------------------- internals

    def _update_ltp(self, quote: StraddleQuote | None) -> None:
        if quote is None:
            return
        self.last_quote = quote
        if self.position is None:
            return
        for leg_quote in (quote.call, quote.put):
            leg = self.position.legs.get(leg_quote.symbol)
            if leg is not None and leg_quote.ltp > 0:
                leg.ltp = leg_quote.ltp

    def _merge_ctx(
        self,
        ctx: GuardContext,
        now: datetime,
        quote: StraddleQuote | None,
        prediction: Prediction | None,
        observation: MarketObservation | None,
        lots: int,
        lots_detail: str = "",
    ) -> GuardContext:
        obs_index = ctx.observation_index
        if obs_index is None and observation is not None and observation.index_bars:
            obs_index = observation.index_bars[-1].close
        vix = ctx.vix if ctx.vix is not None else (observation.vix if observation is not None else None)
        dte = ctx.days_to_expiry if ctx.days_to_expiry is not None else (observation.days_to_expiry if observation else None)
        return replace(
            ctx,
            now=now,
            window=self.window,
            quote=quote,
            prediction=prediction,
            vix=vix,
            days_to_expiry=dte,
            observation_index=obs_index,
            day_pnl=self.day_pnl(),
            entries_today=self.entries_today,
            last_exit_at=self.last_exit_at,
            in_position=self.in_position,
            lots=lots,
            lots_detail=lots_detail or ctx.lots_detail,
            halted=self.state == State.HALTED,
            halt_reason=self.halt_reason,
            pending_intent=ctx.pending_intent or self._pending is not None,
        )

    def _contract(self, leg_quote: Quote, quote: StraddleQuote, option_type: str) -> Contract:
        return Contract(
            symbol=leg_quote.symbol,
            exchange=leg_quote.exchange or str(self.strategy.get("options_exchange", "NFO")),
            underlying=str(self.strategy.get("underlying", "NIFTY")),
            expiry=quote.expiry,
            strike=quote.strike,
            option_type=option_type,
            lot_size=self.lot_size,
            tick_size=TICK,
            freeze_qty=int(self.strategy.get("freeze_qty", 1800)),
        )

    def _new_intent(self, kind: str, legs: tuple[Leg, ...], reason: str, now: datetime) -> Intent:
        self._seq += 1
        intent = Intent(
            intent_id=f"{kind.lower()}-{now:%Y%m%d-%H%M%S}-{self._seq:03d}",
            kind=kind,
            legs=legs,
            reason=reason,
            created_at=now.timestamp(),
        )
        self._intents[intent.intent_id] = intent
        self._pending = intent
        return intent

    def _emit_entry(self, step: EngineStep, quote: StraddleQuote, lots: int, prediction: Prediction, now: datetime, sizing: dict) -> None:
        qty = lots * self.lot_size
        call_c = self._contract(quote.call, quote, "CE")
        put_c = self._contract(quote.put, quote, "PE")
        number = len(self.closed) + 1
        reason = f"readout ENTER {prediction.realized_over_implied:.2f} of implied, straddle {number} of the day"
        intent = self._new_intent(KIND_ENTRY, (Leg(call_c, Side.SELL, qty, None), Leg(put_c, Side.SELL, qty, None)), reason, now)
        self.position = Position(
            number=number,
            strike=quote.strike,
            expiry=quote.expiry,
            lots=lots,
            legs={
                call_c.symbol: LegState(call_c, ltp=quote.call.ltp),
                put_c.symbol: LegState(put_c, ltp=quote.put.ltp),
            },
            sizing=sizing,
            quoted_credit=quote.combined_ltp,
        )
        self.state = State.ENTERING
        step.action = Action.REENTRY.value if self.closed else Action.ENTER.value
        step.intents.append(intent)

    def _emit_exit(self, step: EngineStep, action: Action, reason: str, legs: list[LegState] | None = None, keep_action: bool = False) -> None:
        pos = self.position
        if pos is None:
            return
        intent_legs = self.exit_legs(legs)
        if not intent_legs:
            return
        if action == Action.SQUARE_OFF:
            kind = KIND_SQUARE_OFF
        elif action == Action.STOP_LEG:
            kind = KIND_EXIT_LEG
        else:
            kind = KIND_EXIT
        intent = self._new_intent(kind, intent_legs, reason, step.at)
        pos.exit_action = action.value
        self.state = State.EXITING
        if not keep_action:
            step.action = action.value
        step.intents.append(intent)
        if not step.detail:
            step.detail = reason

    def _check_levels(self, step: EngineStep, now: datetime) -> bool:
        """Square-off, software leg stops, combined stop, target and lock. True when an exit was emitted."""
        pos = self.position
        if pos is None or not pos.is_open():
            return False
        deadline = self.deadline
        if deadline is not None and now >= deadline:
            self._emit_exit(step, Action.SQUARE_OFF, f"square-off at {_hms(deadline)}")
            return True
        if self.leg_stop_mode == "software":
            for leg in pos.open_legs():
                if leg.stop_price is not None and leg.ltp >= leg.stop_price:
                    self._emit_exit(step, Action.STOP_LEG, f"{leg.word} price {leg.ltp:.2f} reached its stop {leg.stop_price:.2f}", legs=[leg])
                    return True
        combined = pos.combined_premium()
        if pos.stop_level is not None and pos.leg_stops_hit == 0 and combined >= pos.stop_level:
            self._emit_exit(step, Action.STOP, f"combined premium {combined:.1f} reached the stop {pos.stop_level:.1f}")
            return True
        if pos.target_level is not None and combined <= pos.target_level:
            self._emit_exit(step, Action.TARGET, f"combined premium {combined:.1f} reached the target {pos.target_level:.1f}")
            return True
        if pos.lock_level is not None and not pos.locked and pos.leg_stops_hit == 0 and combined <= pos.lock_level:
            pos.locked = True
            pos.stop_level = pos.entry_credit
            step.action = Action.LOCK.value
            step.detail = f"premium fell {self.lock_after_pct:g} percent, stop moved to the entry credit"
        return False

    def _apply_fill(self, fill: Fill) -> None:
        pos = self.position
        if pos is None:
            return
        leg = pos.legs.get(fill.symbol)
        if leg is None:
            return
        cost = order_cost_inr(self.cost_model, fill.side, fill.average_price, fill.quantity)
        pos.costs += cost
        pos.fills.append(fill)
        if fill.side == Side.SELL:
            total = leg.entry_price * leg.entry_qty + fill.average_price * fill.quantity
            leg.entry_qty += fill.quantity
            leg.entry_price = total / leg.entry_qty if leg.entry_qty else 0.0
            leg.quantity += fill.quantity
            leg.status = "open"
            pos.gross += fill.quantity * fill.average_price
        else:
            leg.quantity = max(0, leg.quantity - fill.quantity)
            leg.exit_qty += fill.quantity
            leg.exit_value += fill.quantity * fill.average_price
            pos.gross -= fill.quantity * fill.average_price
            if leg.quantity == 0:
                leg.status = "closed"
                if leg.stop_order_id and fill.order_id == leg.stop_order_id:
                    if leg.stop_status != "triggered":
                        leg.stop_status = "triggered"
                        pos.leg_stops_hit += 1
                        self.leg_stops_today += 1
                elif leg.stop_status == "pending":
                    leg.stop_status = "cancelled"
        if fill.average_price > 0 and leg.status == "open":
            leg.ltp = fill.average_price if leg.ltp <= 0 else leg.ltp

    @staticmethod
    def _balanced_open(pos: Position) -> bool:
        legs = list(pos.legs.values())
        return bool(legs) and all(leg.status == "open" for leg in legs) and len({leg.quantity for leg in legs}) == 1

    def _open_position(self, step: EngineStep, now: datetime) -> None:
        pos = self.position
        assert pos is not None
        pos.entry_credit = sum(leg.entry_price for leg in pos.legs.values())
        pos.lots = max(1, pos.quantity() // self.lot_size)
        latest = max((f.timestamp for f in pos.fills), default=0.0)
        pos.entered_at = _to_dt(latest, now)
        credit = pos.entry_credit
        pos.stop_level = credit * (1 + self.stop_pct / 100.0) if self.combined_stop_enabled and self.stop_pct > 0 else None
        pos.target_level = credit * (1 - self.target_pct / 100.0) if self.target_pct > 0 else None
        pos.lock_level = credit * (1 - self.lock_after_pct / 100.0) if self.lock_after_pct > 0 and pos.stop_level is not None else None
        for leg in pos.legs.values():
            if self.leg_stop_pct > 0:
                leg.stop_price = ceil_to_tick(leg.entry_price * (1 + self.leg_stop_pct / 100.0))
                leg.stop_status = "software" if self.leg_stop_mode == "software" else "none"
        self.entries_today += 1
        self.state = State.IN_POSITION
        self._exit_failures = 0
        if self.leg_stop_mode == "broker" and self.leg_stop_pct > 0:
            legs = tuple(
                StopLeg(leg.contract, Side.BUY, leg.quantity, None, leg.stop_price or 0.0) for leg in pos.legs.values()
            )
            step.intents.append(self._new_intent(KIND_STOPS, legs, f"per-leg stops at {self.leg_stop_pct:g} percent", now))

    def _after_leg_exit(self, step: EngineStep, intent: Intent) -> None:
        pos = self.position
        assert pos is not None
        if pos.exit_action == Action.STOP_LEG.value and self.on_leg_stop_policy == "exit_both":
            self._emit_exit(step, Action.EXIT, "on_leg_stop exit_both: closing the remaining leg", keep_action=True)
            pos.exit_action = Action.STOP_LEG.value
            return
        self.state = State.IN_POSITION
        pos.exit_action = None

    def _close_position(self, step: EngineStep, now: datetime, action: str) -> None:
        pos = self.position
        assert pos is not None
        net = pos.gross - pos.costs
        self.realized_day += net
        record = {
            "number": pos.number,
            "strike": pos.strike,
            "expiry": pos.expiry.isoformat(),
            "lots": pos.lots,
            "quantity": pos.quantity(),
            "entry_credit": round(pos.entry_credit, 2),
            "exit_debit": round(pos.exit_debit(), 2),
            "entered_at": _iso(pos.entered_at),
            "exited_at": now.isoformat(),
            "action": action,
            "gross": round(pos.gross, 2),
            "costs": round(pos.costs, 2),
            "net": round(net, 2),
            "leg_stops": pos.leg_stops_hit,
            "locked": pos.locked,
            "legs": {
                leg.symbol: {"entry_price": round(leg.entry_price, 2), "exit_price": round(leg.exit_price or 0.0, 2), "stop_status": leg.stop_status}
                for leg in pos.legs.values()
            },
        }
        if action == Action.STOP.value:
            self.stop_hits += 1
        elif action == Action.TARGET.value:
            self.target_hits += 1
        elif action == Action.SQUARE_OFF.value:
            self.time_exits += 1
        elif action == Action.EXIT.value:
            self.early_exits += 1
        self.closed.append(record)
        self.last_closed = record
        self.last_exit_at = now
        step.closed = record
        self.position = None
        self.state = State.FLAT
        self._exit_failures = 0

    # ---------------------------------------------------------- snapshots

    def state_snapshot(self) -> dict[str, Any]:
        """The GET /api/straddle shape."""
        pos = self.position
        if pos is None or not pos.is_open():
            return {
                "in_position": False,
                "expiry": None,
                "strike": None,
                "lots": None,
                "legs": [],
                "entry_credit": None,
                "combined_ltp": None,
                "stop_level": None,
                "target_level": None,
                "pnl": None,
                "entered_at": None,
                "square_off_at": None,
            }
        legs = []
        for leg in pos.legs.values():
            legs.append(
                {
                    "symbol": leg.symbol,
                    "side": "SELL",
                    "qty": leg.entry_qty or leg.quantity,
                    "entry_price": round(leg.entry_price, 2),
                    "ltp": round(leg.ltp, 2),
                    "stop_price": leg.stop_price,
                    "stop_order_id": leg.stop_order_id,
                    "stop_status": leg.stop_status,
                    "status": leg.status,
                }
            )
        return {
            "in_position": True,
            "expiry": pos.expiry.isoformat(),
            "strike": pos.strike,
            "lots": pos.lots,
            "legs": legs,
            "entry_credit": round(pos.entry_credit, 2),
            "combined_ltp": round(pos.combined_premium(), 2),
            "stop_level": round(pos.stop_level, 2) if pos.stop_level is not None else None,
            "target_level": round(pos.target_level, 2) if pos.target_level is not None else None,
            "pnl": round(pos.pnl_gross(), 2),
            "entered_at": _iso(pos.entered_at),
            "square_off_at": _iso(self.window.square_off) if self.window else None,
        }

    def status(self) -> dict[str, Any]:
        pos = self.position
        return {
            "state": self.state.value,
            "halt_reason": self.halt_reason,
            "trading_date": self.trading_date.isoformat() if self.trading_date else None,
            "entries_today": self.entries_today,
            "straddles_closed": len(self.closed),
            "stop_hits": self.stop_hits,
            "target_hits": self.target_hits,
            "time_exits": self.time_exits,
            "early_exits": self.early_exits,
            "leg_stops": self.leg_stops_today,
            "vetoes": self.vetoes,
            "realized_day": round(self.realized_day, 2),
            "day_pnl": self.day_pnl(),
            "last_exit_at": _iso(self.last_exit_at),
            "locked": pos.locked if pos else False,
            "lock_level": round(pos.lock_level, 2) if pos and pos.lock_level is not None else None,
            "straddle_number": pos.number if pos else None,
            "pending_intent": self._pending.intent_id if self._pending else None,
        }

    def to_trace_step(
        self,
        step: EngineStep,
        i: int,
        *,
        index: float | None = None,
        vix: float | None = None,
        premium: float | None = None,
        days_to_expiry: float | None = None,
        stimulus_hash: str | None = None,
        stimulus_png: str | None = None,
        rates_hz: dict[str, float] | None = None,
        fixed_decoder: dict[str, Any] | None = None,
        compute_seconds: float = 0.0,
        technical: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The docs/api-spec.md step shape. The caller fills the neural fields."""
        obs = step.observation
        if index is None and obs is not None and obs.index_bars:
            index = obs.index_bars[-1].close
        if vix is None and obs is not None:
            vix = obs.vix
        if premium is None and step.quote is not None:
            premium = round(step.quote.combined_ltp, 2)
        if days_to_expiry is None and obs is not None:
            days_to_expiry = obs.days_to_expiry
        pred = step.prediction
        prediction = None
        if pred is not None:
            prediction = {
                "realized_over_implied": round(pred.realized_over_implied, 4),
                "confidence": round(pred.confidence, 4),
                "decision": pred.decision.value,
                "tau": self.tau,
            }
        tech = dict(step.technical)
        if technical:
            tech.update(technical)
        return {
            "i": i,
            "t": step.at.isoformat(),
            "trigger": step.trigger,
            "index": index,
            "vix": vix,
            "premium": premium,
            "days_to_expiry": days_to_expiry,
            "stimulus_hash": stimulus_hash,
            "stimulus_png": stimulus_png,
            "rates_hz": rates_hz or {},
            "fixed_decoder": fixed_decoder or {},
            "prediction": prediction,
            "guard": step.guard.to_dict() if step.guard else None,
            "action": step.action,
            "straddle": step.straddle,
            "fills": [fill_summary(f) for f in step.fills],
            "pnl_day": step.pnl_day,
            "compute_seconds": compute_seconds,
            "narrative": step.narrative,
            "technical": tech,
        }

    # ----------------------------------------------------- step finishing

    def _finish_step(self, step: EngineStep) -> None:
        step.straddle = self.state_snapshot()
        step.pnl_day = self.day_pnl()
        step.technical = self._technical(step)
        step.narrative = self._narrative(step)
        self.last_step = step

    def _technical(self, step: EngineStep) -> dict[str, Any]:
        pos = self.position
        pred = step.prediction
        tech: dict[str, Any] = {
            "state": self.state.value,
            "trigger": step.trigger,
            "detail": step.detail,
            "prediction": None
            if pred is None
            else {
                "realized_over_implied": pred.realized_over_implied,
                "confidence": pred.confidence,
                "decision": pred.decision.value,
                "details": dict(pred.details),
            },
            "thresholds": {
                "tau": self.tau,
                "stop_pct": self.stop_pct,
                "target_pct": self.target_pct,
                "lock_after_pct": self.lock_after_pct,
                "combined_stop_enabled": self.combined_stop_enabled,
                "leg_stop_pct": self.leg_stop_pct,
                "leg_stop_mode": self.leg_stop_mode,
                "on_leg_stop": self.on_leg_stop_policy,
                "square_off": _iso(self.window.square_off) if self.window else None,
                "square_off_deadline": _iso(self.deadline),
                "last_entry": _iso(self.window.last_entry) if self.window else None,
                "reentry_cooldown_minutes": self.strategy.get("reentry_cooldown_minutes", 5),
                "max_entries_per_day": self.strategy.get("max_entries_per_day", 10),
            },
            "counters": {
                "entries_today": self.entries_today,
                "straddles_closed": len(self.closed),
                "stop_hits": self.stop_hits,
                "target_hits": self.target_hits,
                "time_exits": self.time_exits,
                "early_exits": self.early_exits,
                "leg_stops": self.leg_stops_today,
                "vetoes": self.vetoes,
                "last_exit_at": _iso(self.last_exit_at),
            },
            "intents": [intent_summary(i) for i in step.intents],
        }
        if step.quote is not None:
            q = step.quote
            tech["quote"] = {
                "strike": q.strike,
                "expiry": q.expiry.isoformat(),
                "call": {"symbol": q.call.symbol, "ltp": q.call.ltp, "bid": q.call.bid, "ask": q.call.ask},
                "put": {"symbol": q.put.symbol, "ltp": q.put.ltp, "bid": q.put.bid, "ask": q.put.ask},
                "combined_ltp": round(q.combined_ltp, 2),
                "synthetic_forward": round(q.synthetic_forward, 2),
                "implied_move_points": round(q.combined_ltp, 2),
            }
            if pred is not None:
                tech["quote"]["predicted_move_points"] = round(q.combined_ltp * pred.realized_over_implied, 2)
        if step.sizing is not None:
            tech["lots"] = step.sizing
        if pos is not None:
            tech["levels"] = {
                "straddle_number": pos.number,
                "strike": pos.strike,
                "lots": pos.lots,
                "quoted_credit": round(pos.quoted_credit, 2),
                "entry_credit": round(pos.entry_credit, 2),
                "combined_premium": round(pos.combined_premium(), 2),
                "stop_level": round(pos.stop_level, 2) if pos.stop_level is not None else None,
                "target_level": round(pos.target_level, 2) if pos.target_level is not None else None,
                "lock_level": round(pos.lock_level, 2) if pos.lock_level is not None else None,
                "locked": pos.locked,
                "leg_stops": {leg.symbol: {"price": leg.stop_price, "status": leg.stop_status, "order_id": leg.stop_order_id} for leg in pos.legs.values()},
                "gross": round(pos.gross, 2),
                "costs": round(pos.costs, 2),
            }
            tech["costs"] = {
                "trade_costs": round(pos.costs, 2),
                "estimated_round_trip": round_trip_inr(self.cost_model, pos.entry_credit or pos.quoted_credit, pos.lots, self.lot_size),
            }
        if step.closed is not None:
            tech["closed"] = step.closed
        if step.notes:
            tech["notes"] = list(step.notes)
        return tech

    # ---------------------------------------------------------- narrative

    def _readout_sentence(self, pred: Prediction, quote: StraddleQuote | None) -> str:
        pct = pred.realized_over_implied * 100.0
        if quote is not None:
            return f"The readout expects {pct:.0f} percent of the movement the {_strike_label(quote.strike)} straddle is pricing."
        return f"The readout expects {pct:.0f} percent of the movement the straddle is pricing."

    def _levels_sentence(self, pos: Position) -> str:
        parts = []
        if pos.stop_level is not None:
            parts.append(f"Stop {pos.stop_level:.1f}")
        else:
            parts.append("No combined stop")
        if pos.target_level is not None:
            parts.append(f"target {pos.target_level:.1f}")
        if pos.lock_level is not None:
            parts.append(f"lock after {pos.lock_level:.1f}")
        if self.window is not None:
            parts.append(f"hard exit {hhmm(self.window.square_off)}")
        text = ", ".join(parts) + "."
        stops = [leg for leg in pos.legs.values() if leg.stop_price is not None]
        if stops:
            where = "software" if self.leg_stop_mode == "software" else "at the broker"
            legs = ", ".join(f"{leg.word} {leg.stop_price:.2f}" for leg in stops)
            text += f" Leg stops: {legs} ({self.leg_stop_pct:g} percent, {where})."
        return text

    def _result_sentence(self, record: dict[str, Any]) -> str:
        return f"Result {_inr(record['net'])} after {_inr(record['costs'])} costs. Day P&L {_inr(self.day_pnl())}."

    def _narrative(self, step: EngineStep) -> str:
        parts: list[str] = []
        obs = step.observation
        pos = self.position
        if step.trigger == "observation":
            parts.append(f"{hhmm(step.at)}.")
            if obs is not None:
                head = []
                if obs.index_bars:
                    head.append(f"NIFTY {obs.index_bars[-1].close:,.0f}")
                head.append(f"INDIAVIX {obs.vix:.1f}")
                parts.append(", ".join(head) + ".")
            if step.prediction is not None:
                parts.append(self._readout_sentence(step.prediction, step.quote))
        else:
            parts.append(f"{_hms(step.at)}.")

        action = step.action
        if self.state == State.HALTED:
            parts.append(f"Halted: {self.halt_reason}.")
            if step.closed is not None:
                parts.append(self._result_sentence(step.closed))
            return " ".join(parts)

        if action in (Action.ENTER.value, Action.REENTRY.value):
            if step.guard is not None:
                parts.append(step.guard.summary())
            if pos is not None and pos.entered_at is not None:
                prefix = ""
                if pos.number > 1:
                    prefix = f"Straddle {pos.number} of the day"
                    if self.last_closed is not None:
                        prefix += f", re-entry after the {self.last_closed['action'].lower().replace('_', ' ')} at {hhmm(self.last_exit_at)}"
                    prefix += ": sold"
                else:
                    prefix = "Sold"
                qty = pos.quantity()
                parts.append(
                    f"{prefix} {_lots_text(pos.lots)} of the {_expiry_label(pos.expiry)} {_strike_label(pos.strike)} straddle "
                    f"for {pos.entry_credit:.1f} points credit ({_inr(pos.entry_credit * qty)})."
                )
                parts.append(self._levels_sentence(pos))
            elif step.entry_failed:
                parts.append(f"The entry basket was rejected ({step.detail}). Still flat.")
            elif pos is not None:
                parts.append(
                    f"Entry basket sent for {_lots_text(pos.lots)} of the {_strike_label(pos.strike)} straddle at about {pos.quoted_credit:.1f} points."
                )
                if step.detail:
                    parts.append(step.detail.capitalize() + ".")
            for note in step.notes:
                parts.append(note.capitalize() + ".")
        elif action == Action.VETO.value:
            if step.guard is not None:
                parts.append(step.guard.summary())
            parts.append("Staying in the position." if self.in_position else "Still flat.")
        elif action == Action.HOLD.value:
            if self.in_position and pos is not None:
                parts.append(self._holding_sentence(pos))
            elif step.prediction is None:
                parts.append("No prediction. Flat.")
            elif step.prediction.decision == Decision.ENTER:
                parts.append("Waiting for the broker. " + (step.detail.capitalize() + "." if step.detail else ""))
            elif step.prediction.decision == Decision.EXIT:
                parts.append("The readout says exit, but there is no position. Flat.")
            else:
                parts.append("Not calm enough to sell a straddle. Flat.")
        elif action == Action.LOCK.value:
            if pos is not None:
                parts.append(
                    f"Combined premium {pos.combined_premium():.1f} has fallen {self.lock_after_pct:g} percent below the {pos.entry_credit:.1f} credit. "
                    f"Stop moved to the entry credit {pos.entry_credit:.1f}."
                )
        elif action in (Action.STOP.value, Action.TARGET.value, Action.SQUARE_OFF.value, Action.EXIT.value, Action.STOP_LEG.value):
            parts.append(self._exit_narrative(step))
        else:  # NONE
            if self.in_position and pos is not None:
                combined = pos.combined_premium()
                stop = f"stop {pos.stop_level:.1f}" if pos.stop_level is not None else "no combined stop"
                target = f"target {pos.target_level:.1f}" if pos.target_level is not None else "no target"
                parts.append(f"Holding: combined premium {combined:.1f} against {stop} and {target}.")
            elif step.detail:
                parts.append(step.detail.capitalize() + ".")
            else:
                parts.append("Flat.")
        return " ".join(p for p in parts if p)

    def _holding_sentence(self, pos: Position) -> str:
        combined = pos.combined_premium()
        since = hhmm(pos.entered_at)
        text = (
            f"Holding straddle {pos.number} ({_strike_label(pos.strike)}) since {since}: combined premium {combined:.1f} "
            f"against the {pos.entry_credit:.1f} credit, open P&L {_inr(pos.pnl_gross())}."
        )
        stopped = pos.closed_legs()
        if stopped:
            for leg in stopped:
                text += f" The {leg.word} was stopped at {leg.exit_price:.2f}." if leg.exit_price else f" The {leg.word} is closed."
            for leg in pos.open_legs():
                if leg.stop_price is not None:
                    text += f" The {leg.word} runs with its own stop at {leg.stop_price:.2f}."
        else:
            stop = f"Stop {pos.stop_level:.1f}" if pos.stop_level is not None else "No combined stop"
            target = f"target {pos.target_level:.1f}" if pos.target_level is not None else "no target"
            text += f" {stop}, {target}."
        return text

    def _exit_narrative(self, step: EngineStep) -> str:
        action = step.action
        pos = self.position
        record = step.closed
        strike = _strike_label(record["strike"] if record else (pos.strike if pos else 0.0))
        parts: list[str] = []
        # what triggered it
        if step.trigger == "broker" and action == Action.STOP_LEG.value:
            parts.append(step.detail.capitalize() + "." if step.detail else "A leg stop triggered at the broker.")
            buys = [f for f in step.fills if f.side == Side.BUY]
            if buys:
                parts.append(f"Filled at {buys[0].average_price:.2f} for {buys[0].quantity} units.")
            if any(i.kind == KIND_EXIT for i in step.intents):
                parts.append("Exiting the remaining leg as well (on_leg_stop exit_both).")
                for f in buys[1:]:
                    word = "call" if f.symbol.endswith("CE") else "put"
                    parts.append(f"Bought back the {word} for {f.average_price:.2f}.")
            elif record is None and pos is not None:
                remaining = pos.open_legs()
                if remaining:
                    leg = remaining[0]
                    if leg.stop_price is not None:
                        parts.append(f"The {leg.word} stays open with its own fixed stop at {leg.stop_price:.2f}.")
                    else:
                        parts.append(f"The {leg.word} stays open.")
        else:
            if action == Action.STOP.value:
                parts.append(step.detail.capitalize() + "." if step.detail else "The combined stop was hit.")
            elif action == Action.TARGET.value:
                parts.append(step.detail.capitalize() + "." if step.detail else "The combined target was reached.")
            elif action == Action.SQUARE_OFF.value:
                parts.append(f"Square-off time ({_hms(self.deadline)})." if self.deadline else "Square-off.")
            elif action == Action.STOP_LEG.value:
                parts.append(step.detail.capitalize() + "." if step.detail else "A leg stop was hit.")
            elif action == Action.EXIT.value:
                pred = step.prediction
                if pred is not None:
                    parts.append(f"The readout now expects {pred.realized_over_implied * 100:.0f} percent of the priced movement, so the straddle is closed early.")
                else:
                    parts.append(step.detail.capitalize() + "." if step.detail else "Closing the straddle.")
            buy = [f for f in step.fills if f.side == Side.BUY]
            if record is not None:
                parts.append(f"Bought back {_lots_text(record['lots'])} of the {strike} straddle for {record['exit_debit']:.1f} points.")
            elif buy:
                legs = ", ".join(f"{f.symbol[-2:]} at {f.average_price:.2f}" for f in buy)
                parts.append(f"Bought back {legs}.")
                if pos is not None and self.state == State.EXITING:
                    parts.append("Exiting the remaining leg as well (on_leg_stop exit_both)." if pos.exit_action == Action.EXIT.value else "Repairing the pair.")
                elif pos is not None:
                    remaining = pos.open_legs()
                    if remaining and remaining[0].stop_price is not None:
                        parts.append(f"The {remaining[0].word} stays open with its own fixed stop at {remaining[0].stop_price:.2f}.")
            elif step.intents and not step.fills:
                if self.state == State.IN_POSITION:
                    parts.append(f"The exit order was rejected ({step.detail}); still in the position, retrying on the next tick.")
                else:
                    parts.append("Exit basket sent.")
        if record is not None:
            parts.append(self._result_sentence(record))
        for note in step.notes:
            parts.append(note.capitalize() + ".")
        return " ".join(parts)
