"""Websocket tick feed for OpenAlgo (ws://host:8765).

``LtpFeed`` runs a ``websockets`` client on a background thread with its own
asyncio loop. It authenticates with ``{"action": "authenticate", "api_key"}``,
subscribes LTP, Quote or Depth for a list of (exchange, symbol) pairs, keeps
the last :class:`Quote` per symbol, calls listeners on every tick, reconnects
with backoff and re-authenticates and re-subscribes after any disconnect.

``FakeFeed`` has the same surface and is driven programmatically, for tests
and offline replay.

Quote ``timestamp`` is the local receipt time (epoch seconds) as required by
``openfly.interfaces.Quote``; the exchange timestamp of the last tick is kept
separately and available through :meth:`exchange_timestamp`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from openfly.config import SettingsStore
from openfly.interfaces import Quote

logger = logging.getLogger("openfly.market.feed")

TickListener = Callable[[Quote], None]
OrderListener = Callable[[dict[str, Any]], None]

MODE_NAMES = {1: "LTP", 2: "Quote", 3: "Depth"}
MODE_CODES = {"LTP": 1, "QUOTE": 2, "DEPTH": 3}


class FeedError(Exception):
    """Authentication failed or the feed could not start."""


def _normalise_mode(mode: str | int) -> str:
    if isinstance(mode, int):
        return MODE_NAMES[mode]
    upper = str(mode).upper()
    if upper not in MODE_CODES:
        raise ValueError(f"unknown feed mode {mode!r}; use LTP, Quote or Depth")
    return {"LTP": "LTP", "QUOTE": "Quote", "DEPTH": "Depth"}[upper]


class _FeedBase:
    """Shared last-tick store and listener plumbing."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._quotes: dict[str, Quote] = {}
        self._exchange_ts: dict[str, float] = {}
        self._lock = threading.RLock()
        self._listeners: list[TickListener] = []
        self._order_listeners: list[OrderListener] = []
        self._subscriptions: dict[tuple[str, str], str] = {}
        self._orders_wanted = False
        self.ticks = 0

    # listeners -----------------------------------------------------------

    def add_listener(self, callback: TickListener) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: TickListener) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def add_order_listener(self, callback: OrderListener) -> None:
        with self._lock:
            if callback not in self._order_listeners:
                self._order_listeners.append(callback)

    def remove_order_listener(self, callback: OrderListener) -> None:
        with self._lock:
            if callback in self._order_listeners:
                self._order_listeners.remove(callback)

    # state ---------------------------------------------------------------

    def last(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(symbol)

    def age_seconds(self, symbol: str) -> float | None:
        """Seconds since the last tick for ``symbol`` (local clock), None if never seen."""
        quote = self.last(symbol)
        if quote is None:
            return None
        return max(0.0, self._clock() - quote.timestamp)

    def exchange_timestamp(self, symbol: str) -> float | None:
        """Exchange timestamp (epoch seconds) of the last tick, when the feed carried one."""
        with self._lock:
            return self._exchange_ts.get(symbol)

    def snapshot(self) -> dict[str, Quote]:
        with self._lock:
            return dict(self._quotes)

    @property
    def subscriptions(self) -> dict[tuple[str, str], str]:
        with self._lock:
            return dict(self._subscriptions)

    # dispatch ------------------------------------------------------------

    def _store_tick(self, quote: Quote, exchange_ts: float | None = None) -> None:
        with self._lock:
            self._quotes[quote.symbol] = quote
            if exchange_ts is not None:
                self._exchange_ts[quote.symbol] = exchange_ts
            self.ticks += 1
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(quote)
            except Exception:  # noqa: BLE001 - a listener must not kill the feed
                logger.exception("tick listener failed for %s", quote.symbol)

    def _dispatch_order(self, event: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._order_listeners)
        for callback in listeners:
            try:
                callback(event)
            except Exception:  # noqa: BLE001
                logger.exception("order listener failed")

    @staticmethod
    def _quote_from_message(message: dict[str, Any], clock: Callable[[], float]) -> tuple[Quote, float | None]:
        data = message.get("data") or {}
        bid = data.get("bid")
        ask = data.get("ask")
        depth = data.get("depth") or {}
        if bid is None and depth.get("buy"):
            bid = depth["buy"][0].get("price")
        if ask is None and depth.get("sell"):
            ask = depth["sell"][0].get("price")
        raw_ts = data.get("timestamp")
        exchange_ts: float | None = None
        if raw_ts not in (None, ""):
            try:
                value = float(raw_ts)
                exchange_ts = value / 1000.0 if value > 1e11 else value
            except (TypeError, ValueError):
                exchange_ts = None
        quote = Quote(
            symbol=str(message.get("symbol")),
            exchange=str(message.get("exchange")),
            ltp=float(data.get("ltp") or 0.0),
            bid=float(bid or 0.0),
            ask=float(ask or 0.0),
            timestamp=clock(),
        )
        return quote, exchange_ts


class LtpFeed(_FeedBase):
    """Background websocket client for OpenAlgo market data and order updates."""

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        *,
        on_tick: TickListener | None = None,
        on_order: OrderListener | None = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        auth_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__(clock=clock)
        if not api_key:
            raise ValueError("OpenAlgo API key is empty")
        self.ws_url = ws_url
        self._api_key = api_key
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.auth_timeout = auth_timeout
        if on_tick is not None:
            self.add_listener(on_tick)
        if on_order is not None:
            self.add_order_listener(on_order)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop_event: asyncio.Event | None = None
        self._connected = threading.Event()
        self._ready = threading.Event()
        self._auth_failed = False
        self.last_error: str | None = None
        self.connections = 0

    @classmethod
    def from_settings(cls, store: SettingsStore | None = None, **kwargs: Any) -> LtpFeed:
        settings = (store or SettingsStore()).get()
        openalgo = settings.get("openalgo", {})
        return cls(openalgo.get("ws_url", "ws://127.0.0.1:8765"), openalgo.get("api_key", ""), **kwargs)

    # lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, wait: bool = True, timeout: float = 15.0) -> None:
        """Start the background thread. With ``wait`` block until authenticated or failed."""
        if self.running:
            return
        self._ready.clear()
        self._auth_failed = False
        self._thread = threading.Thread(target=self._thread_main, name="openfly-feed", daemon=True)
        self._thread.start()
        if wait:
            if not self._ready.wait(timeout):
                raise FeedError(f"feed did not connect within {timeout} s: {self.last_error}")
            if self._auth_failed:
                raise FeedError(f"feed authentication failed: {self.last_error}")
            if not self.connected:
                # The first attempt failed; the thread keeps retrying in the background.
                raise FeedError(f"feed not connected: {self.last_error}")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop cleanly: close the socket, end the loop, join the thread."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._request_stop)
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None
        self._connected.clear()

    def _request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        ws = self._ws
        if ws is not None and self._loop is not None:
            self._loop.create_task(self._close_ws(ws))

    @staticmethod
    async def _close_ws(ws: Any) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None
                self._ready.set()

    # subscriptions -------------------------------------------------------

    def subscribe(self, symbols: Iterable[tuple[str, str]], mode: str | int = "LTP") -> None:
        """Subscribe (exchange, symbol) pairs. Remembered and re-sent after reconnects."""
        mode_name = _normalise_mode(mode)
        pairs = [(str(exchange), str(symbol)) for exchange, symbol in symbols]
        with self._lock:
            for pair in pairs:
                self._subscriptions[pair] = mode_name
        self._send_threadsafe(self._subscribe_payload(pairs, mode_name))

    def unsubscribe(self, symbols: Iterable[tuple[str, str]], mode: str | int | None = None) -> None:
        pairs = [(str(exchange), str(symbol)) for exchange, symbol in symbols]
        groups: dict[str, list[tuple[str, str]]] = {}
        with self._lock:
            for pair in pairs:
                current = self._subscriptions.pop(pair, None)
                name = _normalise_mode(mode) if mode is not None else current
                if name:
                    groups.setdefault(name, []).append(pair)
        for name, group in groups.items():
            self._send_threadsafe(
                {
                    "action": "unsubscribe",
                    "mode": name,
                    "symbols": [{"exchange": e, "symbol": s} for e, s in group],
                }
            )

    def subscribe_orders(self) -> None:
        """Receive ``order_update`` events for the whole account through order listeners."""
        with self._lock:
            self._orders_wanted = True
        self._send_threadsafe({"action": "subscribe_orders"})

    def unsubscribe_orders(self) -> None:
        with self._lock:
            self._orders_wanted = False
        self._send_threadsafe({"action": "unsubscribe_orders"})

    @staticmethod
    def _subscribe_payload(pairs: list[tuple[str, str]], mode_name: str) -> dict[str, Any]:
        return {
            "action": "subscribe",
            "mode": mode_name,
            "symbols": [{"exchange": e, "symbol": s} for e, s in pairs],
        }

    def _send_threadsafe(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or not loop.is_running() or not self.connected:
            return  # sent on (re)connect from the remembered state
        asyncio.run_coroutine_threadsafe(self._send(payload), loop)

    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("feed send failed: %s", type(exc).__name__)

    # loop ----------------------------------------------------------------

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        self._stop_event = asyncio.Event()
        delay = self.reconnect_delay
        while not self._stop_event.is_set():
            try:
                async with connect(
                    self.ws_url, open_timeout=10, ping_interval=20, ping_timeout=20, max_queue=1024
                ) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    self.connections += 1
                    self._connected.set()
                    self._ready.set()
                    delay = self.reconnect_delay
                    await self._resubscribe(ws)
                    async for raw in ws:
                        self._handle(raw)
                        if self._stop_event.is_set():
                            break
            except FeedError as exc:
                self.last_error = str(exc)
                self._auth_failed = True
                logger.error("feed authentication failed; not retrying")
                self._ready.set()
                break
            except Exception as exc:  # noqa: BLE001 - reconnect on anything else
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("feed disconnected: %s", self.last_error)
            finally:
                self._ws = None
                self._connected.clear()
            if self._stop_event.is_set():
                break
            self._ready.set()  # let start() return even while retrying
            wait = delay * (0.5 + 0.5 * random.random())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
            except TimeoutError:
                pass
            delay = min(self.max_reconnect_delay, delay * 2)

    async def _authenticate(self, ws: Any) -> None:
        await ws.send(json.dumps({"action": "authenticate", "api_key": self._api_key}))
        deadline = asyncio.get_running_loop().time() + self.auth_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ConnectionError("no authentication acknowledgement")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            kind = str(message.get("type", "")).lower()
            text = str(message.get("message", "")).lower()
            if kind in {"auth", "authenticate", "authentication"} or "authenticat" in text:
                if str(message.get("status", "")).lower() == "success":
                    return
                raise FeedError(message.get("message") or "authentication rejected")
            if kind == "error" and "auth" in text:
                raise FeedError(message.get("message") or "authentication rejected")
            self._handle(raw)

    async def _resubscribe(self, ws: Any) -> None:
        with self._lock:
            groups: dict[str, list[tuple[str, str]]] = {}
            for pair, mode_name in self._subscriptions.items():
                groups.setdefault(mode_name, []).append(pair)
            orders = self._orders_wanted
        for mode_name, pairs in groups.items():
            await ws.send(json.dumps(self._subscribe_payload(pairs, mode_name)))
        if orders:
            await ws.send(json.dumps({"action": "subscribe_orders"}))

    def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "market_data":
            quote, exchange_ts = self._quote_from_message(message, self._clock)
            self._store_tick(quote, exchange_ts)
        elif kind == "order_update":
            self._dispatch_order(message)
        elif kind in {"subscribe", "unsubscribe", "subscribe_orders", "unsubscribe_orders"}:
            status = message.get("status")
            if status not in (None, "success"):
                logger.warning("feed %s: %s %s", kind, status, message.get("message"))
        elif kind == "error":
            self.last_error = str(message.get("message"))
            logger.warning("feed error: %s", self.last_error)


class FakeFeed(_FeedBase):
    """In-memory feed with the same surface as :class:`LtpFeed`, driven by :meth:`push`."""

    def __init__(self, clock: Callable[[], float] = time.time):
        super().__init__(clock=clock)
        self._running = False
        self.pushed_orders: list[dict[str, Any]] = []

    @property
    def connected(self) -> bool:
        return self._running

    @property
    def running(self) -> bool:
        return self._running

    def start(self, wait: bool = True, timeout: float = 0.0) -> None:
        self._running = True

    def stop(self, timeout: float = 0.0) -> None:
        self._running = False

    def subscribe(self, symbols: Iterable[tuple[str, str]], mode: str | int = "LTP") -> None:
        mode_name = _normalise_mode(mode)
        with self._lock:
            for exchange, symbol in symbols:
                self._subscriptions[(str(exchange), str(symbol))] = mode_name

    def unsubscribe(self, symbols: Iterable[tuple[str, str]], mode: str | int | None = None) -> None:
        with self._lock:
            for exchange, symbol in symbols:
                self._subscriptions.pop((str(exchange), str(symbol)), None)

    def subscribe_orders(self) -> None:
        with self._lock:
            self._orders_wanted = True

    def unsubscribe_orders(self) -> None:
        with self._lock:
            self._orders_wanted = False

    def push(
        self,
        symbol: str,
        ltp: float,
        exchange: str = "NFO",
        bid: float | None = None,
        ask: float | None = None,
        timestamp: float | None = None,
        exchange_timestamp: float | None = None,
    ) -> Quote:
        """Inject a tick. ``timestamp`` defaults to the feed clock (local receipt time)."""
        quote = Quote(
            symbol=symbol,
            exchange=exchange,
            ltp=float(ltp),
            bid=float(ltp if bid is None else bid),
            ask=float(ltp if ask is None else ask),
            timestamp=self._clock() if timestamp is None else float(timestamp),
        )
        self._store_tick(quote, exchange_timestamp)
        return quote

    def push_message(self, message: dict[str, Any]) -> None:
        """Inject a raw websocket-shaped message (``market_data`` or ``order_update``)."""
        if message.get("type") == "market_data":
            quote, exchange_ts = self._quote_from_message(message, self._clock)
            self._store_tick(quote, exchange_ts)
        elif message.get("type") == "order_update":
            self.push_order(message)

    def push_order(self, event: dict[str, Any]) -> None:
        self.pushed_orders.append(event)
        self._dispatch_order(event)
