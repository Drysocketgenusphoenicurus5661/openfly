# Straddle engine, execution layer and worker

Status: built 2026-09-12. Covers `openfly/straddle`, `openfly/execution` and
`openfly/worker`, their tests under `tests/straddle`, `tests/execution` and
`tests/worker`, and the names these packages expect from the neural, sensory,
readout, market and experiments packages.

Nothing in this layer is neural output. The engine, guard, ledger and brokers
are ordinary risk engineering (docs/PLAN.md, section 1, rule 3).

## 1. Files and public API

### openfly/straddle/guard.py

- `GuardCheck(name, ok, detail)`, `GuardResult(allowed, checks)` with
  `failed`, `summary()` ("All 18 checks passed." or "The guard vetoed: ...")
  and `to_dict()` in the api-spec shape.
- `GuardContext` (dataclass): now, window, is_trading_day, quote, quote_age_s,
  prediction, vix, days_to_expiry, observation_index, index_now, day_pnl,
  entries_today, last_exit_at, in_position, lots, lots_detail,
  margin_available, margin_per_lot, stop_file_present, halted, halt_reason,
  pending_intent. The engine fills in what it knows (time, window, quote,
  prediction, VIX, P&L, counters, lots); the caller supplies the rest.
- `Guard(settings).check_entry(ctx)` runs, in order: trading_day,
  trade_window, last_entry, expiry_min_dte, vix_ceiling, spread, quote_age,
  index_move, daily_loss_limit, entries_per_day, reentry_cooldown,
  position_flat, lots_bounds, margin_available, stop_file, halted,
  pending_intent, prediction_present. `check_exit(ctx)` runs trading_day,
  halted, pending_intent, quote_present. Each check is also a public method.
- Details are plain language, for example "10:20 is inside the trade window
  09:20 to 14:30", "INDIAVIX 21.3 is above the ceiling 20", "last exit at
  10:37, cooldown of 5 minutes runs until 10:42". Checks that cannot run for
  lack of data (no quote age in replay, no margin figure, no depth) pass and
  say "check skipped" in the detail. The spread check measures each leg's
  bid-ask width as a percentage of the combined premium.

### openfly/straddle/engine.py

- `State`: FLAT, ENTERING, IN_POSITION, EXITING, HALTED.
- `Action`: ENTER, EXIT, STOP, STOP_LEG, TARGET, LOCK, SQUARE_OFF, REENTRY,
  HOLD, VETO, NONE.
- `StraddleEngine(settings, cost_model, guard=None)`:
  - `start_day(window)`: resets the per-day counters on a new date.
  - `on_observation(observation, prediction, quote, window, guard_ctx=None) -> EngineStep`
  - `on_tick(quote, now) -> EngineStep` (action NONE when nothing happens).
  - `on_execution(intent_id, fills, status=None, detail="", stop_orders=None) -> EngineStep`:
    the caller reports the broker outcome; the current step is updated in
    place (fills, snapshot, narrative).
  - `on_leg_stop(symbol, fills, now) -> EngineStep`: a broker-side leg stop
    executed (action STOP_LEG).
  - `on_stops_placed(records)`: order ids and statuses of resting stops.
  - `repair_intent(detail) -> Intent | None`: REPAIR after a one-legged fill.
  - `square_off_now(now, reason) -> EngineStep`: STOP file or manual square-off.
  - `size_lots(entry_credit, margin_available=None, margin_per_lot=None) -> (lots, math)`
  - `state_snapshot()`: exactly the GET /api/straddle shape (legs carry
    stop_price, stop_order_id, stop_status, status).
  - `status()`: state, counters, day P&L, lock state, pending intent.
  - `to_trace_step(step, i, *, index, vix, premium, days_to_expiry, stimulus_hash,
    stimulus_png, rates_hz, fixed_decoder, compute_seconds, technical) -> dict`:
    the api-spec step shape; the caller fills the neural fields.
  - `halt(reason)`, `day_pnl()`, properties `in_position`, `is_halted`,
    `is_flat_book`, `pending_intent`, `deadline`.
