from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from openfly.interfaces import Quote
from openfly.market.feed import FakeFeed, FeedError, LtpFeed

# ------------------------------------------------------------------ FakeFeed


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_fake_feed_push_last_age_and_listeners():
    clock = FakeClock()
    feed = FakeFeed(clock=clock)
    seen: list[Quote] = []
    feed.add_listener(seen.append)
    feed.start()
    feed.subscribe([("NFO", "NIFTY15SEP2623400CE"), ("NSE_INDEX", "NIFTY")], mode="LTP")
    assert feed.subscriptions == {("NFO", "NIFTY15SEP2623400CE"): "LTP", ("NSE_INDEX", "NIFTY"): "LTP"}
    assert feed.last("NIFTY") is None and feed.age_seconds("NIFTY") is None
    feed.push("NIFTY15SEP2623400CE", 133.6, bid=133.5, ask=133.7, exchange_timestamp=1756376445.123)
    quote = feed.last("NIFTY15SEP2623400CE")
    assert quote.ltp == 133.6 and quote.bid == 133.5 and quote.timestamp == 1000.0
    assert feed.exchange_timestamp("NIFTY15SEP2623400CE") == 1756376445.123
    clock.t += 4.5
    assert feed.age_seconds("NIFTY15SEP2623400CE") == pytest.approx(4.5)
    assert seen == [quote] and feed.ticks == 1
    feed.remove_listener(seen.append)
    feed.push("NIFTY", 23398.1, exchange="NSE_INDEX")
    assert len(seen) == 1 and feed.snapshot()["NIFTY"].exchange == "NSE_INDEX"
    feed.stop()
    assert not feed.connected


def test_fake_feed_raw_messages_and_orders():
    feed = FakeFeed(clock=FakeClock())
    orders: list[dict] = []
    feed.add_order_listener(orders.append)
    feed.subscribe_orders()
    feed.push_message(
        {"type": "market_data", "symbol": "NIFTY", "exchange": "NSE_INDEX", "mode": 1, "data": {"ltp": 23400.5, "timestamp": 1756376445123}}
    )
    assert feed.last("NIFTY").ltp == 23400.5
    assert feed.exchange_timestamp("NIFTY") == pytest.approx(1756376445.123)
    feed.push_message({"type": "order_update", "orderid": "1", "order_status": "complete"})
    assert orders[0]["orderid"] == "1" and feed.pushed_orders == orders


def test_listener_exceptions_do_not_break_the_feed():
    feed = FakeFeed()

    def bad(quote: Quote) -> None:
        raise RuntimeError("boom")

    feed.add_listener(bad)
    feed.push("X", 1.0)
    assert feed.last("X").ltp == 1.0


# ------------------------------------------------------------------- LtpFeed


class WsServer:
    """Minimal OpenAlgo-like websocket server for tests, on a background loop."""

    def __init__(self):
        self.port = 0
        self.received: list[dict] = []
        self.auth_count = 0
        self.total_connections = 0
        self.connections: list = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(5)

    def stop(self) -> None:
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        from websockets.asyncio.server import serve

        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    async def _handler(self, ws) -> None:
        self.connections.append(ws)
        self.total_connections += 1
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                action = message.get("action")
                if action == "authenticate":
                    self.auth_count += 1
                    if message.get("api_key") != "good-key":
                        await ws.send(json.dumps({"type": "auth", "status": "error", "message": "Invalid API key"}))
                        await ws.close()
                        return
                    await ws.send(json.dumps({"type": "auth", "status": "success", "message": "Authentication successful"}))
                elif action == "subscribe":
                    await ws.send(json.dumps({"type": "subscribe", "status": "success", "message": "ok"}))
                    for item in message["symbols"]:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "market_data",
                                    "symbol": item["symbol"],
                                    "exchange": item["exchange"],
                                    "mode": 1,
                                    "data": {"ltp": 100.0 + self.total_connections, "timestamp": 1756376445123},
                                }
                            )
                        )
                elif action == "subscribe_orders":
                    await ws.send(json.dumps({"type": "subscribe_orders", "status": "success"}))
                    await ws.send(json.dumps({"type": "order_update", "orderid": "42", "order_status": "complete"}))
        finally:
            self.connections.remove(ws)

    def push(self, message: dict) -> None:
        async def _send():
            for ws in list(self.connections):
                await ws.send(json.dumps(message))

        asyncio.run_coroutine_threadsafe(_send(), self.loop).result(5)

    def drop_clients(self) -> None:
        async def _close():
            for ws in list(self.connections):
                await ws.close()

        asyncio.run_coroutine_threadsafe(_close(), self.loop).result(5)


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def server():
    srv = WsServer()
    srv.start()
    yield srv
    srv.stop()


