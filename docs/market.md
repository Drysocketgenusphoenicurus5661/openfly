# OpenFly market layer

Package `openfly.market`. Everything here is plain Python over httpx,
websockets, pandas and DuckDB. Nothing in this package places an order unless
a caller invokes a method marked WRITE.

## Storage: `BarStore` (openfly/market/store.py)

One DuckDB file, `PATHS.market_db` (`data/market.duckdb`), holds every bar
ever fetched from the broker. Parquet files under `data/history` (layout
`openfly.config.history_path`) are imported on first open and whenever they
change; `export_parquet` writes them back for sharing.

Tables:

| Table | Columns | Key |
| --- | --- | --- |
| bars | exchange, symbol, interval, ts TIMESTAMPTZ, open, high, low, close, volume, oi | (exchange, symbol, interval, ts) |
| coverage | exchange, symbol, interval, start_date, end_date, fetched_at | ranges already requested from the broker |
| chains | trading_date, expiry, strike, option_type, symbol, ts, open, high, low, close, volume, oi | (symbol, ts) |
| chain_days | trading_date, expiry, min_strike, max_strike, symbols, rows, status, fetched_at | (trading_date, expiry) |

Gaps are computed from `coverage`, not from bar presence: a holiday inside a
fetched range has no bars and is never requested again. The current IST day
is covered only after 15:45, so a call during the session refreshes today.

Signatures the experiments agent needs:

```python
store = BarStore()                       # PATHS.market_db, imports data/history on first open
store.bars(exchange, symbol, interval, start=None, end=None) -> pandas.DataFrame
    # columns timestamp (tz-aware Asia/Kolkata, bar start), open, high, low, close, volume, oi
    # start and end are IST calendar dates (date or "YYYY-MM-DD"), inclusive
store.available_dates(exchange, symbol, interval) -> list[datetime.date]
store.day(exchange, symbol, interval, day) -> DataFrame
store.chain(trading_date, expiry=None) -> DataFrame
    # columns trading_date, expiry, strike, option_type, symbol, timestamp, open, high, low, close, volume, oi
    # expiry None means the nearest expiry recorded for that date
store.chain_coverage() -> list[dict]     # trading_date, expiry, min_strike, max_strike, symbols, rows
store.atm_path(trading_date, expiry=None, step=50.0) -> DataFrame
    # per index minute: timestamp, index_close, near_strike, forward, atm_strike,
    # ce_close, pe_close, combined, ce_symbol, pe_symbol
store.coverage(exchange, symbol, interval) -> list[tuple[date, date]]
store.upsert_bars(exchange, symbol, interval, frame) -> int
store.import_parquet(root=None, force=False); store.export_parquet(exchange, symbol, interval, path=None)
```

DuckDB allows one writer process at a time; every call opens a short-lived
connection under a file lock. Use `with BarStore() as store:` to hold one
connection across many calls.

## `HistoryCache` (history.py)

```python
cache = HistoryCache(client=None, store=None)   # store defaults to BarStore()
cache.get(exchange, symbol, interval, start, end, fetch=True) -> DataFrame   # fetches only uncovered ranges
cache.update(exchange, symbol, interval, start, end, force=False) -> int     # new bars stored
cache.available_dates(...), cache.coverage(...), cache.day_bars(exchange, symbol, interval, day)
HistoryCache.resample(df_1m, "5m") -> DataFrame   # bins anchored at 09:15 IST; 09:15, 09:20, ..., 15:25
HistoryCache.day(df, day) -> DataFrame
HistoryCache.to_records(df) -> [{"t", "o", "h", "l", "c", "v", "oi"}]    # GET /api/market/bars shape
```

## `OpenAlgoClient` (client.py)

```python
client = OpenAlgoClient.from_settings()          # host, api_key, strategy tag from data/openfly.db
client = OpenAlgoClient(host, api_key, timeout=10, strategy="openfly")
```

