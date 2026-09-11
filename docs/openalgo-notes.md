# OpenAlgo notes for OpenFly

Digest of the OpenAlgo API documentation restricted to what a NIFTY options
straddle bot needs, plus facts verified against the local server on
2026-09-12.

## Connection

| Item | Value |
| --- | --- |
| REST base | http://127.0.0.1:5000/api/v1 |
| WebSocket | ws://127.0.0.1:8765 |
| REST auth | JSON body field `apikey` (query parameter on the few GET routes) |
| WebSocket auth | `{"action": "authenticate", "api_key": "..."}` (note the different key name) |
| Envelope | `{"status": "success" or "error", ...}`; order responses carry `"mode": "live" or "analyze"` |
| Rate limits (this install) | API 100/s, orders 10/s, smart orders 10/s (separate budget), webhooks 100/min |
| 429 body | `{"status": "error", "message": "Rate limit exceeded. Please try again later."}`, no Retry-After |
| Python SDK | `openalgo` 2.0.3; `from openalgo import api`; do not import it from a directory that contains an OpenAlgo server checkout named `openalgo` |

## Instruments (verified)

| Field | Value |
| --- | --- |
| Index quotes and history | NIFTY and INDIAVIX on exchange NSE_INDEX (quote only, not tradable) |
| Options exchange | NFO |
| Option symbol format | NIFTY15SEP2623400CE, NIFTY15SEP2623400PE (DDMMMYY expiry, strike, CE or PE) |
| Futures symbol format | NIFTY29SEP26FUT |
| Lot size, freeze quantity, tick | 65, 1,800, 0.05 |
| Expiry list | `expiry` with instrumenttype `options` or `futures`, returns DD-MMM-YY strings, nearest first |
| Chain resolution | `search` with query `NIFTY15SEP26` filters by expiry; `symbol` confirms one contract |

`optionchain`, `optionsymbol` and `syntheticfuture` returned 404 "No strikes
found" for valid NIFTY and stock option expiries on this install. OpenFly does
not depend on them.

## Order constants

