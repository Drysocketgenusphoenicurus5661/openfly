# Experiments, encoders and readouts

How the sensory encoders, the readouts and the experiment harness fit
together, what they write to disk, and the surfaces other modules call.
Status: built 2026-09-12, runs end to end with the FakeBrain; the connectome
Brain plugs in through `openfly.neural.brain.Brain`.

## Packages

| Package | Contents |
| --- | --- |
| `openfly/sensory` | `make_encoder(name, settings, eye_map)`, encoders A (chart), B (bars, default), C (features), `EyeMap`, `resolve_eye_map(brain)`, `stimulus_hash` |
| `openfly/readout` | `make_readout(name, settings, brain=None)`, `FixedDecoder`, `ReservoirReadout`, `load_readout(dir)` |
| `openfly/experiments` | data access, sessions and expiries, pricer and calibration, minute quotes, costs, observations, targets, reward, feature cache, simulator, metrics, runner, CLI, FakeBrain |

## Command line

```
uv run openfly calibrate-pricer [--moneyness 1.0] [--dry-run]
uv run openfly experiment run --encoder B --readout reservoir --neural-ms 200 \
    --train 2025-08-08:2026-03-31 --validation 2026-04-01:2026-06-30 --test 2026-07-01:2026-09-11 \
    [--limit-days 3] [--interval 1m|5m] [--horizon 60] [--plastic] [--fake-brain] [--seed 0] [--name ...]
uv run openfly experiment list
uv run openfly experiment show <id> [--json]
```

`--fake-brain` uses `openfly.experiments.fakebrain.FakeBrain`, a deterministic
pseudo-random stand-in with every required population and an `eye_map()`,
so the whole harness can be exercised without the connectome graph. Without
it the runner builds `openfly.neural.brain.Brain(half_saturation=..., plastic=...)`
and explains what is missing if it cannot.

## Observation interval and windows

`settings.neural.replay_interval` (default `1m`) sets the bar size: one
observation per completed bar, timestamp at the bar close, trailing window
of 120 bars for the encoders (they use the last 60 plus warm-up). With 1m
bars that is 375 observations a day, about 100,000 for the 271 day history;
`--interval 5m` gives 75 a day for quick runs. `--limit-days N` keeps the
first N trading days of each window.

The horizon (`settings.neural.horizon_minutes`, 60) is always measured in
minutes on the 1 minute index path, whatever the observation interval.

## Data access

`openfly.experiments.data.MarketData` loads NIFTY 1m and INDIAVIX daily bars
from `openfly.market.store.BarStore` (DuckDB at `PATHS.market_db`) when the
module and the file exist, otherwise from the parquet files under
`data/history`. Nothing fetches from the broker: a date that is not stored
raises `MissingData`. `resample(df1m, "5m")` aggregates aligned to 09:15.

## Expiry selection

`settings.strategy.expiry_selection` is `monthly` by default: the strategy
trades the current-month contract, the last Tuesday of the calendar month
(last Thursday before September 2025), shifted back to the previous session
when that day is a holiday known to the calendar (2026-03-31 was one, so the
March 2026 contract expired on 2026-03-30). Once the monthly expiry has
passed the next month's contract is used, including on the expiry day
itself (0 DTE). `weekly` selects the next expiry weekday as before.
`TradingCalendar.monthly_expiry(d)`, `next_expiry(d)` and
`select_expiry(d, selection)` implement this; a calendar carries the
default selection so `days_to_expiry(ts)` needs no expiry. The selection
flows into the observation builder (days to expiry, premium), the quotes
(which expiry's recorded chain may stand in for the synthetic model), the
targets (implied move scaled by trading minutes to the selected expiry), the
reward series, the simulator (every trade records `expiry`) and the runner
(`config.expiry_selection`, `provenance.expiries` per window). The CLI takes
`--expiry monthly|weekly` on `experiment run` and `calibrate-pricer`.

A monthly straddle carries a much larger premium and a smaller gamma than a
weekly one, so for the same expected move the leg stop percentages come out
lower (the same point rise is a smaller fraction of a bigger leg price).

## Pricer

`openfly.experiments.pricer.StraddlePricer(factor=None)`: Black-Scholes
(rate 0) call and put with IV = INDIAVIX / 100 x factor and time to expiry
in trading sessions (full sessions after today up to the weekly expiry plus
the fraction of the current session, over 252 sessions a year). Weekly
expiry is Tuesday from 2025-09-01 (Thursday before); a holiday on the expiry
weekday moves it to the previous session when the calendar knows the dates.

`calibrate(selection=None, settings=None)` fits the factor on the recorded
option chains in the market store (`BarStore.chain(date, expiry)` for every
stored trading date and expiry) plus any parquet option files under
data/history: rows within 1 percent moneyness with volume above zero, only
contracts whose expiry matches the strategy's selection (monthly by
default; when none are recorded yet every contract is used and the result
says so). It writes `data/features/calibration.json` with the factor, the
selection, per-day and per-expiry diagnostics (each expiry's own best
factor); the pricer reads that file by default. Fitted on the weekly
contracts alone the factor is 1.006 (205.6 points for the 23400 weekly
straddle at the Friday 2026-09-11 close against 204.0 recorded); the monthly
figure is reported by `openfly calibrate-pricer`.