def test_ltp_feed_authenticates_subscribes_and_reconnects(server):
    ticks: list[Quote] = []
    orders: list[dict] = []
    feed = LtpFeed(
        f"ws://127.0.0.1:{server.port}",
        "good-key",
        on_tick=ticks.append,
        on_order=orders.append,
        reconnect_delay=0.05,
        max_reconnect_delay=0.1,
    )
    feed.subscribe([("NFO", "NIFTY15SEP2623400CE")], mode="LTP")
    feed.start(wait=True, timeout=5)
    assert feed.connected and server.auth_count == 1
    assert wait_for(lambda: feed.last("NIFTY15SEP2623400CE") is not None)
    first = feed.last("NIFTY15SEP2623400CE")
    assert first.ltp == 101.0 and first.exchange == "NFO"
    assert feed.exchange_timestamp("NIFTY15SEP2623400CE") == pytest.approx(1756376445.123)
    assert feed.age_seconds("NIFTY15SEP2623400CE") < 5
    feed.subscribe([("NSE_INDEX", "NIFTY")], mode="Quote")
    assert wait_for(lambda: feed.last("NIFTY") is not None)
    feed.subscribe_orders()
    assert wait_for(lambda: len(orders) == 1)
    assert orders[0]["orderid"] == "42"
    sent = [m for m in server.received if m.get("action") == "subscribe"]
    assert sent[0]["mode"] == "LTP" and sent[1]["mode"] == "Quote"

    # Drop the connection: the feed must re-authenticate and re-subscribe everything.
    server.drop_clients()
    assert wait_for(lambda: feed.connections == 2 and feed.connected, timeout=10)
    assert server.auth_count == 2
    assert wait_for(lambda: feed.last("NIFTY15SEP2623400CE").ltp == 102.0)
    resent = [m for m in server.received if m.get("action") == "subscribe"][2:]
    modes = {(s["symbol"], m["mode"]) for m in resent for s in m["symbols"]}
    assert modes == {("NIFTY15SEP2623400CE", "LTP"), ("NIFTY", "Quote")}
    assert wait_for(lambda: len(orders) == 2)

    server.push({"type": "market_data", "symbol": "NIFTY", "exchange": "NSE_INDEX", "mode": 2, "data": {"ltp": 23400.0}})
    assert wait_for(lambda: feed.last("NIFTY").ltp == 23400.0)
    assert len(ticks) >= 4
    feed.stop()
    assert not feed.running and not feed.connected


def test_ltp_feed_auth_failure_raises_and_does_not_retry(server):
    feed = LtpFeed(f"ws://127.0.0.1:{server.port}", "bad-key", reconnect_delay=0.05)
    with pytest.raises(FeedError):
        feed.start(wait=True, timeout=5)
    assert wait_for(lambda: not feed.running)
    assert server.auth_count == 1
    feed.stop()


def test_ltp_feed_reports_unreachable_server():
    feed = LtpFeed("ws://127.0.0.1:9", "good-key", reconnect_delay=0.05)
    with pytest.raises(FeedError):
        feed.start(wait=True, timeout=5)
    feed.stop()
    assert not feed.running


def test_from_settings_requires_key(settings):
    settings["openalgo"]["api_key"] = ""
    with pytest.raises(ValueError):
        LtpFeed.from_settings(settings_store_stub(settings))


def settings_store_stub(settings):
    class Stub:
        def get(self):
            return settings

    return Stub()
