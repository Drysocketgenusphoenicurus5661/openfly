"""Reject-only guard for straddle entries and exits.

The guard evaluates a list of named checks against a GuardContext and returns
a GuardResult. It never modifies or substitutes a proposal: it only says yes
or no, and every check carries a plain-language detail for traders.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from openfly.interfaces import Prediction, SessionWindow, StraddleQuote
from openfly.straddle.expiry import expiry_label, selection_of, trading_days_between, weekday_rule


@dataclass(frozen=True)
class GuardCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    checks: tuple[GuardCheck, ...]

    @property
    def failed(self) -> tuple[GuardCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def summary(self) -> str:
        if self.allowed:
            n = len(self.checks)
            return f"All {n} checks passed." if n != 1 else "The single check passed."
        return "The guard vetoed: " + "; ".join(c.detail for c in self.failed) + "."

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "checks": [c.to_dict() for c in self.checks]}


@dataclass
class GuardContext:
    """Facts the guard looks at. The engine fills in what it knows; the caller supplies the rest."""

    now: datetime | None = None
    window: SessionWindow | None = None
    is_trading_day: bool = True
    quote: StraddleQuote | None = None
    quote_age_s: float | None = None
    prediction: Prediction | None = None
    vix: float | None = None
    days_to_expiry: float | None = None
    observation_index: float | None = None
    index_now: float | None = None
    day_pnl: float = 0.0
    entries_today: int = 0
    last_exit_at: datetime | None = None
    in_position: bool = False
    lots: int = 1
    lots_detail: str = ""
    margin_available: float | None = None
    margin_per_lot: float | None = None
    stop_file_present: bool = False
    halted: bool = False
    halt_reason: str = ""
    pending_intent: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def hhmm(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "?"


def _hhmmss(value: datetime | None) -> str:
    return value.strftime("%H:%M:%S") if value else "?"


def _inr(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}INR {int(math.floor(abs(value) + 0.5)):,}"


class Guard:
    """Reject-only checks driven by the settings dict."""

    def __init__(self, settings: dict, is_trading_day: Callable[[date], bool] | None = None):
        self.settings = settings
        self.is_trading_day = is_trading_day or weekday_rule

    # ----------------------------------------------------------- settings

    @property
    def strategy(self) -> dict:
        return self.settings.get("strategy", {})

    @property
    def risk(self) -> dict:
        return self.settings.get("risk", {})

    # ------------------------------------------------------------- entry

    def check_entry(self, ctx: GuardContext) -> GuardResult:
        checks = (
            self.trading_day(ctx),
            self.trade_window(ctx),
            self.last_entry(ctx),
            self.expiry_min_dte(ctx),
            self.vix_ceiling(ctx),
            self.spread(ctx),
            self.quote_age(ctx),
            self.index_move(ctx),
            self.daily_loss_limit(ctx),
            self.entries_per_day(ctx),
            self.reentry_cooldown(ctx),
            self.position_flat(ctx),
            self.lots_bounds(ctx),
            self.margin_available(ctx),
            self.stop_file(ctx),
            self.halted(ctx),
            self.pending_intent(ctx),
            self.prediction_present(ctx),
        )
        return GuardResult(allowed=all(c.ok for c in checks), checks=checks)

    def check_exit(self, ctx: GuardContext) -> GuardResult:
        checks = (
            self.trading_day(ctx),
            self.halted(ctx),
            self.pending_intent(ctx),
            self.quote_present(ctx),
        )
        return GuardResult(allowed=all(c.ok for c in checks), checks=checks)

    # ------------------------------------------------------------ checks

    def trading_day(self, ctx: GuardContext) -> GuardCheck:
        if ctx.window is None:
            return GuardCheck("trading_day", False, "no session window: today is not a trading day")
        day = ctx.window.trading_date.strftime("%d-%b-%Y")
        if not ctx.is_trading_day:
            return GuardCheck("trading_day", False, f"{day} is not a trading day (weekend or holiday)")
        return GuardCheck("trading_day", True, f"{day} is a trading day")

    def trade_window(self, ctx: GuardContext) -> GuardCheck:
        if ctx.window is None or ctx.now is None:
            return GuardCheck("trade_window", False, "no session window or no clock")
        start, end = ctx.window.trade_start, ctx.window.last_entry
        inside = start <= ctx.now <= end
        word = "inside" if inside else "outside"
        return GuardCheck("trade_window", inside, f"{hhmm(ctx.now)} is {word} the trade window {hhmm(start)} to {hhmm(end)}")

    def last_entry(self, ctx: GuardContext) -> GuardCheck:
        if ctx.window is None or ctx.now is None:
            return GuardCheck("last_entry", False, "no session window or no clock")
        cutoff = ctx.window.last_entry
        if ctx.now <= cutoff:
            left = int((cutoff - ctx.now).total_seconds() // 60)
            return GuardCheck("last_entry", True, f"{hhmm(ctx.now)} is before the last entry time {hhmm(cutoff)}, {left} minutes left for new entries")
        return GuardCheck("last_entry", False, f"{hhmm(ctx.now)} is past the last entry time {hhmm(cutoff)}")

    def expiry_min_dte(self, ctx: GuardContext) -> GuardCheck:
        min_dte = int(self.strategy.get("min_days_to_expiry", 0) or 0)
        if ctx.quote is None or ctx.window is None:
            return GuardCheck("expiry_min_dte", min_dte <= 0, "no quote, expiry unknown" if min_dte > 0 else "expiry check not required (min_days_to_expiry 0)")
        expiry = ctx.quote.expiry
        today = ctx.window.trading_date
        days = trading_days_between(today, expiry, self.is_trading_day)
        label = f"the {expiry_label(expiry)} {selection_of(self.settings)} expiry"
        unit = "trading day" if days == 1 else "trading days"
        if min_dte <= 0:
            if expiry <= today:
                return GuardCheck("expiry_min_dte", True, f"today is expiry day for {label} and expiry-day trading is allowed (min_days_to_expiry 0)")
            return GuardCheck("expiry_min_dte", True, f"{label} is {days} {unit} away; no minimum required")
        if days >= min_dte:
            return GuardCheck("expiry_min_dte", True, f"{label} is {days} {unit} away, at least {min_dte} required")
        if expiry <= today:
            return GuardCheck("expiry_min_dte", False, f"today is expiry day for {label} and at least {min_dte} trading day{'s' if min_dte != 1 else ''} to expiry is required")
        return GuardCheck("expiry_min_dte", False, f"{label} is {days} {unit} away, at least {min_dte} required")

    def vix_ceiling(self, ctx: GuardContext) -> GuardCheck:
        ceiling = float(self.strategy.get("vix_ceiling", 20.0))
        if ctx.vix is None:
            return GuardCheck("vix_ceiling", False, f"INDIAVIX unavailable, ceiling {ceiling:g}")
        if ctx.vix <= ceiling:
            return GuardCheck("vix_ceiling", True, f"INDIAVIX {ctx.vix:.1f} is below the ceiling {ceiling:g}")
        return GuardCheck("vix_ceiling", False, f"INDIAVIX {ctx.vix:.1f} is above the ceiling {ceiling:g}")

    def spread(self, ctx: GuardContext) -> GuardCheck:
        limit = float(self.risk.get("spread_pct_max", 0.5))
        if ctx.quote is None:
            return GuardCheck("spread", False, "no straddle quote")
        premium = ctx.quote.combined_ltp
        if premium <= 0:
            return GuardCheck("spread", False, "combined premium is zero")
        parts = []
        ok = True
        for name, q in (("call", ctx.quote.call), ("put", ctx.quote.put)):
            if q.bid <= 0 or q.ask <= 0:
                parts.append(f"{name} has no depth, spread check skipped")
                continue
            width = q.ask - q.bid
            pct = width / premium * 100.0
            if pct > limit:
                ok = False
            parts.append(f"{name} spread {width:.2f} is {pct:.2f} percent of the {premium:.1f} premium")
        detail = "; ".join(parts) + f" (limit {limit:g} percent)"
        return GuardCheck("spread", ok, detail)

    def quote_age(self, ctx: GuardContext) -> GuardCheck:
        limit = float(self.risk.get("quote_max_age_s", 5.0))
        if ctx.quote_age_s is None:
            return GuardCheck("quote_age", True, "quote age not supplied (replay or synthetic quote), check skipped")
        if ctx.quote_age_s <= limit:
            return GuardCheck("quote_age", True, f"quote is {ctx.quote_age_s:.1f} seconds old, within {limit:g} seconds")
        return GuardCheck("quote_age", False, f"quote is {ctx.quote_age_s:.1f} seconds old, older than {limit:g} seconds")

    def index_move(self, ctx: GuardContext) -> GuardCheck:
        limit = float(self.risk.get("index_move_veto_pct", 0.3))
        if ctx.observation_index is None or ctx.index_now is None or ctx.observation_index <= 0:
            return GuardCheck("index_move", True, "index level at observation not supplied, check skipped")
        pct = abs(ctx.index_now - ctx.observation_index) / ctx.observation_index * 100.0
        text = f"NIFTY moved {pct:.2f} percent since the observation ({ctx.observation_index:,.1f} to {ctx.index_now:,.1f})"
        if pct <= limit:
            return GuardCheck("index_move", True, f"{text}, within {limit:g} percent")
        return GuardCheck("index_move", False, f"{text}, more than {limit:g} percent")

    def daily_loss_limit(self, ctx: GuardContext) -> GuardCheck:
        capital = float(self.risk.get("capital", 1000000.0))
        pct = float(self.risk.get("daily_loss_limit_pct", 1.0))
        limit = capital * pct / 100.0
        if ctx.day_pnl > -limit:
            return GuardCheck("daily_loss_limit", True, f"day P&L {_inr(ctx.day_pnl)} is within the daily loss limit {_inr(limit)}")
        return GuardCheck("daily_loss_limit", False, f"day P&L {_inr(ctx.day_pnl)} has reached the daily loss limit {_inr(limit)}")

    def entries_per_day(self, ctx: GuardContext) -> GuardCheck:
        limit = int(self.strategy.get("max_entries_per_day", 10) or 0)
        if limit <= 0:
            return GuardCheck("entries_per_day", True, f"{ctx.entries_today} entries so far today, no daily limit")
        if ctx.entries_today < limit:
            return GuardCheck("entries_per_day", True, f"{ctx.entries_today} of {limit} entries used today")
        return GuardCheck("entries_per_day", False, f"all {limit} entries for the day are used")

    def reentry_cooldown(self, ctx: GuardContext) -> GuardCheck:
        minutes = float(self.strategy.get("reentry_cooldown_minutes", 5) or 0)
        if ctx.last_exit_at is None:
            return GuardCheck("reentry_cooldown", True, "no exit yet today, no cooldown")
        if ctx.now is None:
            return GuardCheck("reentry_cooldown", False, "no clock")
        ready_at = ctx.last_exit_at + timedelta(minutes=minutes)
        if ctx.now >= ready_at:
            return GuardCheck("reentry_cooldown", True, f"last exit at {hhmm(ctx.last_exit_at)}, cooldown of {minutes:g} minutes ended {hhmm(ready_at)}")
        return GuardCheck("reentry_cooldown", False, f"last exit at {hhmm(ctx.last_exit_at)}, cooldown of {minutes:g} minutes runs until {hhmm(ready_at)}")

    def position_flat(self, ctx: GuardContext) -> GuardCheck:
        if ctx.in_position:
            return GuardCheck("position_flat", False, "a straddle is already open; only one at a time")
        return GuardCheck("position_flat", True, "no straddle is open")

    def lots_bounds(self, ctx: GuardContext) -> GuardCheck:
        max_lots = int(self.risk.get("max_lots", 3))
        suffix = f" ({ctx.lots_detail})" if ctx.lots_detail else ""
        if 1 <= ctx.lots <= max_lots:
            return GuardCheck("lots_bounds", True, f"{ctx.lots} lot{'s' if ctx.lots != 1 else ''} is within 1 to {max_lots}{suffix}")
        if ctx.lots < 1:
            return GuardCheck("lots_bounds", False, f"{ctx.lots} lots is below the minimum of 1{suffix}")
        return GuardCheck("lots_bounds", False, f"{ctx.lots} lots is above the maximum of {max_lots}{suffix}")

    def margin_available(self, ctx: GuardContext) -> GuardCheck:
        if ctx.margin_available is None or ctx.margin_per_lot is None:
            return GuardCheck("margin_available", True, "no margin figure supplied, check skipped")
        required = ctx.lots * ctx.margin_per_lot
        if ctx.margin_available >= required:
            return GuardCheck("margin_available", True, f"margin available {_inr(ctx.margin_available)} covers {_inr(required)} for {ctx.lots} lot{'s' if ctx.lots != 1 else ''}")
        return GuardCheck("margin_available", False, f"margin available {_inr(ctx.margin_available)} is below {_inr(required)} needed for {ctx.lots} lot{'s' if ctx.lots != 1 else ''}")

    def stop_file(self, ctx: GuardContext) -> GuardCheck:
        if ctx.stop_file_present:
            return GuardCheck("stop_file", False, "a STOP file is present in the run directory")
        return GuardCheck("stop_file", True, "no STOP file")

    def halted(self, ctx: GuardContext) -> GuardCheck:
        if ctx.halted:
            reason = f": {ctx.halt_reason}" if ctx.halt_reason else ""
            return GuardCheck("halted", False, f"the engine is halted{reason}")
        return GuardCheck("halted", True, "the engine is not halted")

    def pending_intent(self, ctx: GuardContext) -> GuardCheck:
        if ctx.pending_intent:
            return GuardCheck("pending_intent", False, "an earlier intent is still pending with the broker")
        return GuardCheck("pending_intent", True, "no intent is pending")

    def prediction_present(self, ctx: GuardContext) -> GuardCheck:
        p = ctx.prediction
        if p is None:
            return GuardCheck("prediction_present", False, "no readout prediction for this observation")
        return GuardCheck("prediction_present", True, f"readout says {p.decision.value}, expected movement {p.realized_over_implied * 100:.0f} percent of implied")

    def quote_present(self, ctx: GuardContext) -> GuardCheck:
        if ctx.quote is None:
            return GuardCheck("quote_present", False, "no straddle quote to price the exit")
        return GuardCheck("quote_present", True, f"combined premium {ctx.quote.combined_ltp:.1f} at {_hhmmss(ctx.now)}")