Envelope: `status == "success"` returns the payload; `"error"` raises
`OpenAlgoError(message, code, endpoint, payload)`. Read endpoints retry on
connection errors, timeouts, 429 and transient 5xx (5 tries, backoff 0.5 s
doubling with jitter, capped at 8 s); a 5xx whose message is permanent
(permission, invalid, missing, not found) is raised at once. Write endpoints
retry only on connect errors and 429; a timeout after the request may have
been sent raises `openfly.interfaces.UnresolvedOrder` (reconcile, never
resend). Token buckets: orders 10/s, smart orders 10/s (separate), history
2/s, everything else 50/s.

Read methods:

```python
quotes(symbol, exchange) -> Quote            # timestamp = local receipt time
quotes_raw(symbol, exchange) -> dict         # open, high, low, ltp, bid, ask, prev_close, volume, oi
multiquotes([(exchange, symbol), ...]) -> list[Quote]      # failed symbols left out
multiquotes_raw(symbols) -> list[dict]       # per-symbol data or error
depth(symbol, exchange) -> dict
history(symbol, exchange, interval, start_date, end_date) -> DataFrame   # ISO or epoch timestamps handled
intervals() -> dict
symbol(symbol, exchange) -> Contract | dict
search(query, exchange) -> list[dict]
expiry(symbol, exchange, instrumenttype="options") -> list[date]
optiongreeks(symbol, exchange="NFO", **extra) -> dict
holidays(year) -> list[dict]; timings(date) -> list[dict]
funds() -> dict[str, float]; margin(positions) -> dict[str, float]
positionbook() -> list[dict]; openposition(symbol, exchange, product, strategy=None) -> int
orderbook() -> {"orders": [...], "statistics": {...}}; tradebook() -> list[dict]
orderstatus(orderid) -> dict; analyzer_status() -> dict; pnl_symbols() -> dict
```

WRITE methods (never called by tests):

```python
placeorder(symbol, exchange, action, quantity, product="NRML", pricetype="MARKET", price=0,
           trigger_price=0, disclosed_quantity=0, strategy=None) -> {"orderid", "mode"}
placesmartorder(symbol, exchange, action, quantity, position_size, ...) -> {"orderid", "mode"}
basketorder(orders, strategy=None) -> list[{"symbol", "status", "orderid" | "message", "mode"}]
    # OpenAlgo says success when at least one leg succeeded: inspect every entry
cancelorder(orderid, strategy=None) -> dict
modifyorder(orderid, symbol, exchange, action, quantity, price, pricetype="LIMIT", product="NRML",
            trigger_price=0, disclosed_quantity=0, strategy=None) -> dict
analyzer_toggle(mode: bool) -> dict
OpenAlgoClient.make_order(symbol, exchange, action, quantity, product, pricetype, price, trigger_price) -> dict  # one basket leg
```

Helpers: `parse_history(rows, interval)`, `parse_expiry("15-SEP-26")`,
`option_symbol("NIFTY", date(2026, 9, 15), 23400, "CE")`.

## `LtpFeed` and `FakeFeed` (feed.py)

```python
feed = LtpFeed.from_settings()                  # ws_url and api_key from settings
feed.subscribe([("NFO", "NIFTY15SEP2623400CE"), ("NSE_INDEX", "NIFTY")], mode="LTP")  # or Quote, Depth
feed.subscribe_orders()                          # order_update events to order listeners
feed.add_listener(callback: Quote -> None); feed.add_order_listener(callback: dict -> None)
feed.start(wait=True, timeout=15)                # background thread and asyncio loop; raises FeedError
feed.last(symbol) -> Quote | None; feed.age_seconds(symbol) -> float | None
feed.exchange_timestamp(symbol) -> float | None; feed.snapshot() -> dict[str, Quote]
feed.stop()
```

Reconnects with backoff, re-authenticates and re-subscribes after any drop.
`FakeFeed` has the same surface plus `push(symbol, ltp, exchange, bid, ask, timestamp)`,
`push_message(raw_dict)` and `push_order(event)`.

## `SessionCalendar` (session.py)

