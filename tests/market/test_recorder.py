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

EXPIRIES = [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]
FRI = date(2026, 9, 11)
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=IST)


class StubBrokerClient:
    """History for the index, VIX and listed option symbols; counts calls per symbol."""

    def __init__(self, listed_from: dict[date, date] | None = None):
        self.calls: dict[str, int] = {}
        self.listed_from = listed_from or {e: date(2026, 9, 1) for e in EXPIRIES}

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
            bars = minute_bars(start_date, 23349.9, step=100.0 / 374)
            return bars
        if symbol == "INDIAVIX":
            return minute_bars(start_date, 12.0, step=0.0)
        if symbol.startswith("NIFTY") and symbol[-2:] in {"CE", "PE"}:
            code = symbol[5:12]
            expiry = next((e for e in EXPIRIES if e.strftime("%d%b%y").upper() == code), None)
            if expiry is None or start_date < self.listed_from[expiry]:
                return minute_bars(start_date).iloc[0:0]
            strike = float(symbol[-7:-2])
            price = max(1.0, 60.0 - abs(strike - 23400) * 0.3) + (30 if symbol.endswith("CE") else 0)
            return minute_bars(start_date, price, step=0.01)
        return minute_bars(start_date).iloc[0:0]


@pytest.fixture
def recorder(paths, settings, bar_store):
    client = StubBrokerClient()
    cache = HistoryCache(client, store=bar_store, now=NOW)
    calendar = SessionCalendar(None, settings, paths, now=lambda: NOW)
    resolver = ChainResolver(client, settings, calendar)
    return Recorder(client, cache=cache, resolver=resolver, calendar=calendar, store=settings)


def test_strike_range_and_expiry_choice(recorder):
    assert recorder.strike_range(23349.9, 23450.1) == (22700.0, 24100.0)
    assert recorder.strike_range(23350.000000004, 23450.000000004) == (22750.0, 24050.0)
    assert recorder.strike_range(23400.0, 23400.0, n=1) == (23350.0, 23450.0)
    assert recorder.expiries_for(date(2026, 9, 11)) == [date(2026, 9, 15)]
    assert recorder.expiries_for(date(2026, 9, 15)) == [date(2026, 9, 15), date(2026, 9, 22)]
    assert recorder.expiries_for(date(2026, 9, 16)) == [date(2026, 9, 22)]


def test_record_day_is_idempotent(recorder):
    report = recorder.record(FRI)
    assert report.is_trading_day and report.expiries == [date(2026, 9, 15)]
    assert report.index_min == pytest.approx(23350.0, abs=0.01)
    assert report.index_max == pytest.approx(23450.0, abs=0.01)
    assert (report.strike_lo, report.strike_hi) == (22750.0, 24050.0)
    legs = 2 * int((24050 - 22750) / 50 + 1)
    assert legs == 54
    option_calls = {k: v for k, v in recorder.client.calls.items() if k[-2:] in {"CE", "PE"}}
    assert len(option_calls) == 54 and all(v == 1 for v in option_calls.values())
    assert recorder.client.calls["NIFTY"] == 1 and recorder.client.calls["INDIAVIX"] == 1
    assert report.rows == 54 * 375 + 2 * 375

    store = recorder.bars
    chain = store.chain(FRI)
    assert len(chain) == 54 * 375
    assert chain["symbol"].nunique() == 54
    assert set(chain["option_type"]) == {"CE", "PE"}
    assert chain["strike"].min() == 22750.0 and chain["strike"].max() == 24050.0
    assert str(chain["timestamp"].dtype) == "datetime64[us, Asia/Kolkata]"
    assert chain["expiry"].iloc[0] == date(2026, 9, 15) and chain["trading_date"].iloc[0] == FRI
    assert store.chain_expiries(FRI) == [date(2026, 9, 15)]
    coverage = store.chain_coverage()
    assert coverage == [
        {"trading_date": "2026-09-11", "expiry": "2026-09-15", "min_strike": 22750.0, "max_strike": 24050.0, "symbols": 54, "rows": 54 * 375}
    ]
    assert store.chain_day_status(FRI, date(2026, 9, 15))["status"] == "done"
    # Option bars are also queryable as ordinary bars, with coverage recorded.
    assert store.count("NFO", "NIFTY15SEP2623400CE", "1m") == 375
    assert store.is_covered("NFO", "NIFTY15SEP2623400CE", "1m", FRI)

    # Second run: a no-op, nothing fetched.
    again = recorder.record(FRI)
    assert again.rows == 0 and again.symbols == 0
    assert again.already >= 54
    assert recorder.client.calls == {k: 1 for k in recorder.client.calls}


def test_unlisted_contract_is_probed_once_and_marked_empty(paths, settings, bar_store):
    client = StubBrokerClient(listed_from={date(2026, 9, 15): date(2026, 9, 1), date(2026, 9, 22): date(2026, 9, 14), date(2026, 9, 29): date(2026, 9, 1)})
    cache = HistoryCache(client, store=bar_store, now=NOW)
    calendar = SessionCalendar(None, settings, paths, now=lambda: NOW)
    recorder = Recorder(client, cache=cache, resolver=ChainResolver(client, settings, calendar), calendar=calendar, store=settings)
    report = recorder.record(FRI, expiries=[date(2026, 9, 22)])
    option_calls = [k for k in client.calls if k[-2:] in {"CE", "PE"}]
    assert len(option_calls) == 1  # one probe only
    assert bar_store.chain_day_status(FRI, date(2026, 9, 22))["status"] == "empty"
    assert any("not listed yet" in text for text in report.skipped.values())
    recorder.record(FRI, expiries=[date(2026, 9, 22)])
    assert len([k for k in client.calls if k[-2:] in {"CE", "PE"}]) == 1


def test_non_trading_day_records_nothing(recorder):
    report = recorder.record(date(2026, 9, 12))
    assert not report.is_trading_day and report.rows == 0 and recorder.client.calls == {}


def test_backfill_pairs_and_resume(recorder):
    pairs = recorder.backfill_days(days=10, listing_lead_days=21)
    assert pairs[0] == (FRI, date(2026, 9, 15))
    assert (FRI, date(2026, 9, 22)) in pairs and (date(2026, 9, 2), date(2026, 9, 15)) in pairs
    assert all(d.weekday() < 5 and d <= FRI for d, _ in pairs)
    messages: list[str] = []
    reports = recorder.backfill(days=1, progress=messages.append)
    assert len(reports) == 3 and messages[0].startswith("backfill: 3")
    first_calls = dict(recorder.client.calls)
    recorder.backfill(days=1, progress=messages.append)
    assert recorder.client.calls == first_calls


def test_atm_path_from_stored_chain(recorder):
    recorder.record(FRI)
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
