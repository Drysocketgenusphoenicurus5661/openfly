"""Targets for the readouts, one row per observation.

For an observation at bar close t with horizon H (settings.neural.horizon_minutes):

    realized_points  = |index(t + H) - index(t)| on the 1 minute path, inside the session
    implied_points   = synthetic ATM straddle premium at t scaled by sqrt(H_used / minutes to expiry)
    y                = realized_points / implied_points        (the reservoir target)
    label            = y > 1
    pnl_*            = forward P&L of a short straddle entered at t and held for H minutes with the
                       per-leg stops, combined stop, target and lock applied on the minute path

H_used is the horizon clipped at 15:30, so the last observation of a session
has no target (valid = False). Nothing here looks past t + H.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from openfly.experiments.data import MarketData
from openfly.experiments.observations import ObservationBuilder
from openfly.experiments.pricer import implied_move_points
from openfly.experiments.quotes import MinuteQuotes, minute_quotes_for
from openfly.experiments.sessions import SESSION_MINUTES
from openfly.experiments.simulator import Rules, run_trade, square_off_row


def horizon_from_settings(settings: dict | None, default: int = 60) -> int:
    neural = (settings or {}).get("neural", {}) if isinstance(settings, dict) else {}
    return int(neural.get("horizon_minutes", default))


def day_targets(
    d: date,
    builder: ObservationBuilder,
    quotes: MinuteQuotes,
    horizon_minutes: int,
    rules: Rules,
    with_pnl: bool = True,
) -> pd.DataFrame:
    day = quotes.day
    rows = list(builder.day_rows(d))
    n = len(rows)
    out = {
        "timestamp": [builder.close_time(r) for r in rows],
        "date": [d] * n,
        "row": np.zeros(n, dtype=np.int64),
        "minute": np.zeros(n, dtype=np.int64),
        "spot": np.zeros(n),
        "premium": np.zeros(n),
        "days_to_expiry": np.zeros(n),
        "minutes_to_expiry": np.zeros(n),
        "horizon_used": np.zeros(n),
        "realized_points": np.zeros(n),
        "implied_points": np.zeros(n),
        "y": np.full(n, np.nan),
        "label": np.zeros(n, dtype=bool),
        "valid": np.zeros(n, dtype=bool),
        "pnl_points": np.full(n, np.nan),
        "pnl_inr": np.full(n, np.nan),
        "exit_reason": [""] * n,
        "pnl_valid": np.zeros(n, dtype=bool),
    }
    sq_row = square_off_row(quotes, rules)
    vix = builder.market.vix_open(d)
    for i, r in enumerate(rows):
        close_minute = builder.close_minute(r)
        day_row = day.row_for_close_time(close_minute)
        if day_row < 0:
            continue
        spot = float(day.close[day_row])
        days = builder.days_to_expiry(r)
        premium = builder.premium(r, vix)
        target_minute = min(close_minute + horizon_minutes, SESSION_MINUTES)
        end_row = day.row_for_close_time(target_minute)
        used = int(day.minute[end_row] - day.minute[day_row]) if end_row > day_row else 0
        realized = abs(float(day.close[end_row]) - spot) if used > 0 else 0.0
        mte = max(days * SESSION_MINUTES, 1.0)
        implied = float(implied_move_points(premium, used, mte)) if used > 0 else 0.0
        out["row"][i] = day_row
        out["minute"][i] = close_minute
        out["spot"][i] = spot
        out["premium"][i] = premium
        out["days_to_expiry"][i] = days
        out["minutes_to_expiry"][i] = mte
        out["horizon_used"][i] = used
        out["realized_points"][i] = realized
        out["implied_points"][i] = implied
        if used > 0 and implied > 0:
            y = realized / implied
            out["y"][i] = y
            out["label"][i] = y > 1.0
            out["valid"][i] = True
        if with_pnl and day_row < sq_row and used > 0:
            trade = run_trade(quotes, day_row, rules, exit_rows=None, last_row=min(end_row, sq_row),
                              horizon_reason="HORIZON")
            out["pnl_points"][i] = trade.pnl_points
            out["pnl_inr"][i] = trade.net_inr
            out["exit_reason"][i] = trade.exit_reason
            out["pnl_valid"][i] = True
    frame = pd.DataFrame(out)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


def build_targets(
    market: MarketData,
    dates: list[date],
    settings: dict | None = None,
    builder: ObservationBuilder | None = None,
    horizon_minutes: int | None = None,
    rules: Rules | None = None,
    quotes_for=None,
    with_pnl: bool = True,
) -> pd.DataFrame:
    """Targets for every observation of `dates`. `quotes_for(date) -> MinuteQuotes` may be given."""
    builder = builder or ObservationBuilder(market, settings=settings)
    horizon = horizon_minutes or horizon_from_settings(settings)
    rules = rules or Rules.from_settings(settings)
    frames = []
    for d in dates:
        quotes = quotes_for(d) if quotes_for is not None else minute_quotes_for(
            d, market=market, pricer=builder.pricer, settings=settings
        )
        frames.append(day_targets(d, builder, quotes, horizon, rules, with_pnl=with_pnl))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
