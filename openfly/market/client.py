"""Synchronous OpenAlgo REST client built on httpx.

Design:

- Every call is a POST to ``{host}/api/v1/{endpoint}`` with ``{"apikey": ...}``
  plus the endpoint fields. The API key is never logged or included in
  exception messages.
- The envelope ``{"status": "success" | "error", ...}`` is unwrapped. A status
  of ``"error"`` (or a 4xx) raises :class:`OpenAlgoError` carrying the message,
  the HTTP code and the endpoint.
- Read endpoints retry on connection errors, timeouts, HTTP 429 and transient
  5xx responses with exponential backoff and jitter (at most 5 tries, delays
  capped at 8 seconds). A 5xx whose body is an OpenAlgo error envelope with a
  clearly permanent message (permission denied, invalid symbol, missing field)
  is raised at once.
- Write endpoints (orders, analyzer toggle) retry only when the request cannot
  have reached the server (connect errors, HTTP 429). Any other transport
  failure raises :class:`openfly.interfaces.UnresolvedOrder` so the caller
  reconciles from the orderbook instead of resending. A 5xx on a write raises
  :class:`OpenAlgoError` without retry for the same reason.
- Token buckets keep us under OpenAlgo's per-second limits: 10 per second for
  order endpoints, 10 per second for the smart order endpoint (a separate
  budget) and 50 per second for everything else.

Methods that place, modify or cancel orders, or switch the analyzer, are marked
``WRITE`` in their docstrings. Nothing in the test-suite calls them against a
live server.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from openfly.config import SettingsStore
from openfly.interfaces import Contract, Quote, UnresolvedOrder

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger("openfly.market.client")

HISTORY_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]

# Endpoints that consume OpenAlgo's order budget (10 per second).
ORDER_ENDPOINTS = frozenset(
    {
        "placeorder",
        "modifyorder",
        "cancelorder",
        "cancelallorder",
        "basketorder",
        "splitorder",
        "closeposition",
        "optionsorder",
        "optionsmultiorder",
        "placegttorder",
        "modifygttorder",
        "cancelgttorder",
    }
)
# Separate 10 per second budget.
SMART_ORDER_ENDPOINTS = frozenset({"placesmartorder"})
# Endpoints that change broker or server state. Never retried after the request
# may have been delivered.
WRITE_ENDPOINTS = ORDER_ENDPOINTS | SMART_ORDER_ENDPOINTS | frozenset({"analyzer/toggle"})

# A 5xx carrying one of these phrases is a permanent condition, not a hiccup.
_PERMANENT_MARKERS = (
    "permission",
    "invalid",
    "missing",
    "not found",
    "no strikes",
    "no data",
    "unsupported",
    "not supported",
)

_EXPIRY_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d%b%y")


class OpenAlgoError(Exception):
    """An OpenAlgo call failed. ``code`` is the HTTP status when known."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        endpoint: str | None = None,
        payload: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.endpoint = endpoint
        self.payload = payload

    def __str__(self) -> str:  # pragma: no cover - formatting only
        where = f" [{self.endpoint}]" if self.endpoint else ""
        code = f" (HTTP {self.code})" if self.code is not None else ""
        return f"{self.message}{where}{code}"

    @property
    def permanent(self) -> bool:
        text = self.message.lower()
        return any(marker in text for marker in _PERMANENT_MARKERS)


