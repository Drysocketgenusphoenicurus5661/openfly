"""Read-only integration tests against the local OpenAlgo server.

Run with ``OPENFLY_LIVE_TESTS=1 uv run pytest tests/market/test_live_readonly.py -q -s``.
Only read endpoints are called. Tests skip (never fail) when the broker
session behind OpenAlgo is not authorised for the call, which is what a
Saturday with an expired broker token looks like.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from openfly.market.chain import ChainResolver
from openfly.market.client import IST, OpenAlgoClient, OpenAlgoError, option_symbol

pytestmark = pytest.mark.skipif(os.environ.get("OPENFLY_LIVE_TESTS") != "1", reason="set OPENFLY_LIVE_TESTS=1")

READ_ONLY_ENDPOINTS = {
    "quotes", "multiquotes", "depth", "history", "intervals", "symbol", "search", "expiry", "optiongreeks",
    "margin", "funds", "positionbook", "orderbook", "tradebook", "market/holidays", "market/timings",
    "analyzer", "openposition",
}


@pytest.fixture(scope="module")
def client() -> OpenAlgoClient:
    from openfly.config import SettingsStore

    settings = SettingsStore().get()
    if not settings["openalgo"].get("api_key"):
        pytest.skip("no OpenAlgo API key in settings")
    live = OpenAlgoClient.from_settings()
    original = live._post

    def guarded(endpoint, body=None):
        assert endpoint in READ_ONLY_ENDPOINTS, f"refusing non read-only endpoint {endpoint}"
        return original(endpoint, body)

    live._post = guarded  # type: ignore[method-assign]
    return live


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except OpenAlgoError as exc:
        if exc.permanent or exc.code in {403, 500}:
            pytest.skip(f"broker declined {exc.endpoint}: {exc.message}")
        raise


def test_quotes_nifty(client):
    quote = call(client.quotes, "NIFTY", "NSE_INDEX")
    print(f"\nquotes NIFTY: ltp {quote.ltp} bid {quote.bid} ask {quote.ask}")
    assert quote.ltp > 10000


def test_expiry_list(client):
    expiries = call(client.expiry, "NIFTY", "NFO", "options")
    print(f"\nexpiry NIFTY options: {[e.isoformat() for e in expiries[:8]]} ({len(expiries)} total)")
    assert expiries and expiries[0] >= date.today() - timedelta(days=7)
    assert all(a < b for a, b in zip(expiries, expiries[1:], strict=False))


def test_search_current_week_strike(client):
    expiries = call(client.expiry, "NIFTY", "NFO", "options")
    query = f"NIFTY{expiries[0]:%d%b%y}".upper()
    rows = call(client.search, query, "NFO")
    strikes = sorted({r["strike"] for r in rows})
    print(f"\nsearch {query}: {len(rows)} rows, strikes {strikes[0]:g} to {strikes[-1]:g}, example {rows[0]['symbol']}")
    assert rows and all(r["instrumenttype"] in {"CE", "PE"} for r in rows)
    assert all(r["expiry"].upper() == expiries[0].strftime("%d-%b-%y").upper() for r in rows)


def test_multiquotes_two_option_symbols(client):
    expiries = call(client.expiry, "NIFTY", "NFO", "options")
    rows = call(client.search, f"NIFTY{expiries[0]:%d%b%y}".upper(), "NFO")
    strikes = sorted({r["strike"] for r in rows})
    middle = strikes[len(strikes) // 2]
    symbols = [("NFO", option_symbol("NIFTY", expiries[0], middle, "CE")), ("NFO", option_symbol("NIFTY", expiries[0], middle, "PE"))]
    quotes = call(client.multiquotes, symbols)
    print(f"\nmultiquotes: {[(q.symbol, q.ltp, q.bid, q.ask) for q in quotes]}")
    assert len(quotes) == 2


def test_history_nifty_1m_last_3_days(client):
    end = date.today()
    frame = call(client.history, "NIFTY", "NSE_INDEX", "1m", end - timedelta(days=4), end)
    print(f"\nhistory NIFTY 1m: {len(frame)} bars, first {frame['timestamp'].iloc[0]}, last {frame['timestamp'].iloc[-1]}")
    assert len(frame) > 300
    assert str(frame["timestamp"].dt.tz) == "Asia/Kolkata"
    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def test_timings_and_holidays(client):
    rows = call(client.timings, "2026-09-11")
    nfo = next(r for r in rows if r["exchange"] == "NFO")
    from datetime import datetime

    start = datetime.fromtimestamp(nfo["start_time"] / 1000, IST)
    end = datetime.fromtimestamp(nfo["end_time"] / 1000, IST)
    print(f"\ntimings 2026-09-11 NFO: {start.time()} to {end.time()} ({len(rows)} exchanges)")
    assert start.time().hour == 9 and end.time().hour == 15
    holidays = call(client.holidays, 2026)
    nse = [h["date"] for h in holidays if "NSE" in (h.get("closed_exchanges") or [])]
    print(f"holidays 2026: {len(holidays)} entries, NSE closed on {nse}")
    assert holidays


def test_analyzer_status_and_funds(client):
    status = call(client.analyzer_status)
    print(f"\nanalyzer: {status}")
    assert status["mode"] in {"live", "analyze"}
    funds = call(client.funds)
    print(f"funds keys: {sorted(funds)} (values withheld)")
    assert isinstance(funds, dict)


def test_chain_snapshot_end_to_end(client):
    resolver = ChainResolver(client)
    try:
        snapshot = resolver.chain_snapshot(strikes_each_side=3)
    except OpenAlgoError as exc:
        pytest.skip(f"chain unavailable: {exc.message}")
    print(f"\nchain {snapshot.expiry}: index {snapshot.index_ltp} forward {snapshot.synthetic_forward:.1f} atm {snapshot.atm_strike:g} combined {snapshot.atm.combined_ltp:.1f}")
    assert snapshot.atm is not None
