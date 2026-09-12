"""Lazy factories for the packages other agents own, with stand-ins when absent.

Expected names (documented in docs/execution.md):
- openfly.neural.brain.Brain: Brain.from_settings(settings) | Brain.load(graph_path) | Brain(settings)
- openfly.sensory.encoders.make_encoder(name, settings) -> EncoderProtocol
- openfly.readout.make_readout(name, settings) -> ReadoutProtocol
- openfly.experiments.pricer.synthetic_minute_quotes(day, bars1m=..., vix=..., settings=..., expiry=...)
  -> callable(timestamp, strike=None) -> StraddleQuote
- openfly.experiments.reward.reward_for(observation_index) -> float | None
- openfly.market.session.SessionCalendar(client, settings).window_for(day) -> SessionWindow
- openfly.market.history.BarStore or HistoryCache for 1 minute index bars
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from openfly.config import PATHS, history_path
from openfly.interfaces import Bar, SessionWindow
from openfly.straddle.replay import default_session_window
from openfly.worker.stubs import (
    FixedTimeReadout,
    NullBrain,
    NullEncoder,
    next_weekly_expiry,
    simple_minute_quotes,
    synthetic_index_bars,
)

logger = logging.getLogger("openfly.worker")


def load_brain(settings: dict) -> tuple[Any, str]:
    try:
        from openfly.neural.brain import Brain
    except ImportError as exc:
        return NullBrain(), f"NullBrain (openfly.neural.brain not importable: {exc})"
    attempts = (
        ("Brain.from_settings(settings)", lambda: Brain.from_settings(settings)),
        ("Brain.load(graph)", lambda: Brain.load(str(PATHS.graph))),
        ("Brain(settings)", lambda: Brain(settings)),
        ("Brain()", lambda: Brain()),
    )
    for label, factory in attempts:
        try:
            brain = factory()
        except (AttributeError, TypeError):
            continue
        except Exception as exc:
            return NullBrain(), f"NullBrain ({label} failed: {exc})"
        if hasattr(brain, "populations") and hasattr(brain, "observe"):
            return brain, label
    return NullBrain(), "NullBrain (no known Brain constructor matched)"


def load_encoder(name: str, settings: dict) -> tuple[Any, str]:
    try:
        from openfly.sensory.encoders import make_encoder
    except ImportError as exc:
        return NullEncoder(), f"NullEncoder (openfly.sensory.encoders not importable: {exc})"
    try:
        return make_encoder(name, settings), f"make_encoder({name!r})"
    except Exception as exc:
        return NullEncoder(), f"NullEncoder (make_encoder failed: {exc})"


def load_readout(name: str, settings: dict) -> tuple[Any, str]:
    make = None
    for module_name in ("openfly.readout", "openfly.readout.readouts"):
        try:
            module = __import__(module_name, fromlist=["make_readout"])
        except ImportError:
            continue
        make = getattr(module, "make_readout", None)
        if make is not None:
            break
    if make is None:
        return FixedTimeReadout.from_settings(settings), "FixedTimeReadout (openfly.readout.make_readout not importable)"
    try:
        return make(name, settings), f"make_readout({name!r})"
    except Exception as exc:
        return FixedTimeReadout.from_settings(settings), f"FixedTimeReadout (make_readout failed: {exc})"


def load_minute_quotes(day: date, bars1m: list[Bar], vix: float, settings: dict, expiry: date | None = None) -> tuple[Any, str]:
    strategy = settings.get("strategy", {})
    expiry = expiry or next_weekly_expiry(day)
    try:
        from openfly.experiments.pricer import synthetic_minute_quotes
    except ImportError:
        return (
            simple_minute_quotes(
                day,
                bars1m,
                vix,
                expiry,
                strike_step=float(strategy.get("strike_step", 50)),
                exchange=str(strategy.get("options_exchange", "NFO")),
                underlying=str(strategy.get("underlying", "NIFTY")),
            ),
            "simple_minute_quotes (openfly.experiments.pricer not importable)",
        )
    for label, call in (
        ("synthetic_minute_quotes(day, bars1m=, vix=, settings=, expiry=)", lambda: synthetic_minute_quotes(day, bars1m=bars1m, vix=vix, settings=settings, expiry=expiry)),
        ("synthetic_minute_quotes(day, bars1m, vix, settings)", lambda: synthetic_minute_quotes(day, bars1m, vix, settings)),
        ("synthetic_minute_quotes(day)", lambda: synthetic_minute_quotes(day)),
    ):
        try:
            return call(), label
        except TypeError:
            continue
    return simple_minute_quotes(day, bars1m, vix, expiry), "simple_minute_quotes (synthetic_minute_quotes signature unknown)"


def load_session_window(day: date, settings: dict) -> tuple[SessionWindow, bool, str]:
    """(window, is_trading_day, note)."""
    try:
        from openfly.market.session import SessionCalendar
    except ImportError:
        return default_session_window(day, settings), day.weekday() < 5, "default_session_window (openfly.market.session not importable)"
    try:
        calendar = SessionCalendar(None, settings)
        window = calendar.window_for(day)
        trading = bool(calendar.is_trading_day(day))
        return window, trading, "SessionCalendar"
    except Exception as exc:
        return default_session_window(day, settings), day.weekday() < 5, f"default_session_window (SessionCalendar failed: {exc})"


def frame_to_bars(frame: Any) -> list[Bar]:
    bars: list[Bar] = []
    if frame is None or len(frame) == 0:
        return bars
    columns = set(frame.columns)
    if "timestamp" not in columns:
        frame = frame.reset_index()
    for row in frame.itertuples(index=False):
        ts = row.timestamp
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        bars.append(Bar(ts, float(row.open), float(row.high), float(row.low), float(row.close), float(getattr(row, "volume", 0.0) or 0.0)))
    return bars


def load_bars(day: date, settings: dict, interval: str = "1m", days_back: int = 1) -> tuple[list[Bar], str]:
    """Index bars for the day plus `days_back` earlier calendar days, from the local store only."""
    strategy = settings.get("strategy", {})
    exchange = str(strategy.get("index_exchange", "NSE_INDEX"))
    symbol = str(strategy.get("underlying", "NIFTY"))
    start = day - timedelta(days=days_back)
    frame = None
    note = ""
    try:
        from openfly.market import history as market_history

        store_cls = getattr(market_history, "BarStore", None)
        if store_cls is not None:
            store = store_cls()
            for method in ("bars", "get", "load"):
                fn = getattr(store, method, None)
                if fn is None:
                    continue
                try:
                    frame = fn(exchange, symbol, interval, start, day)
                    note = f"BarStore.{method}"
                    break
                except TypeError:
                    try:
                        frame = fn(exchange, symbol, interval)
                        note = f"BarStore.{method}"
                        break
                    except TypeError:
                        continue
        if frame is None or len(frame) == 0:
            cache = market_history.HistoryCache(None)
            frame = cache.load(exchange, symbol, interval)
            note = "HistoryCache.load"
    except Exception as exc:
        logger.info("market history unavailable (%s), trying parquet", exc)
        frame = None
    if frame is None or len(frame) == 0:
        path = history_path(exchange, symbol, interval)
        if path.exists():
            try:
                import pandas as pd

                frame = pd.read_parquet(path)
                note = f"parquet {path}"
            except Exception as exc:
                logger.info("parquet unreadable (%s)", exc)
                frame = None
    bars: list[Bar] = []
    if frame is not None and len(frame) > 0:
        bars = [b for b in frame_to_bars(frame) if start <= b.timestamp.date() <= day]
        bars = [b for b in bars if b.timestamp.date() == day or b.timestamp.date() < day]
    if not any(b.timestamp.date() == day for b in bars):
        return synthetic_index_bars(day), "synthetic random walk (no local bars for this date)"
    return bars, note


def load_vix(day: date, settings: dict, default: float = 12.0) -> tuple[float, str]:
    strategy = settings.get("strategy", {})
    exchange = str(strategy.get("index_exchange", "NSE_INDEX"))
    symbol = str(strategy.get("vix_symbol", "INDIAVIX"))
    try:
        from openfly.market.history import HistoryCache

        cache = HistoryCache(None)
        for interval in ("D", "1m", "5m"):
            frame = cache.load(exchange, symbol, interval)
            if frame is None or len(frame) == 0:
                continue
            bars = [b for b in frame_to_bars(frame) if b.timestamp.date() <= day]
            if bars:
                return float(bars[-1].close), f"HistoryCache {symbol} {interval}"
    except Exception as exc:
        logger.info("VIX history unavailable (%s)", exc)
    return default, f"default {default:g} (no local INDIAVIX history)"
