"""Market data for the API: stored bars, the session calendar, the live chain and the OpenAlgo probe.

Index bars come only from the DuckDB BarStore (real NIFTY series, never
generated). Chain, orders and positions call OpenAlgo and report the failure
text instead of raising when the broker declines.
"""

from __future__ import annotations

import calendar as _calendar
import logging
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING, Any

from filelock import Timeout

from openfly.api.events import IST
from openfly.market.history import HistoryCache
from openfly.market.store import BarStore

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
PROBE_TTL_SECONDS = 5.0
COVERAGE_TTL_SECONDS = 30.0
# DuckDB admits one writer process; a recorder or backfill job holds the file lock while
# it writes, so API reads wait briefly and then report the store as busy instead of hanging.
STORE_LOCK_TIMEOUT_SECONDS = 8.0


class StoreBusy(RuntimeError):
    """The market store is locked by another process (a recorder or backfill job)."""


def _busy_message(exc: BaseException) -> str:
    return (
        "the market store is locked by another process (a recorder or backfill job is writing); "
        f"retry in a moment ({type(exc).__name__})"
    )


def choose_expiry(expiries: list[date], selection: str, today: date, min_days_to_expiry: int = 0) -> date | None:
    """The expiry the strategy trades.

    ``weekly``: the nearest expiry at least ``min_days_to_expiry`` days away.
    ``monthly`` (default): the last expiry of the current calendar month, or of
    the next month that has one when the current month's has passed.
    """
    horizon = today + timedelta(days=int(min_days_to_expiry or 0))
    future = sorted(e for e in expiries if e >= horizon)
    if not future:
        return None
    if str(selection).lower() == "weekly":
        return future[0]
    year, month = today.year, today.month
    for _ in range(24):
        last_day = date(year, month, _calendar.monthrange(year, month)[1])
        in_month = [e for e in future if e.year == year and e.month == month]
        if in_month:
            return max(in_month)
        if future[0] > last_day:
            month += 1
            if month > 12:
                month, year = 1, year + 1
            continue
        break
    return future[0]


def days_to(expiry: date | None, now: datetime) -> float | None:
    if expiry is None:
        return None
    close = datetime.combine(expiry, dtime(15, 30), IST)
    return round(max(0.0, (close - now).total_seconds() / 86400.0), 4)