```python
cal = SessionCalendar(client=None, store=None)     # holidays and timings cached in data/session/calendar.json
cal.is_trading_day(date) -> bool
cal.window_for(date=None) -> SessionWindow          # trade_start 09:20, last_entry 14:30, square_off 15:15 from settings
cal.session_info(date=None) -> dict                 # the session object of GET /api/status
cal.is_expiry_day(date) -> bool                     # expiry list when known, else Tuesday (or the day before a Tuesday holiday)
cal.now_ist(), cal.today(), cal.next_trading_day(date), cal.previous_trading_day(date)
cal.trading_days_between(start, end) -> int         # in (start, end]
cal.in_trade_window(when=None), cal.can_enter(when=None)
```

## `ChainResolver` (chain.py)

```python
resolver = ChainResolver(client, store=None, calendar=None)
resolver.expiries() -> list[date]
resolver.current_week(min_days_to_expiry=0, today=None) -> date
resolver.contracts(expiry) -> {strike: ChainRow(strike, ce: Contract, pe: Contract)}
resolver.contract(expiry, strike, "CE") -> Contract; resolver.straddle_contracts(expiry, strike)
resolver.symbol_for(expiry, strike, "CE") -> str          # no network
resolver.chain_snapshot(expiry=None, index_ltp=None, strikes_each_side=5, vix=None, with_iv=False) -> ChainSnapshot
    # .synthetic_forward, .atm_strike, .atm: StraddleQuote, .rows, .to_dict() = GET /api/market/chain
resolver.straddle_quote(expiry, strike) -> StraddleQuote
resolver.straddle_margin([ce, pe], lots, product=None, action="SELL") -> dict
resolver.greeks(symbol) -> dict
```

ATM is the strike-step multiple nearest the synthetic forward (strike + CE -
PE at the listed strike nearest the index), not the spot index.

## `CostModel` (costs.py)

```python
model = CostModel.from_settings(settings)
model.order_cost(side, premium, quantity, notional=None) -> CostBreakdown
model.round_trip(entry_credit_points, lots, lot_size=65, exit_debit_points=None) -> CostBreakdown
model.straddle_round_trip(call_entry, put_entry, call_exit, put_exit, lots, lot_size=65)
model.round_trip_per_lot_points(entry_credit_points, lot_size=65) -> float
```

One lot of a 204 point straddle round trip costs about INR 119 (brokerage
80, STT 13.26, exchange 9.29, SEBI 0.03, stamp 0.40, GST 16.07).

## `Recorder` (recorder.py)

```python
recorder = Recorder(client)
recorder.record(day=None, strikes_each_side=None, expiries=None, force=False, progress=print) -> RecordReport
recorder.backfill(days=30, listing_lead_days=21, progress=print) -> list[RecordReport]
```

For a trading date the recorder stores NIFTY and INDIAVIX 1m bars, then for
the current-week expiry (and the next week's on expiry day) every listed
strike from `floor(min_close / 50) * 50 - 12 * 50` to
`ceil(max_close / 50) * 50 + 12 * 50` (settings
`strategy.chain_strikes_each_side`), both legs, 1m, into `chains` and
`bars`, recording coverage so a re-run is a no-op. Days before a contract's
listing are probed with one call and marked `empty`. A broker that declines
history stops the run with `declined` set on the report.

## CLI

```
openfly record [--date YYYY-MM-DD] [--strikes N] [--force]
openfly backfill-chains [--days 30] [--lead 21]
openfly history [--exchange E --symbol S --interval I --days N] [--no-fetch]
openfly history import-parquet [--root DIR] [--force]
openfly history export --exchange E --symbol S --interval I [--out FILE]
openfly history status
openfly chain [--expiry YYYY-MM-DD] [--strikes 5] [--iv]
openfly session [--date YYYY-MM-DD]
openfly costs --credit 204 --lots 1
```

## Tests

`uv run pytest tests/market -q` runs offline. `OPENFLY_LIVE_TESTS=1` adds
read-only calls against the local server; they skip when the broker session
behind OpenAlgo declines a call.