## Minute quotes (for the straddle replay and the worker)

```python
from openfly.experiments.quotes import minute_quotes_for, leg_quote
from openfly.experiments.pricer import synthetic_minute_quotes

quotes = minute_quotes_for(date)                  # recorded chain first, synthetic fallback
quotes = synthetic_minute_quotes(date, bars1m, vix_series, expiry)   # always synthetic
sq = quotes(timestamp)                            # StraddleQuote at the ATM strike of that minute
sq = quotes(timestamp, strike=23400.0)            # a specific strike
quotes.pinned_strike = 23400.0                    # hold a strike for later plain calls
q = quotes.leg(timestamp, 23400.0, "PE")          # one leg
q = leg_quote(date, timestamp, 23400.0, "CE")     # module-level, cached per date
quotes.synthetic_fraction, quotes.source          # 0..1, "recorded" | "synthetic" | "mixed"
```

Quotes are `SourcedQuote` / `SourcedStraddleQuote` (subclasses of the
interface types) with a `source` field. ltp is the close of the last
completed 1 minute bar at or before the timestamp, bid = ltp - 0.05, ask =
ltp + 0.05. Recorded chains come from `BarStore().chain(date, expiry)`
(columns trading_date, expiry, strike, option_type, symbol, ts, open, high,
low, close, volume, oi); minutes before a strike's first print, and strikes
outside the stored range, fall back to the synthetic model per minute.

## Reward

`openfly.experiments.reward.RewardSeries.build(bars_by_day, pricer, settings)`
computes, per bar close t, `r_t = clip(1 - realized(t, t+H) / premium(t), -1, 1) - cost_fraction`
and the advantage `r_t - baseline`, the baseline being the mean of r over the
trailing 20 trading days (strictly before t's date; the first day uses the
expanding mean of its own earlier observations). `series.reward_for(key, trade=None)`
accepts a tz-aware datetime, a pandas Timestamp, an ISO string or an integer
index; with `trade={"pnl_inr": ..., "stop_distance_inr": ...}` the clipped
P&L over the stop distance replaces the counterfactual. Module-level
`reward_for(key, trade=None, series=None)` builds a default series from the
local history on first use.

## Targets

Per observation: `realized_points` (absolute 1 minute close-to-close move
over the horizon, clipped at 15:30), `implied_points` (premium scaled by
sqrt(horizon used / trading minutes to expiry)), `y = realized / implied`,
`label = y > 1`, and the forward P&L of a short straddle held for the horizon
with every exit rule applied. Nothing looks past t + H. In this data the
label is true about 11 percent of the time (implied volatility runs above
realized), so accuracy alone is not informative: the runner reports the base
rate and balanced accuracy and tests accuracy against max(0.52, majority rate).

## Feature cache

`data/features/<encoder_hash16>_<neural_ms>_<interval>[_plastic]/<date>.parquet`
plus `meta.json` (neuron ids of the columns, populations, encoder parameters,
brain provenance). Columns per observation: timestamp, counts (int32 fixed
size list), compute_seconds, stimulus_hash, sim_ms, rate_<population> (Hz)
for every population. Default cached populations: DN, MBON, random2000 and
the small named cells (MBON07, MBON11, PAM11, PPL101, DNp20_L, DNp20_R,
DNpe017). A pass warms the brain with one discarded observation at 09:15
per day, runs every observation of the day, writes the day, and skips days
already present, so it resumes after an interruption.

## Volatility-adaptive stops

