from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from openfly.market.chain import ChainResolver
from openfly.market.client import IST
from openfly.market.history import HistoryCache
from openfly.market.recorder import Recorder
from openfly.market.session import SessionCalendar
from tests.market.conftest import minute_bars
from tests.market.test_chain import search_rows

EXPIRIES = [
    date(2026, 9, 15),
    date(2026, 9, 22),
    date(2026, 9, 29),
    date(2026, 10, 6),
    date(2026, 10, 27),
    date(2026, 11, 23),
    date(2026, 12, 29),
]
FRI = date(2026, 9, 11)
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=IST)
DEFAULT_LISTED = {
    date(2026, 9, 15): date(2026, 9, 1),
    date(2026, 9, 22): date(2026, 9, 8),
    date(2026, 9, 29): date(2026, 8, 20),
    date(2026, 10, 6): date(2026, 9, 22),
    date(2026, 10, 27): date(2026, 9, 3),
    date(2026, 11, 23): date(2026, 8, 1),
    date(2026, 12, 29): date(2026, 8, 1),
}


class StubBrokerClient:
    """History for the index, VIX and listed option symbols; counts calls per symbol."""

    def __init__(self, listed_from: dict[date, date] | None = None, dead_symbols: set[str] = frozenset()):
        self.calls: dict[str, int] = {}
        self.listed_from = listed_from or dict(DEFAULT_LISTED)
        self.dead_symbols = set(dead_symbols)

    def expiry(self, symbol, exchange, instrumenttype="options"):
        return list(EXPIRIES)

    def search(self, query, exchange):
        for expiry in EXPIRIES:
            if query == f"NIFTY{expiry.strftime('%d%b%y').upper()}":
                return search_rows(expiry, range(21000, 26050, 50))
        return []

    def history(self, symbol, exchange, interval, start_date, end_date):
        self.calls[symbol] = self.calls.get(symbol, 0) + 1
        if symbol == "NIFTY":
            # Index path 23350 to 23450 within the day (closes rise 0.1 per minute from 23349.9).
            return minute_bars(start_date, 23349.9, step=100.0 / 374)
        if symbol == "INDIAVIX":
            return minute_bars(start_date, 12.0, step=0.0)
        if symbol.startswith("NIFTY") and symbol[-2:] in {"CE", "PE"}:
            code = symbol[5:12]
            expiry = next((e for e in EXPIRIES if e.strftime("%d%b%y").upper() == code), None)
            if expiry is None or start_date < self.listed_from[expiry] or symbol in self.dead_symbols:
                return minute_bars(start_date).iloc[0:0]
            strike = float(symbol[-7:-2])
            price = max(1.0, 60.0 - abs(strike - 23400) * 0.3) + (30 if symbol.endswith("CE") else 0)
            return minute_bars(start_date, price, step=0.01)
        return minute_bars(start_date).iloc[0:0]

    def option_calls(self) -> dict[str, int]:
        return {k: v for k, v in self.calls.items() if k[-2:] in {"CE", "PE"}}


def build(paths, settings, bar_store, client=None) -> Recorder:
    client = client or StubBrokerClient()
    cache = HistoryCache(client, store=bar_store, now=NOW)
    calendar = SessionCalendar(None, settings, paths, now=lambda: NOW)
    resolver = ChainResolver(client, settings, calendar)
    return Recorder(client, cache=cache, resolver=resolver, calendar=calendar, store=settings)


@pytest.fixture
def recorder(paths, settings, bar_store):
    return build(paths, settings, bar_store)


def test_strike_range_and_expiry_choice(recorder):
    assert recorder.strike_range(23349.9, 23450.1) == (22700.0, 24100.0)
    assert recorder.strike_range(23350.000000004, 23450.000000004) == (22750.0, 24050.0)
    assert recorder.strike_range(23400.0, 23400.0, n=1) == (23350.0, 23450.0)
    # Weekly and monthly as of the date; the following one of each kind on its expiry day.
    assert recorder.expiries_for(date(2026, 9, 11)) == [date(2026, 9, 15), date(2026, 9, 29)]
    assert recorder.expiries_for(date(2026, 9, 15)) == [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]
    assert recorder.expiries_for(date(2026, 9, 29)) == [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 27)]
    assert recorder.expiries_for(date(2026, 9, 30)) == [date(2026, 10, 6), date(2026, 10, 27)]