- `EngineStep`: at, trigger (observation, tick, broker, manual), action,
  guard, intents, fills, straddle, pnl_day, narrative, technical, prediction,
  quote, observation, detail, sizing, closed, entry_failed, notes.

Rules implemented:

- Entry: prediction ENTER, guard allows, FLAT. Sizing
  lots = floor(risk_budget_pct percent of capital / (credit x stop_pct percent x lot_size)),
  clipped to [1, max_lots], to `strategy.lots` when it is above 0 (the
  `--lots` request is a cap, not a floor) and to floor(margin_available /
  margin_per_lot) when a margin figure is given. With the market facts (204
  point credit, 25 percent stop, INR 10 lakh, 1 percent budget) that is 3 lots.
- Combined stop at credit x (1 + stop_pct/100) when `combined_stop_enabled`,
  target at credit x (1 - target_pct/100), lock: once the premium is
  lock_after_pct below the credit the stop moves to the credit.
- Per-leg fixed stops at entry_price x (1 + leg_stop_pct/100) rounded up to
  the tick. `leg_stop_mode` broker: a STOPS intent (one BUY SL-M leg per
  option, `StopLeg.trigger_price`) is emitted right after the entry fills.
  `leg_stop_mode` software: `on_tick` compares each leg's LTP with its stop
  and emits an EXIT_LEG intent (action STOP_LEG). The stops are never
  trailed; the lock applies only to the combined stop.
