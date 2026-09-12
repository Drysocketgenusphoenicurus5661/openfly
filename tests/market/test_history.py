from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from openfly.market.client import IST
from openfly.market.history import HistoryCache
from openfly.market.store import BarStore, group_consecutive
from tests.market.conftest import minute_bars

MON, TUE, WED, THU, FRI = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11))
SAT = date(2026, 9, 12)


class FakeHistoryClient:
    """Serves synthetic bars for known symbols and records every request."""

    def __init__(self, listed: dict[str, float] | None = None, holidays: set[date] = frozenset()):
        self.listed = listed or {"NIFTY": 23000.0}
        self.holidays = set(holidays)
        self.calls: list[tuple[str, str, str, date, date]] = []

    def history(self, symbol, exchange, interval, start_date, end_date):
        self.calls.append((symbol, exchange, interval, start_date, end_date))
        frames = []
        if symbol in self.listed:
            d = start_date
            while d <= end_date:
                if d.weekday() < 5 and d not in self.holidays:
                    frames.append(minute_bars(d, self.listed[symbol]))
                d = date.fromordinal(d.toordinal() + 1)
        if not frames:
            return minute_bars(FRI).iloc[0:0]
        return pd.concat(frames, ignore_index=True)


# ------------------------------------------------------------------ BarStore


def test_upsert_is_idempotent_and_replaces(bar_store: BarStore):
    frame = minute_bars(FRI, 100.0)
    assert bar_store.upsert_bars("NSE_INDEX", "NIFTY", "1m", frame) == 375
    assert bar_store.upsert_bars("NSE_INDEX", "NIFTY", "1m", frame) == 0
    assert bar_store.count("NSE_INDEX", "NIFTY", "1m") == 375
    changed = frame.copy()
    changed.loc[0, "close"] = 999.0
    assert bar_store.upsert_bars("NSE_INDEX", "NIFTY", "1m", changed) == 0
    out = bar_store.bars("NSE_INDEX", "NIFTY", "1m")
    assert out["close"].iloc[0] == 999.0
    assert len(out) == 375


def test_timestamps_round_trip_as_ist(bar_store: BarStore):
    bar_store.upsert_bars("NSE_INDEX", "NIFTY", "1m", minute_bars(FRI))
    out = bar_store.bars("NSE_INDEX", "NIFTY", "1m", FRI, FRI)
    assert str(out["timestamp"].dtype) == "datetime64[us, Asia/Kolkata]"
    assert out["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    assert bar_store.available_dates("NSE_INDEX", "NIFTY", "1m") == [FRI]
    assert len(bar_store.day("NSE_INDEX", "NIFTY", "1m", THU)) == 0
    assert bar_store.symbols() == [("NSE_INDEX", "NIFTY", "1m")]


def test_coverage_gap_computation_ignores_holidays_inside_covered_range(bar_store: BarStore):
    # Bars exist Mon, Tue, Thu, Fri; Wed was a holiday with no bars, but the range was fetched.
    for d in (MON, TUE, THU, FRI):
        bar_store.upsert_bars("NSE_INDEX", "NIFTY", "1m", minute_bars(d))
    bar_store.add_coverage("NSE_INDEX", "NIFTY", "1m", MON, FRI)
    assert bar_store.missing_ranges("NSE_INDEX", "NIFTY", "1m", MON, FRI, today=SAT) == []
    assert bar_store.missing_ranges("NSE_INDEX", "NIFTY", "1m", WED, WED, today=SAT) == []
    prev_fri, next_mon = date(2026, 9, 4), date(2026, 9, 14)
    assert bar_store.missing_ranges("NSE_INDEX", "NIFTY", "1m", prev_fri, next_mon, today=date(2026, 9, 20)) == [
        (prev_fri, prev_fri),
        (next_mon, next_mon),
    ]
    bar_store.add_coverage("NSE_INDEX", "NIFTY", "1m", date(2026, 9, 14), date(2026, 9, 18))
    bar_store.add_coverage("NSE_INDEX", "NIFTY", "1m", date(2026, 9, 12), date(2026, 9, 13))
    assert bar_store.coverage("NSE_INDEX", "NIFTY", "1m") == [(MON, date(2026, 9, 18))]


def test_group_consecutive_bridges_weekends():
    days = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 21)]
    assert group_consecutive(days) == [(date(2026, 9, 10), date(2026, 9, 14)), (date(2026, 9, 21), date(2026, 9, 21))]