def test_record_day_is_idempotent(recorder):
    report = recorder.record(FRI, expiries=[date(2026, 9, 15)])
    assert report.is_trading_day and report.expiries == [date(2026, 9, 15)]
    assert report.index_min == pytest.approx(23350.0, abs=0.01)
    assert report.index_max == pytest.approx(23450.0, abs=0.01)
    assert (report.strike_lo, report.strike_hi) == (22750.0, 24050.0)
    legs = 2 * int((24050 - 22750) / 50 + 1)
    assert legs == 54
    option_calls = recorder.client.option_calls()
    assert len(option_calls) == 54 and all(v == 1 for v in option_calls.values())
    assert recorder.client.calls["NIFTY"] == 1 and recorder.client.calls["INDIAVIX"] == 1
    assert report.rows == 54 * 375 + 2 * 375
    assert report.status_of(date(2026, 9, 15)) == "done"

    store = recorder.bars
    chain = store.chain(FRI)
    assert len(chain) == 54 * 375
    assert chain["symbol"].nunique() == 54
    assert set(chain["option_type"]) == {"CE", "PE"}
    assert chain["strike"].min() == 22750.0 and chain["strike"].max() == 24050.0
    assert str(chain["timestamp"].dtype) == "datetime64[us, Asia/Kolkata]"
    assert chain["expiry"].iloc[0] == date(2026, 9, 15) and chain["trading_date"].iloc[0] == FRI
    assert store.chain_expiries(FRI) == [date(2026, 9, 15)]
    assert store.chain_coverage() == [
        {"trading_date": "2026-09-11", "expiry": "2026-09-15", "min_strike": 22750.0, "max_strike": 24050.0, "symbols": 54, "rows": 54 * 375}
    ]
    assert store.chain_day_status(FRI, date(2026, 9, 15))["status"] == "done"
    # Option bars are also queryable as ordinary bars, with coverage recorded.
    assert store.count("NFO", "NIFTY15SEP2623400CE", "1m") == 375
    assert store.is_covered("NFO", "NIFTY15SEP2623400CE", "1m", FRI)

    # Second run: a no-op, nothing fetched.
    again = recorder.record(FRI, expiries=[date(2026, 9, 15)])
    assert again.rows == 0 and again.symbols == 0
    assert again.already >= 54
    assert recorder.client.calls == {k: 1 for k in recorder.client.calls}


def test_record_default_expiries_cover_weekly_and_monthly(recorder):
    report = recorder.record(FRI)
    assert report.expiries == [date(2026, 9, 15), date(2026, 9, 29)]
    assert recorder.bars.chain_expiries(FRI) == [date(2026, 9, 15), date(2026, 9, 29)]
    assert len(recorder.bars.chain(FRI, date(2026, 9, 29))) == 54 * 375


def test_unlisted_contract_is_probed_once_and_marked_empty(paths, settings, bar_store):
    client = StubBrokerClient(listed_from={**DEFAULT_LISTED, date(2026, 9, 22): date(2026, 9, 14)})
    recorder = build(paths, settings, bar_store, client)
    report = recorder.record(FRI, expiries=[date(2026, 9, 22)])
    assert len(client.option_calls()) == 1  # one probe only
    assert bar_store.chain_day_status(FRI, date(2026, 9, 22))["status"] == "empty"
    assert report.status_of(date(2026, 9, 22)) == "empty"
    assert any("not listed yet" in text for text in report.skipped.values())
    recorder.record(FRI, expiries=[date(2026, 9, 22)])
    assert len(client.option_calls()) == 1


def test_non_trading_day_records_nothing(recorder):
    report = recorder.record(date(2026, 9, 12))
    assert not report.is_trading_day and report.rows == 0 and recorder.client.calls == {}