`settings.strategy.stop_mode` is `adaptive` by default. At every straddle
entry (strategy, fixed 09:20 control, random-entry control and the forward
P&L in the targets alike) the simulator computes, from the minute's prices:

    implied  = combined premium x sqrt(stop_horizon_minutes / trading minutes to expiry)
    realized = std(trailing 60 one-minute log returns) x sqrt(stop_horizon_minutes) x index
    m        = max(implied, realized)
    leg rise      = |delta| x m + 0.5 x gamma x m^2           (Black-Scholes delta and gamma from the pricer)
    combined rise = 0.5 x (gamma_ce + gamma_pe) x m^2 + |delta_ce + delta_pe| x m
    leg_stop_pct      = clip(stop_buffer x leg rise / leg price x 100, leg_stop_min_pct, leg_stop_max_pct)
    combined_stop_pct = clip(stop_buffer x combined rise / combined x 100, combined_stop_min_pct, combined_stop_max_pct)

The percentages are held for the life of that straddle and recomputed for
the next one; nothing is trailed. The trailing return window reaches into
the previous session (`MinuteQuotes.prior_closes`) so an entry at 09:20 has
a full 60 returns. `stop_mode` `fixed` uses `leg_stop_pct` and `stop_pct`.
Every trade in `trades.json` carries `stop_basis` (mode, horizon_minutes,
expected_move_points, implied_move_points, realized_move_points, delta_ce,
delta_pe, gamma, rise_points, leg_stop_pct {ce, pe}, combined_stop_pct) and
the metrics per window report `mean_leg_stop_pct`, `mean_combined_stop_pct`
and `mean_expected_move_points`.

Observed on the NIFTY history: at the 09:20 open the realized move dominates
and leg stops widen to 60 to 70 percent; mid-day they settle near 25
percent. The combined rise of an ATM straddle is small (its net delta is
near zero, so only gamma x m^2 remains), which puts the combined stop at the
10 percent floor for nearly every entry; raise `combined_stop_min_pct` or
`stop_buffer` if a looser combined stop is wanted.

## Simulator rules

From `settings.strategy`: entries at observation rows between trade_start
(09:20) and last_entry (14:30), one straddle at a time at the ATM strike of
that minute (dynamic re-strike), re-entry after `reentry_cooldown_minutes`,
at most `max_entries_per_day` straddles (0 unlimited). Exits, evaluated per
minute on per-leg closes: per-leg stop (adaptive or `leg_stop_pct`), then
either both legs out (`on_leg_stop = exit_both`) or the other leg runs on
with its own stop, its target, a readout EXIT or the square-off
(`hold_other`); combined stop (`combined_stop_enabled`, adaptive or
`stop_pct`), target and lock on the summed premium; readout EXIT; square-off
at 15:15. Legs are sold at ltp - 0.05 and
bought at ltp + 0.05; costs follow `openfly.experiments.costs.round_trip_cost`
(brokerage, STT, exchange, SEBI, stamp, GST).

## Runner and result.json

`ExperimentRunner(config, brain_factory, settings).run()` does: feature pass
over the three windows; targets; reservoir fit on train with the ridge
closed form, selection of populations, alpha and tau on validation by net
P&L per lot after costs (grid in `config.alphas`, `config.taus`,
`config.population_grid`), then the logistic classifier; evaluation of the
three windows; controls (fixed 09:20 entry with the same exits and
re-entries, random entry matched on trade count, shuffled labels refit,
flat); metrics; verdict. Output under `runs/experiments/<id>/`:
`result.json` (id, name, state, config, progress, observations, selection,
metrics per window, controls for the test window, controls_by_window,
curves with cumulative daily P&L per series, passed, verdict, provenance),
`trades.json`, `predictions/<window>.parquet`, `readout/` (npz plus json).

Metrics per window: net_pnl_per_lot, gross_pnl_per_lot, costs_per_lot,
sharpe (daily, sqrt 252), max_drawdown, trades, stop_hits, stop_hits_leg,
target_hits, lock_hits, exit_hits, square_offs, win_rate,
avg_holding_minutes, mean_leg_stop_pct, mean_combined_stop_pct,
mean_expected_move_points, accuracy with a 95 percent block bootstrap interval
(blocks of 5 days) and p-value, base_rate_above_1, balanced_accuracy,
ridge_accuracy, synthetic_fraction, n_observations.

`passed` requires, on the test window: accuracy above max(0.52, majority
rate) with bootstrap p below 0.05, and net P&L per lot above both the fixed
09:20 control and the random-entry control.
