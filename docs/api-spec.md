# OpenFly API contract

The FastAPI backend serves this contract under `/api`. The React frontend
codes against it. All timestamps are ISO 8601 with the +05:30 offset unless
stated. Money is INR as JSON numbers. Errors return `{"detail": "..."}` with
a 4xx or 5xx status.

## GET /api/status

```json
{
  "version": "0.1.0",
  "python": "3.11.16",
  "data": {"ready": true, "neurons": 166700, "edges": 25582938, "graph_path": "data/graph.npz"},
  "openalgo": {"reachable": true, "host": "http://127.0.0.1:5000", "analyzer_mode": true, "broker": "the broker id OpenAlgo reports"},
  "worker": {"state": "stopped", "mode": null, "run_dir": null, "started_at": null, "last_event_at": null},
  "session": {"trading_date": "2026-09-15", "is_trading_day": true, "trade_start": "2026-09-15T09:20:00+05:30",
              "last_entry": "2026-09-15T14:30:00+05:30", "square_off": "2026-09-15T15:15:00+05:30",
              "is_expiry_day": true, "now": "2026-09-15T10:04:12+05:30"}
}
```

`worker.state` is one of `stopped`, `starting`, `running`, `halted`.

## GET /api/data/status and POST /api/data/prepare

Status:

```json
{"stage": "compiled", "files": [{"name": "body-annotations-male-cns-v1.0-minconf-0.5.feather", "present": true, "verified": true, "bytes": 14483314}],
 "graph": {"present": true, "neurons": 166700, "edges": 25582938, "verified": true},
 "progress": {"stage": "download", "file": "connectome-weights...", "done_bytes": 1000, "total_bytes": 1051241946}}
```

`stage` is one of `missing`, `downloading`, `downloaded`, `compiling`, `compiled`, `error`. POST prepare starts the pipeline in the background and returns `{"started": true}`; progress arrives on the event stream as `data.progress` events.

## GET /api/market/bars?symbol=NIFTY&exchange=NSE_INDEX&interval=5m&days=5

```json
{"symbol": "NIFTY", "exchange": "NSE_INDEX", "interval": "5m",
 "bars": [{"t": "2026-09-11T09:15:00+05:30", "o": 23270.3, "h": 23290.0, "l": 23250.1, "c": 23281.4, "v": 0}]}
```

## GET /api/market/chain

Current-week chain around the ATM.

```json
{"underlying": "NIFTY", "expiry": "2026-09-15", "days_to_expiry": 3.44, "index_ltp": 23398.1, "vix": 12.29,
 "synthetic_forward": 23463.2, "atm_strike": 23450, "lot_size": 65,
 "rows": [{"strike": 23400, "ce": {"symbol": "NIFTY15SEP2623400CE", "ltp": 133.6, "bid": 133.65, "ask": 135.0, "iv": 10.88},
                             "pe": {"symbol": "NIFTY15SEP2623400PE", "ltp": 70.4, "bid": 70.55, "ask": 71.25, "iv": 10.88}}]}
```

## GET /api/market/session?date=2026-09-15

Same shape as `status.session`.

## GET /api/brain/circuits

```json
{"n": 166700, "populations": [{"name": "KC", "size": 5000}, {"name": "DNp20_L", "size": 1}]}
```

## GET /api/brain/state

Last observation of the running worker (or of the last experiment step if no worker).

```json
{"observed_at": "2026-09-15T10:05:00+05:30", "neural_ms": 500, "sim_ms": 123500, "compute_seconds": 0.8,
 "rates_hz": {"KC": 1.2, "MBON": 4.5, "DN": 2.1, "DNp20_L": 6.0, "DNp20_R": 8.0, "DNpe017": 2.0},
 "fixed_decoder": {"left_hz": 6.0, "right_hz": 8.0, "difference_hz": 2.0, "gate_spikes": 1, "side": "ENTER"},
 "prediction": {"realized_over_implied": 0.82, "confidence": 0.61, "decision": "ENTER"},
 "stimulus_hash": "sha256..."}
```

## GET /api/brain/stimulus.png

PNG rendering of the last stimulus (encoder A: the chart; B and C: a bar map).

## GET /api/experiments and POST /api/experiments

List:

