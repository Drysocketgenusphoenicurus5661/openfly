"""The session-aware worker loop for one paper or live trading day.

Waits for the session, resolves the ATM legs, subscribes LTP for the two
legs, the index and INDIAVIX, ticks the engine on every quote, observes on
each completed bar of `settings.neural.live_interval`, executes intents,
reconciles with the broker, squares off, honours a STOP file, holds a
portable file lock, appends every step to events.jsonl (fsync) and keeps
state.json current for the API. Halts on UnresolvedOrder.
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from openfly.execution.costs import load_cost_model
from openfly.execution.dispatch import apply_broker_events, execute_step
from openfly.execution.ledger import Ledger
from openfly.execution.types import execution_settings, quote_lookup_for
from openfly.interfaces import Bar, Quote, StraddleQuote, UnresolvedOrder
from openfly.straddle.engine import Action, EngineStep, State, StraddleEngine
from openfly.straddle.expiry import select_expiry_for, selection_of
from openfly.straddle.guard import GuardContext
from openfly.straddle.replay import (
    _json_default,
    build_observation,
    days_to_expiry,
    fixed_decoder,
    interval_minutes,
    load_reward_fn,
    population_rates,
    reward_pulses,
    stimulus_hash,
)

IST = ZoneInfo("Asia/Kolkata")
LIVE_ENV = "OPENFLY_LIVE"
LIVE_VALUE = "I_ACCEPT_REAL_TRADES"

logger = logging.getLogger("openfly.worker")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class BarBuilder:
    """Aggregates index LTP ticks into bars of a fixed interval aligned to the session open."""

    def __init__(self, interval_min: int, origin: datetime):
        self.interval = timedelta(minutes=interval_min)
        self.origin = origin
        self.current: dict[str, Any] | None = None
        self.completed: list[Bar] = []

    def _bucket(self, when: datetime) -> datetime:
        elapsed = when - self.origin
        n = int(elapsed.total_seconds() // self.interval.total_seconds())
        return self.origin + n * self.interval

    def add(self, when: datetime, price: float) -> None:
        if price <= 0 or when < self.origin:
            return
        start = self._bucket(when)
        if self.current is not None and self.current["start"] != start:
            self._close()
        if self.current is None:
            self.current = {"start": start, "o": price, "h": price, "l": price, "c": price}
            return
        cur = self.current
        cur["h"] = max(cur["h"], price)
        cur["l"] = min(cur["l"], price)
        cur["c"] = price

    def flush_before(self, when: datetime) -> None:
        if self.current is not None and self.current["start"] + self.interval <= when:
            self._close()

    def _close(self) -> None:
        cur = self.current
        if cur is not None:
            self.completed.append(Bar(cur["start"], cur["o"], cur["h"], cur["l"], cur["c"], 0.0))
        self.current = None


class Worker:
    """One trading day in paper or live mode. Construct, then `run()`."""

    def __init__(
        self,
        settings: dict,
        mode: str,
        run_dir: str | Path,
        brain: Any,
        encoder: Any,
        readout: Any,
        client: Any,
        feed: Any,
        calendar: Any,
        chain: Any,
        broker: Any,
        *,
        ledger: Ledger | None = None,
        cost_model: Any = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        history_bars: Callable[[], Sequence[Bar]] | None = None,
        trading_date: date | None = None,
        engine: StraddleEngine | None = None,
        reward_fn: Callable[[int], float | None] | None = None,
        interval: str | None = None,
    ):
        self.settings = settings
        self.mode = mode
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.brain = brain
        self.encoder = encoder
        self.readout = readout
        self.client = client
        self.feed = feed
        self.calendar = calendar
        self.chain = chain
        self.broker = broker
        self.cost_model = cost_model if cost_model is not None else load_cost_model(settings)
        self.ledger = ledger if ledger is not None else Ledger(self.run_dir, self.cost_model)
        self.engine = engine if engine is not None else StraddleEngine(
            settings, self.cost_model, is_trading_day=getattr(calendar, "is_trading_day", None)
        )
        self._clock = clock or (lambda: datetime.now(IST))
        self._sleep = sleep or _time.sleep
        self.history_bars = history_bars
        self.trading_date = trading_date
        self.reward_fn = reward_fn if reward_fn is not None else load_reward_fn(settings)
        neural = settings.get("neural", {})
        self.interval = str(interval or neural.get("live_interval", "5m"))
        self.neural_ms = float(neural.get("neural_ms", 200.0))
        ex = execution_settings(settings)
        self.tick_interval = float(ex["tick_interval_s"])
        self.reconcile_interval = float(ex["reconcile_interval_s"])
        strategy = settings.get("strategy", {})
        self.underlying = str(strategy.get("underlying", "NIFTY"))
        self.index_exchange = str(strategy.get("index_exchange", "NSE_INDEX"))
        self.options_exchange = str(strategy.get("options_exchange", "NFO"))
        self.vix_symbol = str(strategy.get("vix_symbol", "INDIAVIX"))
        self.lots = int(strategy.get("lots", 1) or 1)
        self.state = "stopped"
        self.started_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.halt_reason: str | None = None
        self.window = None
        self.legs: tuple[str, str] | None = None
        self.strike: float | None = None
        self.expiry: date | None = None
        self.preflight_result: dict | None = None
        self.steps_written = 0
        self._bars: list[Bar] = []
        self._builder: BarBuilder | None = None
        self._obs_i = 0
        self._last_neural: dict[str, Any] = {"rates_hz": {}, "fixed_decoder": {}, "stimulus_hash": None}
        self._last_trace: dict[str, Any] | None = None
        self._stop = False
        self._expiry_choice: tuple[date, date] | None = None
        self._events_path = self.run_dir / "events.jsonl"
        self._state_path = self.run_dir / "state.json"

    # ------------------------------------------------------------- public

    def now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=IST)

    def stop(self) -> None:
        self._stop = True

    def run(self) -> int:
        lock = FileLock(str(self.run_dir / "worker.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            self._log("another worker holds the lock for this run directory")
            return 3
        try:
            return self._run()
        except UnresolvedOrder as exc:
            self._halt(f"unresolved order: {exc}")
            return 2
        finally:
            self._write_state()
            lock.release()

    # --------------------------------------------------------------- loop

    def _run(self) -> int:
        self._set_state("starting")
        self.started_at = self.now()
        if self.mode not in ("paper", "live"):
            self._halt(f"unknown mode {self.mode!r}")
            return 2
        if self.mode == "live" and os.environ.get(LIVE_ENV) != LIVE_VALUE:
            self._halt(f"live mode requires the environment variable {LIVE_ENV}={LIVE_VALUE}")
            return 2
        day = self.trading_date or self.now().date()
        window = self.calendar.window_for(day)
        is_trading_day = getattr(self.calendar, "is_trading_day", None)
        if is_trading_day is not None and not is_trading_day(day):
            self._log(f"{day.isoformat()} is not a trading day; nothing to do")
            self._set_state("stopped")
            return 0
        self.window = window
        self.engine.start_day(window)
        self.ledger.set_trading_date(day)
        self._builder = BarBuilder(interval_minutes(self.interval), window.market_open)
        self._seed_history()
        self._write_state()

        if not self._wait_until(window.market_open - timedelta(minutes=2)):
            self._set_state("stopped")
            return 0
        resolved = False
        for _ in range(60):
            if self._resolve_legs():
                resolved = True
                break
            if not self._pause(10.0):
                self._set_state("stopped")
                return 0
        if not resolved:
            self._halt("could not resolve the ATM legs from the chain")
            return 2
        assert self.legs is not None
        pre = self.broker.preflight(symbols=list(self.legs), lots=self.lots)
        self.preflight_result = pre
        self._write_state()
        if not pre.get("ok", False):
            failed = "; ".join(c["detail"] for c in pre.get("checks", []) if not c.get("ok"))
            self._halt(f"preflight failed: {failed}")
            return 2
        self._set_state("running")

        step_delta = timedelta(minutes=interval_minutes(self.interval))
        next_obs = window.market_open + step_delta
        next_reconcile = self.now()
        while not self._stop:
            now = self.now()
            if now > window.market_close:
                break
            if self._stop_file_present():
                if self.engine.in_position:
                    step = self.engine.square_off_now(now, "STOP file present")
                    self._run_step(step, now, {"trigger": "stop_file"})
                self._log("STOP file present; stopping")
                self._stop = True
                break
            squareoff_file = self.run_dir / "SQUAREOFF"
            if squareoff_file.exists():
                try:
                    squareoff_file.unlink()
                except OSError:
                    pass
                if self.engine.in_position:
                    step = self.engine.square_off_now(now, "manual square-off requested")
                    self._run_step(step, now, {"trigger": "manual"})
                else:
                    self._log("SQUAREOFF requested with a flat book; nothing to do")
            index = self._index_ltp()
            if index is not None and self._builder is not None:
                self._builder.add(now, index)
            quote, _age = self._quote()
            if quote is not None:
                step = self.engine.on_tick(quote, now)
                if step.intents or step.action != Action.NONE.value or step.fills:
                    self._run_step(step, now, {"trigger": "tick"}, quote=quote)
            if now >= next_obs:
                if self._builder is not None:
                    self._builder.flush_before(next_obs)
                    self._collect_bars()
                if self.engine.state == State.FLAT:
                    self._resolve_legs()
                self._observe(next_obs)
                while next_obs <= now:
                    next_obs += step_delta
            if now >= next_reconcile:
                self._reconcile(now)
                next_reconcile = now + timedelta(seconds=self.reconcile_interval)
            if self.engine.is_halted:
                self._halt(self.engine.halt_reason)
                return 2
            if now >= window.square_off and not self.engine.in_position and self.engine.state == State.FLAT:
                self._log("square-off time passed and the book is flat; done for the day")
                break
            self._sleep(self.tick_interval)
        if self.engine.in_position:
            self._log("stopping with an open straddle; sending a square-off")
            step = self.engine.square_off_now(self.now(), "worker stop")
            self._run_step(step, self.now(), {"trigger": "stop"})
        self._set_state("halted" if self.engine.is_halted else "stopped")
        return 2 if self.engine.is_halted else 0

    # ----------------------------------------------------------- helpers

    def _wait_until(self, when: datetime) -> bool:
        while self.now() < when:
            if self._stop or self._stop_file_present():
                self._log("stop requested while waiting for the session")
                return False
            remaining = (when - self.now()).total_seconds()
            self._sleep(max(0.05, min(30.0, remaining)))
        return True

    def _pause(self, seconds: float) -> bool:
        if self._stop or self._stop_file_present():
            return False
        self._sleep(seconds)
        return True

    def _stop_file_present(self) -> bool:
        return (self.run_dir / "STOP").exists() or (self.run_dir.parent / "STOP").exists()

    def _seed_history(self) -> None:
        if self.history_bars is None or self.window is None:
            return
        try:
            bars = list(self.history_bars())
        except Exception as exc:
            self._log(f"history seed failed: {exc}")
            return
        self._bars = sorted((b for b in bars if b.timestamp < self.window.market_open), key=lambda b: b.timestamp)[-200:]

    def _collect_bars(self) -> None:
        if self._builder is None or not self._builder.completed:
            return
        known = {b.timestamp for b in self._bars}
        for bar in self._builder.completed:
            if bar.timestamp not in known:
                self._bars.append(bar)
                known.add(bar.timestamp)
        self._builder.completed.clear()

    def _selected_expiry(self) -> date | None:
        """The expiry of `strategy.expiry_selection` for today, from the chain resolver or the rule."""
        day = self.window.trading_date if self.window is not None else self.now().date()
        if self._expiry_choice is not None and self._expiry_choice[0] == day:
            return self._expiry_choice[1]
        try:
            expiry, source = select_expiry_for(
                day, self.settings, resolver=self.chain, is_trading_day=getattr(self.calendar, "is_trading_day", None)
            )
        except Exception as exc:
            self._log(f"expiry selection failed: {exc}")
            return None
        self._expiry_choice = (day, expiry)
        self._log(f"{selection_of(self.settings)} expiry for {day.isoformat()}: {expiry.isoformat()} ({source})")
        return expiry

    def _resolve_legs(self) -> bool:
        selected = self._selected_expiry()
        try:
            try:
                snap = self.chain.chain_snapshot(expiry=selected) if selected is not None else self.chain.chain_snapshot()
            except TypeError:
                snap = self.chain.chain_snapshot()
        except Exception as exc:
            self._log(f"chain snapshot failed: {exc}")
            return False
        strike = _get(snap, "atm_strike")
        expiry = _get(snap, "expiry")
        if isinstance(expiry, str):
            expiry = date.fromisoformat(expiry)
        atm = _get(snap, "atm")
        if atm is not None and hasattr(atm, "call"):
            ce, pe = atm.call.symbol, atm.put.symbol
            strike = strike if strike is not None else atm.strike
            expiry = expiry or atm.expiry
        else:
            rows = _get(snap, "rows") or []
            row = next((r for r in rows if strike is not None and float(_get(r, "strike", -1)) == float(strike)), None)
            if row is None:
                self._log("chain snapshot has no row at the ATM strike")
                return False
            ce = _get(_get(row, "ce"), "symbol")
            pe = _get(_get(row, "pe"), "symbol")
        if not ce or not pe or strike is None or expiry is None:
            self._log("chain snapshot is incomplete (legs, strike or expiry missing)")
            return False
        if selected is not None and expiry != selected:
            self._log(f"chain snapshot quotes expiry {expiry.isoformat()} while the selection is {selected.isoformat()}; using the quoted contracts")
        changed = self.legs != (ce, pe)
        self.legs = (ce, pe)
        self.strike = float(strike)
        self.expiry = expiry
        if changed:
            self._subscribe()
            self._log(f"ATM legs {ce} and {pe}, strike {self.strike:g}, expiry {expiry.isoformat()}")
        return True

    def _subscribe(self) -> None:
        if self.legs is None:
            return
        pairs = [
            (self.options_exchange, self.legs[0]),
            (self.options_exchange, self.legs[1]),
            (self.index_exchange, self.underlying),
            (self.index_exchange, self.vix_symbol),
        ]
        subscribe = getattr(self.feed, "subscribe", None)
        if subscribe is None:
            return
        try:
            subscribe(pairs)
        except Exception as exc:
            self._log(f"feed subscribe failed: {exc}")

    def _last(self, symbol: str) -> Any:
        try:
            return self.feed.last(symbol)
        except Exception:
            return None

    def _as_quote(self, value: Any, symbol: str, exchange: str) -> Quote | None:
        if value is None:
            return None
        if hasattr(value, "ltp"):
            return value
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return Quote(symbol, exchange, price, 0.0, 0.0, self.now().timestamp())

    def _index_ltp(self) -> float | None:
        q = self._as_quote(self._last(self.underlying), self.underlying, self.index_exchange)
        return q.ltp if q is not None and q.ltp > 0 else None

    def _vix(self) -> float:
        q = self._as_quote(self._last(self.vix_symbol), self.vix_symbol, self.index_exchange)
        return q.ltp if q is not None else 0.0

    def _quote(self) -> tuple[StraddleQuote | None, float | None]:
        if self.legs is None or self.strike is None or self.expiry is None:
            return None, None
        ce = self._as_quote(self._last(self.legs[0]), self.legs[0], self.options_exchange)
        pe = self._as_quote(self._last(self.legs[1]), self.legs[1], self.options_exchange)
        if ce is None or pe is None or ce.ltp <= 0 or pe.ltp <= 0:
            return None, None
        ages = []
        age_fn = getattr(self.feed, "age_seconds", None)
        if age_fn is not None:
            for symbol in self.legs:
                try:
                    age = age_fn(symbol)
                except Exception:
                    age = None
                if age is not None:
                    ages.append(float(age))
        return StraddleQuote(call=ce, put=pe, strike=self.strike, expiry=self.expiry), (max(ages) if ages else None)

    def _observe(self, when: datetime) -> None:
        if self.window is None:
            return
        bars = tuple(self._bars[-60:])
        if not bars:
            self._log(f"{when.strftime('%H:%M')}: no completed bars yet, observation skipped")
            return
        quote, age = self._quote()
        pos = self.engine.position if self.engine.in_position else None
        observation = build_observation(
            timestamp=when,
            index_bars=bars,
            vix=self._vix(),
            vix_bars=(),
            straddle_premium=round(quote.combined_ltp, 2) if quote else None,
            entry_credit=round(pos.entry_credit, 2) if pos else None,
            days_to_expiry=days_to_expiry(self.expiry, when),
            minutes_since_open=int((when - self.window.market_open).total_seconds() // 60),
            position_lots=-pos.lots if pos else 0,
        )
        stimulus = self.encoder.encode(observation, self.brain)
        pulses = reward_pulses(self.settings, self.reward_fn, self._obs_i, self.interval)
        if pulses:
            from dataclasses import replace

            stimulus = replace(stimulus, pulses=tuple(stimulus.pulses) + pulses)
        started = _time.perf_counter()
        result = self.brain.observe(stimulus, self.neural_ms)
        compute = float(getattr(result, "compute_seconds", 0.0) or 0.0) or (_time.perf_counter() - started)
        prediction = self.readout.predict(result.counts, self.brain, observation)
        self._last_neural = {
            "rates_hz": population_rates(result.counts, self.brain, self.neural_ms),
            "fixed_decoder": fixed_decoder(result.counts, self.brain, self.neural_ms),
            "stimulus_hash": stimulus_hash(stimulus),
        }
        ctx = GuardContext(
            quote_age_s=age,
            index_now=self._index_ltp(),
            stop_file_present=self._stop_file_present(),
            pending_intent=bool(self.ledger.pending()),
            halted=self.ledger.halted() is not None,
        )
        step = self.engine.on_observation(observation, prediction, quote, self.window, ctx)
        self._run_step(
            step,
            when,
            {
                "trigger": "observation",
                "observation_i": self._obs_i,
                "encoder": getattr(self.encoder, "name", type(self.encoder).__name__),
                "readout": getattr(self.readout, "name", type(self.readout).__name__),
                "neural_ms": self.neural_ms,
                "interval": self.interval,
                "pulses": [list(p) for p in pulses],
            },
            quote=quote,
            compute=compute,
        )
        self._obs_i += 1

    def _run_step(self, step: EngineStep, when: datetime, extra: dict[str, Any], quote: StraddleQuote | None = None, compute: float = 0.0) -> None:
        quote = quote or step.quote or self.engine.last_quote
        if step.intents:
            execute_step(self.engine, step, self.broker, self.ledger, quote_lookup_for(quote))
        self._emit(self._trace(step, extra, compute))

    def _reconcile(self, now: datetime) -> None:
        try:
            events = self.broker.reconcile()
        except UnresolvedOrder as exc:
            self._halt(f"unresolved order during reconciliation: {exc}")
            return
        except Exception as exc:
            self._log(f"reconcile failed: {exc}")
            return
        if not events:
            return
        for step in apply_broker_events(self.engine, self.broker, events, now):
            self._run_step(step, now, {"trigger": "broker"})
        if any(e.get("type") == "intent" for e in events):
            self._write_state()

    def _trace(self, step: EngineStep, extra: dict[str, Any], compute: float = 0.0) -> dict[str, Any]:
        quote = step.quote or self.engine.last_quote
        trace = self.engine.to_trace_step(
            step,
            self.steps_written,
            index=self._index_ltp(),
            vix=self._vix(),
            premium=round(quote.combined_ltp, 2) if quote else None,
            days_to_expiry=days_to_expiry(self.expiry, step.at),
            stimulus_hash=self._last_neural["stimulus_hash"],
            rates_hz=self._last_neural["rates_hz"],
            fixed_decoder=self._last_neural["fixed_decoder"],
            compute_seconds=round(compute, 4),
            technical={"mode": self.mode, **extra},
        )
        self.steps_written += 1
        return trace

    # ----------------------------------------------------------- outputs

    def _emit(self, trace: dict[str, Any]) -> None:
        self._last_trace = trace
        self.last_event_at = self.now()
        line = json.dumps({"type": "step", **trace}, default=_json_default)
        with open(self._events_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._write_state()

    def _log(self, message: str) -> None:
        logger.info("%s", message)
        try:
            line = json.dumps({"type": "log", "t": self.now().isoformat(), "message": message})
            with open(self._events_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    def _set_state(self, state: str) -> None:
        self.state = state
        self._write_state()

    def _halt(self, reason: str) -> None:
        self.halt_reason = reason
        self.ledger.halt(reason)
        if not self.engine.is_halted:
            self.engine.halt(reason)
        self._log(f"HALTED: {reason}")
        self._set_state("halted")

    def state_dict(self) -> dict[str, Any]:
        return {
            "worker": {
                "state": self.state,
                "mode": self.mode,
                "run_dir": str(self.run_dir),
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
                "halt_reason": self.halt_reason,
                "interval": self.interval,
                "trading_date": self.window.trading_date.isoformat() if self.window else None,
                "legs": list(self.legs) if self.legs else None,
                "strike": self.strike,
                "expiry": self.expiry.isoformat() if self.expiry else None,
                "expiry_selection": selection_of(self.settings),
                "steps": self.steps_written,
            },
            "straddle": self.engine.state_snapshot(),
            "engine": self.engine.status(),
            "last_step": self._last_trace,
            "preflight": self.preflight_result,
        }

    def _write_state(self) -> None:
        tmp = self._state_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.state_dict(), default=_json_default, indent=1), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            logger.warning("state.json write failed: %s", exc)