- action: BUY, SELL
- product: NRML (F&O carry; OpenFly's choice, squared off by OpenFly itself), MIS (intraday, broker auto square-off), CNC (equity delivery)
- pricetype: MARKET, LIMIT (price), SL (price and trigger_price), SL-M (trigger_price)
- quantity: positive whole number, a multiple of the lot size for F&O; docs send it as a string
- strategy: free text tag, mandatory on placeorder, placesmartorder, modifyorder, basketorder, splitorder. OpenFly uses `openfly`.

## Multi-leg placement

`basketorder` takes `orders: [{symbol, exchange, action, quantity, pricetype, product, price?}]`.
BUY legs are processed before SELL legs; live execution runs batches of ten
with a one-second delay; each leg is independent, and `status` is success if
at least one leg succeeded, so inspect `results[]` per leg. A short
straddle entry is two SELL legs, its exit two BUY legs. A one-legged fill
must be detected from `results[]` and `positionbook` and handled at once.

`placesmartorder` with `position_size` is the target-position primitive for
single symbols. Gotcha from the documented table: with `position_size` 0
and a current position of 0 it places a fresh order of `quantity`. Read
`openposition` (which requires `product`) first.

`closeposition` and `cancelallorder` are account-wide across all strategies
and exchanges. OpenFly never calls them from automation; the UI exposes
them behind a confirmation as manual tools.

## Endpoints used

| Endpoint | Use in OpenFly |
| --- | --- |
| /basketorder | straddle entry and exit legs |
| /placeorder, /cancelorder, /modifyorder | single-leg repair and working limit orders |
| /orderstatus, /orderbook, /tradebook | fills, reconciliation by strategy tag |
| /positionbook, /openposition | position truth per leg |
| /funds, /margin | capital and straddle margin (1 lot short straddle NRML needed INR 188,700) |
| /quotes, /depth, /multiquotes | guard checks and synthetic forward |
| /optiongreeks | IV, delta, gamma, theta, vega per contract |
| /history | index, VIX and listed option contracts; intervals 1m, 3m, 5m, 10m, 15m, 30m, 1h, D |
| /expiry, /search, /symbol | chain resolution |
| /market/timings, /market/holidays | session calendar (epoch milliseconds, IST) |
| /analyzer, /analyzer/toggle | paper mode status and switch |
| /pnl/symbols | analyzer-mode per-symbol P&L |

## History response shape (verified through the SDK)

`client.history(symbol="NIFTY", exchange="NSE_INDEX", interval="1m",
start_date="2025-08-08", end_date="2026-09-12")` returned a pandas
DataFrame with a tz-aware DatetimeIndex named `timestamp` in Asia/Kolkata
and columns `close, high, low, oi, open, volume` (alphabetical), 375 bars
per day. Option contracts return history only from their listing date. The
documentation says timestamps may also come as epoch seconds through the
raw REST route, so the wrapper handles both.

## WebSocket

Modes: 1 LTP, 2 Quote, 3 Depth. Subscribe:

```json
{"action": "subscribe", "mode": "LTP", "symbols": [
  {"exchange": "NFO", "symbol": "NIFTY15SEP2623400CE"},
  {"exchange": "NFO", "symbol": "NIFTY15SEP2623400PE"},
  {"exchange": "NSE_INDEX", "symbol": "NIFTY"}]}
```

Tick:

```json
{"type": "market_data", "symbol": "NIFTY15SEP2623400CE", "exchange": "NFO", "mode": 1,
 "data": {"ltp": 133.6, "timestamp": 1756376445123}}
```

Timestamps are epoch milliseconds. After any disconnect the client must
re-authenticate and re-subscribe. Order updates: `{"action":
"subscribe_orders"}` gives `order_update` events with lowercase statuses
(open, trigger pending, complete, rejected, cancelled).

## Analyzer (paper) mode

`/analyzer` returns `{"analyze_mode": bool, "mode": "analyze" or "live"}`.
`/analyzer/toggle` with `{"mode": true}` switches to analyze. Orders are then
simulated against live broker prices with a default INR 1 crore sandbox
capital; MIS positions are squared off at 15:15 IST, NRML positions are
not. The toggle is global to the installation. On 2026-09-12 this install
was in live mode with zero available cash.

## Session

NSE and NFO 09:15 to 15:30 IST. `market/timings` for a date returns
start and end epoch milliseconds per exchange and an empty list on
holidays. OpenFly trades between 09:20 and 15:15 and squares off at 15:15.

## Costs used by OpenFly (options)

| Charge | Rate |
| --- | --- |
| Brokerage | min(0.03 percent, INR 20) per executed order (configurable) |
| STT | 0.1 percent of premium on the sell side |
| Exchange transaction | 0.03503 percent of premium turnover (NSE options) |
| SEBI | 0.0001 percent |
| Stamp duty | 0.003 percent of premium on the buy side |
| GST | 18 percent on brokerage plus exchange charges |

Measured: one lot of the 204 point straddle round trip costs about INR 119.

## Gotchas list

1. SDK kwargs are `price_type` and `order_id`; REST fields are `pricetype` and `orderid`.
2. All money and quantity fields in positionbook, openposition and funds come back as strings.
3. Positionbook lists closed positions with quantity "0".
4. quotes may return bid or ask 0 outside market hours; index quotes always have bid and ask 0.
5. Symbol master must be downloaded after broker login or `/symbol` fails.
6. Analyzer mode is global; verify it on every worker start.
7. No Retry-After on 429; back off client-side.
8. `basketorder` and `cancelallorder` report success on partial success; inspect per-leg results.
9. Order and smart-order budgets are separate 10 per second pools.
10. The greeks endpoint uses the synthetic forward as spot; ATM by forward can differ from ATM by index by one strike.