```json
{"experiments": [{"id": "exp_20260912_193000_b_reservoir", "name": "encoder B, reservoir, frozen", "state": "done",
  "created_at": "...", "config": {"encoder": "B", "readout": "reservoir", "plastic": false, "neural_ms": 200,
  "train": ["2025-08-08", "2026-03-31"], "validation": ["2026-04-01", "2026-06-30"], "test": ["2026-07-01", "2026-09-11"]},
  "progress": {"done": 1200, "total": 21000}}]}
```

POST body is the `config` object; response `{"id": "..."}`.

## GET /api/experiments/{id}

```json
{"id": "...", "config": {...}, "state": "done",
 "metrics": {"test": {"net_pnl_per_lot": 12345.0, "sharpe": 1.1, "max_drawdown": -8000.0, "trades": 40,
                      "stop_hits": 12, "target_hits": 9, "accuracy": 0.54, "accuracy_ci": [0.49, 0.59]}},
 "controls": {"fixed_0920": {...}, "random_entry": {...}, "shuffled": {...}, "flat": {...}},
 "curves": {"test": {"t": ["2026-07-01"], "strategy": [0.0], "fixed_0920": [0.0], "random_entry": [0.0]}},
 "passed": false, "verdict": "no edge found: accuracy 0.51 within bootstrap interval of 0.5"}
```

## POST /api/worker/start, POST /api/worker/stop, POST /api/worker/squareoff

Start body: `{"mode": "paper" | "live", "lots": 1, "run_dir": null}`. Paper requires the analyzer to be on; live requires `OPENFLY_LIVE` and a passed experiment. Responses: `{"state": "starting", "run_dir": "runs/paper-2026-09-15"}`. Stop and squareoff return `{"ok": true}`.

## GET /api/straddle

```json
{"in_position": true, "expiry": "2026-09-15", "strike": 23450, "lots": 1,
 "legs": [{"symbol": "NIFTY15SEP2623450CE", "side": "SELL", "qty": 65, "entry_price": 101.2, "ltp": 95.0,
           "stop_price": 131.6, "stop_order_id": "2509...", "stop_status": "pending", "status": "open"},
          {"symbol": "NIFTY15SEP2623450PE", "side": "SELL", "qty": 65, "entry_price": 98.4, "ltp": 90.1,
           "stop_price": 127.9, "stop_order_id": "2509...", "stop_status": "pending", "status": "open"}],
 "entry_credit": 199.6, "combined_ltp": 185.1, "stop_level": 249.5, "target_level": 119.8,
 "pnl": 942.5, "entered_at": "2026-09-15T10:05:04+05:30", "square_off_at": "2026-09-15T15:15:00+05:30"}
```

When flat, `in_position` is false and the other fields are null.

## GET /api/ledger/intents, GET /api/orders, GET /api/positions

Intents:

```json
{"intents": [{"intent_id": "...", "kind": "ENTRY", "status": "SETTLED", "created_at": "...", "reason": "readout ENTER 0.82",
  "legs": [{"symbol": "...", "side": "SELL", "quantity": 65, "order_id": "2509...", "status": "complete", "average_price": 101.2}]}]}
```

Orders and positions mirror the OpenAlgo orderbook and positionbook filtered to the strategy tag, with the same field names OpenAlgo uses.

## GET /api/settings and PUT /api/settings

```json
{"strategy": {"underlying": "NIFTY", "expiry_selection": "monthly", "lot_size": 65, "lots": 1, "product": "NRML",
              "stop_mode": "adaptive", "stop_horizon_minutes": 60, "stop_buffer": 1.25,
              "leg_stop_min_pct": 15, "leg_stop_max_pct": 80, "combined_stop_min_pct": 10, "combined_stop_max_pct": 50,
              "leg_stop_pct": 30, "leg_stop_mode": "broker", "on_leg_stop": "hold_other",
              "combined_stop_enabled": true, "stop_pct": 25, "target_pct": 40, "lock_after_pct": 15, "trade_start": "09:20", "last_entry": "14:30", "square_off": "15:15",
              "max_entries_per_day": 10, "min_hold_minutes": 10, "reentry_cooldown_minutes": 5, "vix_ceiling": 20, "min_days_to_expiry": 0},
 "risk": {"daily_loss_limit_pct": 1.0, "risk_budget_pct": 1.0, "max_lots": 3, "spread_pct_max": 0.5, "quote_max_age_s": 5,
          "index_move_veto_pct": 0.3},
 "neural": {"neural_ms": 100, "live_interval": "5m", "replay_interval": "1m", "encoder": "B", "readout": "reservoir", "plastic": false},
 "costs": {"brokerage_per_order": 20, "brokerage_pct": 0.03, "stt_sell_pct": 0.1, "exchange_pct": 0.03503, "sebi_pct": 0.0001,
           "stamp_buy_pct": 0.003, "gst_pct": 18},
 "openalgo": {"host": "http://127.0.0.1:5000", "ws_url": "ws://127.0.0.1:8765", "api_key_set": true}}
```

