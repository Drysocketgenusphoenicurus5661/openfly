"""Intent-before-send ledger in SQLite.

One ledger per run directory. Every intent is written as PREPARED before any
network call, marked UNKNOWN while the call is in flight, and settled with
its fills afterwards. Only one intent may be pending at a time. Resting stop
orders (per-leg SL-M) have their own table so the broker can maintain them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openfly.execution.costs import order_cost_inr
from openfly.execution.types import KIND_STOPS, PendingIntentError, StopLeg
from openfly.interfaces import Contract, Fill, Intent, IntentStatus, Leg, Side

IST = ZoneInfo("Asia/Kolkata")

PENDING_STATUSES = (IntentStatus.PREPARED.value, IntentStatus.UNKNOWN.value, IntentStatus.ACCEPTED.value)

STOP_PENDING = "pending"
STOP_TRIGGERED = "triggered"
STOP_CANCELLED = "cancelled"
STOP_REJECTED = "rejected"
STOP_REPLACED = "replaced"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    limit_price REAL,
    order_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'prepared',
    filled_qty INTEGER NOT NULL DEFAULT 0,
    average_price REAL NOT NULL DEFAULT 0,
    UNIQUE(intent_id, symbol, side)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    average_price REAL NOT NULL,
    order_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    cost REAL NOT NULL DEFAULT 0,
    UNIQUE(intent_id, symbol, side, order_id)
);
CREATE TABLE IF NOT EXISTS stop_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'NFO',
    side TEXT NOT NULL DEFAULT 'BUY',
    quantity INTEGER NOT NULL,
    trigger_price REAL NOT NULL,
    order_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    average_price REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), IST).isoformat()


def leg_to_payload(leg: Leg) -> dict[str, Any]:
    c = leg.contract
    out = {
        "symbol": c.symbol,
        "exchange": c.exchange,
        "underlying": c.underlying,
        "expiry": c.expiry.isoformat(),
        "strike": c.strike,
        "option_type": c.option_type,
        "lot_size": c.lot_size,
        "tick_size": c.tick_size,
        "freeze_qty": c.freeze_qty,
        "side": leg.side.value,
        "quantity": leg.quantity,
        "limit_price": leg.limit_price,
    }
    if isinstance(leg, StopLeg):
        out["trigger_price"] = leg.trigger_price
    return out


def leg_from_payload(item: dict[str, Any]) -> Leg:
    contract = Contract(
        symbol=item["symbol"],
        exchange=item["exchange"],
        underlying=item.get("underlying", ""),
        expiry=date.fromisoformat(item["expiry"]),
        strike=float(item["strike"]),
        option_type=item.get("option_type", ""),
        lot_size=int(item.get("lot_size", 0)),
        tick_size=float(item.get("tick_size", 0.05)),
        freeze_qty=int(item.get("freeze_qty", 0)),
    )
    side = Side(item["side"])
    if "trigger_price" in item:
        return StopLeg(contract, side, int(item["quantity"]), item.get("limit_price"), float(item["trigger_price"]))
    return Leg(contract, side, int(item["quantity"]), item.get("limit_price"))


def intent_to_payload(intent: Intent) -> dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "kind": intent.kind,
        "reason": intent.reason,
        "created_at": intent.created_at,
        "observation_hash": intent.observation_hash,
        "legs": [leg_to_payload(leg) for leg in intent.legs],
    }


def intent_from_payload(payload: dict[str, Any]) -> Intent:
    return Intent(
        intent_id=payload["intent_id"],
        kind=payload["kind"],
        legs=tuple(leg_from_payload(item) for item in payload["legs"]),
        reason=payload.get("reason", ""),
        created_at=float(payload.get("created_at", 0.0)),
        observation_hash=payload.get("observation_hash"),
    )


class Ledger:
    """SQLite ledger (WAL, synchronous FULL) for one run directory."""

    def __init__(self, run_dir: str | Path, cost_model=None, filename: str = "ledger.db"):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / filename
        self.cost_model = cost_model
        self._lock = threading.RLock()
        self._con = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.execute("PRAGMA busy_timeout=30000")
        self._con.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, default=str)),
            )

    def trading_date(self) -> date | None:
        value = self.get_meta("trading_date")
        return date.fromisoformat(value) if value else None

    def set_trading_date(self, day: date) -> None:
        """Start a trading day. A new date resets the per-day counters."""
        if self.trading_date() != day:
            self.set_meta("trading_date", day.isoformat())
            self.set_meta("entries_today", 0)
            self.set_meta("day_pnl", 0.0)

    def entries_today(self) -> int:
        return int(self.get_meta("entries_today", 0))

    def increment_entries(self) -> int:
        value = self.entries_today() + 1
        self.set_meta("entries_today", value)
        return value

    def halt(self, reason: str) -> None:
        self.set_meta("halted", {"reason": reason, "at": time.time(), "at_iso": _iso(time.time())})

    def halted(self) -> dict | None:
        return self.get_meta("halted")

    def clear_halt(self) -> None:
        with self._lock:
            self._con.execute("DELETE FROM meta WHERE key = 'halted'")

    def save_checkpoint(self, state: dict) -> None:
        self.set_meta("checkpoint", state)

    def load_checkpoint(self) -> dict | None:
        return self.get_meta("checkpoint")

    # --------------------------------------------------------------- intents

    def reserve(self, intent: Intent) -> None:
        """Insert the intent as PREPARED. Refuses when another intent is still pending."""
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                existing = self._con.execute("SELECT status FROM intents WHERE intent_id = ?", (intent.intent_id,)).fetchone()
                if existing is not None:
                    raise PendingIntentError(f"intent {intent.intent_id} already recorded with status {existing['status']}")
                pending = self._con.execute(
                    "SELECT intent_id, status FROM intents WHERE status IN (?, ?, ?)", PENDING_STATUSES
                ).fetchall()
                if pending:
                    other = pending[0]
                    raise PendingIntentError(f"intent {other['intent_id']} is still {other['status']}")
                now = time.time()
                self._con.execute(
                    "INSERT INTO intents(intent_id, kind, status, created_at, updated_at, reason, detail, payload) VALUES(?, ?, ?, ?, ?, ?, '', ?)",
                    (
                        intent.intent_id,
                        intent.kind,
                        IntentStatus.PREPARED.value,
                        intent.created_at,
                        now,
                        intent.reason,
                        json.dumps(intent_to_payload(intent)),
                    ),
                )
                for leg in intent.legs:
                    self._con.execute(
                        "INSERT INTO legs(intent_id, symbol, side, quantity, limit_price, status) VALUES(?, ?, ?, ?, ?, 'prepared')",
                        (intent.intent_id, leg.contract.symbol, leg.side.value, leg.quantity, leg.limit_price),
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def mark(self, intent_id: str, status: IntentStatus | str, detail: str | None = None) -> None:
        value = status.value if isinstance(status, IntentStatus) else str(status)
        with self._lock:
            if detail is None:
                self._con.execute("UPDATE intents SET status = ?, updated_at = ? WHERE intent_id = ?", (value, time.time(), intent_id))
            else:
                self._con.execute(
                    "UPDATE intents SET status = ?, updated_at = ?, detail = ? WHERE intent_id = ?",
                    (value, time.time(), detail, intent_id),
                )

    def status(self, intent_id: str) -> IntentStatus | None:
        with self._lock:
            row = self._con.execute("SELECT status FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        return IntentStatus(row["status"]) if row else None

    def record_leg_order(
        self,
        intent_id: str,
        symbol: str,
        side: Side | str,
        order_id: str,
        status: str,
        filled_qty: int = 0,
        average_price: float = 0.0,
    ) -> None:
        side_value = side.value if isinstance(side, Side) else str(side)
        with self._lock:
            self._con.execute(
                "UPDATE legs SET order_id = ?, status = ?, filled_qty = ?, average_price = ? WHERE intent_id = ? AND symbol = ? AND side = ?",
                (order_id or "", status, int(filled_qty), float(average_price), intent_id, symbol, side_value),
            )

    def settle(self, intent_id: str, fills: Sequence[Fill], status: IntentStatus | str | None = None) -> IntentStatus:
        """Record fills (idempotent) and set the final status.

        Without an explicit status: SETTLED when every leg is fully filled,
        REJECTED when nothing filled, PARTIAL otherwise.
        """
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                for fill in fills:
                    cost = order_cost_inr(self.cost_model, fill.side, fill.average_price, fill.quantity)
                    self._con.execute(
                        "INSERT OR IGNORE INTO fills(intent_id, symbol, side, quantity, average_price, order_id, timestamp, cost) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (intent_id, fill.symbol, fill.side.value, int(fill.quantity), float(fill.average_price), fill.order_id, float(fill.timestamp), cost),
                    )
                    self._con.execute(
                        "UPDATE legs SET order_id = CASE WHEN order_id = '' THEN ? ELSE order_id END, filled_qty = ?, average_price = ?, status = ? "
                        "WHERE intent_id = ? AND symbol = ? AND side = ?",
                        (
                            fill.order_id,
                            int(fill.quantity),
                            float(fill.average_price),
                            "complete",
                            intent_id,
                            fill.symbol,
                            fill.side.value,
                        ),
                    )
                legs = self._con.execute("SELECT quantity, filled_qty FROM legs WHERE intent_id = ?", (intent_id,)).fetchall()
                if status is None:
                    if legs and all(r["filled_qty"] >= r["quantity"] for r in legs):
                        final = IntentStatus.SETTLED
                    elif all(r["filled_qty"] == 0 for r in legs):
                        final = IntentStatus.REJECTED
                    else:
                        final = IntentStatus.PARTIAL
                else:
                    final = status if isinstance(status, IntentStatus) else IntentStatus(str(status))
                self._con.execute(
                    "UPDATE intents SET status = ?, updated_at = ? WHERE intent_id = ?", (final.value, time.time(), intent_id)
                )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
        return final

    def add_fills(self, intent_id: str, fills: Sequence[Fill]) -> None:
        """Record fills without changing the intent status (used for triggered stop orders)."""
        with self._lock:
            for fill in fills:
                cost = 0.0
                if self.cost_model is not None:
                    cost = float(self.cost_model.order_cost(fill.side, fill.quantity, fill.average_price))
                self._con.execute(
                    "INSERT OR IGNORE INTO fills(intent_id, symbol, side, quantity, average_price, order_id, timestamp, cost) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (intent_id, fill.symbol, fill.side.value, int(fill.quantity), float(fill.average_price), fill.order_id, float(fill.timestamp), cost),
                )

    def _intent_row(self, row: sqlite3.Row) -> dict[str, Any]:
        legs = self._con.execute(
            "SELECT symbol, side, quantity, limit_price, order_id, status, filled_qty, average_price FROM legs WHERE intent_id = ? ORDER BY id",
            (row["intent_id"],),
        ).fetchall()
        return {
            "intent_id": row["intent_id"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": _iso(row["created_at"]),
            "created_at_epoch": row["created_at"],
            "updated_at": _iso(row["updated_at"]),
            "reason": row["reason"],
            "detail": row["detail"],
            "payload": json.loads(row["payload"]),
            "legs": [
                {
                    "symbol": leg["symbol"],
                    "side": leg["side"],
                    "quantity": leg["quantity"],
                    "limit_price": leg["limit_price"],
                    "order_id": leg["order_id"] or None,
                    "status": leg["status"],
                    "filled_qty": leg["filled_qty"],
                    "average_price": leg["average_price"],
                }
                for leg in legs
            ],
        }

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            return self._intent_row(row) if row else None

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM intents WHERE status IN (?, ?, ?) ORDER BY created_at", PENDING_STATUSES
            ).fetchall()
            return [self._intent_row(r) for r in rows]

    def intents(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent intents first, in the GET /api/ledger/intents shape."""
        with self._lock:
            rows = self._con.execute("SELECT * FROM intents ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),)).fetchall()
            out = []
            for r in rows:
                item = self._intent_row(r)
                item.pop("payload", None)
                out.append(item)
            return out

    def fills(self, intent_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if intent_id is None:
                rows = self._con.execute("SELECT * FROM fills ORDER BY id").fetchall()
            else:
                rows = self._con.execute("SELECT * FROM fills WHERE intent_id = ? ORDER BY id", (intent_id,)).fetchall()
        return [dict(r) for r in rows]

    def open_legs(self) -> dict[str, int]:
        """Net quantity per symbol from every recorded fill (BUY positive, SELL negative), non-zero only.

        This is the position truth every exit is checked against: an exit must
        close exactly these contracts and quantities, never a freshly computed ATM.
        """
        with self._lock:
            rows = self._con.execute(
                "SELECT symbol, SUM(CASE WHEN side = 'BUY' THEN quantity ELSE -quantity END) AS net FROM fills GROUP BY symbol"
            ).fetchall()
        return {r["symbol"]: int(r["net"]) for r in rows if int(r["net"]) != 0}

    def day_pnl(self) -> float:
        """Realized P&L from the fills table: sold value minus bought value minus booked costs."""
        with self._lock:
            row = self._con.execute(
                "SELECT COALESCE(SUM(CASE WHEN side = 'SELL' THEN quantity * average_price ELSE -quantity * average_price END), 0) AS gross, "
                "COALESCE(SUM(cost), 0) AS cost FROM fills"
            ).fetchone()
        return round(float(row["gross"]) - float(row["cost"]), 2)

    # ----------------------------------------------------------- stop orders

    def record_stop_order(
        self,
        intent_id: str,
        symbol: str,
        quantity: int,
        trigger_price: float,
        order_id: str,
        status: str = STOP_PENDING,
        exchange: str = "NFO",
    ) -> int:
        now = time.time()
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO stop_orders(intent_id, symbol, exchange, side, quantity, trigger_price, order_id, status, created_at, updated_at) "
                "VALUES(?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?)",
                (intent_id, symbol, exchange, int(quantity), float(trigger_price), order_id or "", status, now, now),
            )
            return int(cur.lastrowid)

    def update_stop_order(self, order_id: str, status: str, filled_qty: int | None = None, average_price: float | None = None) -> None:
        with self._lock:
            if filled_qty is None:
                self._con.execute(
                    "UPDATE stop_orders SET status = ?, updated_at = ? WHERE order_id = ?", (status, time.time(), order_id)
                )
            else:
                self._con.execute(
                    "UPDATE stop_orders SET status = ?, filled_qty = ?, average_price = ?, updated_at = ? WHERE order_id = ?",
                    (status, int(filled_qty), float(average_price or 0.0), time.time(), order_id),
                )

    def stop_orders(self, active_only: bool = False, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM stop_orders"
        clauses = []
        params: list[Any] = []
        if active_only:
            clauses.append("status = ?")
            params.append(STOP_PENDING)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self._lock:
            rows = self._con.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def last_stop_for(self, symbol: str) -> dict[str, Any] | None:
        rows = self.stop_orders(symbol=symbol)
        return rows[-1] if rows else None

    # --------------------------------------------------------------- helpers

    @staticmethod
    def is_stops_intent(row: dict[str, Any]) -> bool:
        return row.get("kind") == KIND_STOPS
