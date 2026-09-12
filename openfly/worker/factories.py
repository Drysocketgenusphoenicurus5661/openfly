"""Factories that build the real packages for the worker, the replay command and the API.

- Brain: ``openfly.neural.brain.Brain(half_saturation=..., plastic=..., r8_ame12_excitatory=...)``
  from ``settings["neural"]``; the NullBrain stand-in is used only when the neural
  package cannot be imported or the compiled graph is missing, and the note says so.
- Encoder: ``openfly.sensory.encoders.make_encoder(name, settings)``.
- Readout: ``openfly.readout.make_readout(name, settings)``, or the fitted readout
  saved under ``runs/experiments/<id>/readout`` when an experiment id is given.
- Minute quotes: ``openfly.experiments.quotes.minute_quotes_for(day)`` (recorded
  chains from the DuckDB store first, the calibrated pricer for minutes without a
  recorded print), then ``openfly.experiments.pricer.synthetic_minute_quotes``.
- Reward: ``openfly.experiments.reward.reward_for``.
- Session window: ``openfly.market.session.SessionCalendar`` (offline from its cache).
- Index bars and INDIAVIX: ``openfly.market.store.BarStore`` only. Bars are never
  generated; a date without stored bars raises :class:`MissingBars`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from openfly.config import PATHS, Paths
from openfly.interfaces import Bar, SessionWindow
from openfly.straddle.expiry import select_expiry_for, selection_of
from openfly.straddle.replay import default_session_window
from openfly.worker.stubs import (
    FixedTimeReadout,
    NullBrain,
    NullEncoder,
    simple_minute_quotes,
)

logger = logging.getLogger("openfly.worker")


class MissingBars(ValueError):
    """No stored index bars for the requested date (bars are never generated)."""


def load_brain(settings: dict) -> tuple[Any, str]:
    neural = settings.get("neural", {})
    kwargs = {
        "half_saturation": float(neural.get("half_saturation", 0.5)),
        "plastic": bool(neural.get("plastic", False)),
        "r8_ame12_excitatory": bool(neural.get("r8_ame12_excitatory", True)),
    }
    try:
        from openfly.neural.brain import Brain
    except ImportError as exc:
        return NullBrain(), f"NullBrain (openfly.neural.brain not importable: {exc})"
    try:
        brain = Brain(**kwargs)
    except FileNotFoundError as exc:
        return NullBrain(), f"NullBrain (compiled graph missing: {exc}; run `openfly prepare`)"
    label = (
        f"Brain(half_saturation={kwargs['half_saturation']:g}, plastic={kwargs['plastic']}, "
        f"r8_ame12_excitatory={kwargs['r8_ame12_excitatory']})"
    )
    return brain, label


def load_encoder(name: str, settings: dict) -> tuple[Any, str]:
    try:
        from openfly.sensory.encoders import make_encoder
    except ImportError as exc:
        return NullEncoder(), f"NullEncoder (openfly.sensory.encoders not importable: {exc})"
    encoder = make_encoder(name, settings)
    return encoder, f"make_encoder({name!r})"


def load_readout(
    name: str, settings: dict, experiment_id: str | None = None, paths: Paths = PATHS, brain: Any = None
) -> tuple[Any, str]:
    try:
        from openfly.readout import load_readout as load_saved
        from openfly.readout import make_readout
    except ImportError as exc:
        return FixedTimeReadout.from_settings(settings), f"FixedTimeReadout (openfly.readout not importable: {exc})"
    if experiment_id:
        directory = paths.experiments / experiment_id / "readout"
        if not (directory / "readout.json").exists():
            raise FileNotFoundError(f"experiment {experiment_id} has no fitted readout under {directory}")
        readout = load_saved(directory, brain=brain)
        return readout, f"load_readout({experiment_id!r})"
    return make_readout(name, settings, brain), f"make_readout({name!r})"


def load_reward(settings: dict) -> tuple[Callable[[int], float | None] | None, str]:
    """The experiments package's reward function, bound to these settings (plastic arm only)."""
    if not settings.get("neural", {}).get("plastic", False):
        return None, "no reward (neural.plastic is off)"
    try:
        from openfly.experiments.reward import reward_for
    except ImportError as exc:
        return None, f"no reward (openfly.experiments.reward not importable: {exc})"

    def reward(key, trade=None):
        return reward_for(key, trade, settings=settings)

    return reward, "reward_for"