PUT accepts a partial object and returns the merged settings. All settings live in `data/openfly.db` (zero config, no .env). The API key is write-only: PUT `{"openalgo": {"api_key": "..."}}` stores it in the database; GET returns only `api_key_set`.

## POST /api/settings/analyzer

Body `{"mode": true}`. Toggles OpenAlgo's global analyzer mode and returns `{"analyzer_mode": true}`. The UI must confirm before calling.

## WS /api/events

Server pushes JSON messages `{"type": "...", "at": "...", "data": {...}}` with types: `observation`, `prediction`, `guard`, `straddle`, `intent`, `order`, `reconcile`, `worker`, `data.progress`, `experiment.progress`, `log`. Clients may send `{"action": "ping"}` and receive `{"type": "pong"}`.

## Replay and decision traces

A decision trace is the record of one trading day as OpenFly experienced
it: one step per observation with everything that went into the decision.
Traces come from three sources: experiment runs (every day in the window),
paper or live worker runs (from `events.jsonl`), and on-demand replays.

Step shape:

```json
{"i": 12, "t": "2026-09-11T10:20:00+05:30", "index": 23350.2, "vix": 12.1, "premium": 201.3, "days_to_expiry": 3.6,
 "stimulus_hash": "sha256...", "stimulus_png": "/api/replay/rp_.../stimulus/12.png",
 "rates_hz": {"KC": 1.2, "MBON": 4.5, "DN": 2.1, "DNp20_L": 6.0, "DNp20_R": 8.0, "DNpe017": 2.0},
 "fixed_decoder": {"left_hz": 6.0, "right_hz": 8.0, "difference_hz": 2.0, "gate_spikes": 1, "side": "ENTER"},
 "prediction": {"realized_over_implied": 0.82, "confidence": 0.61, "decision": "ENTER", "tau": 0.1},
 "guard": {"allowed": true, "checks": [{"name": "trade_window", "ok": true, "detail": "10:20 within 09:20 to 14:30"},
                                        {"name": "vix_ceiling", "ok": true, "detail": "12.1 below 20"}]},
 "action": "ENTER",
 "straddle": {"in_position": true, "strike": 23350, "lots": 1, "entry_credit": 201.3, "combined_ltp": 201.3,
              "stop_level": 251.6, "target_level": 120.8, "pnl": 0.0},
 "fills": [{"symbol": "NIFTY15SEP2623350CE", "side": "SELL", "qty": 65, "price": 110.2}],
 "pnl_day": 0.0, "compute_seconds": 0.4,
 "narrative": "10:20. The fly watched the last 60 bars of NIFTY (drifting up 0.2 percent, calm), INDIAVIX 12.1. Its readout puts expected movement at 82 percent of what the 23350 straddle is pricing. All 7 guard checks passed. Sold 1 lot of the 23350 straddle for 201.3 points credit. Stop at 251.6 (25 percent above credit), target 120.8, hard exit 15:15.",
 "technical": {"encoder": "B", "readout": "reservoir", "neural_ms": 200, "features": 3500, "top_populations": [["DN", 2.1], ["MBON", 4.5]], "ridge_alpha": 10.0, "implied_move_points": 201.3, "predicted_move_points": 165.1}}
```

Every step carries `narrative`, a plain-language account for traders of what
OpenFly saw, concluded and did, and `technical`, the numbers behind it. The
backend writes both; the frontend shows them side by side.