class MarketService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self._probe: dict[str, Any] | None = None
        self._probe_at = 0.0
        self._probe_lock = threading.Lock()
        self._coverage: list[dict] | None = None
        self._coverage_at = 0.0
        self._coverage_error: str | None = None

    # ------------------------------------------------------------ stores

    def bar_store(self) -> BarStore:
        return BarStore(self.ctx.paths.market_db, history_root=self.ctx.paths.history, lock_timeout=STORE_LOCK_TIMEOUT_SECONDS)

    def strategy(self) -> dict[str, Any]:
        return self.ctx.settings().get("strategy", {})

    def index_symbol(self) -> tuple[str, str]:
        s = self.strategy()
        return str(s.get("index_exchange", "NSE_INDEX")), str(s.get("underlying", "NIFTY"))

    def available_dates(self, exchange: str | None = None, symbol: str | None = None, interval: str = "1m") -> list[date]:
        ex, sym = self.index_symbol()
        try:
            return self.bar_store().available_dates(exchange or ex, symbol or sym, interval)
        except Timeout as exc:
            raise StoreBusy(_busy_message(exc)) from exc

    def bars(self, symbol: str, exchange: str, interval: str, days: int) -> dict[str, Any]:
        interval = str(interval)
        if interval not in INTERVAL_MINUTES and interval != "D":
            raise ValueError(f"unknown interval {interval!r}; use one of {sorted(INTERVAL_MINUTES)} or D")
        days = max(1, min(int(days), 400))
        base = "1m" if interval in INTERVAL_MINUTES else "D"
        try:
            with self.bar_store() as store:
                dates = store.available_dates(exchange, symbol, base)
                if not dates and interval != base:
                    dates = store.available_dates(exchange, symbol, interval)
                    base = interval
                if not dates:
                    return {"symbol": symbol, "exchange": exchange, "interval": interval, "bars": [], "source": "BarStore", "dates": []}
                window = dates[-days:]
                frame = store.bars(exchange, symbol, base, window[0], window[-1])
        except Timeout as exc:
            raise StoreBusy(_busy_message(exc)) from exc
        if interval != base:
            frame = HistoryCache.resample(frame, interval)
        return {
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "bars": HistoryCache.to_records(frame),
            "source": "BarStore",
            "dates": [d.isoformat() for d in window],
        }

    def chain_coverage(self, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._coverage is None or refresh or now - self._coverage_at > COVERAGE_TTL_SECONDS:
            try:
                rows = self.bar_store().chain_coverage()
                self._coverage_error = None
            except Timeout as exc:
                self._coverage_error = _busy_message(exc)
                logger.info("chain coverage: %s", self._coverage_error)
                rows = self._coverage or []
            except Exception as exc:
                self._coverage_error = str(exc)
                logger.info("chain coverage unavailable: %s", exc)
                rows = self._coverage or []
            out = []
            for row in rows:
                item = dict(row)
                for key in ("trading_date", "expiry"):
                    value = item.get(key)
                    if isinstance(value, date | datetime):
                        item[key] = value.isoformat()[:10]
                    elif value is not None:
                        item[key] = str(value)[:10]
                out.append(item)
            self._coverage = out
            self._coverage_at = now
        return list(self._coverage or [])

    def chains_summary(self) -> dict[str, Any]:
        rows = self.chain_coverage()
        dates = sorted({r["trading_date"] for r in rows if r.get("trading_date")})
        return {
            "days": len(dates),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "error": self._coverage_error,
            "rows": int(sum(int(r.get("rows") or 0) for r in rows)),
            "coverage": rows,
        }

    # ---------------------------------------------------------- calendar

    def calendar(self, client: Any = None):
        from openfly.market.session import SessionCalendar

        return SessionCalendar(client, self.ctx.settings(), paths=self.ctx.paths)

    def expiries(self, cal: Any = None) -> list[date]:
        cal = cal or self.calendar()
        try:
            return sorted(cal.expiries())
        except Exception:
            return []

    def selected_expiry(self, today: date | None = None, expiries: list[date] | None = None) -> dict[str, Any]:
        strategy = self.strategy()
        selection = str(strategy.get("expiry_selection", "monthly"))
        today = today or self.ctx.now().date()
        expiries = self.expiries() if expiries is None else expiries
        chosen = choose_expiry(expiries, selection, today, int(strategy.get("min_days_to_expiry", 0) or 0))
        return {
            "expiry": chosen.isoformat() if chosen else None,
            "expiry_selection": selection,
            "days_to_expiry": days_to(chosen, self.ctx.now()),
        }

    def session(self, day: date | str | None = None) -> dict[str, Any]:
        cal = self.calendar()
        info = cal.session_info(day)
        try:
            target = date.fromisoformat(str(day)[:10]) if day else self.ctx.now().date()
            info.update(self.selected_expiry(target, self.expiries(cal)))
        except Exception as exc:
            logger.info("expiry selection unavailable: %s", exc)
        return info

    # -------------------------------------------------------------- probe

    def probe(self, refresh: bool = False) -> dict[str, Any]:
        """Reachability, analyzer mode and broker id, cached for a few seconds."""
        now = time.monotonic()
        with self._probe_lock:
            if self._probe is not None and not refresh and now - self._probe_at < PROBE_TTL_SECONDS:
                return dict(self._probe)
        result = self._probe_now()
        with self._probe_lock:
            self._probe = result
            self._probe_at = time.monotonic()
        return dict(result)

    def _probe_now(self) -> dict[str, Any]:
        host = str(self.ctx.settings().get("openalgo", {}).get("host", ""))
        result: dict[str, Any] = {"reachable": False, "host": host, "analyzer_mode": None, "broker": None, "error": None}
        try:
            client = self.ctx.probe_client()
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        except Exception as exc:
            result["error"] = f"client unavailable: {exc}"
            return result
        try:
            status = client.analyzer_status()
            result["reachable"] = True
            result["analyzer_mode"] = _analyze_mode(status)
            result["broker"] = _broker_of(client)
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            _close(client)
        return result

    def analyzer_mode(self) -> tuple[bool | None, str | None]:
        """A fresh analyzer reading: (mode, error)."""
        probe = self.probe(refresh=True)
        if not probe["reachable"]:
            return None, probe.get("error") or "OpenAlgo unreachable"
        return probe["analyzer_mode"], None

    def analyzer_toggle(self, mode: bool) -> bool:
        client = self.ctx.client()
        try:
            client.analyzer_toggle(bool(mode))
            status = client.analyzer_status()
        finally:
            _close(client)
        value = _analyze_mode(status)
        with self._probe_lock:
            self._probe = None
        return bool(mode) if value is None else bool(value)

    # -------------------------------------------------------------- chain

    def chain(self, strikes_each_side: int | None = None, with_iv: bool = False) -> dict[str, Any]:
        from openfly.market.chain import ChainResolver

        settings = self.ctx.settings()
        strategy = settings.get("strategy", {})
        client = self.ctx.client()
        try:
            cal = self.calendar(client)
            resolver = ChainResolver(client, settings, cal)
            expiries = resolver.expiries()
            selection = str(strategy.get("expiry_selection", "monthly"))
            select = getattr(resolver, "select_expiry", None)
            expiry = None
            if callable(select):
                try:
                    expiry = select(settings)
                except TypeError:
                    expiry = None
            if expiry is None:
                expiry = choose_expiry(expiries, selection, self.ctx.now().date(), int(strategy.get("min_days_to_expiry", 0) or 0))
            snapshot = resolver.chain_snapshot(
                expiry,
                strikes_each_side=int(strikes_each_side or 5),
                with_iv=bool(with_iv),
                min_days_to_expiry=int(strategy.get("min_days_to_expiry", 0) or 0),
            )
        finally:
            _close(client)
        payload = snapshot.to_dict()
        payload["expiry_selection"] = selection
        payload["expiries"] = [e.isoformat() for e in expiries]
        atm = snapshot.atm
        payload["atm_straddle"] = (
            {
                "strike": atm.strike,
                "expiry": atm.expiry.isoformat(),
                "ce": {"symbol": atm.call.symbol, "ltp": atm.call.ltp, "bid": atm.call.bid, "ask": atm.call.ask},
                "pe": {"symbol": atm.put.symbol, "ltp": atm.put.ltp, "bid": atm.put.bid, "ask": atm.put.ask},
                "combined_ltp": round(atm.combined_ltp, 2),
                "synthetic_forward": round(atm.synthetic_forward, 2),
            }
            if atm is not None
            else None
        )
        return payload

    # ------------------------------------------------------------- books

    def orders(self) -> dict[str, Any]:
        tag = str(self.strategy().get("strategy_tag", "openfly"))
        try:
            client = self.ctx.client()
        except Exception as exc:
            return {"orders": [], "error": str(exc)}
        try:
            book = client.orderbook()
        except Exception as exc:
            return {"orders": [], "error": str(exc)}
        finally:
            _close(client)
        rows = list(book.get("orders") or []) if isinstance(book, dict) else list(book or [])
        tagged = [r for r in rows if str(r.get("strategy", "")).lower() == tag.lower()]
        if not tagged and rows and not any("strategy" in r for r in rows):
            tagged = rows
        return {"orders": tagged, "statistics": book.get("statistics", {}) if isinstance(book, dict) else {}, "strategy": tag}

    def positions(self, symbols: set[str] | None = None) -> dict[str, Any]:
        strategy = self.strategy()
        exchange = str(strategy.get("options_exchange", "NFO"))
        product = str(strategy.get("product", "NRML"))
        try:
            client = self.ctx.client()
        except Exception as exc:
            return {"positions": [], "error": str(exc)}
        try:
            rows = client.positionbook()
        except Exception as exc:
            return {"positions": [], "error": str(exc)}
        finally:
            _close(client)
        out = []
        for row in rows or []:
            if str(row.get("exchange", "")).upper() != exchange.upper():
                continue
            if row.get("product") and str(row.get("product")).upper() != product.upper():
                continue
            if symbols and str(row.get("symbol")) not in symbols:
                continue
            out.append(row)
        return {"positions": out, "exchange": exchange, "product": product}


def _analyze_mode(status: Any) -> bool | None:
    if isinstance(status, dict):
        if "analyze_mode" in status:
            return bool(status["analyze_mode"])
        mode = status.get("mode")
        if isinstance(mode, str):
            return mode.lower() == "analyze"
        if "data" in status and isinstance(status["data"], dict):
            return _analyze_mode(status["data"])
    return None


def _broker_of(client: Any) -> str | None:
    post = getattr(client, "_post", None)
    if post is None:
        return None
    try:
        payload = post("ping")
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        broker = data.get("broker")
        return str(broker) if broker else None
    return None


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
