from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from openfly.interfaces import Contract, Quote, UnresolvedOrder
from openfly.market.client import (
    IST,
    OpenAlgoClient,
    OpenAlgoError,
    TokenBucket,
    option_symbol,
    parse_expiry,
    parse_history,
)
from tests.market.conftest import (
    TEST_KEY,
    body_of,
    error,
    make_client,
    minute_bars,
    ok,
    rows_from_frame,
)

# ------------------------------------------------------------------ envelope


def test_envelope_success_returns_data_and_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        assert body["apikey"] == TEST_KEY
        if request.url.path.endswith("/funds"):
            return ok({"availablecash": "320.66", "collateral": "0.00", "m2mrealized": "3.27"})
        return error("Invalid symbol")

    client = make_client(handler)
    assert client.funds() == {"availablecash": 320.66, "collateral": 0.0, "m2mrealized": 3.27}
    with pytest.raises(OpenAlgoError) as info:
        client.quotes("NOPE", "NSE")
    assert info.value.endpoint == "quotes"
    assert info.value.code == 200
    assert "Invalid symbol" in str(info.value)
    assert client.retries == 0


def test_error_message_never_contains_key():
    client = make_client(lambda request: error("bad request", 400))
    with pytest.raises(OpenAlgoError) as info:
        client.intervals()
    assert TEST_KEY not in str(info.value)
    assert TEST_KEY not in repr(client)


# ------------------------------------------------------------------- retries


def test_retries_5xx_then_succeeds_with_capped_jittered_backoff():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            return httpx.Response(503, text="gateway down")
        return ok({"minutes": ["1m"]})

    client = make_client(handler)
    assert client.intervals() == {"minutes": ["1m"]}
    assert attempts["n"] == 4
    assert client.retries == 3
    # rng fixed at 1.0 gives the full backoff: 0.5, 1.0, 2.0 seconds.
    assert client.sleeps == [0.5, 1.0, 2.0]


def test_backoff_is_capped_at_8_seconds():
    client = make_client(lambda request: httpx.Response(502), max_tries=8)
    with pytest.raises(OpenAlgoError):
        client.intervals()
    assert max(client.sleeps) == 8.0
    assert len(client.sleeps) == 7


def test_retries_on_429_and_connection_errors():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        if attempts["n"] == 2:
            return httpx.Response(429, json={"status": "error", "message": "Rate limit exceeded"})
        return ok({"analyze_mode": False, "mode": "live"})

    client = make_client(handler)
    assert client.analyzer_status()["mode"] == "live"
    assert attempts["n"] == 3


def test_gives_up_after_five_tries():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    client = make_client(handler)
    with pytest.raises(OpenAlgoError) as info:
        client.intervals()
    assert attempts["n"] == 5
    assert info.value.code == 500
    assert "gave up after 5" in str(info.value)


def test_permanent_5xx_is_not_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return error("API Permission denied: Insufficient permission for that call..", 500)

    client = make_client(handler)
    with pytest.raises(OpenAlgoError) as info:
        client.quotes("NIFTY", "NSE_INDEX")
    assert attempts["n"] == 1
    assert info.value.permanent


def test_write_endpoint_timeout_raises_unresolved_without_retry():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(handler)
    with pytest.raises(UnresolvedOrder):
        client.placeorder("NIFTY15SEP2623400CE", "NFO", "SELL", 65)
    assert attempts["n"] == 1


def test_write_endpoint_retries_only_connect_errors_and_429():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        if attempts["n"] == 2:
            return httpx.Response(429, json={"status": "error", "message": "Rate limit exceeded"})
        return httpx.Response(200, json={"status": "success", "orderid": 250408000989443, "mode": "analyze"})

    client = make_client(handler)
    result = client.placeorder("NIFTY15SEP2623400CE", "NFO", "SELL", 65)
    assert result == {"orderid": "250408000989443", "mode": "analyze"}
    assert attempts["n"] == 3


def test_write_endpoint_5xx_not_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={"status": "error", "message": "broker timeout"})

    client = make_client(handler)
    with pytest.raises(OpenAlgoError):
        client.cancelorder("1")
    assert attempts["n"] == 1


# --------------------------------------------------------------- rate limits


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_token_bucket_allows_burst_then_throttles():
    clock = FakeClock()
    bucket = TokenBucket(50, clock=clock, sleep=clock.sleep)
    for _ in range(50):
        assert bucket.acquire() == 0.0
    waited = bucket.acquire()
    assert waited == pytest.approx(0.02)
    clock.t += 1.0
    assert bucket.tokens == pytest.approx(50.0)