`action` is one of `ENTER`, `EXIT`, `STOP`, `STOP_LEG`, `TARGET`, `LOCK`, `SQUARE_OFF`,
`REENTRY`, `HOLD`, `VETO`, `NONE`. `STOP` is the combined stop; `STOP_LEG` is a
per-leg fixed stop order executing at the broker (the other leg keeps its own
fixed stop unless `on_leg_stop` is `exit_both`). The frontend Replay page plays steps
back with a scrubber and speed control and renders the decision panel for
the selected step.

### GET /api/replay/dates

`{"dates": ["2026-09-11", "2026-09-10"], "source": "history cache"}`: dates
for which index bars exist.

### POST /api/replay/run

Body: `{"date": "2026-09-11", "encoder": "B", "readout": "reservoir", "neural_ms": 200, "lots": 1,
"stop_pct": 25, "target_pct": 40, "experiment_id": null}`. Runs the fly over
that day's 1 minute bars (375 steps) with the replay broker and the synthetic or recorded
straddle prices. Returns `{"id": "rp_20260911_..."}`; progress arrives as
`replay.progress` events.

### GET /api/replay and GET /api/replay/{id}

List: `{"replays": [{"id": "...", "date": "...", "state": "done", "config": {...}, "summary": {"pnl": 1234.5, "trades": 2}}]}`.
One replay: `{"id", "date", "config", "state", "summary", "steps": [...]}`.
`GET /api/replay/{id}/stimulus/{i}.png` returns the stimulus image for step i.

## Per-leg fixed stop losses

Right after a straddle entry fills, OpenFly places one BUY SL-M order per
leg at `entry_price x (1 + leg_stop_pct / 100)` (default 30 percent), tagged
with the strategy, and keeps them in place ("maintains" them): if a stop
order is cancelled or rejected it is re-placed; when the position is exited
for any other reason the stop orders are cancelled first. The stops are
fixed, never trailed. `leg_stop_mode` `broker` places real SL-M orders;
`software` monitors the leg price in the worker instead. When one leg's stop
executes, `on_leg_stop` decides whether the other leg keeps running with its
own fixed stop (`hold_other`, default) or is exited too (`exit_both`). The
combined stop, target and lock remain available as software rules on the
summed premium (`combined_stop_enabled`).

## Dynamic straddles

OpenFly trades straddles dynamically: after any exit (stop, target, readout
EXIT) it may enter a fresh straddle at the then-current ATM strike as soon
as the readout says calm again and the guard allows, subject to
`reentry_cooldown_minutes` (default 5, one observation) and
`max_entries_per_day` (default 10, 0 means unlimited). Only one straddle is
open at a time: entry, then exit, then the next entry. Each straddle is a
separate round trip in the ledger with its own stops.

## Volatility-adaptive stops

With `stop_mode` `adaptive` (default) the stop distances are computed at
each entry and then held for that straddle:

1. expected move m over `stop_horizon_minutes` = max(implied, realized),
   where implied = combined premium x sqrt(horizon / minutes to expiry) and
   realized = std of the last 60 one-minute log returns x sqrt(horizon) x index.
2. per leg: premium rise for a move m against that leg = |delta| x m + 0.5 x
   gamma x m squared (delta and gamma from the greeks endpoint or the
   Black-Scholes pricer); leg stop percent = `stop_buffer` x rise / leg price,
   clipped to [`leg_stop_min_pct`, `leg_stop_max_pct`].
3. combined: rise = 0.5 x (gamma_ce + gamma_pe) x m squared + |net delta| x m;
   combined stop percent = `stop_buffer` x rise / combined premium, clipped to
   [`combined_stop_min_pct`, `combined_stop_max_pct`].

The straddle payload gains `stop_basis`: `{"mode": "adaptive", "horizon_minutes": 60,
"expected_move_points": 95.0, "implied_move_points": 88.0, "realized_move_points": 95.0,
"leg_stop_pct": {"ce": 31.2, "pe": 28.7}, "combined_stop_pct": 18.4}` and every
narrative states the expected move and the resulting stop levels. Each new
straddle recomputes its stops from the then-current volatility. `stop_mode`
`fixed` uses `leg_stop_pct` and `stop_pct` as before. Stops are never trailed.