class TokenBucket:
    """Thread-safe token bucket. ``acquire`` blocks until a token is available."""

    def __init__(
        self,
        rate_per_second: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate = float(rate_per_second)
        self.capacity = float(capacity if capacity is not None else rate_per_second)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens`` from the bucket, sleeping if needed. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                wait = (tokens - self._tokens) / self.rate
            self._sleep(wait)
            waited += wait

    @property
    def tokens(self) -> float:
        with self._lock:
            now = self._clock()
            return min(self.capacity, self._tokens + (now - self._last) * self.rate)


def parse_expiry(text: str) -> date:
    """Parse OpenAlgo expiry strings: ``15-SEP-26``, ``25-Aug-2026``, ``2026-09-15``, ``15SEP26``."""
    raw = str(text).strip()
    candidate = raw.title() if "-" in raw else raw.upper().title()
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised expiry format: {text!r}")


def expiry_code(expiry: date) -> str:
    """``date(2026, 9, 15)`` -> ``15SEP26`` as used inside OpenAlgo symbols."""
    return expiry.strftime("%d%b%y").upper()


def option_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    """Build an OpenAlgo option symbol, e.g. ``NIFTY15SEP2623400CE``."""
    strike_text = str(int(strike)) if float(strike).is_integer() else f"{strike:g}"
    return f"{underlying.upper()}{expiry_code(expiry)}{strike_text}{option_type.upper()}"


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_date_text(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def contract_from_row(row: dict[str, Any]) -> Contract:
    """Build a :class:`Contract` from a ``search`` or ``symbol`` row."""
    return Contract(
        symbol=str(row["symbol"]),
        exchange=str(row.get("exchange", "NFO")),
        underlying=str(row.get("name") or row.get("underlying") or ""),
        expiry=parse_expiry(row["expiry"]),
        strike=float(row.get("strike") or 0.0),
        option_type=str(row.get("instrumenttype") or ""),
        lot_size=int(_as_float(row.get("lotsize"), 1)),
        tick_size=float(_as_float(row.get("tick_size"), 0.05)),
        freeze_qty=int(_as_float(row.get("freeze_qty"), 0)),
    )


def _empty_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series([], dtype="datetime64[us, Asia/Kolkata]"),
            "open": pd.Series([], dtype="float64"),
            "high": pd.Series([], dtype="float64"),
            "low": pd.Series([], dtype="float64"),
            "close": pd.Series([], dtype="float64"),
            "volume": pd.Series([], dtype="float64"),
            "oi": pd.Series([], dtype="float64"),
        }
    )


def _parse_timestamps(values: pd.Series, interval: str) -> pd.Series:
    """Return tz-aware Asia/Kolkata timestamps from ISO strings or epoch numbers.

    Intraday epochs from OpenAlgo are true UTC epoch seconds. Daily epochs are
    the IST wall clock read as UTC (the server adds 5:30 before converting),
    which is what the SDK also assumes, so they are localised rather than
    converted.
    """
    daily = interval.upper() in {"D", "W", "M"}
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().all() and len(numeric) > 0:
        unit = "ms" if numeric.abs().max() > 1e11 else "s"
        if daily:
            stamped = pd.to_datetime(numeric, unit=unit).dt.tz_localize(IST)
        else:
            stamped = pd.to_datetime(numeric, unit=unit, utc=True).dt.tz_convert(IST)
        return stamped.dt.as_unit("us")
    text = values.astype(str)
    try:
        stamped = pd.to_datetime(text, format="ISO8601")
    except (ValueError, TypeError):
        stamped = pd.to_datetime(text, format="ISO8601", utc=True)
    if stamped.dt.tz is None:
        stamped = stamped.dt.tz_localize(IST)
    else:
        stamped = stamped.dt.tz_convert(IST)
    return stamped.dt.as_unit("us")


def parse_history(rows: Iterable[dict[str, Any]], interval: str = "1m") -> pd.DataFrame:
    """Turn OpenAlgo history rows into the canonical OpenFly frame.

    Columns: timestamp (tz-aware Asia/Kolkata, bar start), open, high, low,
    close, volume, oi. Handles ISO strings with or without offset and epoch
    seconds or milliseconds; accepts columns in any order and a missing ``oi``.
    Duplicate timestamps keep the last row; output is sorted.
    """
    rows = list(rows)
    if not rows:
        return _empty_history()
    raw = pd.DataFrame(rows)
    if "timestamp" not in raw.columns:
        raise ValueError("history rows have no timestamp column")
    out = pd.DataFrame({"timestamp": _parse_timestamps(raw["timestamp"], interval)})
    for column in ("open", "high", "low", "close", "volume", "oi"):
        if column in raw.columns:
            out[column] = pd.to_numeric(raw[column], errors="coerce").astype("float64")
        else:
            out[column] = 0.0
    out = out.dropna(subset=["timestamp"])
    out = out.sort_values("timestamp", kind="mergesort").drop_duplicates("timestamp", keep="last")
    return out.reset_index(drop=True)[HISTORY_COLUMNS]


