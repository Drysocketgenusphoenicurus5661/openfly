"""One trading day through the full pipeline.

`run_day` is the single shared function used by experiments, the API replay
route and the `openfly replay-day` command. It observes on every completed bar
of `interval` (default 1m, 375 observations per day), feeds the engine with
minute-level straddle quotes for stop, target and square-off checks between
observations, executes intents through the given broker, and collects steps
in the docs/api-spec.md shape. It depends on nothing from openfly.experiments:
everything (brain, encoder, readout, quotes, broker, window, reward function)
is passed in.
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from openfly.execution.costs import load_cost_model
from openfly.execution.dispatch import apply_broker_events, execute_step
from openfly.execution.types import quote_lookup_for
from openfly.interfaces import Bar, MarketObservation, SessionWindow, Stimulus, StraddleQuote
from openfly.straddle.engine import Action, EngineStep, StraddleEngine
from openfly.straddle.expiry import selection_of
from openfly.straddle.guard import GuardContext

IST = ZoneInfo("Asia/Kolkata")

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}

REWARD_PULSE_MV = 20.0
REWARD_PULSE_MS = 200.0
FIXED_DECODER_THRESHOLD_HZ = 2.0


def interval_minutes(interval: str) -> int:
    try:
        return INTERVAL_MINUTES[str(interval).lower()]
    except KeyError as exc:
        raise ValueError(f"unknown bar interval {interval!r}; use one of {sorted(INTERVAL_MINUTES)}") from exc


def stimulus_hash(stimulus: Stimulus) -> str:
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(np.asarray(stimulus.r16, dtype=np.float64)).tobytes())
    h.update(np.ascontiguousarray(np.asarray(stimulus.r8, dtype=np.float64)).tobytes())
    h.update(repr(tuple(stimulus.pulses)).encode())
    return "sha256:" + h.hexdigest()


def population_rates(counts: np.ndarray, brain: Any, neural_ms: float) -> dict[str, float]:
    """Mean spikes per neuron per second for every named population."""
    seconds = max(float(neural_ms), 1e-9) / 1000.0
    counts = np.asarray(counts)
    out: dict[str, float] = {}
    for name, idx in getattr(brain, "populations", {}).items():
        idx = np.asarray(idx)
        if idx.size == 0 or counts.size == 0:
            out[name] = 0.0
        else:
            out[name] = round(float(counts[idx].mean() / seconds), 4)
    return out


def fixed_decoder(counts: np.ndarray, brain: Any, neural_ms: float, threshold_hz: float = FIXED_DECODER_THRESHOLD_HZ) -> dict[str, Any]:
    """Readout 1 of docs/PLAN.md: DNp20 right minus left, gated by any DNpe017 spike, 2 Hz threshold."""
    seconds = max(float(neural_ms), 1e-9) / 1000.0
    counts = np.asarray(counts)
    pops = getattr(brain, "populations", {})

    def mean_rate(name: str) -> float:
        idx = np.asarray(pops.get(name, np.zeros(0, dtype=np.int32)))
        if idx.size == 0 or counts.size == 0:
            return 0.0
        return float(counts[idx].mean() / seconds)

    left = mean_rate("DNp20_L")
    right = mean_rate("DNp20_R")
    gate_idx = np.asarray(pops.get("DNpe017", np.zeros(0, dtype=np.int32)))
    gate = int(counts[gate_idx].sum()) if gate_idx.size and counts.size else 0
    diff = right - left
    side = "HOLD"
    if gate > 0:
        if diff >= threshold_hz:
            side = "ENTER"
        elif diff <= -threshold_hz:
            side = "EXIT"
    return {
        "left_hz": round(left, 4),
        "right_hz": round(right, 4),
        "difference_hz": round(diff, 4),
        "gate_spikes": gate,
        "threshold_hz": threshold_hz,
        "side": side,
    }


def days_to_expiry(expiry: date | None, now: datetime) -> float:
    if expiry is None:
        return 0.0
    close = datetime.combine(expiry, time(15, 30), IST)
    return max(0.0, round((close - now).total_seconds() / 86400.0, 4))


def build_observation(
    *,
    timestamp: datetime,
    index_bars: Sequence[Bar],
    vix: float,
    vix_bars: Sequence[Bar] = (),
    straddle_premium: float | None,
    entry_credit: float | None,
    days_to_expiry: float,
    minutes_since_open: int,
    position_lots: int,
) -> MarketObservation:
    return MarketObservation(
        timestamp=timestamp,
        index_bars=tuple(index_bars),
        vix=float(vix),
        vix_bars=tuple(vix_bars),
        straddle_premium=straddle_premium,
        entry_credit=entry_credit,
        days_to_expiry=float(days_to_expiry),
        minutes_since_open=int(minutes_since_open),
        position_lots=int(position_lots),
    )


def _parse_hhmm(text: str) -> time:
    hh, mm = str(text).split(":")[:2]
    return time(int(hh), int(mm))


def default_session_window(day: date, settings: dict, is_expiry_day: bool | None = None) -> SessionWindow:
    """A session window from the settings alone (09:15 to 15:30 IST; expiry day is Tuesday)."""
    strategy = settings.get("strategy", {})
    return SessionWindow(
        trading_date=day,
        market_open=datetime.combine(day, time(9, 15), IST),
        market_close=datetime.combine(day, time(15, 30), IST),
        trade_start=datetime.combine(day, _parse_hhmm(strategy.get("trade_start", "09:20")), IST),
        last_entry=datetime.combine(day, _parse_hhmm(strategy.get("last_entry", "14:30")), IST),
        square_off=datetime.combine(day, _parse_hhmm(strategy.get("square_off", "15:15")), IST),
        is_expiry_day=(day.weekday() == 1) if is_expiry_day is None else is_expiry_day,
    )


def reward_pulses(settings: dict, reward_fn: Callable[[int], float | None] | None, observation_index: int, interval: str, offset: int = 0) -> tuple[tuple[str, float, float], ...]:
    """Dopamine pulses for the plastic arm: the reward of the observation `horizon_minutes` earlier.

    Positive rewards pulse PAM11, negative ones PPL101, both 20 mV x |r| for 200 ms.
    Rewards are never derived from per-tick equity changes; `reward_fn` is the
    experiments package's `reward_for(observation_index)`.
    """
    neural = settings.get("neural", {})
    if not neural.get("plastic", False) or reward_fn is None:
        return ()
    horizon = int(neural.get("horizon_minutes", 60) or 0)
    back = max(1, horizon // max(1, interval_minutes(interval)))
    earlier = observation_index - back
    if earlier < 0:
        return ()
    try:
        reward = reward_fn(earlier + offset)
    except Exception:
        return ()
    if reward is None:
        return ()
    r = max(-1.0, min(1.0, float(reward)))
    if r > 0:
        return (("PAM11", REWARD_PULSE_MV * r, REWARD_PULSE_MS),)
    if r < 0:
        return (("PPL101", REWARD_PULSE_MV * abs(r), REWARD_PULSE_MS),)
    return ()


def load_reward_fn(settings: dict) -> Callable[[int], float | None] | None:
    if not settings.get("neural", {}).get("plastic", False):
        return None
    try:
        from openfly.experiments.reward import (
            reward_for,  # lazy: another agent owns openfly.experiments
        )
    except ImportError:
        return None
    return reward_for


class _QuoteSource:
    """Wraps `minute_quotes(timestamp, strike=None)`; falls back to the one-argument form."""

    def __init__(self, fn: Callable[..., StraddleQuote | None]):
        self.fn = fn
        self._one_arg = False

    def __call__(self, when: datetime, strike: float | None) -> StraddleQuote | None:
        if not self._one_arg:
            try:
                return self.fn(when, strike=strike)
            except TypeError:
                self._one_arg = True
        return self.fn(when)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


@dataclass
class DayTrace:
    date: str
    steps: list[dict[str, Any]]
    summary: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)
    stimulus_pngs: dict[int, bytes] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "config": self.config, "summary": self.summary, "steps": self.steps}

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), default=_json_default, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> DayTrace:
        data = json.loads(text)
        return cls(date=data["date"], steps=list(data["steps"]), summary=dict(data["summary"]), config=dict(data.get("config", {})))

    def save(self, run_dir: str | Path) -> Path:
        """Write trace.json and, when the encoder rendered them, stimulus/{i}.png per step."""
        out = Path(run_dir)
        out.mkdir(parents=True, exist_ok=True)
        if self.stimulus_pngs:
            png_dir = out / "stimulus"
            png_dir.mkdir(exist_ok=True)
            for i, png in self.stimulus_pngs.items():
                (png_dir / f"{i}.png").write_bytes(png)
                if 0 <= i < len(self.steps):
                    self.steps[i]["stimulus_png"] = f"stimulus/{i}.png"
        path = out / "trace.json"
        path.write_text(self.to_json(indent=1), encoding="utf-8")
        return path


def run_day(
    date_: date | str,
    bars: Sequence[Bar],
    minute_quotes: Callable[..., StraddleQuote | None],
    brain: Any,
    encoder: Any,
    readout: Any,
    settings: dict,
    broker: Any,
    session_window: SessionWindow,
    observation_builder: Callable[..., MarketObservation] | None = None,
    *,
    interval: str = "1m",
    vix: float | Callable[[datetime], float] | None = None,
    vix_bars: Sequence[Bar] = (),
    ledger: Any = None,
    reward_fn: Callable[[int], float | None] | None = None,
    reward_offset: int = 0,
    guard_context: GuardContext | Callable[[datetime, StraddleQuote | None], GuardContext] | None = None,
    trailing_bars: int = 60,
    engine: StraddleEngine | None = None,
    cost_model: Any = None,
    on_step: Callable[[int, int, dict[str, Any]], None] | None = None,
    render_png: bool = True,
    expiry: date | None = None,
    is_trading_day: Callable[[date], bool] | None = None,
) -> DayTrace:
    """Replay one trading day. `bars` may include earlier days (they seed the trailing window).

    `expiry` is the selected contract expiry for the day (monthly by default; the
    quotes decide the actual contracts); `is_trading_day` is the calendar rule used
    for trading-minute arithmetic (weekdays when None).
    """
    day = date.fromisoformat(date_) if isinstance(date_, str) else date_
    step_minutes = interval_minutes(interval)
    window = session_window
    neural = settings.get("neural", {})
    neural_ms = float(neural.get("neural_ms", 200.0))
    model = cost_model if cost_model is not None else load_cost_model(settings)
    eng = engine if engine is not None else StraddleEngine(settings, model, is_trading_day=is_trading_day)
    eng.start_day(window)
    builder = observation_builder or build_observation
    quotes = _QuoteSource(minute_quotes)
    if reward_fn is None:
        reward_fn = load_reward_fn(settings)

    ordered = sorted(bars, key=lambda b: b.timestamp)
    completed: list[Bar] = [b for b in ordered if b.timestamp.date() < day]
    day_bars = [b for b in ordered if b.timestamp.date() == day]
    if not day_bars:
        raise ValueError(f"no {interval} bars for {day.isoformat()}")
    obs_times: dict[datetime, Bar] = {b.timestamp + timedelta(minutes=step_minutes): b for b in day_bars}

    vix_sorted = sorted(vix_bars, key=lambda b: b.timestamp)

    def vix_at(when: datetime) -> float:
        if callable(vix):
            return float(vix(when))
        if vix is not None:
            return float(vix)
        last = 0.0
        for b in vix_sorted:
            if b.timestamp <= when:
                last = b.close
            else:
                break
        return float(last)

    def vix_window(when: datetime) -> tuple[Bar, ...]:
        return tuple(b for b in vix_sorted if b.timestamp <= when)[-trailing_bars:]

    def guard_ctx(when: datetime, quote: StraddleQuote | None) -> GuardContext:
        if guard_context is None:
            return GuardContext()
        if callable(guard_context):
            return guard_context(when, quote)
        return replace(guard_context)

    tick_times: set[datetime] = set()
    t = window.market_open
    while t <= window.market_close:
        tick_times.add(t)
        t += timedelta(minutes=1)
    deadline = eng.deadline
    if deadline is not None and window.market_open <= deadline <= window.market_close:
        tick_times.add(deadline)
    timeline = sorted(tick_times | set(obs_times))

    steps: list[dict[str, Any]] = []
    pngs: dict[int, bytes] = {}
    can_render = render_png and hasattr(encoder, "render_png")
    last_neural: dict[str, Any] = {"rates_hz": {}, "fixed_decoder": {}, "stimulus_hash": None}
    last_index: float | None = completed[-1].close if completed else None
    last_expiry: date | None = expiry
    obs_i = 0
    total_obs = len(obs_times)
    compute_total = 0.0
    encoder_name = getattr(encoder, "name", type(encoder).__name__)
    readout_name = getattr(readout, "name", type(readout).__name__)

    premium_sources: list[str] = []

    def record(step: EngineStep, *, index: float | None, when: datetime, quote: StraddleQuote | None, extra: dict[str, Any], compute: float = 0.0) -> dict[str, Any]:
        i = len(steps)
        source = premium_source(quote)
        extra = {**extra, "premium_source": source}
        trace_step = eng.to_trace_step(
            step,
            i,
            index=index,
            vix=vix_at(when),
            premium=round(quote.combined_ltp, 2) if quote else None,
            days_to_expiry=days_to_expiry(quote.expiry if quote else last_expiry, when),
            stimulus_hash=last_neural["stimulus_hash"],
            rates_hz=last_neural["rates_hz"],
            fixed_decoder=last_neural["fixed_decoder"],
            compute_seconds=round(compute, 4),
            technical=extra,
        )
        trace_step["premium_source"] = source
        if isinstance(trace_step.get("straddle"), dict):
            trace_step["straddle"]["premium_source"] = source
        steps.append(trace_step)
        if on_step is not None:
            on_step(obs_i, total_obs, trace_step)
        return trace_step

    for when in timeline:
        strike = eng.position.strike if eng.position is not None else None
        quote = quotes(when, strike)
        if quote is not None:
            last_expiry = quote.expiry
            lookup = quote_lookup_for(quote)
            on_quote = getattr(broker, "on_quote", None)
            if on_quote is not None:
                events = on_quote(quote, when)
                if events:
                    for stop_step in apply_broker_events(eng, broker, events, when):
                        if stop_step.intents:
                            execute_step(eng, stop_step, broker, ledger, lookup)
                        record(stop_step, index=last_index, when=when, quote=quote, extra={"observation_i": obs_i - 1, "trigger": "broker"})
            tick_step = eng.on_tick(quote, when)
            if tick_step.intents:
                execute_step(eng, tick_step, broker, ledger, lookup)
            if tick_step.action != Action.NONE.value or tick_step.fills or tick_step.intents:
                record(tick_step, index=last_index, when=when, quote=quote, extra={"observation_i": obs_i - 1, "trigger": "tick"})

        bar = obs_times.get(when)
        if bar is None:
            continue
        premium_sources.append(premium_source(quote))
        completed.append(bar)
        last_index = bar.close
        trailing = tuple(completed[-trailing_bars:])
        pos = eng.position if eng.in_position else None
        expiry = quote.expiry if quote else last_expiry
        observation = builder(
            timestamp=when,
            index_bars=trailing,
            vix=vix_at(when),
            vix_bars=vix_window(when),
            straddle_premium=round(quote.combined_ltp, 2) if quote else None,
            entry_credit=round(pos.entry_credit, 2) if pos else None,
            days_to_expiry=days_to_expiry(expiry, when),
            minutes_since_open=int((when - window.market_open).total_seconds() // 60),
            position_lots=-pos.lots if pos else 0,
        )
        stimulus = encoder.encode(observation, brain)
        pulses = reward_pulses(settings, reward_fn, obs_i, interval, reward_offset)
        if pulses:
            stimulus = replace(stimulus, pulses=tuple(stimulus.pulses) + pulses)
        started = _time.perf_counter()
        result = brain.observe(stimulus, neural_ms)
        compute = float(getattr(result, "compute_seconds", 0.0) or 0.0) or (_time.perf_counter() - started)
        compute_total += compute
        prediction = readout.predict(result.counts, brain, observation)
        last_neural = {
            "rates_hz": population_rates(result.counts, brain, neural_ms),
            "fixed_decoder": fixed_decoder(result.counts, brain, neural_ms),
            "stimulus_hash": stimulus_hash(stimulus),
        }
        if can_render:
            try:
                png = encoder.render_png(stimulus)
                if png:
                    pngs[len(steps)] = bytes(png)
            except Exception:
                can_render = False
        step = eng.on_observation(observation, prediction, quote, window, guard_ctx(when, quote))
        if step.intents and quote is not None:
            execute_step(eng, step, broker, ledger, quote_lookup_for(quote))
        record(
            step,
            index=bar.close,
            when=when,
            quote=quote,
            extra={
                "observation_i": obs_i,
                "encoder": encoder_name,
                "readout": readout_name,
                "neural_ms": neural_ms,
                "sim_ms": float(getattr(result, "sim_ms", 0.0) or 0.0),
                "interval": interval,
                "pulses": [list(p) for p in pulses],
                "bar": {"o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close, "v": bar.volume},
            },
            compute=compute,
        )
        obs_i += 1

    summary = {
        "date": day.isoformat(),
        "pnl": round(eng.realized_day, 2),
        "pnl_gross": round(sum(c["gross"] for c in eng.closed), 2),
        "costs": round(sum(c["costs"] for c in eng.closed), 2),
        "trades": len(eng.closed),
        "entries": eng.entries_today,
        "stop_hits": eng.stop_hits,
        "target_hits": eng.target_hits,
        "time_exits": eng.time_exits,
        "early_exits": eng.early_exits,
        "stop_hits_leg": eng.leg_stops_today,
        "vetoes": eng.vetoes,
        "observations": obs_i,
        "steps": len(steps),
        "compute_seconds": round(compute_total, 3),
        "halted": eng.halt_reason or None,
        "open_at_close": eng.in_position,
        "closed": list(eng.closed),
        **premium_summary(premium_sources, minute_quotes),
    }
    config = {
        "interval": interval,
        "neural_ms": neural_ms,
        "encoder": encoder_name,
        "readout": readout_name,
        "encoder_hash": _safe_hash(encoder),
        "readout_hash": _safe_hash(readout),
        "lots": settings.get("strategy", {}).get("lots"),
        "stop_pct": settings.get("strategy", {}).get("stop_pct"),
        "target_pct": settings.get("strategy", {}).get("target_pct"),
        "leg_stop_pct": settings.get("strategy", {}).get("leg_stop_pct"),
        "leg_stop_mode": settings.get("strategy", {}).get("leg_stop_mode"),
        "plastic": bool(neural.get("plastic", False)),
        "broker": type(broker).__name__,
        "expiry": (expiry or last_expiry).isoformat() if (expiry or last_expiry) else None,
        "expiry_selection": selection_of(settings),
    }
    return DayTrace(date=day.isoformat(), steps=steps, summary=summary, config=config, stimulus_pngs=pngs)


PREMIUM_RECORDED = "recorded"
PREMIUM_SYNTHETIC = "synthetic"


def premium_source(quote: Any) -> str:
    """"recorded" when the quote came from a stored option chain, else "synthetic"."""
    source = getattr(quote, "source", None)
    return PREMIUM_RECORDED if source == PREMIUM_RECORDED else PREMIUM_SYNTHETIC


def premium_summary(sources: Sequence[str], minute_quotes: Any = None) -> dict[str, Any]:
    """premium_source ("recorded", "synthetic" or "mixed") and the fraction of synthetic minutes."""
    fraction = getattr(minute_quotes, "synthetic_fraction", None)
    if fraction is None:
        n = len(sources)
        fraction = (sum(1 for s in sources if s != PREMIUM_RECORDED) / n) if n else 1.0
    fraction = float(fraction)
    if fraction >= 1.0:
        label = PREMIUM_SYNTHETIC
    elif fraction <= 0.0:
        label = PREMIUM_RECORDED
    else:
        label = "mixed"
    return {"premium_source": label, "synthetic_fraction": round(fraction, 4)}


def _safe_hash(component: Any) -> str | None:
    fn = getattr(component, "config_hash", None)
    if fn is None:
        return None
    try:
        return str(fn())
    except Exception:
        return None
