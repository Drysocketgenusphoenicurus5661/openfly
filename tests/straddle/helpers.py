"""Builders shared by the straddle tests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from openfly.config import DEFAULT_SETTINGS, deep_merge
from openfly.execution.brokers import ReplayBroker
from openfly.execution.costs import SimpleCostModel
from openfly.execution.dispatch import apply_broker_events, execute_step
from openfly.execution.types import quote_lookup_for
from openfly.interfaces import (
    Bar,
    Decision,
    MarketObservation,
    Prediction,
    Quote,
    SessionWindow,
    StraddleQuote,
)
from openfly.straddle.engine import StraddleEngine
from openfly.straddle.guard import GuardContext

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 9, 11)  # Friday
EXPIRY = date(2026, 9, 15)  # Tuesday


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), IST)


def window(day: date = DAY, is_expiry_day: bool = False) -> SessionWindow:
    return SessionWindow(
        trading_date=day,
        market_open=at(9, 15, day=day),
        market_close=at(15, 30, day=day),
        trade_start=at(9, 20, day=day),
        last_entry=at(14, 30, day=day),
        square_off=at(15, 15, day=day),
        is_expiry_day=is_expiry_day,
    )


def sq(when: datetime, call: float, put: float, strike: float = 23350.0, expiry: date = EXPIRY, spread: float = 0.0) -> StraddleQuote:
    code = expiry.strftime("%d%b%y").upper()
    ts = when.timestamp()
    return StraddleQuote(
        call=Quote(f"NIFTY{code}{int(strike)}CE", "NFO", call, round(call - spread / 2, 2), round(call + spread / 2, 2), ts),
        put=Quote(f"NIFTY{code}{int(strike)}PE", "NFO", put, round(put - spread / 2, 2), round(put + spread / 2, 2), ts),
        strike=strike,
        expiry=expiry,
    )


def settings_with(**overrides) -> dict:
    """DEFAULT_SETTINGS with fixed stops (the adaptive tests opt in) plus section__key overrides."""
    update: dict = {"strategy": {"stop_mode": "fixed", "expiry_selection": "weekly", "target_mode": "fixed"}}
    for key, value in overrides.items():
        section, _, name = key.partition("__")
        update.setdefault(section, {})[name] = value
    return deep_merge(DEFAULT_SETTINGS, update)


def make_engine(day: date = DAY, is_expiry_day: bool = False, slippage_ticks: int = 0, **overrides):
    settings = settings_with(**overrides)
    cost_model = SimpleCostModel.from_settings(settings)
    broker = ReplayBroker(cost_model, slippage_ticks=slippage_ticks)
    engine = StraddleEngine(settings, cost_model)
    engine.start_day(window(day, is_expiry_day))
    return engine, broker, settings


def volatile_bars(when: datetime, index: float, ret_std: float, n: int = 60) -> tuple[Bar, ...]:
    """n one-minute bars ending at `when` whose consecutive log returns alternate between +ret_std and -ret_std."""
    import math

    bars = []
    level = index
    start = when - timedelta(minutes=n)
    for k in range(n):
        level = level * math.exp(ret_std if k % 2 == 0 else -ret_std) if k > 0 else index
        bars.append(Bar(start + timedelta(minutes=k), level, level, level, level, 0.0))
    return tuple(bars)


def observation(
    when: datetime,
    index: float = 23350.0,
    vix: float = 12.1,
    premium: float | None = None,
    engine: StraddleEngine | None = None,
    ret_std: float = 0.0,
    n_bars: int = 60,
) -> MarketObservation:
    if ret_std > 0 or n_bars != 60:
        bars = volatile_bars(when, index, ret_std, n_bars)
    else:
        bars = tuple(Bar(at(9, 15) + (when - at(9, 15)) * k / 60, index, index, index, index, 0.0) for k in range(60))
    pos = engine.position if engine is not None and engine.in_position else None
    return MarketObservation(
        timestamp=when,
        index_bars=bars,
        vix=vix,
        vix_bars=(),
        straddle_premium=premium,
        entry_credit=pos.entry_credit if pos else None,
        days_to_expiry=3.6,
        minutes_since_open=int((when - at(9, 15, day=when.date())).total_seconds() // 60),
        position_lots=-pos.lots if pos else 0,
    )


def observe(engine, broker, when, decision, call, put, *, roi=0.82, strike=23350.0, expiry=EXPIRY, ctx=None, index=23350.0, vix=12.1, win=None, ret_std=0.0, n_bars=60, quote=None):
    quote = quote or sq(when, call, put, strike, expiry)
    obs = observation(when, index=index, vix=vix, premium=quote.combined_ltp, engine=engine, ret_std=ret_std, n_bars=n_bars)
    pred = Prediction(roi, 0.6, decision)
    step = engine.on_observation(obs, pred, quote, win or engine.window, ctx or GuardContext())
    if step.intents:
        execute_step(engine, step, broker, None, quote_lookup_for(quote))
    return step


def tick(engine, broker, when, call, put, strike=23350.0, expiry=EXPIRY):
    quote = sq(when, call, put, strike, expiry)
    stop_steps = []
    on_quote = getattr(broker, "on_quote", None)
    if on_quote is not None:
        events = on_quote(quote, when)
        stop_steps = apply_broker_events(engine, broker, events, when)
        for s in stop_steps:
            if s.intents:
                execute_step(engine, s, broker, None, quote_lookup_for(quote))
    step = engine.on_tick(quote, when)
    if step.intents:
        execute_step(engine, step, broker, None, quote_lookup_for(quote))
    return step, stop_steps


ENTER = Decision.ENTER
EXIT = Decision.EXIT
HOLD = Decision.HOLD