- After a leg stop with `on_leg_stop` hold_other, the other leg keeps its own
  fixed stop; the combined stop and lock no longer apply (the combined
  premium then includes the stopped leg's exit price), while the target,
  readout EXIT and square-off still do. With exit_both the remaining leg is
  exited at once in the same step.
- Early exit on prediction EXIT (exit guard), time exit at square_off minus
  square_off_lead_seconds (15:14:30 by default) on the first tick at or past
  the deadline.
- Dynamic straddles: after any exit the engine may enter a fresh straddle at
  the strike of the latest StraddleQuote once `reentry_cooldown_minutes` have
  passed (guard check reentry_cooldown) and `max_entries_per_day` allows (0
  means unlimited). Strictly one straddle at a time. The first entry of the
  day is action ENTER, later ones REENTRY; narratives number them
  ("Straddle 2 of the day, re-entry after the stop at 10:37: sold ...").
- Expiry day is allowed when min_days_to_expiry is 0.
- An exit whose orders are rejected is retried on the next tick; three
  failures halt the engine.
- Strike guarantee. The strike of a new entry is computed from the latest
  StraddleQuote at that minute (the worker re-resolves the chain snapshot at
  every observation while flat; run_day asks the pricer for the ATM when
  flat), so straddles re-strike over the day. Once a straddle is open, every
  exit (leg stop, combined stop, target, readout EXIT, square-off, repair)
  closes exactly the contracts that were entered: `engine.exit_legs()` builds
  the BUY legs from the position's own leg states (the same CE and PE
  symbols, strike and expiry, with each leg's open quantity), never from a
  freshly computed ATM; after a leg stop only the remaining leg is included.
  The dispatcher cross-checks every closing leg against `Ledger.open_legs()`
  (net quantity per symbol from the recorded fills) before sending and halts
  the worker on any disagreement instead of sending the basket. While a
  straddle is open the worker keeps quoting the open legs, and run_day passes
  the position's strike to the quote source, so level checks always see the
  right contracts.

P&L conventions: `straddle.pnl` on the card is gross (sold value minus
bought value minus the liquidation value of the open legs), matching the
api-spec example. `pnl_day` is the realized net P&L of the closed straddles
plus the open straddle's gross P&L minus its booked costs. Each closed
straddle is recorded in `engine.closed` with gross, costs and net.

### openfly/straddle/replay.py

- `run_day(date, bars, minute_quotes, brain, encoder, readout, settings, broker,
  session_window, observation_builder=None, *, interval="1m", vix=None,
  vix_bars=(), ledger=None, reward_fn=None, reward_offset=0, guard_context=None,
  trailing_bars=60, engine=None, cost_model=None, on_step=None, render_png=True) -> DayTrace`
  - observes on every completed bar of `interval` (375 per day at 1m); `bars`
    may include earlier days, which seed the trailing 60 bar window;
  - ticks every minute from market open to close plus the square-off
    deadline: simulated broker stops (`broker.on_quote`) first, then
    `engine.on_tick`, then the observation of a bar closing at that minute;
  - `minute_quotes(timestamp, strike=None)`: when a straddle is open the
    position's strike is passed so the quotes track the open legs; a
    one-argument callable is accepted too;
  - plastic arm: when `settings.neural.plastic` is true and a reward function
    is available (argument, else `openfly.experiments.reward.reward_for`),
    the reward of the observation `horizon_minutes` earlier is delivered as a
    pulse ("PAM11", 20 x r mV, 200 ms) for r > 0 or ("PPL101", 20 x |r| mV,
    200 ms) for r < 0, clamped to [-1, 1]. Never derived from equity ticks.
- Helpers: `population_rates`, `fixed_decoder` (DNp20 right minus left, gated
  by DNpe017, 2 Hz), `stimulus_hash`, `build_observation`, `days_to_expiry`,
  `default_session_window`, `reward_pulses`, `interval_minutes`.
- `DayTrace(date, steps, summary, config, stimulus_pngs)`: `to_json()`,
  `from_json()`, `save(run_dir)` (trace.json plus stimulus/{i}.png when the
  encoder has `render_png(stimulus) -> bytes`; the step's `stimulus_png` is
  then the relative path, which the API maps to its URL).
- Summary: pnl (net), pnl_gross, costs, trades, entries, stop_hits,
  target_hits, time_exits, early_exits, stop_hits_leg, vetoes, observations,
  steps, compute_seconds, halted, open_at_close, closed.

Trace steps: one per observation plus one per tick or broker event that did
something (STOP, TARGET, LOCK, SQUARE_OFF, STOP_LEG, fills). Tick steps carry
`trigger` "tick" or "broker", the neural fields of the last observation and
`technical.observation_i` of that observation; `prediction` is null and
`guard` is null on steps where the guard did not run.

### openfly/execution/types.py

- Intent kinds: ENTRY, EXIT, SQUARE_OFF, REPAIR, STOPS, EXIT_LEG.
- `StopLeg(Leg)` adds `trigger_price`.
- `BrokerProtocol`: preflight(symbols, lots), execute(intent, quote_lookup),
  reconcile(), positions(), stop_orders(). `ClientProtocol`: the subset of
  the OpenAlgo client used (basketorder, placeorder, orderstatus, orderbook,
  positionbook, cancelorder, analyzer_status; funds, symbol and margin are
  optional).
- Exceptions: `BrokerError`, `LostResponse`, `OneLegged`, `PendingIntentError`.
- `EXECUTION_DEFAULTS` (read from `settings["execution"]`, all optional):
  order_type LIMIT, limit_offset_ticks 2, fill_timeout_s 10, poll_interval_s
  0.5, reconcile_window_s 900, reconcile_interval_s 5, repair_policy unwind,
  margin_per_lot 190000, tick_interval_s 0.5.
- `quote_lookup_for(straddle_quote)`, `classify_fills`, `is_balanced`,
  `round_to_tick`, `ceil_to_tick`, `floor_to_tick`.

### openfly/execution/costs.py

- `order_cost_inr(model, side, premium, quantity)` and
  `round_trip_inr(model, credit_points, lots, lot_size)` adapt any cost model
  with the openfly.market.costs.CostModel signature (results may be floats or
  objects with `.total`).
- `SimpleCostModel.from_settings(settings)`: the fallback with the documented
  formula (INR 119.05 for one lot of a 204 point straddle round trip).
- `load_cost_model(settings)`: the market package's CostModel when
  importable, else the fallback.

### openfly/execution/ledger.py

`Ledger(run_dir, cost_model=None)` opens `run_dir/ledger.db` (WAL,
synchronous FULL). Tables: intents (intent_id, kind, status, created_at,
updated_at, reason, detail, payload json), legs (per intent and symbol:
side, quantity, limit_price, order_id, status, filled_qty, average_price),
fills (idempotent on intent_id, symbol, side, order_id; cost booked with the
cost model), stop_orders (resting SL-M orders with trigger_price, order_id,
status pending, triggered, cancelled, rejected), meta (trading_date,
entries_today, day_pnl, halted, checkpoint).

Methods: reserve(intent) (PREPARED; refuses while another intent is PREPARED,
UNKNOWN or ACCEPTED), mark(intent_id, status, detail), status(intent_id),
record_leg_order(...), settle(intent_id, fills, status=None) (idempotent;
derives SETTLED, PARTIAL or REJECTED when no status is given), add_fills
(stop executions), pending(), intents(limit) in the api-spec shape, get,
fills, day_pnl() (realized from fills, net of booked costs), halt(reason),
halted(), clear_halt(), set_trading_date, entries_today, increment_entries,
save_checkpoint, load_checkpoint, record_stop_order, update_stop_order,
stop_orders(active_only, symbol), last_stop_for(symbol).

### openfly/execution/brokers.py

- `ReplayBroker(cost_model, slippage_ticks=1, tick=0.05)`: SELL at bid minus
  slippage, BUY at ask plus slippage, LTP when depth is missing; books costs,
  tracks positions, holds simulated stop orders and triggers them from
  `on_quote(quote, now)` at max(ask, trigger) plus slippage; cancels stops on
  the legs before any exit or repair. `gross_pnl()`, `net_pnl()`, `fills`,
  `costs`, `events`, `cancelled_stops`.
- `OpenAlgoBroker(client, settings, ledger, mode="paper", clock=time.time, sleep=time.sleep)`:
  - preflight(symbols, lots): analyzer status matches the mode, funds against
    the margin (margin endpoint when available, else margin_per_lot), symbol
    master resolves the legs, no open orders on the legs from other
    strategies, no pending intent, ledger not halted;
  - execute: for exits and repairs cancels the resting stops on those legs
    first (a stop found already executed becomes a fill and its leg is
    dropped from the basket); marks UNKNOWN before the network call; sends
    one basketorder tagged with the strategy with marketable LIMIT prices (LTP
    minus or plus offset ticks) or MARKET; inspects results per leg; polls
    orderstatus per leg until complete, rejected or cancelled or the timeout;
    cancels the unfilled remainder; settles; raises `OneLegged` on an uneven
    fill (balanced partial fills settle as PARTIAL);
  - STOPS intents are placed as individual placeorder calls with pricetype
    SL-M and trigger_price, polled once for an immediate rejection, recorded
    in stop_orders, and the intent settles SETTLED, PARTIAL or REJECTED;
  - reconcile(): PREPARED intents that were never sent become REJECTED;
    UNKNOWN intents are matched in the orderbook by symbol, side, quantity,
    product, strategy tag (when the entry carries one), not a stop order type,
    and timestamp within [created - 60 s, created + reconcile_window_s]; a
    unique match per leg is adopted and polled, anything else raises
    `UnresolvedOrder`; ACCEPTED intents are re-polled. Then stop maintenance:
    a pending stop that completed becomes a `stop_triggered` event, a
    cancelled or rejected stop on a leg that is still short is re-placed
    (`stop_replaced`), and an open short leg with no active stop is re-placed
    from its last record (`stop_missing` when there is none);
  - positions(): positionbook filtered to the product (non-zero quantities);
  - stop_orders(): the ledger's records.
- Helpers `order_state(data)` and `parse_broker_time(value)` normalise
  OpenAlgo order dicts and timestamps.

### openfly/execution/dispatch.py

- `execute_step(engine, step, broker, ledger, quote_lookup)`: reserves,
  executes and settles every intent of a step in order, including the ones
  the engine appends while executing (STOPS after an entry, REPAIR after a
  one-legged fill). `OneLegged` -> `engine.repair_intent`; `LostResponse` ->
  `broker.reconcile()`; `UnresolvedOrder` -> ledger and engine halt and the
  exception propagates; other `BrokerError`s reject the intent.
- `apply_broker_events(engine, broker, events, now, step=None)`: feeds
  reconcile() or ReplayBroker.on_quote() events into the engine and returns
  the STOP_LEG steps it produced.

Repair policy: "unwind" (default) buys back whatever is short after a
one-legged entry; "complete" sells the missing leg. Exits are always
completed (the remaining short leg is bought back). A repair that leaves the
book unbalanced halts the engine.

### openfly/worker/loop.py and cli.py

- `Worker(settings, mode, run_dir, brain, encoder, readout, client, feed, calendar, chain, broker, *, ledger=None, cost_model=None, clock=None, sleep=None, history_bars=None, trading_date=None, engine=None, reward_fn=None, interval=None)`, `run() -> int`
  (0 done, 2 halted or refused, 3 lock held). Holds `run_dir/worker.lock`
  (filelock), refuses live mode without OPENFLY_LIVE=I_ACCEPT_REAL_TRADES,
  asks the calendar for the window and trading day, waits for the session,
  resolves the ATM legs from `chain.chain_snapshot()` (re-resolved at every
  observation while flat, so a fresh straddle uses the current ATM),
  subscribes LTP for the two legs, the index and INDIAVIX, runs the broker
  preflight, then loops: index ticks build bars of `neural.live_interval`
  (seeded from `history_bars()` when given), every quote ticks the engine,
  each completed bar produces an observation, `broker.reconcile()` runs every
  reconcile_interval_s, a STOP file in the run directory or its parent
  squares off and stops, and the loop ends after square-off with a flat
  book. Every step is appended to `events.jsonl` (fsync) as
  `{"type": "step", ...}` in the api-spec step shape (log lines are
  `{"type": "log", ...}`); `state.json` carries worker, straddle (GET
  /api/straddle shape), engine status, last_step and preflight.
- `openfly worker --mode paper|live [--lots N] [--run-dir DIR] [--date YYYY-MM-DD] [--interval 5m]`
- `openfly replay-day --date YYYY-MM-DD [--encoder B] [--readout reservoir] [--neural-ms 200] [--lots 1] [--stop-pct 25] [--target-pct 40] [--interval 1m] [--out DIR] [--no-png]`
  runs `run_day` offline with the ReplayBroker and writes trace.json (plus
  stimulus PNGs) under runs/replays/rp_<date>_<time>.

### openfly/worker/factories.py and stubs.py

Lazy loaders that fall back to stand-ins so both commands run on a partial
checkout: `NullBrain` (required population names, zero spikes),
`NullEncoder`, `FixedTimeReadout` (ENTER on the observation at trade_start),
`simple_minute_quotes` (approximate ATM straddle from the index path and a
VIX level), `synthetic_index_bars`, `aggregate_bars`.

## 2. Names expected from the other packages

| Package | Name and call | Used by |
| --- | --- | --- |
| openfly.neural.brain | `Brain.from_settings(settings)`, else `Brain.load(graph_path)`, else `Brain(settings)`; must satisfy BrainProtocol | factories.load_brain |
| openfly.sensory.encoders | `make_encoder(name, settings) -> EncoderProtocol`; optional `render_png(stimulus) -> bytes` | factories, run_day |
| openfly.readout | `make_readout(name, settings) -> ReadoutProtocol` (also tried in openfly.readout.readouts) | factories |
| openfly.experiments.pricer | `synthetic_minute_quotes(day, bars1m=, vix=, settings=, expiry=) -> callable(timestamp, strike=None) -> StraddleQuote` (positional `(day, bars1m, vix, settings)` and `(day)` are tried too) | replay-day |
| openfly.experiments.reward | `reward_for(observation_index) -> float or None in [-1, 1]` | run_day, worker (plastic arm) |
| openfly.market.client | `OpenAlgoClient.from_settings(store)`; basketorder(orders, strategy) returns the results list; placeorder(...) returns {"orderid"}; orderstatus(orderid, strategy) returns the order dict; orderbook() returns {"orders", "statistics"}; positionbook() a list; cancelorder; analyzer_status(); funds(); symbol(); margin(positions) | OpenAlgoBroker, worker CLI |
| openfly.market.feed | `LtpFeed.from_settings(store)`, start(), stop(), subscribe([(exchange, symbol)]), last(symbol) -> Quote, age_seconds(symbol) | worker |
| openfly.market.session | `SessionCalendar(client, settings)`, window_for(day), is_trading_day(day) | worker, replay-day |
| openfly.market.chain | `ChainResolver(client, settings, calendar)`, chain_snapshot() with atm_strike, expiry, rows or atm | worker |
| openfly.market.history | `HistoryCache(client).load(exchange, symbol, interval)`; `BarStore` (bars, get or load with exchange, symbol, interval, start, end) is tried first for replay bars | worker CLI, replay-day |
| openfly.market.costs | `CostModel.from_settings(settings)` with order_cost(side, premium, quantity) and round_trip(entry_credit_points, lots, lot_size) | everywhere through load_cost_model |

The replay-day command reads bars from the local store only (BarStore,
HistoryCache or the parquet under data/history), never from the broker, and
falls back to a synthetic random walk labelled as such in the trace config.

## 3. Sample narratives (from the tests)

Entry:

    10:20. NIFTY 23,350, INDIAVIX 12.1. The readout expects 82 percent of the movement the 23350 straddle is pricing. All 18 checks passed. Sold 1 lot of the 15-SEP-26 23350 straddle for 201.3 points credit (INR 13,085). Stop 251.6, target 120.8, lock after 171.1, hard exit 15:15. Leg stops: call 131.60, put 130.15 (30 percent, at the broker).

Combined stop on a tick:

    10:37:12. Combined premium 252.0 reached the stop 251.6. Bought back 1 lot of the 23350 straddle for 252.0 points. Result -INR 3,416 after INR 120 costs. Day P&L -INR 3,416.

Leg stop at the broker:

    11:05:00. Call leg stop at 131.60 triggered at the broker. Filled at 132.00 for 65 units. The put stays open with its own fixed stop at 130.15.

Re-entry:

    10:45. NIFTY 23,350, INDIAVIX 12.1. The readout expects 82 percent of the movement the 23400 straddle is pricing. All 18 checks passed. Straddle 2 of the day, re-entry after the stop at 10:37: sold 1 lot of the 15-SEP-26 23400 straddle for 200.0 points credit (INR 13,000). Stop 250.0, target 120.0, lock after 170.0, hard exit 15:15. Leg stops: call 130.00, put 130.00 (30 percent, at the broker).

Veto:

    14:35. NIFTY 23,350, INDIAVIX 12.1. The readout expects 82 percent of the movement the 23350 straddle is pricing. The guard vetoed: 14:35 is outside the trade window 09:20 to 14:30; 14:35 is past the last entry time 14:30. Still flat.

## 4. Deviations and open issues

- `strategy.lots` caps the risk-based size (min of requested, risk budget,
  max_lots and margin). Set it to 0 to size purely from the risk budget.
- Reconciliation with zero orderbook matches raises UnresolvedOrder (halt
  for review) rather than assuming the basket was never placed; the plan
  says never resend.
- Zero-argument `reconcile()` also maintains the stop orders; the worker calls
  it every reconcile_interval_s (5 s). Broker order updates over the
  websocket are not consumed yet; leg stops are detected by polling.
- The STOPS intent is one intent for both legs; if one stop is rejected the
  intent settles PARTIAL and the next reconcile re-places the missing stop.
- Freeze quantity (1,800 per order) is not split; max_lots 3 keeps orders
  far below it.
- Re-centering (`strategy.recenter`) is not implemented (default off).
- The guard's spread check uses the combined premium as the base; with the
  Friday close quotes in docs/nifty-market-facts.md the call spread is 0.66
  percent and would veto at the 0.5 percent default. Live intraday spreads
  are far tighter; the setting may still need tuning.
- In broker mode the engine relies on reconcile to learn about leg stops; a
  stop that fires between reconciles is discovered when the exit basket
  cancels the stops (the fill is then attributed to the stop order).
- The worker builds live bars from LTP ticks; if the history cache is
  unavailable the first observation happens after the first completed bar.