def _bars_frame(bars: list[Bar]):
    import pandas as pd

    return pd.DataFrame(
        {
            "timestamp": [b.timestamp for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )


def load_is_trading_day(settings: dict, paths: Paths = PATHS) -> Any:
    """The market calendar's is_trading_day when available, else None (weekday rule)."""
    try:
        from openfly.market.session import SessionCalendar

        return SessionCalendar(None, settings, paths=paths).is_trading_day
    except Exception:
        return None


def select_replay_expiry(day: date, settings: dict, paths: Paths = PATHS) -> tuple[date, str]:
    """The selected expiry for a replayed date: ChainResolver.expiry_for_date when available, else the rule.

    `strategy.expiry_selection` monthly (default) means the last expiry of the
    calendar month; the rule fallback is the last Tuesday of the month moved to
    the previous trading day on a holiday.
    """
    selection = selection_of(settings)
    try:
        from openfly.market.chain import ChainResolver

        fn = getattr(ChainResolver, "expiry_for_date", None)
        if fn is not None:
            for call in (lambda: fn(day, selection), lambda: ChainResolver(None, settings).expiry_for_date(day, selection)):
                try:
                    value = call()
                except Exception:
                    continue
                if isinstance(value, date):
                    return value, "ChainResolver.expiry_for_date"
    except ImportError:
        pass
    return select_expiry_for(day, settings, is_trading_day=load_is_trading_day(settings, paths))


def load_minute_quotes(
    day: date, bars1m: list[Bar], vix: float, settings: dict, expiry: date | None = None, paths: Paths = PATHS
) -> tuple[Any, str]:
    """Straddle quotes per minute: recorded chain legs when stored, the calibrated pricer otherwise.

    The contract expiry is the selected one (monthly by default) unless given.
    """
    strategy = settings.get("strategy", {})
    expiry = expiry or select_replay_expiry(day, settings, paths)[0]
    try:
        from openfly.experiments.quotes import minute_quotes_for
    except ImportError as exc:
        logger.info("openfly.experiments.quotes not importable (%s)", exc)
    else:
        try:
            quotes = minute_quotes_for(day, settings=settings, expiry=expiry, paths=paths)
        except Exception as exc:
            logger.info("minute_quotes_for failed (%s), trying the synthetic pricer", exc)
        else:
            source = getattr(quotes, "source", "synthetic")
            frac = getattr(quotes, "synthetic_fraction", 1.0)
            return quotes, f"minute_quotes_for ({source}, synthetic fraction {frac:.2f})"
    try:
        from openfly.experiments.pricer import synthetic_minute_quotes
    except ImportError:
        pass
    else:
        try:
            day_bars = [b for b in bars1m if b.timestamp.date() == day]
            quotes = synthetic_minute_quotes(
                day, _bars_frame(day_bars), float(vix), expiry, underlying=str(strategy.get("underlying", "NIFTY"))
            )
            return quotes, "synthetic_minute_quotes (calibrated pricer, synthetic)"
        except Exception as exc:
            logger.info("synthetic_minute_quotes failed (%s), using the simple pricer", exc)
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
        "simple_minute_quotes (stand-in pricer, synthetic)",
    )


def load_session_window(day: date, settings: dict, paths: Paths = PATHS) -> tuple[SessionWindow, bool, str]:
    """(window, is_trading_day, note)."""
    try:
        from openfly.market.session import SessionCalendar
    except ImportError:
        return default_session_window(day, settings), day.weekday() < 5, "default_session_window (openfly.market.session not importable)"
    try:
        calendar = SessionCalendar(None, settings, paths=paths)
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


def bar_store(paths: Paths = PATHS):
    from openfly.market.store import BarStore

    return BarStore(paths.market_db, history_root=paths.history)


def load_bars(day: date, settings: dict, interval: str = "1m", days_back: int = 1, paths: Paths = PATHS) -> tuple[list[Bar], str]:
    """Real index bars for the day plus ``days_back`` earlier calendar days, from the DuckDB store.

    Raises :class:`MissingBars` when the store has no bars for ``day``; nothing is generated.
    """
    strategy = settings.get("strategy", {})
    exchange = str(strategy.get("index_exchange", "NSE_INDEX"))
    symbol = str(strategy.get("underlying", "NIFTY"))
    start = day - timedelta(days=days_back)
    store = bar_store(paths)
    frame = store.bars(exchange, symbol, interval, start, day)
    bars = [b for b in frame_to_bars(frame) if start <= b.timestamp.date() <= day]
    if not any(b.timestamp.date() == day for b in bars):
        raise MissingBars(
            f"no {interval} bars for {exchange}:{symbol} on {day.isoformat()} in {Path(store.path).name}; "
            "record the day (`openfly record --date ...`) or pick a date from `openfly history status`"
        )
    return bars, f"BarStore {exchange}:{symbol} {interval}"


def load_vix(day: date, settings: dict, default: float = 12.0, paths: Paths = PATHS) -> tuple[float, str]:
    strategy = settings.get("strategy", {})
    exchange = str(strategy.get("index_exchange", "NSE_INDEX"))
    symbol = str(strategy.get("vix_symbol", "INDIAVIX"))
    try:
        store = bar_store(paths)
        for interval in ("D", "1m", "5m"):
            frame = store.bars(exchange, symbol, interval, day - timedelta(days=10), day)
            if frame is None or len(frame) == 0:
                continue
            bars = [b for b in frame_to_bars(frame) if b.timestamp.date() <= day]
            if bars:
                return float(bars[-1].close), f"BarStore {symbol} {interval}"
    except Exception as exc:
        logger.info("VIX history unavailable (%s)", exc)
    return default, f"default {default:g} (no stored INDIAVIX history)"