def test_backfill_plan_windows(recorder):
    plan = dict(recorder.backfill_plan(weekly_days=3, monthly_days=10))
    # Current and next monthly, then the weeklies; far monthlies (23-NOV, 29-DEC) are not planned at all.
    assert list(plan) == [date(2026, 9, 29), date(2026, 10, 27), date(2026, 9, 15), date(2026, 9, 22), date(2026, 10, 6)]
    assert plan[date(2026, 9, 15)] == [date(2026, 9, 11), date(2026, 9, 10), date(2026, 9, 9)]
    assert len(plan[date(2026, 9, 29)]) == 10 and plan[date(2026, 9, 29)][0] == FRI
    assert plan[date(2026, 9, 29)][-1] == date(2026, 8, 31)
    only = recorder.backfill_plan(weekly_days=3, monthly_days=10, only_expiry=date(2026, 10, 27))
    assert len(only) == 1 and only[0][0] == date(2026, 10, 27)


def test_backfill_stops_an_expiry_at_its_first_unlisted_day_and_resumes(recorder):
    messages: list[str] = []
    reports = recorder.backfill(weekly_days=3, monthly_days=6, only_expiry=None, progress=messages.append)
    # 22-SEP listed from 08-SEP: three weekly days all present. 06-OCT lists 22-SEP: one probe then stop.
    visited = {(r.date, r.expiries[0]) for r in reports}
    assert (FRI, date(2026, 10, 6)) in visited and (date(2026, 9, 10), date(2026, 10, 6)) not in visited
    assert (date(2026, 9, 9), date(2026, 9, 22)) in visited
    # 29-SEP listed from 20-AUG: all six monthly days present; 27-OCT from 03-SEP: six days too.
    assert sum(1 for d, e in visited if e == date(2026, 9, 29)) == 6
    assert any("not listed earlier" in m for m in messages)
    first_calls = dict(recorder.client.calls)
    recorder.backfill(weekly_days=3, monthly_days=6, progress=messages.append)
    assert recorder.client.calls == first_calls  # resumable: coverage and chain_days make it a no-op


def test_symbol_exhausted_after_first_empty_response_for_older_dates(paths, settings, bar_store):
    dead = {"NIFTY29SEP2622750CE", "NIFTY29SEP2622750PE"}
    client = StubBrokerClient(dead_symbols=dead)
    recorder = build(paths, settings, bar_store, client)
    recorder.backfill(weekly_days=0, monthly_days=3, only_expiry=date(2026, 9, 29))
    assert client.calls["NIFTY29SEP2622750CE"] == 1  # empty on the newest day, never asked for older days
    assert client.calls["NIFTY29SEP2623400CE"] == 3
    assert bar_store.is_covered("NFO", "NIFTY29SEP2622750CE", "1m", date(2026, 9, 9))
    assert bar_store.chain_day_status(date(2026, 9, 9), date(2026, 9, 29))["status"] == "done"


def test_atm_path_from_stored_chain(recorder):
    recorder.record(FRI, expiries=[date(2026, 9, 15)])
    path = recorder.bars.atm_path(FRI, step=50.0)
    assert len(path) == 375
    assert list(path.columns)[:5] == ["timestamp", "index_close", "near_strike", "forward", "atm_strike"]
    row = path.iloc[0]
    assert row["near_strike"] == 23350.0  # index 23350.0 at the open
    # forward = strike + CE - PE with the stub's prices: CE = PE + 30 at every strike.
    assert row["forward"] == pytest.approx(23350.0 + 30.0, abs=0.05)
    assert row["atm_strike"] == 23400.0
    assert row["combined"] == pytest.approx(row["ce_close"] + row["pe_close"])
    assert row["ce_symbol"] == "NIFTY15SEP2623400CE" and row["pe_symbol"] == "NIFTY15SEP2623400PE"
    assert path["atm_strike"].iloc[-1] == 23500.0  # index 23450 at the close, forward 23480
    assert str(path["timestamp"].dtype) == "datetime64[us, Asia/Kolkata]"
    assert isinstance(path, pd.DataFrame)
    assert len(recorder.bars.atm_path(date(2026, 9, 10))) == 0