def test_import_parquet_on_first_open_and_export(paths):
    frame = minute_bars(FRI, 12.0)
    target = paths.history / "NSE_INDEX" / "INDIAVIX" / "1m.parquet"
    target.parent.mkdir(parents=True)
    frame.to_parquet(target, index=False)
    store = BarStore(paths.market_db, history_root=paths.history)
    assert store.count("NSE_INDEX", "INDIAVIX", "1m") == 375
    assert store.coverage("NSE_INDEX", "INDIAVIX", "1m") == [(FRI, FRI)]
    # A second open does not re-import an unchanged file.
    again = BarStore(paths.market_db, history_root=paths.history)
    assert again.import_parquet() == []
    out = again.export_parquet("NSE_INDEX", "INDIAVIX", "1m", paths.data / "export.parquet")
    back = pd.read_parquet(out)
    assert len(back) == 375 and str(back["timestamp"].dtype) == "datetime64[us, Asia/Kolkata]"


def test_context_manager_holds_one_connection(bar_store: BarStore):
    with bar_store as store:
        store.upsert_bars("X", "Y", "1m", minute_bars(FRI))
        assert store.count("X", "Y", "1m") == 375
    assert bar_store.count("X", "Y", "1m") == 375


# -------------------------------------------------------------- HistoryCache


def test_get_fetches_only_missing_ranges(bar_store: BarStore):
    client = FakeHistoryClient()
    cache = HistoryCache(client, store=bar_store, now=datetime(2026, 9, 12, 12, 0, tzinfo=IST))
    out = cache.get("NSE_INDEX", "NIFTY", "1m", MON, FRI)
    assert len(out) == 5 * 375
    assert len(client.calls) == 1 and client.calls[0][3:] == (MON, FRI)
    cache.get("NSE_INDEX", "NIFTY", "1m", MON, FRI)
    assert len(client.calls) == 1  # fully covered: no broker call
    cache.get("NSE_INDEX", "NIFTY", "1m", date(2026, 9, 3), FRI)
    assert len(client.calls) == 2 and client.calls[1][3:] == (date(2026, 9, 3), date(2026, 9, 4))
    assert cache.available_dates("NSE_INDEX", "NIFTY", "1m")[0] == date(2026, 9, 3)
    assert cache.coverage("NSE_INDEX", "NIFTY", "1m") == [(date(2026, 9, 3), FRI)]


def test_holiday_inside_fetched_range_is_not_refetched(bar_store: BarStore):
    client = FakeHistoryClient(holidays={WED})
    cache = HistoryCache(client, store=bar_store, now=datetime(2026, 9, 12, 12, 0, tzinfo=IST))
    assert len(cache.get("NSE_INDEX", "NIFTY", "1m", MON, FRI)) == 4 * 375
    cache.get("NSE_INDEX", "NIFTY", "1m", WED, WED)
    cache.get("NSE_INDEX", "NIFTY", "1m", MON, FRI)
    assert len(client.calls) == 1


def test_today_is_refetched_until_the_session_settles(bar_store: BarStore):
    client = FakeHistoryClient()
    cache = HistoryCache(client, store=bar_store, now=datetime(2026, 9, 11, 12, 0, tzinfo=IST))
    cache.get("NSE_INDEX", "NIFTY", "1m", FRI, FRI)
    cache.get("NSE_INDEX", "NIFTY", "1m", FRI, FRI)
    assert len(client.calls) == 2  # during the session today is never considered covered
    assert cache.coverage("NSE_INDEX", "NIFTY", "1m") == []
    cache = HistoryCache(client, store=bar_store, now=datetime(2026, 9, 11, 16, 0, tzinfo=IST))
    cache.get("NSE_INDEX", "NIFTY", "1m", FRI, FRI)
    cache.get("NSE_INDEX", "NIFTY", "1m", FRI, FRI)
    assert len(client.calls) == 3
    assert cache.coverage("NSE_INDEX", "NIFTY", "1m") == [(FRI, FRI)]