def test_client_uses_separate_buckets_per_endpoint_class():
    clock = FakeClock()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"status": "success", "orderid": "1", "data": {}})

    client = OpenAlgoClient(
        "http://test.local",
        TEST_KEY,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    for _ in range(10):
        client.placeorder("X", "NFO", "BUY", 65)
    assert clock.t == 0.0
    client.placeorder("X", "NFO", "BUY", 65)
    assert clock.t == pytest.approx(0.1)  # 11th order waits one tenth of a second
    # The smart order budget is separate: ten more go through without waiting.
    before = clock.t
    for _ in range(10):
        client.placesmartorder("X", "NFO", "BUY", 65, 0)
    assert clock.t == before
    # General endpoints have their own 50 per second budget.
    for _ in range(50):
        client.funds()
    assert clock.t == before
    client.funds()
    assert clock.t == pytest.approx(before + 0.02)
    assert client._bucket_for("history") is client._buckets["history"]


# ------------------------------------------------------------------- history


def test_parse_history_iso_with_offset_and_alphabetical_columns():
    frame = minute_bars(date(2026, 9, 11), 23000.0)
    rows = rows_from_frame(frame)
    assert list(rows[0].keys()) == ["close", "high", "low", "oi", "open", "timestamp", "volume"]
    parsed = parse_history(rows, "1m")
    assert list(parsed.columns) == ["timestamp", "open", "high", "low", "close", "volume", "oi"]
    assert str(parsed["timestamp"].dtype) == "datetime64[us, Asia/Kolkata]"
    assert parsed["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    assert parsed["timestamp"].iloc[-1] == datetime(2026, 9, 11, 15, 29, tzinfo=IST)
    assert parsed["open"].iloc[0] == 23000.0
    assert len(parsed) == 375


def test_parse_history_epoch_seconds_intraday_is_utc():
    epoch = int(datetime(2026, 9, 11, 9, 15, tzinfo=IST).timestamp())
    rows = [{"timestamp": epoch, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3}]
    parsed = parse_history(rows, "1m")
    assert parsed["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    assert parsed["oi"].iloc[0] == 0.0  # missing oi filled


def test_parse_history_epoch_daily_is_ist_wall_clock():
    # OpenAlgo adds 5:30 before converting daily bars, so the epoch is the IST date at 00:00 read as UTC.
    epoch = int(datetime(2026, 9, 11, tzinfo=UTC).timestamp())
    parsed = parse_history([{"timestamp": epoch, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}], "D")
    assert parsed["timestamp"].iloc[0] == datetime(2026, 9, 11, 0, 0, tzinfo=IST)


def test_parse_history_epoch_milliseconds_and_naive_iso():
    ms = int(datetime(2026, 9, 11, 9, 15, tzinfo=IST).timestamp() * 1000)
    parsed = parse_history([{"timestamp": ms, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}], "1m")
    assert parsed["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)
    naive = parse_history([{"timestamp": "2026-09-11 09:15:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}])
    assert naive["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)


def test_parse_history_sorts_and_deduplicates_keeping_last():
    rows = [
        {"timestamp": "2026-09-11 09:16:00+05:30", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 0},
        {"timestamp": "2026-09-11 09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
        {"timestamp": "2026-09-11 09:16:00+05:30", "open": 3, "high": 3, "low": 3, "close": 3, "volume": 0},
    ]
    parsed = parse_history(rows)
    assert len(parsed) == 2
    assert parsed["open"].tolist() == [1.0, 3.0]


def test_history_method_sends_dates_and_parses(monkeypatch):
    frame = minute_bars(date(2026, 9, 11), 50.0)

    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        assert body["start_date"] == "2026-09-10" and body["end_date"] == "2026-09-11"
        assert body["interval"] == "1m"
        return ok(rows_from_frame(frame, epoch=True))

    client = make_client(handler)
    out = client.history("NIFTY", "NSE_INDEX", "1m", date(2026, 9, 10), "2026-09-11")
    assert len(out) == 375
    assert out["timestamp"].iloc[0] == datetime(2026, 9, 11, 9, 15, tzinfo=IST)


# ------------------------------------------------------------------- symbols


def test_parse_expiry_formats_and_option_symbol():
    assert parse_expiry("15-SEP-26") == date(2026, 9, 15)
    assert parse_expiry("25-Aug-2026") == date(2026, 8, 25)
    assert parse_expiry("2026-09-15") == date(2026, 9, 15)
    assert option_symbol("NIFTY", date(2026, 9, 15), 23400, "CE") == "NIFTY15SEP2623400CE"
    assert option_symbol("nifty", date(2026, 10, 6), 23400.0, "pe") == "NIFTY06OCT2623400PE"


def test_expiry_returns_sorted_dates():
    client = make_client(lambda request: ok(["22-SEP-26", "15-SEP-26", "29-SEP-26"], message="Found 3"))
    assert client.expiry("NIFTY", "NFO", "options") == [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]


def test_symbol_returns_contract_for_options_and_dict_otherwise():
    row = {
        "brexchange": "NFO",
        "brsymbol": "NIFTY2691523400CE",
        "exchange": "NFO",
        "expiry": "15-SEP-26",
        "freeze_qty": 1800,
        "instrumenttype": "CE",
        "lotsize": 65,
        "name": "NIFTY",
        "strike": 23400.0,
        "symbol": "NIFTY15SEP2623400CE",
        "tick_size": 0.05,
        "token": "x",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if body_of(request)["symbol"] == "NIFTY15SEP2623400CE":
            return ok(row)
        return ok({"symbol": "NIFTY", "instrumenttype": "", "expiry": "", "strike": -0.01})

    client = make_client(handler)
    contract = client.symbol("NIFTY15SEP2623400CE", "NFO")
    assert isinstance(contract, Contract)
    assert contract.expiry == date(2026, 9, 15)
    assert contract.strike == 23400.0 and contract.lot_size == 65 and contract.freeze_qty == 1800
    assert isinstance(client.symbol("NIFTY", "NSE_INDEX"), dict)


# -------------------------------------------------------------------- quotes


def test_quotes_and_multiquotes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/quotes"):
            return ok({"ltp": 23398.1, "bid": 0, "ask": 0, "open": 23270.3})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "results": [
                    {"symbol": "A", "exchange": "NFO", "data": {"ltp": 133.6, "bid": 133.65, "ask": 135.0, "oi": 5}},
                    {"symbol": "B", "exchange": "NFO", "error": "Symbol not found"},
                ],
            },
        )

    client = make_client(handler)
    quote = client.quotes("NIFTY", "NSE_INDEX")
    assert isinstance(quote, Quote) and quote.ltp == 23398.1 and quote.timestamp > 0
    quotes = client.multiquotes([("NFO", "A"), {"symbol": "B", "exchange": "NFO"}])
    assert [q.symbol for q in quotes] == ["A"]
    assert quotes[0].bid == 133.65


# -------------------------------------------------------------------- orders


def test_openposition_sends_strategy_and_returns_int():
    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        assert body["strategy"] == "openfly" and body["product"] == "NRML"
        return httpx.Response(200, json={"status": "success", "quantity": "-65"})

    assert make_client(handler).openposition("NIFTY15SEP2623400CE", "NFO", "NRML") == -65


def test_placeorder_and_basket_bodies_carry_strategy_tag_and_string_quantities():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        seen.append(body)
        if request.url.path.endswith("/basketorder"):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "mode": "analyze",
                    "results": [
                        {"symbol": "C", "status": "success", "orderid": "1"},
                        {"symbol": "P", "status": "error", "message": "margin"},
                    ],
                },
            )
        return httpx.Response(200, json={"status": "success", "orderid": "9"})

    client = make_client(handler)
    client.placeorder("C", "NFO", "sell", 65, pricetype="limit", price=133.5)
    assert seen[-1]["strategy"] == "openfly"
    assert seen[-1]["quantity"] == "65" and seen[-1]["action"] == "SELL" and seen[-1]["pricetype"] == "LIMIT"
    legs = [
        OpenAlgoClient.make_order("C", "NFO", "SELL", 65, price=133.5, pricetype="LIMIT"),
        {"symbol": "P", "exchange": "NFO", "action": "SELL", "quantity": 65, "product": "NRML"},
    ]
    results = client.basketorder(legs)
    assert seen[-1]["orders"][0]["quantity"] == "65" and seen[-1]["orders"][1]["quantity"] == "65"
    assert results[1]["status"] == "error" and results[0]["mode"] == "analyze"


def test_margin_and_orderbook_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/margin"):
            body = body_of(request)
            assert body["positions"][0]["quantity"] == "65"
            return ok({"total_margin_required": 188700.31, "span_margin": 141125.25, "exposure_margin": 60835.06})
        return ok({"orders": [{"orderid": "1", "order_status": "complete"}], "statistics": {"total_buy_orders": 1}})

    client = make_client(handler)
    margin = client.margin([{"symbol": "X", "exchange": "NFO", "action": "SELL", "quantity": 65}])
    assert margin["total_margin_required"] == 188700.31
    book = client.orderbook()
    assert book["orders"][0]["orderid"] == "1" and book["statistics"]["total_buy_orders"] == 1
