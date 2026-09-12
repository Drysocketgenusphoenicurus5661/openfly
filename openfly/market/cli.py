"""Market subcommands for the ``openfly`` CLI.

    openfly record [--date YYYY-MM-DD] [--strikes N] [--force]
    openfly backfill-chains [--days 30] [--lead 21]
    openfly history [--exchange NSE_INDEX --symbol NIFTY --interval 1m --days 5] [--no-fetch]
    openfly history import-parquet [--root DIR] [--force]
    openfly history export --exchange E --symbol S --interval I [--out FILE]
    openfly history status
    openfly chain [--expiry YYYY-MM-DD] [--strikes 5] [--iv]
    openfly session [--date YYYY-MM-DD]
    openfly costs --credit 204 --lots 1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any

from openfly.config import PATHS, SettingsStore
from openfly.market.chain import ChainResolver
from openfly.market.client import IST, OpenAlgoClient, OpenAlgoError
from openfly.market.costs import CostModel
from openfly.market.history import HistoryCache
from openfly.market.recorder import Recorder
from openfly.market.session import SessionCalendar
from openfly.market.store import BarStore

logger = logging.getLogger("openfly.market.cli")


def _say(text: str) -> None:
    print(text, flush=True)


def _client(store: SettingsStore) -> OpenAlgoClient | None:
    try:
        return OpenAlgoClient.from_settings(store)
    except ValueError as exc:
        _say(f"OpenAlgo client unavailable: {exc}")
        return None


def _date(text: str | None) -> date:
    return date.fromisoformat(text) if text else datetime.now(IST).date()


def _print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        _say("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    _say("  ".join(c.ljust(widths[c]) for c in columns))
    for row in rows:
        _say("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


# ----------------------------------------------------------------- handlers


def cmd_record(args: argparse.Namespace) -> int:
    store = SettingsStore()
    client = _client(store)
    if client is None:
        return 1
    recorder = Recorder(client, store=store)
    report = recorder.record(
        _date(args.date), strikes_each_side=args.strikes, force=args.force, progress=_say
    )
    _say(json.dumps(report.to_dict(), indent=1))
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    store = SettingsStore()
    client = _client(store)
    if client is None:
        return 1
    recorder = Recorder(client, store=store)
    reports = recorder.backfill(days=args.days, listing_lead_days=args.lead, progress=_say, strikes_each_side=args.strikes)
    fetched = sum(r.rows for r in reports)
    symbols = sum(r.symbols for r in reports)
    _say(f"backfill finished: {len(reports)} day-expiry pairs, {symbols} symbol-days fetched, {fetched} rows")
    _say("chain coverage:")
    _print_table(
        recorder.bars.chain_coverage(),
        ["trading_date", "expiry", "min_strike", "max_strike", "symbols", "rows"],
    )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    command = getattr(args, "history_command", None)
    bar_store = BarStore(PATHS.market_db, history_root=PATHS.history)
    if command == "import-parquet":
        results = bar_store.import_parquet(args.root, force=args.force)
        if not results:
            _say("nothing new to import")
        for item in results:
            _say(f"{item['exchange']} {item['symbol']} {item['interval']}: {item['rows']} rows, {item['added']} new")
        return 0
    if command == "export":
        path = bar_store.export_parquet(args.exchange, args.symbol, args.interval, args.out)
        _say(f"exported {args.exchange} {args.symbol} {args.interval} to {path}")
        return 0
    if command == "status":
        _print_table(
            [
                {**row, "coverage": ", ".join(f"{a}..{b}" for a, b in row["coverage"])}
                for row in bar_store.summary()
            ],
            ["exchange", "symbol", "interval", "bars", "first_date", "last_date", "coverage"],
        )
        _say("chain days:")
        _print_table(
            bar_store.chain_days(),
            ["trading_date", "expiry", "min_strike", "max_strike", "symbols", "rows", "status"],
        )
        return 0

    settings = SettingsStore()
    client = None if args.no_fetch else _client(settings)
    cache = HistoryCache(client, store=bar_store)
    end = _date(args.end)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)
    frame = cache.get(args.exchange, args.symbol, args.interval, start, end, fetch=client is not None)
    _say(f"{args.exchange} {args.symbol} {args.interval}: {len(frame)} bars from {start} to {end}")
    if len(frame):
        _say(f"first {frame['timestamp'].iloc[0]}  last {frame['timestamp'].iloc[-1]}")
        _say(frame.tail(args.tail).to_string(index=False))
    dates = cache.available_dates(args.exchange, args.symbol, args.interval)
    if dates:
        _say(f"cached dates: {len(dates)} ({dates[0]} to {dates[-1]})")
    coverage = cache.coverage(args.exchange, args.symbol, args.interval)
    _say("coverage: " + (", ".join(f"{a}..{b}" for a, b in coverage) or "(none)"))
    return 0


def cmd_chain(args: argparse.Namespace) -> int:
    store = SettingsStore()
    client = _client(store)
    if client is None:
        return 1
    calendar = SessionCalendar(client, store)
    resolver = ChainResolver(client, store, calendar)
    try:
        expiry = date.fromisoformat(args.expiry) if args.expiry else None
        snapshot = resolver.chain_snapshot(expiry, strikes_each_side=args.strikes, with_iv=args.iv)
    except OpenAlgoError as exc:
        _say(f"chain unavailable: {exc}")
        try:
            expiries = resolver.expiries()
            _say("expiries: " + ", ".join(e.isoformat() for e in expiries[:8]))
        except OpenAlgoError as inner:
            _say(f"expiry list unavailable: {inner}")
        return 1
    _say(
        f"{snapshot.underlying} expiry {snapshot.expiry} ({snapshot.days_to_expiry:.2f} days)  "
        f"index {snapshot.index_ltp}  vix {snapshot.vix}  forward {snapshot.synthetic_forward:.2f} "
        f"(at {snapshot.forward_strike:g})  ATM {snapshot.atm_strike:g}  lot {snapshot.lot_size}"
    )
    rows = []
    for row in snapshot.rows:
        rows.append(
            {
                "strike": f"{row.strike:g}",
                "ce_ltp": row.ce.ltp if row.ce else "",
                "ce_bid": row.ce.bid if row.ce else "",
                "ce_ask": row.ce.ask if row.ce else "",
                "pe_ltp": row.pe.ltp if row.pe else "",
                "pe_bid": row.pe.bid if row.pe else "",
                "pe_ask": row.pe.ask if row.pe else "",
                "straddle": round(row.ce.ltp + row.pe.ltp, 2) if row.ce and row.pe else "",
                "atm": "*" if row.strike == snapshot.atm_strike else "",
            }
        )
    _print_table(rows, ["strike", "ce_ltp", "ce_bid", "ce_ask", "pe_ltp", "pe_bid", "pe_ask", "straddle", "atm"])
    if snapshot.atm:
        _say(
            f"ATM straddle {snapshot.atm.strike:g}: call {snapshot.atm.call.ltp} put {snapshot.atm.put.ltp} "
            f"combined {snapshot.atm.combined_ltp:.2f} points"
        )
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    store = SettingsStore()
    client = _client(store)
    calendar = SessionCalendar(client, store)
    if client is not None:
        resolver = ChainResolver(client, store, calendar)
        calendar.expiry_source = resolver.expiries
    d = _date(args.date)
    try:
        info = calendar.session_info(d)
    except OpenAlgoError as exc:
        _say(f"session unavailable: {exc}")
        return 1
    _say(json.dumps(info, indent=1))
    try:
        _say(f"next trading day: {calendar.next_trading_day(d)}")
    except RuntimeError as exc:
        _say(str(exc))
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    store = SettingsStore()
    settings = store.get()
    model = CostModel.from_settings(settings)
    lot_size = int(args.lot_size or settings["strategy"].get("lot_size", 65))
    breakdown = model.round_trip(args.credit, args.lots, lot_size, args.exit)
    credit_inr = args.credit * args.lots * lot_size
    _say(f"short straddle round trip: credit {args.credit} points x {args.lots} lot(s) x {lot_size} = INR {credit_inr:,.2f}")
    for key, value in breakdown.to_dict().items():
        _say(f"  {key:<10} {value:>12,.2f}")
    pct = 100.0 * breakdown.total / credit_inr if credit_inr else 0.0
    _say(f"total INR {breakdown.total:,.2f} = {pct:.2f} percent of the credit, "
         f"{breakdown.total / (args.lots * lot_size):.2f} points per unit")
    return 0


# ---------------------------------------------------------------- registry


def register_cli(subparsers: Any) -> None:
    record = subparsers.add_parser("record", help="store the day's option chain and index bars into DuckDB")
    record.add_argument("--date", default=None, help="trading date YYYY-MM-DD (default today)")
    record.add_argument("--strikes", type=int, default=None, help="strikes each side of the day's index path")
    record.add_argument("--force", action="store_true", help="refetch even when covered")
    record.set_defaults(handler=cmd_record)

    backfill = subparsers.add_parser("backfill-chains", help="record every listed expiry for the last N days")
    backfill.add_argument("--days", type=int, default=30)
    backfill.add_argument("--lead", type=int, default=21, help="calendar days before expiry a weekly contract lists")
    backfill.add_argument("--strikes", type=int, default=None)
    backfill.set_defaults(handler=cmd_backfill)

    history = subparsers.add_parser("history", help="fetch, show, import or export cached bars")
    history.add_argument("--exchange", default="NSE_INDEX")
    history.add_argument("--symbol", default="NIFTY")
    history.add_argument("--interval", default="1m")
    history.add_argument("--days", type=int, default=5)
    history.add_argument("--start", default=None)
    history.add_argument("--end", default=None)
    history.add_argument("--tail", type=int, default=5)
    history.add_argument("--no-fetch", action="store_true", help="read the store only")
    history.set_defaults(handler=cmd_history)
    hsub = history.add_subparsers(dest="history_command")
    imp = hsub.add_parser("import-parquet", help="import parquet files under data/history into DuckDB")
    imp.add_argument("--root", default=None)
    imp.add_argument("--force", action="store_true")
    exp = hsub.add_parser("export", help="export one series to parquet")
    exp.add_argument("--exchange", required=True)
    exp.add_argument("--symbol", required=True)
    exp.add_argument("--interval", required=True)
    exp.add_argument("--out", default=None)
    hsub.add_parser("status", help="list stored series, coverage and recorded chain days")

    chain = subparsers.add_parser("chain", help="current-week chain with synthetic forward and ATM")
    chain.add_argument("--expiry", default=None)
    chain.add_argument("--strikes", type=int, default=5)
    chain.add_argument("--iv", action="store_true", help="add implied volatility per leg (one call per leg)")
    chain.set_defaults(handler=cmd_chain)

    session = subparsers.add_parser("session", help="session window and expiry-day flag for a date")
    session.add_argument("--date", default=None)
    session.set_defaults(handler=cmd_session)

    costs = subparsers.add_parser("costs", help="round-trip cost of a short straddle")
    costs.add_argument("--credit", type=float, required=True, help="entry credit in points (both legs)")
    costs.add_argument("--lots", type=int, default=1)
    costs.add_argument("--lot-size", type=int, default=None)
    costs.add_argument("--exit", type=float, default=None, help="exit debit in points (default: same as credit)")
    costs.set_defaults(handler=cmd_costs)


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(prog="openfly")
    register_cli(parser.add_subparsers(dest="command", required=True))
    ns = parser.parse_args()
    sys.exit(ns.handler(ns))