def test_fetch_chunks_long_ranges(bar_store: BarStore):
    client = FakeHistoryClient()
    cache = HistoryCache(client, store=bar_store, chunk_days={"1m": 2}, now=datetime(2026, 9, 12, 12, 0, tzinfo=IST))
    cache.get("NSE_INDEX", "NIFTY", "1m", MON, FRI)
    assert [(c[3], c[4]) for c in client.calls] == [(MON, TUE), (WED, THU), (FRI, FRI)]


def test_unlisted_symbol_records_coverage_without_bars(bar_store: BarStore):
    client = FakeHistoryClient()
    cache = HistoryCache(client, store=bar_store, now=datetime(2026, 9, 12, 12, 0, tzinfo=IST))
    assert len(cache.get("NFO", "NOPE", "1m", MON, FRI)) == 0
    assert len(cache.get("NFO", "NOPE", "1m", MON, FRI)) == 0
    assert len(client.calls) == 1


def test_merge_without_duplicates():
    a = minute_bars(FRI, 1.0)
    b = minute_bars(FRI, 2.0).iloc[100:200]
    merged = HistoryCache.merge(a, b)
    assert len(merged) == 375
    assert merged["open"].iloc[100] == 2.0 + 100 * 0.1
    assert merged["open"].iloc[99] == 1.0 + 99 * 0.1


# ---------------------------------------------------------------- transforms


def test_resample_aligns_to_0915_and_last_bar_covers_to_1529():
    bars = minute_bars(FRI, 100.0)
    five = HistoryCache.resample(bars, "5m")
    assert len(five) == 75
    assert five["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    assert five["timestamp"].iloc[1] == datetime(2026, 9, 11, 9, 20, tzinfo=IST)
    assert five["timestamp"].iloc[-1] == datetime(2026, 9, 11, 15, 25, tzinfo=IST)
    assert five["open"].iloc[0] == bars["open"].iloc[0]
    assert five["close"].iloc[0] == bars["close"].iloc[4]
    assert five["high"].iloc[0] == bars["high"].iloc[:5].max()
    assert five["low"].iloc[-1] == bars["low"].iloc[370:375].min()
    assert five["close"].iloc[-1] == bars["close"].iloc[374]
    assert five["volume"].iloc[0] == 50.0
    fifteen = HistoryCache.resample(bars, "15m")
    assert len(fifteen) == 25 and fifteen["timestamp"].iloc[-1] == datetime(2026, 9, 11, 15, 15, tzinfo=IST)
    hourly = HistoryCache.resample(bars, "1h")
    assert hourly["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    assert hourly["timestamp"].iloc[-1] == datetime(2026, 9, 11, 15, 15, tzinfo=IST)


def test_resample_across_days_has_no_overnight_bars():
    bars = pd.concat([minute_bars(THU), minute_bars(FRI)], ignore_index=True)
    five = HistoryCache.resample(bars, "5m")
    assert len(five) == 150
    assert five["timestamp"].iloc[75] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)


def test_day_and_records():
    bars = pd.concat([minute_bars(THU), minute_bars(FRI)], ignore_index=True)
    friday = HistoryCache.day(bars, FRI)
    assert len(friday) == 375 and friday["timestamp"].dt.date.nunique() == 1
    records = HistoryCache.to_records(friday.head(1))
    assert records[0]["t"] == "2026-09-11T09:15:00+05:30" and records[0]["o"] == 100.0


def test_resample_rejects_bad_interval():
    with pytest.raises(ValueError):
        HistoryCache.resample(minute_bars(FRI), "D")
