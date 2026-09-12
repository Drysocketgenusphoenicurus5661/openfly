"""The one place where intents meet the ledger, the broker and the engine.

`execute_step` runs every intent of an engine step in order, including the
intents the engine appends while executing (resting stops after an entry, a
repair after a one-legged fill), and reports each outcome back to the engine.
`apply_broker_events` feeds reconciliation and stop-order events back in.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from openfly.execution.ledger import Ledger
from openfly.execution.types import (
    EXIT_KINDS,
    KIND_REPAIR,
    KIND_STOPS,
    BrokerError,
    LostResponse,
    OneLegged,
    PendingIntentError,
    QuoteLookup,
    classify_fills,
)
from openfly.interfaces import Fill, Intent, IntentStatus, Side, UnresolvedOrder

logger = logging.getLogger("openfly.execution")


def _halt(engine: Any, ledger: Ledger | None, reason: str) -> None:
    if ledger is not None:
        ledger.halt(reason)
    engine.halt(reason)


def execute_step(engine: Any, step: Any, broker: Any, ledger: Ledger | None, quote_lookup: QuoteLookup) -> Any:
    """Execute the intents of one engine step; returns the same step, now carrying fills."""
    i = 0
    while i < len(step.intents):
        intent = step.intents[i]
        i += 1
        execute_intent(engine, step, broker, ledger, intent, quote_lookup)
        if engine.is_halted:
            break
    return step


def ledger_disagreement(ledger: Ledger, intent: Intent) -> str | None:
    """Why a closing intent does not match the ledger's open legs, or None when it does.

    Every BUY leg of an exit, square-off, leg exit or repair must close a contract
    the ledger shows short in at least that quantity. Opening (SELL) legs are not
    checked here; the engine sizes them from the latest quote on purpose.
    """
    if intent.kind not in EXIT_KINDS and intent.kind != KIND_REPAIR:
        return None
    open_legs = ledger.open_legs()
    problems = []
    for leg in intent.legs:
        if leg.side != Side.BUY:
            continue
        short = -open_legs.get(leg.contract.symbol, 0)
        if short < leg.quantity:
            problems.append(f"BUY {leg.quantity} {leg.contract.symbol} but the ledger shows {short} short")
    return "; ".join(problems) or None


def execute_intent(engine: Any, step: Any, broker: Any, ledger: Ledger | None, intent: Intent, quote_lookup: QuoteLookup) -> list[Fill]:
    if ledger is not None:
        problem = ledger_disagreement(ledger, intent)
        if problem is not None:
            reason = f"exit legs do not match the ledger's open legs: {problem}"
            engine.on_execution(intent.intent_id, [], IntentStatus.REJECTED, reason)
            _halt(engine, ledger, reason)
            return []
        try:
            ledger.reserve(intent)
        except PendingIntentError as exc:
            engine.on_execution(intent.intent_id, [], IntentStatus.REJECTED, f"ledger refused the intent: {exc}")
            return []
    try:
        fills = broker.execute(intent, quote_lookup)
    except OneLegged as exc:
        if ledger is not None:
            ledger.settle(intent.intent_id, [f for f in exc.fills if f.intent_id == intent.intent_id], IntentStatus.PARTIAL)
        engine.on_execution(intent.intent_id, exc.fills, IntentStatus.PARTIAL, f"one-legged: {exc.detail}")
        repair = engine.repair_intent(exc.detail)
        if repair is not None:
            step.intents.append(repair)
        elif not engine.is_flat_book:
            _halt(engine, ledger, f"one-legged outcome with no repair available: {exc.detail}")
        return exc.fills
    except LostResponse as exc:
        logger.warning("%s", exc)
        try:
            events = broker.reconcile()
        except UnresolvedOrder as unresolved:
            _halt(engine, ledger, f"unresolved order after lost response: {unresolved}")
            raise
        apply_broker_events(engine, broker, events, step.at, step=step)
        return [f for e in events for f in e.get("fills", [])]
    except UnresolvedOrder as exc:
        _halt(engine, ledger, f"unresolved order: {exc}")
        raise
    except BrokerError as exc:
        if ledger is not None:
            ledger.settle(intent.intent_id, [], IntentStatus.REJECTED)
            ledger.mark(intent.intent_id, IntentStatus.REJECTED, str(exc))
        engine.on_execution(intent.intent_id, [], IntentStatus.REJECTED, str(exc))
        return []

    if intent.kind == KIND_STOPS:
        if ledger is not None and ledger.status(intent.intent_id) in (IntentStatus.PREPARED, IntentStatus.UNKNOWN):
            ledger.settle(intent.intent_id, [], IntentStatus.SETTLED)
        engine.on_execution(intent.intent_id, [], IntentStatus.SETTLED, stop_orders=broker.stop_orders())
        return []

    status = classify_fills(intent, fills)
    if ledger is not None:
        ledger.settle(intent.intent_id, [f for f in fills if f.intent_id == intent.intent_id], status)
    engine.on_execution(intent.intent_id, fills, status)
    return fills


def apply_broker_events(engine: Any, broker: Any, events: list[dict], now: datetime, step: Any | None = None) -> list[Any]:
    """Feed reconcile() or ReplayBroker.on_quote() events into the engine. Returns the steps produced."""
    steps: list[Any] = []
    refresh_stops = False
    for event in events:
        kind = event.get("type")
        if kind == "intent":
            status = event.get("status")
            engine.on_execution(
                event["intent_id"],
                list(event.get("fills", [])),
                IntentStatus(status) if status else None,
                event.get("detail", ""),
            )
            if event.get("one_legged"):
                repair = engine.repair_intent(event.get("detail", ""))
                if repair is not None and step is not None:
                    step.intents.append(repair)
        elif kind == "stop_triggered":
            steps.append(engine.on_leg_stop(event["symbol"], [event["fill"]], now))
        elif kind in ("stop_replaced", "stop_missing"):
            refresh_stops = True
    if refresh_stops:
        engine.on_stops_placed(broker.stop_orders())
    return steps
