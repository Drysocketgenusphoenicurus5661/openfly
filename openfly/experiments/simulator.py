"""Light straddle simulator on the 1 minute path.

Rules (all from settings.strategy):

- entries at observation rows inside the trade window (trade_start to
  last_entry), strictly one straddle at a time, at the ATM strike of that
  minute (dynamic re-strike); after any exit a fresh straddle may be entered
  once `reentry_cooldown_minutes` have passed, capped by
  `max_entries_per_day` (0 means unlimited);
- per-leg stops above each leg's entry price (never trailed); on a leg stop
  either both legs exit ("exit_both") or the other leg keeps running with
  its own stop until its target, a readout EXIT or the square-off
  ("hold_other");
- on the summed premium: combined stop (when combined_stop_enabled), target,
  and the lock (after the premium has fallen lock_after_pct the stop moves to
  the entry credit);
- readout EXIT at observation rows once the straddle is at least
  min_hold_minutes old (stops, targets, lock and square-off are immediate);
  square-off at 15:15.

Stop distances (`stop_mode`): "adaptive" (default) computes them at each
entry from the expected index move over stop_horizon_minutes, the larger of
the move the straddle itself prices for that horizon and the recent realized
move (std of the trailing 60 one-minute log returns x sqrt(horizon) x index),
translated into a premium rise per leg with Black-Scholes delta and gamma,
times stop_buffer, clipped to the configured bounds and held for the life of
that straddle; "fixed" uses leg_stop_pct and stop_pct. Targets
(`target_mode`): "adaptive" (default) sets the target percent to
target_ratio x the adaptive combined stop percent, clipped to
[target_min_pct, target_max_pct], and arms the lock at lock_ratio x target;
"fixed" uses target_pct and lock_after_pct. See `stop_basis`.

Prices are the minute closes of each leg (recorded chain when stored,
otherwise synthetic), sold at ltp minus the spread and bought back at ltp
plus the spread. Costs use the shared formula in costs.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from openfly.experiments.costs import costs_from_settings, round_trip_cost
from openfly.experiments.quotes import MinuteQuotes
from openfly.experiments.sessions import SESSION_MINUTES

EXIT_PRIORITY = ("LEG_STOP", "STOP", "TARGET", "LOCK", "EXIT", "SQUARE_OFF", "HORIZON")


def _hhmm_minute(text: str) -> int:
    hh, mm = (int(x) for x in str(text).split(":"))
    return (hh * 60 + mm) - (9 * 60 + 15)


@dataclass
class Rules:
    stop_pct: float = 25.0
    target_pct: float = 40.0
    lock_after_pct: float = 15.0
    combined_stop_enabled: bool = True
    leg_stop_pct: float = 30.0
    on_leg_stop: str = "hold_other"
    stop_mode: str = "adaptive"
    stop_horizon_minutes: int = 60
    stop_buffer: float = 1.25
    leg_stop_min_pct: float = 15.0
    leg_stop_max_pct: float = 80.0
    combined_stop_min_pct: float = 3.0
    combined_stop_max_pct: float = 50.0
    target_mode: str = "adaptive"
    target_ratio: float = 1.0
    target_min_pct: float = 2.0
    target_max_pct: float = 40.0
    lock_ratio: float = 0.5
    min_hold_minutes: int = 10
    trade_start: str = "09:20"
    last_entry: str = "14:30"
    square_off: str = "15:15"
    reentry_cooldown_minutes: int = 5
    max_entries_per_day: int = 10
    lot_size: int = 65
    lots: int = 1
    spread: float = 0.05
    costs: dict = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: dict | None) -> Rules:
        s = (settings or {}).get("strategy", {}) if isinstance(settings, dict) else {}
        return cls(
            stop_pct=float(s.get("stop_pct", 25.0)),
            target_pct=float(s.get("target_pct", 40.0)),
            lock_after_pct=float(s.get("lock_after_pct", 15.0)),
            combined_stop_enabled=bool(s.get("combined_stop_enabled", True)),
            leg_stop_pct=float(s.get("leg_stop_pct", 30.0)),
            on_leg_stop=str(s.get("on_leg_stop", "hold_other")),
            stop_mode=str(s.get("stop_mode", "adaptive")),
            stop_horizon_minutes=int(s.get("stop_horizon_minutes", 60)),
            stop_buffer=float(s.get("stop_buffer", 1.25)),
            leg_stop_min_pct=float(s.get("leg_stop_min_pct", 15.0)),
            leg_stop_max_pct=float(s.get("leg_stop_max_pct", 80.0)),
            combined_stop_min_pct=float(s.get("combined_stop_min_pct", 3.0)),
            combined_stop_max_pct=float(s.get("combined_stop_max_pct", 50.0)),
            target_mode=str(s.get("target_mode", "adaptive")),
            target_ratio=float(s.get("target_ratio", 1.0)),
            target_min_pct=float(s.get("target_min_pct", 2.0)),
            target_max_pct=float(s.get("target_max_pct", 40.0)),
            lock_ratio=float(s.get("lock_ratio", 0.5)),
            min_hold_minutes=int(s.get("min_hold_minutes", 10)),
            trade_start=str(s.get("trade_start", "09:20")),
            last_entry=str(s.get("last_entry", "14:30")),
            square_off=str(s.get("square_off", "15:15")),
            reentry_cooldown_minutes=int(s.get("reentry_cooldown_minutes", 5)),
            max_entries_per_day=int(s.get("max_entries_per_day", 10)),
            lot_size=int(s.get("lot_size", 65)),
            lots=int(s.get("lots", 1)),
            costs=costs_from_settings(settings),
        )

    @property
    def trade_start_minute(self) -> int:
        return _hhmm_minute(self.trade_start)

    @property
    def last_entry_minute(self) -> int:
        return _hhmm_minute(self.last_entry)

    @property
    def square_off_minute(self) -> int:
        return _hhmm_minute(self.square_off)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    date: str
    strike: float
    entry_row: int
    entry_minute: int
    exit_row: int
    exit_minute: int
    call_entry: float
    put_entry: float
    credit: float
    call_exit: float
    put_exit: float
    call_exit_row: int
    put_exit_row: int
    call_reason: str
    put_reason: str
    exit_reason: str
    pnl_points: float
    gross_inr: float
    cost_inr: float
    net_inr: float
    minutes_held: int
    synthetic_fraction: float
    source: str
    leg_stops: int
    leg_stop_pct_ce: float = 30.0
    leg_stop_pct_pe: float = 30.0
    combined_stop_pct: float = 25.0
    target_pct: float = 40.0
    lock_after_pct: float = 15.0
    stop_basis: dict = field(default_factory=dict)
    expiry: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _first(cond: np.ndarray) -> int:
    idx = np.flatnonzero(cond)
    return int(idx[0]) if idx.size else int(cond.size)


def square_off_row(quotes: MinuteQuotes, rules: Rules) -> int:
    """Row whose bar completes at the square-off minute (or the last row of a short session)."""
    row = quotes.day.row_for_close_time(rules.square_off_minute)
    if row < 0:
        return quotes.n - 1
    return min(row, quotes.n - 1)


def stop_basis(quotes: MinuteQuotes, entry_row: int, strike: float, rules: Rules, ce: float, pe: float) -> dict:
    """Stop, target and lock percentages for one straddle entered at `entry_row` (held for its life).

    adaptive stops: m = max(implied, realized) over stop_horizon_minutes, with
    implied = combined premium x sqrt(horizon / minutes to expiry) and
    realized = std(trailing 60 one-minute log returns) x sqrt(horizon) x index;
    leg rise = |delta| x m + 0.5 x gamma x m^2, combined rise = 0.5 x (gamma_ce +
    gamma_pe) x m^2 + |delta_ce + delta_pe| x m (the straddle's net delta);
    percent = stop_buffer x rise / price x 100, clipped to the configured bounds.
    adaptive target: target_ratio x the adaptive combined stop percent, clipped
    to [target_min_pct, target_max_pct]; the lock arms at lock_ratio x target.
    """
    horizon = int(rules.stop_horizon_minutes)
    combined = float(ce + pe)
    adaptive_stops = rules.stop_mode == "adaptive"
    adaptive_target = rules.target_mode == "adaptive"
    if not adaptive_stops and not adaptive_target:
        return {
            "mode": "fixed",
            "target_mode": "fixed",
            "horizon_minutes": horizon,
            "expected_move_points": None,
            "implied_move_points": None,
            "realized_move_points": None,
            "leg_stop_pct": {"ce": float(rules.leg_stop_pct), "pe": float(rules.leg_stop_pct)},
            "combined_stop_pct": float(rules.stop_pct),
            "target_pct": float(rules.target_pct),
            "lock_after_pct": float(rules.lock_after_pct),
        }
    day = quotes.day
    spot = float(day.close[entry_row])
    days = float(quotes.days_to_expiry[entry_row])
    mte = max(days * SESSION_MINUTES, 1.0)
    implied = combined * float(np.sqrt(min(1.0, horizon / mte)))
    closes = quotes.trailing_closes(entry_row, 61)
    rets = np.diff(np.log(np.clip(closes, 1e-9, None))) if closes.size >= 2 else np.zeros(0)
    realized = float(np.std(rets) * np.sqrt(horizon) * spot) if rets.size >= 2 else 0.0
    m = max(implied, realized)
    g = quotes.pricer.greeks(spot, strike, days, quotes.vix)
    gamma = g["gamma"] if np.isfinite(g["gamma"]) else 0.0
    d_ce = g["delta_ce"] if np.isfinite(g["delta_ce"]) else 0.5
    d_pe = g["delta_pe"] if np.isfinite(g["delta_pe"]) else -0.5
    rise_ce = abs(d_ce) * m + 0.5 * gamma * m * m
    rise_pe = abs(d_pe) * m + 0.5 * gamma * m * m
    rise_comb = gamma * m * m + abs(d_ce + d_pe) * m
    floor = max(quotes.tick, 1e-6)

    def pct(rise: float, price: float, lo: float, hi: float) -> float:
        raw = rules.stop_buffer * rise / max(price, floor) * 100.0
        return float(np.clip(raw, lo, hi))

    adaptive_combined = pct(rise_comb, combined, rules.combined_stop_min_pct, rules.combined_stop_max_pct)
    if adaptive_stops:
        leg_ce = pct(rise_ce, ce, rules.leg_stop_min_pct, rules.leg_stop_max_pct)
        leg_pe = pct(rise_pe, pe, rules.leg_stop_min_pct, rules.leg_stop_max_pct)
        combined_pct = adaptive_combined
    else:
        leg_ce = leg_pe = float(rules.leg_stop_pct)
        combined_pct = float(rules.stop_pct)
    if adaptive_target:
        target_pct = float(np.clip(rules.target_ratio * adaptive_combined, rules.target_min_pct, rules.target_max_pct))
        lock_pct = float(rules.lock_ratio * target_pct)
    else:
        target_pct = float(rules.target_pct)
        lock_pct = float(rules.lock_after_pct)
    return {
        "mode": "adaptive" if adaptive_stops else "fixed",
        "target_mode": "adaptive" if adaptive_target else "fixed",
        "horizon_minutes": horizon,
        "expected_move_points": float(m),
        "implied_move_points": float(implied),
        "realized_move_points": float(realized),
        "delta_ce": float(d_ce),
        "delta_pe": float(d_pe),
        "gamma": float(gamma),
        "rise_points": {"ce": float(rise_ce), "pe": float(rise_pe), "combined": float(rise_comb)},
        "leg_stop_pct": {"ce": leg_ce, "pe": leg_pe},
        "combined_stop_pct": combined_pct,
        "adaptive_combined_stop_pct": adaptive_combined,
        "target_pct": target_pct,
        "lock_after_pct": lock_pct,
    }


def run_trade(
    quotes: MinuteQuotes,
    entry_row: int,
    rules: Rules,
    exit_rows: np.ndarray | None = None,
    last_row: int | None = None,
    strike: float | None = None,
    horizon_reason: str = "SQUARE_OFF",
) -> Trade:
    """Enter a short straddle at `entry_row` and walk it forward to its exit."""
    if last_row is None:
        last_row = square_off_row(quotes, rules)
    last_row = int(min(max(last_row, entry_row), quotes.n - 1))
    if strike is None:
        strike = float(quotes.atm_strikes[entry_row])
    call, put, synthetic = quotes.straddle_path(strike)
    ce, pe = float(call[entry_row]), float(put[entry_row])
    credit = ce + pe
    basis = stop_basis(quotes, entry_row, strike, rules, ce, pe)
    leg_mult_ce = 1.0 + basis["leg_stop_pct"]["ce"] / 100.0
    leg_mult_pe = 1.0 + basis["leg_stop_pct"]["pe"] / 100.0
    stop_mult = 1.0 + basis["combined_stop_pct"] / 100.0
    target_mult = 1.0 - basis["target_pct"] / 100.0
    lock_mult = 1.0 - basis["lock_after_pct"] / 100.0

    def finish(call_row, call_exit, put_row, put_exit, call_reason, put_reason, exit_reason, leg_stops):
        return _finish(quotes, strike, entry_row, ce, pe, call_row, call_exit, put_row, put_exit,
                       call_reason, put_reason, exit_reason, synthetic, rules, leg_stops, basis)

    r0 = entry_row + 1
    if r0 > last_row:
        # Nothing to walk: exit at the entry row (degenerate window).
        return finish(entry_row, ce, entry_row, pe, "SQUARE_OFF", "SQUARE_OFF", "SQUARE_OFF", 0)
    cs = call[r0 : last_row + 1]
    ps = put[r0 : last_row + 1]
    comb = cs + ps
    n = cs.size
    exit_mask = None
    if exit_rows is not None:
        exit_mask = np.asarray(exit_rows, dtype=bool)[r0 : last_row + 1].copy()
        # A readout EXIT counts only once the straddle is min_hold_minutes old.
        age = quotes.day.minute[r0 : last_row + 1] - quotes.day.minute[entry_row]
        exit_mask &= age >= int(rules.min_hold_minutes)

    t_call = _first(cs >= ce * leg_mult_ce)
    t_put = _first(ps >= pe * leg_mult_pe)
    t_stop = _first(comb >= credit * stop_mult) if rules.combined_stop_enabled else n
    t_target = _first(comb <= credit * target_mult)
    t_lock_trig = _first(comb <= credit * lock_mult)
    t_lock = n
    if t_lock_trig < n - 1:
        after = _first(comb[t_lock_trig + 1 :] >= credit)
        t_lock = t_lock_trig + 1 + after if after < comb.size - t_lock_trig - 1 else n
    t_exit = _first(exit_mask) if exit_mask is not None else n
    t_end = n - 1  # square-off (or horizon end)

    t_leg = min(t_call, t_put)
    t_first = min(t_leg, t_stop, t_target, t_lock, t_exit, t_end)
    leg_stops = 0

    if t_first == t_leg and t_leg < n:
        call_stopped = t_call == t_leg
        put_stopped = t_put == t_leg
        leg_stops = int(call_stopped) + int(put_stopped)
        row = r0 + t_leg
        both = call_stopped and put_stopped
        if both or rules.on_leg_stop != "hold_other" or t_leg == t_end:
            # Both legs leave at this minute: the stopped leg on its stop, the other one
            # because the mode says exit_both or because the session is ending.
            other = horizon_reason if t_leg == t_end else "LEG_STOP_OTHER"
            reason_c = "LEG_STOP" if call_stopped else other
            reason_p = "LEG_STOP" if put_stopped else other
            return finish(row, float(call[row]), row, float(put[row]), reason_c, reason_p, "LEG_STOP", leg_stops)
        # hold_other: the stopped leg leaves, the other continues on its own rules.
        if call_stopped:
            keep, keep_entry, keep_mult, kept_name = ps, pe, leg_mult_pe, "put"
        else:
            keep, keep_entry, keep_mult, kept_name = cs, ce, leg_mult_ce, "call"
        seg = slice(t_leg + 1, n)
        k = keep[seg]
        m = k.size
        if m == 0:
            other_row, other_price, other_reason = row, float(keep[t_leg]), "SQUARE_OFF"
        else:
            u_stop = _first(k >= keep_entry * keep_mult)
            u_target = _first(k <= keep_entry * target_mult)
            u_exit = _first(exit_mask[seg]) if exit_mask is not None else m
            u_end = m - 1
            u = min(u_stop, u_target, u_exit, u_end)
            if u == u_stop:
                other_reason = "LEG_STOP"
                leg_stops += 1
            elif u == u_target:
                other_reason = "TARGET"
            elif u == u_exit:
                other_reason = "EXIT"
            else:
                other_reason = horizon_reason
            other_row = r0 + t_leg + 1 + u
            other_price = float(k[u])
        if kept_name == "put":
            return finish(row, float(call[row]), other_row, other_price, "LEG_STOP", other_reason, "LEG_STOP", leg_stops)
        return finish(other_row, other_price, row, float(put[row]), other_reason, "LEG_STOP", "LEG_STOP", leg_stops)

    if t_first == t_stop:
        reason = "STOP"
    elif t_first == t_target:
        reason = "TARGET"
    elif t_first == t_lock:
        reason = "LOCK"
    elif t_first == t_exit:
        reason = "EXIT"
    else:
        reason = horizon_reason
    row = r0 + t_first
    return finish(row, float(call[row]), row, float(put[row]), reason, reason, reason, leg_stops)


def _finish(quotes, strike, entry_row, ce, pe, call_row, call_exit, put_row, put_exit, call_reason, put_reason,
            exit_reason, synthetic, rules: Rules, leg_stops: int, basis: dict) -> Trade:
    day = quotes.day
    exit_row = max(call_row, put_row)
    spread = rules.spread
    pnl_points = (ce - spread - (call_exit + spread)) + (pe - spread - (put_exit + spread))
    qty = rules.lot_size * rules.lots
    gross = pnl_points * qty
    cost = round_trip_cost(ce + pe, call_exit + put_exit, rules.lot_size, rules.lots, rules.costs)
    seg = synthetic[entry_row : exit_row + 1]
    frac = float(seg.mean()) if seg.size else 1.0
    return Trade(
        date=day.date.isoformat(),
        strike=float(strike),
        entry_row=int(entry_row),
        entry_minute=int(day.minute[entry_row] + 1),
        exit_row=int(exit_row),
        exit_minute=int(day.minute[exit_row] + 1),
        call_entry=float(ce),
        put_entry=float(pe),
        credit=float(ce + pe),
        call_exit=float(call_exit),
        put_exit=float(put_exit),
        call_exit_row=int(call_row),
        put_exit_row=int(put_row),
        call_reason=call_reason,
        put_reason=put_reason,
        exit_reason=exit_reason,
        pnl_points=float(pnl_points),
        gross_inr=float(gross),
        cost_inr=float(cost),
        net_inr=float(gross - cost),
        minutes_held=int(day.minute[exit_row] - day.minute[entry_row]),
        synthetic_fraction=frac,
        source="synthetic" if frac >= 1.0 else ("recorded" if frac <= 0.0 else "mixed"),
        leg_stops=int(leg_stops),
        leg_stop_pct_ce=float(basis["leg_stop_pct"]["ce"]),
        leg_stop_pct_pe=float(basis["leg_stop_pct"]["pe"]),
        combined_stop_pct=float(basis["combined_stop_pct"]),
        target_pct=float(basis["target_pct"]),
        lock_after_pct=float(basis["lock_after_pct"]),
        stop_basis=basis,
        expiry=quotes.expiry.isoformat() if hasattr(quotes.expiry, "isoformat") else str(quotes.expiry),
    )


def observation_rows(quotes: MinuteQuotes, interval_minutes: int) -> np.ndarray:
    """Rows whose bar completes on the observation grid (every `interval_minutes`)."""
    close_minute = quotes.day.minute + 1
    return np.flatnonzero(close_minute % int(interval_minutes) == 0)


def eligible_entry_rows(quotes: MinuteQuotes, rules: Rules, obs_rows: np.ndarray) -> np.ndarray:
    close_minute = quotes.day.minute[obs_rows] + 1
    ok = (close_minute >= rules.trade_start_minute) & (close_minute <= rules.last_entry_minute)
    return obs_rows[ok]


def simulate_day(
    quotes: MinuteQuotes,
    rules: Rules,
    obs_rows: np.ndarray,
    entry_signal: np.ndarray | None = None,
    exit_signal: np.ndarray | None = None,
    entry_rows: np.ndarray | None = None,
    always_enter: bool = False,
) -> list[Trade]:
    """One session. `entry_signal` and `exit_signal` are boolean arrays over rows.

    Modes: readout (entry_signal True at ENTER rows), fixed (`always_enter`:
    enter at the first eligible observation and re-enter after each exit),
    random (`entry_rows`: candidate rows, used when not in position or cooling
    down). Exits from `exit_signal` apply in every mode when given.
    """
    eligible = eligible_entry_rows(quotes, rules, obs_rows)
    if eligible.size == 0:
        return []
    last = square_off_row(quotes, rules)
    if always_enter:
        candidates = eligible
    elif entry_rows is not None:
        wanted = set(int(r) for r in np.asarray(entry_rows))
        candidates = np.array([r for r in eligible if int(r) in wanted], dtype=np.int64)
    else:
        sig = np.asarray(entry_signal, dtype=bool) if entry_signal is not None else np.zeros(quotes.n, bool)
        candidates = eligible[sig[eligible]]
    trades: list[Trade] = []
    cursor_minute = -1
    for r in candidates:
        r = int(r)
        if r >= last:
            break
        if quotes.day.minute[r] + 1 < cursor_minute:
            continue
        if rules.max_entries_per_day and len(trades) >= rules.max_entries_per_day:
            break
        trade = run_trade(quotes, r, rules, exit_rows=exit_signal, last_row=last)
        trades.append(trade)
        cursor_minute = trade.exit_minute + rules.reentry_cooldown_minutes
    return trades


@dataclass
class WindowResult:
    trades: list[Trade]
    daily_pnl: pd.Series  # net INR per day (0 on days without trades), index date
    synthetic_fraction: float
    days: list[date]

    def trade_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


def simulate_window(
    days: list[tuple[date, MinuteQuotes]],
    rules: Rules,
    interval_minutes: int,
    signals=None,
    always_enter: bool = False,
    entry_rows_by_day: dict | None = None,
) -> WindowResult:
    """Simulate a list of (date, quotes). `signals(date, quotes) -> (entry_bool, exit_bool)`."""
    trades: list[Trade] = []
    pnl = {}
    synth = []
    dates = []
    for d, quotes in days:
        obs_rows = observation_rows(quotes, interval_minutes)
        entry = exit_ = None
        if signals is not None:
            entry, exit_ = signals(d, quotes)
        rows = entry_rows_by_day.get(d) if entry_rows_by_day is not None else None
        day_trades = simulate_day(
            quotes, rules, obs_rows,
            entry_signal=entry, exit_signal=exit_,
            entry_rows=rows if entry_rows_by_day is not None else None,
            always_enter=always_enter,
        )
        trades.extend(day_trades)
        pnl[d] = float(sum(t.net_inr for t in day_trades))
        synth.append(quotes.synthetic_fraction)
        dates.append(d)
    series = pd.Series(pnl, dtype="float64")
    series.index.name = "date"
    return WindowResult(
        trades=trades,
        daily_pnl=series,
        synthetic_fraction=float(np.mean(synth)) if synth else 1.0,
        days=dates,
    )