class OpenAlgoClient:
    """Typed, rate-limited, retrying client for the OpenAlgo REST API.

    Read methods return plain dicts, lists or dataclasses from
    ``openfly.interfaces``. Methods marked WRITE change broker state.
    """

    def __init__(
        self,
        host: str,
        api_key: str,
        timeout: float = 10.0,
        strategy: str = "openfly",
        *,
        max_tries: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        history_rate_per_second: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_key:
            raise ValueError("OpenAlgo API key is empty; set it in Settings or OPENALGO_API_KEY")
        self.host = host.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.strategy = strategy
        self.max_tries = max(1, int(max_tries))
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._buckets = {
            "order": TokenBucket(10, clock=clock, sleep=sleep),
            "smart": TokenBucket(10, clock=clock, sleep=sleep),
            "general": TokenBucket(50, clock=clock, sleep=sleep),
            # The broker's history endpoint is the slow, rate-limited one: about two per second.
            "history": TokenBucket(history_rate_per_second, clock=clock, sleep=sleep),
        }
        self.calls = 0
        self.retries = 0

    # ------------------------------------------------------------------ setup

    @classmethod
    def from_settings(cls, store: SettingsStore | None = None, **overrides: Any) -> OpenAlgoClient:
        """Build a client from the settings database (host, api_key, strategy tag)."""
        settings = (store or SettingsStore()).get()
        openalgo = settings.get("openalgo", {})
        strategy = settings.get("strategy", {}).get("strategy_tag", "openfly")
        kwargs: dict[str, Any] = {
            "host": openalgo.get("host", "http://127.0.0.1:5000"),
            "api_key": openalgo.get("api_key", ""),
            "strategy": strategy,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OpenAlgoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"OpenAlgoClient(host={self.host!r}, strategy={self.strategy!r})"

    # -------------------------------------------------------------- transport

    def _bucket_for(self, endpoint: str) -> TokenBucket:
        if endpoint in ORDER_ENDPOINTS:
            return self._buckets["order"]
        if endpoint in SMART_ORDER_ENDPOINTS:
            return self._buckets["smart"]
        if endpoint == "history":
            return self._buckets["history"]
        return self._buckets["general"]

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_cap, self.backoff_base * (2**attempt))
        return base * (0.5 + 0.5 * self._rng())

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"status": "error", "message": response.text[:500] or f"HTTP {response.status_code}"}

    def _post(self, endpoint: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST to an endpoint, unwrap the envelope, retry per the module policy."""
        url = f"{self.host}/api/v1/{endpoint}"
        payload = {"apikey": self._api_key, **(body or {})}
        is_write = endpoint in WRITE_ENDPOINTS
        bucket = self._bucket_for(endpoint)
        last_error: Exception | None = None
        for attempt in range(self.max_tries):
            bucket.acquire()
            self.calls += 1
            try:
                response = self._http.post(url, json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                logger.warning("%s: connection failed (%s), attempt %d", endpoint, type(exc).__name__, attempt + 1)
            except httpx.TransportError as exc:
                if is_write:
                    raise UnresolvedOrder(
                        f"{endpoint}: transport failure after the request may have been sent: "
                        f"{type(exc).__name__}; reconcile from the orderbook, do not resend"
                    ) from exc
                last_error = exc
                logger.warning("%s: transport error (%s), attempt %d", endpoint, type(exc).__name__, attempt + 1)
            else:
                decoded = self._decode(response)
                status = response.status_code
                if status == 429:
                    last_error = OpenAlgoError("rate limited", 429, endpoint, decoded)
                    logger.warning("%s: HTTP 429, attempt %d", endpoint, attempt + 1)
                elif status >= 500:
                    error = OpenAlgoError(self._message(decoded, status), status, endpoint, decoded)
                    if is_write or error.permanent:
                        raise error
                    last_error = error
                    logger.warning("%s: HTTP %d (%s), attempt %d", endpoint, status, error.message, attempt + 1)
                elif status >= 400 or not isinstance(decoded, dict) or decoded.get("status") != "success":
                    raise OpenAlgoError(self._message(decoded, status), status, endpoint, decoded)
                else:
                    return decoded
            if attempt + 1 < self.max_tries:
                self.retries += 1
                self._sleep(self._backoff(attempt))
        message = f"{endpoint}: gave up after {self.max_tries} tries: {last_error}"
        code = last_error.code if isinstance(last_error, OpenAlgoError) else None
        raise OpenAlgoError(message, code, endpoint) from last_error

    @staticmethod
    def _message(decoded: Any, status: int) -> str:
        if isinstance(decoded, dict):
            message = decoded.get("message") or decoded.get("error") or decoded.get("detail")
            if message:
                return str(message)
        return f"HTTP {status}"

    # ------------------------------------------------------------ market data

    def quotes_raw(self, symbol: str, exchange: str) -> dict[str, Any]:
        """Full quote dict: open, high, low, ltp, bid, ask, prev_close, volume, oi when present."""
        return dict(self._post("quotes", {"symbol": symbol, "exchange": exchange}).get("data") or {})

    def quotes(self, symbol: str, exchange: str) -> Quote:
        """Top of book for one symbol; ``timestamp`` is the local receipt time."""
        data = self.quotes_raw(symbol, exchange)
        return self._quote_from(symbol, exchange, data)

    @staticmethod
    def _quote_from(symbol: str, exchange: str, data: dict[str, Any]) -> Quote:
        return Quote(
            symbol=symbol,
            exchange=exchange,
            ltp=_as_float(data.get("ltp")),
            bid=_as_float(data.get("bid")),
            ask=_as_float(data.get("ask")),
            timestamp=time.time(),
        )

    def multiquotes_raw(self, symbols: Iterable[tuple[str, str] | dict[str, str]]) -> list[dict[str, Any]]:
        """Results list as returned by OpenAlgo; entries may carry ``error``."""
        wire = [self._symbol_entry(item) for item in symbols]
        if not wire:
            return []
        return list(self._post("multiquotes", {"symbols": wire}).get("results") or [])

    def multiquotes(self, symbols: Iterable[tuple[str, str] | dict[str, str]]) -> list[Quote]:
        """Quotes for many symbols in one call. Symbols that failed are left out and logged."""
        quotes: list[Quote] = []
        for entry in self.multiquotes_raw(symbols):
            data = entry.get("data")
            if not data or entry.get("error"):
                logger.warning("multiquotes: no data for %s (%s)", entry.get("symbol"), entry.get("error"))
                continue
            quotes.append(self._quote_from(str(entry.get("symbol")), str(entry.get("exchange")), data))
        return quotes

    @staticmethod
    def _symbol_entry(item: tuple[str, str] | dict[str, str]) -> dict[str, str]:
        if isinstance(item, dict):
            return {"symbol": item["symbol"], "exchange": item["exchange"]}
        exchange, symbol = item
        return {"symbol": symbol, "exchange": exchange}

    def depth(self, symbol: str, exchange: str) -> dict[str, Any]:
        """Level 2 snapshot: ltp, bids, asks, totals, oi."""
        return dict(self._post("depth", {"symbol": symbol, "exchange": exchange}).get("data") or {})

    def history(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        start_date: date | str,
        end_date: date | str,
    ) -> pd.DataFrame:
        """OHLCV bars as a DataFrame with tz-aware Asia/Kolkata timestamps (see :func:`parse_history`)."""
        payload = self._post(
            "history",
            {
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "start_date": _as_date_text(start_date),
                "end_date": _as_date_text(end_date),
            },
        )
        return parse_history(payload.get("data") or [], interval)

    def intervals(self) -> dict[str, list[str]]:
        return dict(self._post("intervals").get("data") or {})

    # ---------------------------------------------------------------- symbols

    def symbol_raw(self, symbol: str, exchange: str) -> dict[str, Any]:
        return dict(self._post("symbol", {"symbol": symbol, "exchange": exchange}).get("data") or {})

    def symbol(self, symbol: str, exchange: str) -> Contract | dict[str, Any]:
        """Symbol master row; a :class:`Contract` for CE/PE rows, otherwise the raw dict."""
        row = self.symbol_raw(symbol, exchange)
        if str(row.get("instrumenttype", "")).upper() in {"CE", "PE"} and row.get("expiry"):
            return contract_from_row(row)
        return row

    def search(self, query: str, exchange: str) -> list[dict[str, Any]]:
        return list(self._post("search", {"query": query, "exchange": exchange}).get("data") or [])

    def expiry(self, symbol: str, exchange: str, instrumenttype: str = "options") -> list[date]:
        """Sorted expiry dates for ``symbol`` (``options`` or ``futures``)."""
        payload = self._post(
            "expiry", {"symbol": symbol, "exchange": exchange, "instrumenttype": instrumenttype}
        )
        dates = [parse_expiry(item) for item in payload.get("data") or []]
        return sorted(set(dates))

    def optiongreeks(self, symbol: str, exchange: str = "NFO", **extra: Any) -> dict[str, Any]:
        """IV and greeks. Optional extras: interest_rate, underlying_symbol, underlying_exchange, forward_price, expiry_time."""
        body = {"symbol": symbol, "exchange": exchange}
        body.update({k: v for k, v in extra.items() if v is not None})
        payload = dict(self._post("optiongreeks", body))
        payload.pop("status", None)
        return payload

    # --------------------------------------------------------------- calendar

    def holidays(self, year: int | None = None) -> list[dict[str, Any]]:
        body = {"year": int(year)} if year is not None else {}
        return list(self._post("market/holidays", body).get("data") or [])

    def timings(self, day: date | str) -> list[dict[str, Any]]:
        """Per-exchange session times for a date (epoch milliseconds); empty on holidays."""
        return list(self._post("market/timings", {"date": _as_date_text(day)}).get("data") or [])

    # ---------------------------------------------------------------- account

    def funds(self) -> dict[str, float]:
        """Available cash, collateral, m2m and utilised margin as floats (empty dict when the broker returns none)."""
        data = self._post("funds").get("data") or {}
        return {key: _as_float(value) for key, value in dict(data).items()}

    def margin(self, positions: list[dict[str, Any]]) -> dict[str, float]:
        """Margin for a basket of up to 50 positions; keys total_margin_required, span_margin, exposure_margin, margin_benefit."""
        wire = [self._position_entry(p) for p in positions]
        data = self._post("margin", {"positions": wire}).get("data") or {}
        return {key: _as_float(value) for key, value in dict(data).items()}

    def _position_entry(self, position: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "symbol": position["symbol"],
            "exchange": position.get("exchange", "NFO"),
            "action": position["action"],
            "product": position.get("product", "NRML"),
            "pricetype": position.get("pricetype", "MARKET"),
            "quantity": str(int(position["quantity"])),
        }
        if position.get("price") not in (None, 0, "0", ""):
            entry["price"] = str(position["price"])
        return entry

    def positionbook(self) -> list[dict[str, Any]]:
        """All positions for the day, including closed ones with quantity "0". Values are strings."""
        return list(self._post("positionbook").get("data") or [])

    def openposition(
        self, symbol: str, exchange: str, product: str, strategy: str | None = None
    ) -> int:
        """Net quantity for symbol, exchange and product (0 when flat). ``strategy`` is required by this install."""
        payload = self._post(
            "openposition",
            {
                "symbol": symbol,
                "exchange": exchange,
                "product": product,
                "strategy": strategy or self.strategy,
            },
        )
        return int(_as_float(payload.get("quantity"), 0))

    def orderbook(self) -> dict[str, Any]:
        """``{"orders": [...], "statistics": {...}}`` for the day."""
        data = self._post("orderbook").get("data") or {}
        if isinstance(data, list):
            return {"orders": data, "statistics": {}}
        return {"orders": list(data.get("orders") or []), "statistics": dict(data.get("statistics") or {})}

    def tradebook(self) -> list[dict[str, Any]]:
        return list(self._post("tradebook").get("data") or [])

    def orderstatus(self, orderid: str, strategy: str | None = None) -> dict[str, Any]:
        payload = self._post(
            "orderstatus", {"orderid": str(orderid), "strategy": strategy or self.strategy}
        )
        return dict(payload.get("data") or {})

    def pnl_symbols(self) -> dict[str, Any]:
        """Analyzer-mode per-symbol P&L. Returns HTTP 400 in live mode."""
        payload = dict(self._post("pnl/symbols"))
        payload.pop("status", None)
        return payload

    # --------------------------------------------------------------- analyzer

    def analyzer_status(self) -> dict[str, Any]:
        """``{"analyze_mode": bool, "mode": "analyze" | "live", "total_logs": int}``."""
        return dict(self._post("analyzer").get("data") or {})

    def analyzer_toggle(self, mode: bool) -> dict[str, Any]:
        """WRITE. Switch the installation-wide analyzer (paper) mode. Never called by tests."""
        return dict(self._post("analyzer/toggle", {"mode": bool(mode)}).get("data") or {})

    # ----------------------------------------------------------------- orders

    @staticmethod
    def make_order(
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        product: str = "NRML",
        pricetype: str = "MARKET",
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> dict[str, Any]:
        """One basket leg (also accepted by :meth:`margin`)."""
        leg: dict[str, Any] = {
            "symbol": symbol,
            "exchange": exchange,
            "action": action.upper(),
            "quantity": str(int(quantity)),
            "pricetype": pricetype.upper(),
            "product": product.upper(),
        }
        if price is not None:
            leg["price"] = str(price)
        if trigger_price is not None:
            leg["trigger_price"] = str(trigger_price)
        return leg

    def placeorder(
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        product: str = "NRML",
        pricetype: str = "MARKET",
        price: float = 0,
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """WRITE. Place one order. Returns ``{"orderid": str, "mode": "live"|"analyze"}``."""
        body = {
            "strategy": strategy or self.strategy,
            "symbol": symbol,
            "exchange": exchange,
            "action": action.upper(),
            "quantity": str(int(quantity)),
            "pricetype": pricetype.upper(),
            "product": product.upper(),
            "price": str(price),
            "trigger_price": str(trigger_price),
            "disclosed_quantity": str(int(disclosed_quantity)),
        }
        return self._order_result(self._post("placeorder", body))

    def placesmartorder(
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        position_size: int,
        product: str = "NRML",
        pricetype: str = "MARKET",
        price: float = 0,
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """WRITE. Position-aware order: brings the net position to ``position_size``.

        Gotcha: with ``position_size`` 0 and no open position OpenAlgo places a
        fresh order of ``quantity``. Read :meth:`openposition` first.
        """
        body = {
            "strategy": strategy or self.strategy,
            "symbol": symbol,
            "exchange": exchange,
            "action": action.upper(),
            "quantity": str(int(quantity)),
            "position_size": str(int(position_size)),
            "pricetype": pricetype.upper(),
            "product": product.upper(),
            "price": str(price),
            "trigger_price": str(trigger_price),
            "disclosed_quantity": str(int(disclosed_quantity)),
        }
        return self._order_result(self._post("placesmartorder", body))

    def basketorder(self, orders: list[dict[str, Any]], strategy: str | None = None) -> list[dict[str, Any]]:
        """WRITE. Place several legs in one call; returns the per-leg ``results`` list.

        OpenAlgo reports ``status: success`` when at least one leg succeeded, so
        inspect every entry: ``{"symbol", "status", "orderid" | "message"}``.
        BUY legs are processed before SELL legs.
        """
        wire = [self.make_order(**o) if "quantity" in o and not isinstance(o["quantity"], str) else dict(o) for o in orders]
        payload = self._post("basketorder", {"strategy": strategy or self.strategy, "orders": wire})
        results = list(payload.get("results") or [])
        mode = payload.get("mode")
        if mode is not None:
            for entry in results:
                entry.setdefault("mode", mode)
        return results

    def cancelorder(self, orderid: str, strategy: str | None = None) -> dict[str, Any]:
        """WRITE. Cancel one open order."""
        body = {"orderid": str(orderid), "strategy": strategy or self.strategy}
        return self._order_result(self._post("cancelorder", body))

    def modifyorder(
        self,
        orderid: str,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        price: float,
        pricetype: str = "LIMIT",
        product: str = "NRML",
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        strategy: str | None = None,
    ) -> dict[str, Any]:
        """WRITE. Modify price, quantity or trigger of an open order."""
        body = {
            "orderid": str(orderid),
            "strategy": strategy or self.strategy,
            "symbol": symbol,
            "exchange": exchange,
            "action": action.upper(),
            "quantity": int(quantity),
            "price": price,
            "pricetype": pricetype.upper(),
            "product": product.upper(),
            "trigger_price": trigger_price,
            "disclosed_quantity": int(disclosed_quantity),
        }
        return self._order_result(self._post("modifyorder", body))

    @staticmethod
    def _order_result(payload: dict[str, Any]) -> dict[str, Any]:
        result = {k: v for k, v in payload.items() if k != "status"}
        if "orderid" in result:
            result["orderid"] = str(result["orderid"])
        return result
