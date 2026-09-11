# OpenFly plan: an intraday NIFTY straddle driven by the fruit fly connectome

Status: plan, written 2026-09-12. Nothing in this document has been built yet.

OpenFly simulates the complete central nervous system of an adult male
fruit fly from the public MaleCNS v1.0 connectome (166,700 neurons, 25.6
million connections) and uses its activity to run one well-defined options
strategy on NSE through OpenAlgo: an intraday short straddle on current-week
NIFTY options with a combined stop loss. It is inspired by stonkfly
(https://github.com/nftechie/stonkfly), which first wired this connectome to
a live market. OpenFly is a fresh design for the Indian market, for Windows,
and for a person operating it through a browser rather than a terminal.

## 1. What this is and what it is not

A connectome is a wiring diagram. It says neuron A synapses onto neuron B with
N contacts. It does not contain a trading strategy, and no published work
shows that a wiring-constrained spiking network learns to trade. This plan
does not claim otherwise.

What a connectome simulation can honestly be, for a trading system, is a very
large fixed nonlinear dynamical system. Fed with a stream of market
observations it produces a high-dimensional stream of spike counts. Whether
that stream carries information about the next hour of NIFTY movement is an
empirical question, and it is the right question for a straddle: a short
straddle wins when the market moves less than the premium implies and loses
when it moves more. The fly is therefore asked a volatility question, not a
direction question. OpenFly is built to answer it with controls, and to trade
only if the answer is yes.

Three rules follow:

1. The neural core stays biologically faithful: full graph, no pruning,
   measured contact counts, transmitter-based signs, identified cell types,
   an exact event-driven leaky integrate-and-fire kernel, no random numbers.
2. The readout that turns spikes into a decision is a measurable statistical
   component with controls. A fixed "biological" decoder is kept only as a
   comparison point.
3. Money handling is conventional and boring. The straddle engine, its
   combined stop loss, targets, time exits, square-off, position sizing,
   reconciliation and paper mode are ordinary risk engineering and are never
   presented as neural output.

## 2. The strategy

Instrument: NIFTY options on NFO, current-week expiry (weekly expiries fall
on Tuesdays: 15-SEP-26, 22-SEP-26, 29-SEP-26, 06-OCT-26, 13-OCT-26), lot size
65, strike step 50, tick 0.05, freeze quantity 1,800 per order. Product NRML.
The broker does not square off NRML positions, so OpenFly does it itself.

Structure: sell one ATM call and one ATM put of the same expiry (a short
straddle), N lots each, entered as one basket. ATM is the strike nearest
the synthetic forward (call price minus put price plus strike), not the
spot index, because the two differ by tens of points.

Exits, all mechanical and all on the combined premium (call LTP plus put
LTP):

- Combined stop loss: exit both legs when the combined premium rises to
  entry credit x (1 + stop percent). Default 25 percent.
- Combined target: exit both legs when the combined premium falls to entry
  credit x (1 - target percent). Default 40 percent. Optional lock: after
  the premium has fallen 15 percent, move the stop to the entry credit.
- Time exit: square off at 15:15, with the exit basket sent 30 seconds early so the book is flat at 15:15. No position exists after
  15:15 on any day.
- Re-centering: if the index moves more than one strike step from the
  straddle strike, the straddle may be closed and reopened at the new ATM
  (configurable, default off in early versions).
- Re-entry after a stop: at most once per day, and only if the fly's
  readout says the regime is calm.

Entry: within the trade window (09:20 to 15:15; last new entry 14:30 by default, configurable) when the fly's
readout predicts that realized movement over the next holding window will
be smaller than what the premium implies, subject to the guard. Expiry day
(Tuesday) trades the expiring contract by default, which has the fastest
decay and the sharpest gamma; a setting can require at least one day to
expiry.

Sizing: lots = floor(risk budget / (entry credit x stop percent x 65)),
capped by the margin endpoint and a hard maximum lot count. With a 25
percent stop on a 204 point credit, one lot risks about INR 3,300 before
slippage; a 1 percent daily risk budget on INR 10 lakh allows 3 lots.

What the fly decides: whether to enter now, whether to exit early before
the mechanical stop when its volatility forecast flips, whether a re-entry
is allowed, and (a later experiment) whether to lean the strikes by one
step in the direction of a directional readout.

## 3. Measured facts that shape the design

Measured on Saturday 2026-09-12 through the local OpenAlgo server (Friday
2026-09-11 closing values). Details in `nifty-market-facts.md`.

| Fact | Value | Consequence |
| --- | --- | --- |
| NIFTY close, INDIAVIX | 23,398.1 and 12.29 (one-year VIX mean 13.67, range 9.15 to 27.89) | Low-volatility regime at the time of writing |
| Current-week ATM straddle (23400) | call 133.6, put 70.4, combined 204.0 points = INR 13,260 per lot at 3.4 days to expiry | Entry credit in the low tens of thousands per lot |
| ATM greeks per leg | IV 10.9 percent, theta -13.9 points/day, gamma 0.00156, vega 8.8 | Straddle decays about 28 points (INR 1,800) per day; a 100 point move costs about 15.5 points (INR 1,000) |
| Margin, short straddle, 1 lot | INR 188,700 NRML (same as MIS on this broker) | Capital requirement is margin, not premium |
| Margin, long straddle, 1 lot | INR 13,260 | Long straddle is a cheap hedge arm for experiments |
| NIFTY 1m return std | 0.0426 percent | Index is calmer than a single stock |
| Forward move std | 15 min 0.115 percent, 30 min 0.158, 60 min 0.216, 120 min 0.300, full day 0.879 | Full-day realized move (about 206 points) roughly equals the straddle premium: on average the straddle is fairly priced, so any edge is in timing and regime |
| Intraday profile | 09:15 to 09:30 std 0.163 percent, mid-day 0.023 to 0.028, 15:00 to 15:15 0.038 | Trading starts at 09:20 by rule, after the most violent minutes; mid-day is where a short straddle earns |
| Gap and range | mean absolute gap 0.378 percent, daily range 0.89 percent | Overnight is not our problem (flat by 15:15) |
| History coverage | NIFTY index 1m for 400 days (101,310 bars), INDIAVIX daily and 1m, option contracts only while listed (about two weeks before expiry) | Backtests need a synthetic straddle model plus a forward-collected real option dataset |
| Costs per straddle round trip, 1 lot | about INR 120 (four orders at INR 20, STT 0.1 percent on sold premium, exchange 0.035 percent, GST) = 0.9 percent of credit | Costs are small relative to the stop |
| Account state | live mode, zero available cash | Paper mode through the OpenAlgo analyzer is the default and is verified at every start |

## 4. Design principles

1. Simulate once, fit many. A pass of the fly over a year of 5 minute bars
   is expensive. The spike-count features of that pass are cached and every
   readout, threshold and policy is fitted and evaluated from the cache.
2. Chronological everything. Train, validation and test windows never
   overlap and never look backwards. Nothing is tuned on the test window.
3. Controls before claims. Every arm is compared with frozen weights,
   shuffled labels, a random entry policy matched on trade count, an
   always-enter-at-09:20 straddle, and staying flat.
4. Reject-only guard. The guard can veto a proposal. It never chooses a
   different trade. Stops, targets, time exits and square-off are
   operational actions logged as such.
5. Paper by default. The worker refuses live mode unless the OpenAlgo
   analyzer is confirmed off, the environment says
   `OPENFLY_LIVE=I_ACCEPT_REAL_TRADES`, and the request carries a live flag.
6. Windows first. The kernel is numba, not C++. Everything runs from `uv`
   on Python 3.11.
7. One strategy. Current-week NIFTY short straddle, NRML, intraday. Other
   structures are experiments layered on the same engine later.
8. Every number on screen has a source. The UI shows what the fly saw, what
   it spiked, what the readout predicted, what the guard did and why.

## 5. Architecture

```
openfly/
  backend/
    openfly/
      connectome/    download, verify, normalize, compile graph.npz
      neural/        numba LIF kernel, brain state, circuits, checkpoints
      sensory/       market observation to photoreceptor currents
      readout/       fixed decoder, reservoir readout, policy with hysteresis
      market/        OpenAlgo client, history cache, option chain resolver,
                     straddle pricer, session calendar, tick feed
      straddle/      straddle engine: entry, combined stop, target, time
                     exit, re-centering, sizing
      execution/     guard, ledger, brokers (replay, analyzer, live), reconciler
      experiments/   replay runner, feature cache, walk-forward, metrics
      api/           FastAPI routers and websocket event stream
      worker/        the live/paper loop process
    tests/
  frontend/          Vite, React, TypeScript, Tailwind v4, shadcn (new-york)
  data/              git-ignored: connectome sources, graph.npz, history, features
  runs/              git-ignored: ledgers, checkpoints, events
  docs/
```

Runtime processes:

- API server (FastAPI, uvicorn) serves the frontend, the REST API and a
  websocket event stream.
- Worker: one long-running process per run directory that owns the brain,
  the straddle engine, the ledger and the OpenAlgo connection. The API
  starts and stops it and reads its state through SQLite and the event
  stream.
- Experiment jobs: background processes that replay history through the
  fly and write feature caches and results.
- Recorder: a small daily job that stores 1 minute history of the
  current-week NIFTY chain (ATM plus and minus five strikes) and INDIAVIX
  into parquet, building the real option dataset that no broker provides
  for expired contracts.

## 6. The neural core

Data: the three MaleCNS v1.0 flat-connectome feather files (annotations,
neurotransmitters, weights; about 1.1 GB, CC-BY 4.0), verified by SHA-256,
with a locked hash for every compiled array. Node policy: every body with
an assigned superclass, excluding glia. Edge policy: every edge between
retained bodies, including weak edges and self-connections. Expected
result: 166,700 neurons, 25,582,938 directed connections, 124,177,617
synaptic contacts.

Graph: CSR by presynaptic neuron. Weight = contact count x sign x 0.275 mV,
sign +1 for acetylcholine, -1 for GABA, glutamate and histamine, +1 for
ambiguous or modulator-only cells (a declared proxy, reported per run).

Kernel: numba `@njit`, event-driven leaky integrate-and-fire with exact
closed-form integration between events. Constants: 0.1 ms step, 20 ms
membrane, 5 ms synapse, -45 mV threshold, -52 mV rest (-60 mV for Kenyon
cells with an 8 mV adaptation step decaying over 200 ms), 1.8 ms delay,
2.2 ms refractory. Lazy active list: a neuron is skipped only while its
voltage and both its instantaneous and asymptotic drive are below
threshold, which is exact, not an approximation. Verified on this machine:
numba 0.67 on uv-managed Python 3.11 with numpy 2.4.6.

Circuits exposed as named populations with stable indices: R1-R6
photoreceptors (3,335 with inferred eye coordinates), R8p and R8y (811),
lamina L1/L2/L3/L5, Kenyon cells, PAM11 (15) and PPL101 (2) dopamine
neurons, MBON07 (4) and MBON11 (2), all MBONs, DNp20 and DNpe017, all
descending neurons, central complex, and a fixed random sample of 2,000
central brain neurons. Counts are asserted at load.

Declared modeling choices that differ from the inspiration:

- Photoreceptor transfer is graded, with half-saturation at mid-grey, so
  luminance differences produce rate differences of tens of hertz rather
  than a few percent. The constant is configuration and is in every run's
  provenance.
- Both eyes project the full stimulus field. Lateral asymmetry then means
  something in the stimulus, not in the layout.
- Modulatory (dopamine, serotonin, octopamine) neurons have no postsynaptic
  effect unless the plastic arm is on; there are no half-implemented
  traces.
- Checkpoints store only mutable state (membrane arrays and the plastic
  weights), not the full weight array.
- Benchmark first: Phase 0 measures wall time per 100 ms of neural time on
  this machine and fixes the neural time per market bar so that one pass
  over 400 days of 5 minute bars (about 21,000 observations) finishes in
  hours. A CUDA kernel for the RTX 4060 is an option, not a dependency.

## 7. Sensory encoders

The fly only receives currents into photoreceptors (plus dopamine pulses in
the plastic arm). All encoders produce luminance in [0, 1] per
photoreceptor per observation, are deterministic, see only the past, and
are hashed into provenance. The observation is built from NIFTY index bars,
INDIAVIX, the live straddle premium and time to expiry; position and P&L
are never part of encoders A and B.

Encoder A, chart. A rendered picture of the last 60 five-minute NIFTY
closes as a thick line on a light field, vertical scale a rolling 3 x ATR14
window centred on the current index so level information survives across
frames, rising and falling segments in different colours for the R8
channels. No text, no grid, no header.

Encoder B, retinotopic bars (default). No rasterization. Each column of
ommatidia is one of the last N bars, oldest at the periphery and newest at
the centre. R1-R6 luminance is a sigmoid of the bar's return over the
rolling return std; R8y (green) carries the bar's range relative to ATR;
R8p (blue) carries a slow channel: VIX z-score and the straddle premium's
change since entry.

Encoder C, feature patches. Engineered features (5 and 15 bar returns and
absolute returns, realized-versus-implied ratio, VIX change, time of day,
time to expiry) mapped to disjoint retinal patches. Least biological, most
informative; it tells us whether the network adds anything to what it is
given.

## 8. Readouts

Readout 1, fixed decoder (control). Mean right DNp20 rate minus mean left
DNp20 rate gated by any DNpe017 spike, 2 Hz threshold, mapped to enter,
exit, hold. Kept to show what an untrained decoder is worth.

Readout 2, reservoir readout (primary). The connectome is a liquid state
machine. Per observation the feature vector is log(1 + spike count) for
every neuron in the selected populations (descending neurons, MBONs, the
random 2,000 sample, about 3,500 features), standardized on the training
window. A ridge regression predicts the forward realized absolute move of
NIFTY over the holding window in units of the straddle's implied move; a
logistic variant predicts whether it exceeds one. Fitting is closed form
from the feature cache, so regularization, population and horizon are
chosen on the validation window only. Policy: enter the short straddle
when predicted realized-over-implied is below 1 - tau, exit early when it
rises above 1 + tau, with a hysteresis band so a prediction hovering near
the boundary does not churn.

Readout 3, plastic arm (experiment). A dopamine-gated Kenyon cell to MBON
rule: reinforcement is delivered once per closed straddle in proportion to
its realized return in units of the stop distance (PAM11 for gains, PPL101
for losses), and the readout is the reservoir readout restricted to MBON
populations so the learned quantity and the decision share a pathway.
Always run against a frozen twin.

Success criterion: on the untouched test window, the readout's realized
versus implied classification must beat 52 percent accuracy with a
block-bootstrap p-value below 0.05, and the straddle P&L after measured
costs must beat both the fixed 09:20 entry straddle and the random-entry
control with the same trade count. Otherwise the result is "no edge
found", the worker stays in paper mode, and the fixed-time straddle with
the combined stop is what runs.

## 9. Market and execution through OpenAlgo

API notes and gotchas are in `openalgo-notes.md`. Decisions:

- Symbols: OpenAlgo's option chain helper endpoints returned 404 for the
  current NIFTY expiries on this install, so OpenFly resolves contracts
  itself: `expiry` for the weekly list, `search` and `symbol` for the
  strikes, symbol format `NIFTY15SEP2623400CE`. ATM is chosen from the
  synthetic forward computed from the chain quotes.
- Data: `history` 1m and 5m on NIFTY (NSE_INDEX) and INDIAVIX for backtests
  and warm-up; websocket LTP (mode 1) on the two legs and the index for the
  live worker; `quotes` and `depth` for guard checks. Parquet cache under
  `data/history/`. The recorder job runs after every close.
- Session: `market/timings` and `market/holidays` decide whether today
  trades. Observation window 09:15 to 15:15. Trade window 09:20 to 15:15, last new entry 14:30 by default.
  Forced square-off at 15:15 with the exit basket sent 30 seconds early. Nothing runs on weekends and holidays.
- Cadence: the fly observes each completed 5 minute bar; the straddle
  engine evaluates the combined stop, target and time exit on every LTP
  tick of either leg.
- Orders: entry and exit legs go through `basketorder` (both SELL on entry,
  both BUY on exit) tagged strategy `openfly`; the basket status is
  inspected per leg and a one-legged fill is treated as an incident: the
  engine immediately tries to complete or unwind the pair and halts if it
  cannot. Marketable LIMIT prices at LTP plus or minus a configurable
  offset, cancelled and reconciled after a timeout.
- Reconciliation: intent is persisted before the call. If a response is
  lost, the reconciler searches `orderbook` for the strategy tag, symbol,
  side, quantity and time window. A unique match is adopted; anything else
  halts the worker for review, never a resend.
- Position truth: `positionbook` per leg (product NRML), read before every
  order and on every reconciliation; the ledger must agree or the worker
  halts.
- Costs: brokerage min(0.03 percent, INR 20) per order, STT 0.1 percent on
  sold premium, exchange 0.03503 percent of premium turnover, SEBI 0.0001
  percent, stamp 0.003 percent on bought premium, GST 18 percent on
  brokerage plus exchange charges. One formula for backtests, the paper
  ledger and the expected-cost check.
- Operational rules (guard, reject-only): combined stop and target as in
  Section 2, daily loss limit 1 percent of capital, at most two straddle
  entries a day, no entries before 09:20 or after the last-entry time,
  spread on either leg above 0.5 percent of premium vetoes, quote older
  than 5 seconds vetoes, index moved more than 0.3 percent since the
  observation vetoes, VIX above a configurable ceiling vetoes, kill switch
  and STOP file halt everything.
- Paper mode: the OpenAlgo analyzer. The worker checks `analyzer` at start
  and refuses paper mode unless it is on. The toggle is global to the
  OpenAlgo installation, so the UI confirms before switching and shows the
  mode on every screen. Sandbox capital defaults to INR 1 crore, which
  covers the INR 1.89 lakh per lot margin.
- Live mode: additionally requires `OPENFLY_LIVE=I_ACCEPT_REAL_TRADES`, a
  preflight (funds above margin for the configured lots, no foreign open
  orders in the chain, symbol master present, expiry list fresh) and a
  readout that passed Section 8 in the experiment registry.

## 10. Experiment protocol

Backtests need straddle prices that brokers do not keep. Two sources:

1. Synthetic straddle from the index. Black-Scholes repricing of the ATM
   call and put each minute from the NIFTY index path and an IV series
   derived from INDIAVIX, calibrated against the currently listed
   contracts where both real and synthetic prices exist. Reported as an
   approximation; it captures gamma cost and decay, not microstructure.
2. Real recorded chains. From the day the recorder starts, real 1 minute
   option prices accumulate; after a few weeks these replace the synthetic
   model for validation and test.

Split on the 400 day index history:

| Window | Dates | Use |
| --- | --- | --- |
| Train | 2025-08-08 to 2026-03-31 | Fit readouts |
| Validation | 2026-04-01 to 2026-06-30 | Choose regularization, populations, horizon, tau, encoder |
| Test | 2026-07-01 to 2026-09-11 | Reported once, never tuned on |

Later, a rolling monthly walk-forward.

Arms: encoder {A, B, C} x readout {fixed, reservoir} x weights {frozen,
plastic}. Controls per arm: shuffled labels, random-entry with matched
trade count, fixed 09:20 entry with the same stop and target, flat.

Metrics: net P&L after costs per lot, Sharpe from daily P&L, maximum
drawdown, stop-hit rate, target-hit rate, average holding time, realized
versus implied classification accuracy and its bootstrap interval.

Reproducibility: every run records the connectome array hashes, kernel
version, encoder and readout configuration hashes, data window hashes and
the git commit.

## 11. FastAPI backend

Python 3.11 through uv. Pydantic settings from `.env` (never committed).

| Route | Purpose |
| --- | --- |
| GET /api/status | versions, data readiness, worker state, OpenAlgo reachability, analyzer mode |
| GET /api/data/status, POST /api/data/prepare | connectome download, verify, compile, with progress events |
| GET /api/market/bars, /api/market/chain, /api/market/session | cached index bars, current-week chain with synthetic forward and ATM, today's session windows |
| GET /api/brain/circuits, /api/brain/state, /api/brain/stimulus.png | populations, last observation's rates, the exact stimulus |
| GET, POST /api/experiments; GET /api/experiments/{id} | list, start, inspect results and controls |
| POST /api/worker/start, /api/worker/stop, /api/worker/squareoff | run control (mode paper or live) |
| GET /api/straddle | current straddle: legs, entry credit, combined premium, stop and target levels, time to exit |
| GET /api/ledger/intents, /api/orders, /api/positions | ledger and OpenAlgo mirrors |
| GET, PUT /api/settings; POST /api/settings/analyzer | configuration and the guarded analyzer toggle |
| WS /api/events | observation, prediction, guard, straddle, order and reconciliation events |

The worker and the API share one SQLite database (WAL) plus the run
directory files; the API never touches the brain in-process.

## 12. React and shadcn frontend

Same toolchain as OpenAlgo's own frontend so components can be shared:
Vite, React 19, TypeScript, Tailwind v4, shadcn new-york style, lucide
icons, TanStack Query, zustand, lightweight-charts, biome, vitest and
Playwright.

Pages:

1. Setup. OpenAlgo host and API key with a test button, connectome
   download and compile progress, environment checks.
2. Dashboard. NIFTY 5 minute candles with entry and exit markers, the
   combined premium line with stop and target levels, current straddle
   card (strike, expiry, lots, credit, P&L), mode badge (Analyzer, or Live
   in red), session clock with the 09:20 start, the last-entry time and the 15:15 cutoff, the fly's last
   observation summary, kill switch.
3. Brain. The stimulus the fly saw, firing-rate heatmaps per population,
   the DNp20 gauge, the reservoir prediction of realized over implied with
   its tau band, plastic-edge statistics.
4. Experiments. Run table, P&L curves against all controls, metrics table,
   launch form, pass or fail against the Section 8 criterion.
5. Orders and positions. Ledger intents with lifecycle states next to the
   OpenAlgo orderbook, positionbook and tradebook, per leg.
6. Settings. Stop and target percents, lots and risk budget, entry window,
   VIX ceiling, cost model, analyzer toggle with confirmation.

Documents and logs are plain text with no icons; the UI uses lucide icons
for navigation only.

## 13. Roadmap

Phase 0, foundation. Repo, uv project, numba kernel with tests that
reproduce the expected counts and the sensory and dopamine checks,
connectome download and compile, circuits module, kernel benchmark, choice
of neural time per bar.

Phase 1, market and straddle model. OpenAlgo client wrapper with retries
and rate-limit awareness, history cache, option chain resolver and
synthetic forward, session calendar, cost model, synthetic straddle pricer
calibrated on listed contracts, the recorder job, and a fixed 09:20
straddle backtest with the combined stop as the first baseline number.

Phase 2, API and UI skeleton. FastAPI status, data, market and experiments
routes; React Setup, Dashboard (history only) and Experiments pages. Early,
so every later result is visible.

Phase 3, encoders and readouts. Encoders A, B, C, feature cache, reservoir
readout, walk-forward evaluation, controls, the pass or fail report.

Phase 4, execution. Straddle engine, guard, ledger, replay broker, analyzer
broker, reconciler, worker loop, Brain and Orders pages, paper trading
during market hours in analyzer mode.

Phase 5, plastic arm and live gating. Dopamine arm experiments, strike-lean
experiment, live preflight, optional CUDA kernel.

Each phase ends with tests passing, docs updated and a push to GitHub;
pushes also happen at checkpoints inside a phase.

## 14. Risks and open questions

- Kernel speed on this machine is unmeasured. Fallbacks: shorter neural
  time per bar, a CUDA kernel, a subsampled training window.
- The synthetic straddle model is an approximation until the recorder has
  collected real chains. Early backtest numbers carry that caveat.
- OpenAlgo's option helper endpoints (optionchain, optionsymbol,
  syntheticfuture) returned "No strikes found" for valid NIFTY expiries on
  this install; OpenFly does not depend on them, but the cause should be
  reported upstream.
- Basket legs are not atomic. A one-legged fill leaves a naked option for
  seconds; the engine's completion or unwind logic and its halt path need
  the most testing of anything in the project.
- Expiry-day behaviour (0 DTE) is different from other days; it may need
  its own stop percent and entry window.
- The analyzer toggle is global; running OpenFly in paper mode changes the
  behaviour of every other OpenAlgo client on this machine.
- The readout may find no edge. That is a valid result and is what the
  controls are for; the fixed-time straddle with the combined stop still
  works without the fly.

## 15. Decisions already made

- Strategy: current-week NIFTY short straddle, NRML, intraday, combined
  stop loss, flat by 15:15, lot 65.
- Kernel: numba, not C++. Python 3.11 via uv.
- Paper mode is the OpenAlgo analyzer, verified on every start.
- Frontend mirrors OpenAlgo's frontend stack.
- MIT license. Plain text documents, no icons, no em dashes.
