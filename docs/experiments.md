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

## Pricer

`openfly.experiments.pricer.StraddlePricer(factor=None)`: Black-Scholes
(rate 0) call and put with IV = INDIAVIX / 100 x factor and time to expiry
in trading sessions (full sessions after today up to the weekly expiry plus
the fraction of the current session, over 252 sessions a year). Weekly
expiry is Tuesday from 2025-09-01 (Thursday before); a holiday on the expiry
weekday moves it to the previous session when the calendar knows the dates.

`calibrate()` fits the factor on every listed contract in the cache
(rows within 1 percent moneyness, volume above zero) and writes
`data/features/calibration.json`; the pricer reads that file by default.
With the one listed contract (NIFTY15SEP2623400CE, 2026-09-08 to 09-11) the
factor is 1.006: VIX with trading-time day count reproduces the recorded
prices with a 16 point RMSE. At the Friday 2026-09-11 close (2.0 sessions to
expiry, which is the 3.4 calendar days quoted in the market facts) the model
gives 205.6 points for the 23400 straddle against the recorded 204.0.

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

## Simulator rules

From `settings.strategy`: entries at observation rows between trade_start
(09:20) and last_entry (14:30), one straddle at a time at the ATM strike of
that minute (dynamic re-strike), re-entry after `reentry_cooldown_minutes`,
at most `max_entries_per_day` straddles (0 unlimited). Exits, evaluated per
minute on per-leg closes: per-leg fixed stop at `leg_stop_pct`, then either
both legs out (`on_leg_stop = exit_both`) or the other leg runs on with its
own stop, its target, a readout EXIT or the square-off (`hold_other`);
combined stop (`combined_stop_enabled`), target and lock on the summed
premium; readout EXIT; square-off at 15:15. Legs are sold at ltp - 0.05 and
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
avg_holding_minutes, accuracy with a 95 percent block bootstrap interval
(blocks of 5 days) and p-value, base_rate_above_1, balanced_accuracy,
ridge_accuracy, synthetic_fraction, n_observations.

`passed` requires, on the test window: accuracy above max(0.52, majority
rate) with bootstrap p below 0.05, and net P&L per lot above both the fixed
09:20 control and the random-entry control.
