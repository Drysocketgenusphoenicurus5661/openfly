"""Option chain resolution for the current-week NIFTY straddle.

OpenAlgo's optionchain, optionsymbol and syntheticfuture helpers return 404 on
this install, so the chain is built from ``expiry`` (weekly list) and ``search``
(``NIFTY15SEP26`` returns every strike of that expiry). ATM is chosen from the
synthetic forward (strike + call - put at the strike nearest the index), which
differs from the spot index by tens of points.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from openfly.config import DEFAULT_SETTINGS, SettingsStore
from openfly.interfaces import Contract, Quote, StraddleQuote
from openfly.market.client import (
    IST,
    OpenAlgoClient,
    OpenAlgoError,
    contract_from_row,
    expiry_code,
    option_symbol,
)
from openfly.market.session import SessionCalendar

logger = logging.getLogger("openfly.market.chain")

EXPIRY_TIME_IST = (15, 30)


@dataclass(frozen=True)
class ChainRow:
    """Listed contracts for one strike."""

    strike: float
    ce: Contract | None
    pe: Contract | None


@dataclass(frozen=True)
class OptionQuote:
    """One leg in a chain snapshot (matches the ``ce`` / ``pe`` objects of GET /api/market/chain)."""

    symbol: str
    ltp: float
    bid: float
    ask: float
    iv: float | None = None
    oi: float | None = None
    volume: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotRow:
    strike: float
    ce: OptionQuote | None
    pe: OptionQuote | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strike": self.strike,
            "ce": self.ce.to_dict() if self.ce else None,
            "pe": self.pe.to_dict() if self.pe else None,
        }


@dataclass(frozen=True)
class ChainSnapshot:
    """The current-week chain around the ATM, as served by GET /api/market/chain."""

    underlying: str
    expiry: date
    days_to_expiry: float
    index_ltp: float
    vix: float | None
    synthetic_forward: float
    atm_strike: float
    lot_size: int
    rows: tuple[SnapshotRow, ...]
    atm: StraddleQuote | None
    forward_strike: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "days_to_expiry": round(self.days_to_expiry, 4),
            "index_ltp": self.index_ltp,
            "vix": self.vix,
            "synthetic_forward": round(self.synthetic_forward, 2),
            "atm_strike": self.atm_strike,
            "lot_size": self.lot_size,
            "rows": [row.to_dict() for row in self.rows],
            "atm_combined": self.atm.combined_ltp if self.atm else None,
            "timestamp": datetime.fromtimestamp(self.timestamp, IST).isoformat(),
        }

    def row(self, strike: float) -> SnapshotRow | None:
        for row in self.rows:
            if row.strike == strike:
                return row
        return None


def nearest_strike(value: float, step: float) -> float:
    return float(round(value / step) * step)


class ChainResolver:
    """Expiry choice, contract lookup and chain snapshots for the configured underlying."""

    def __init__(
        self,
        client: OpenAlgoClient,
        store: SettingsStore | dict[str, Any] | None = None,
        calendar: SessionCalendar | None = None,
        expiry_ttl_seconds: float = 6 * 3600,
    ):
        self.client = client
        if isinstance(store, dict):
            settings = store
        else:
            settings = (store or SettingsStore()).get() if store is not None else DEFAULT_SETTINGS
        strategy = settings.get("strategy", {})
        self.underlying = strategy.get("underlying", "NIFTY")
        self.index_exchange = strategy.get("index_exchange", "NSE_INDEX")
        self.options_exchange = strategy.get("options_exchange", "NFO")
        self.vix_symbol = strategy.get("vix_symbol", "INDIAVIX")
        self.strike_step = float(strategy.get("strike_step", 50))
        self.lot_size = int(strategy.get("lot_size", 65))
        self.calendar = calendar
        self.expiry_ttl = expiry_ttl_seconds
        self._expiries: list[date] = []
        self._expiries_at = 0.0
        self._contracts: dict[date, dict[float, ChainRow]] = {}

    # expiries ------------------------------------------------------------

    def expiries(self, refresh: bool = False) -> list[date]:
        """Sorted option expiries for the underlying (cached for ``expiry_ttl_seconds``)."""
        fresh = time.time() - self._expiries_at < self.expiry_ttl
        if self._expiries and fresh and not refresh:
            return list(self._expiries)
        try:
            found = self.client.expiry(self.underlying, self.options_exchange, "options")
        except OpenAlgoError:
            if self._expiries:
                return list(self._expiries)
            if self.calendar is not None and self.calendar.expiries():
                return self.calendar.expiries()
            raise
        self._expiries = sorted(found)
        self._expiries_at = time.time()
        if self.calendar is not None:
            self.calendar.set_expiries(self._expiries)
        return list(self._expiries)

    def _is_trading_day(self, d: date) -> bool:
        if self.calendar is not None:
            return self.calendar.is_trading_day(d)
        return d.weekday() < 5

    def trading_days_to(self, expiry: date, today: date | None = None) -> int:
        """Trading days strictly after ``today`` up to and including ``expiry`` (0 on expiry day)."""
        start = today or datetime.now(IST).date()
        if self.calendar is not None:
            return self.calendar.trading_days_between(start, expiry)
        count = 0
        d = start
        while d < expiry:
            d = d.fromordinal(d.toordinal() + 1)
            if self._is_trading_day(d):
                count += 1
        return count

    def current_week(self, min_days_to_expiry: int = 0, today: date | None = None) -> date:
        """Nearest expiry with at least ``min_days_to_expiry`` trading days left (0 accepts today's expiry)."""
        start = today or datetime.now(IST).date()
        for expiry in self.expiries():
            if expiry < start:
                continue
            if self.trading_days_to(expiry, start) >= int(min_days_to_expiry):
                return expiry
        raise OpenAlgoError(
            f"no {self.underlying} expiry with at least {min_days_to_expiry} trading days left",
            endpoint="expiry",
        )

    # contracts -----------------------------------------------------------

    def symbol_for(self, expiry: date, strike: float, option_type: str) -> str:
        """Symbol text without any network call, e.g. NIFTY15SEP2623400CE."""
        return option_symbol(self.underlying, expiry, strike, option_type)

    def straddle_symbols(self, expiry: date, strike: float) -> tuple[str, str]:
        return self.symbol_for(expiry, strike, "CE"), self.symbol_for(expiry, strike, "PE")

    def contracts(self, expiry: date, refresh: bool = False) -> dict[float, ChainRow]:
        """Listed strikes for ``expiry`` as ``{strike: ChainRow}`` via ``search``."""
        if expiry in self._contracts and not refresh:
            return dict(self._contracts[expiry])
        query = f"{self.underlying}{expiry_code(expiry)}"
        rows = self.client.search(query, self.options_exchange)
        by_strike: dict[float, dict[str, Contract]] = {}
        for row in rows:
            kind = str(row.get("instrumenttype", "")).upper()
            if kind not in {"CE", "PE"}:
                continue
            if str(row.get("name", self.underlying)).upper() != self.underlying.upper():
                continue
            try:
                contract = contract_from_row(row)
            except (KeyError, ValueError):
                continue
            if contract.expiry != expiry:
                continue
            by_strike.setdefault(contract.strike, {})[kind] = contract
        chain = {
            strike: ChainRow(strike=strike, ce=legs.get("CE"), pe=legs.get("PE"))
            for strike, legs in sorted(by_strike.items())
        }
        if chain:
            self._contracts[expiry] = chain
        return dict(chain)

    def contract(self, expiry: date, strike: float, option_type: str) -> Contract:
        row = self.contracts(expiry).get(float(strike))
        leg = getattr(row, option_type.lower(), None) if row else None
        if leg is None:
            raise OpenAlgoError(
                f"{self.underlying} {expiry} {strike:g} {option_type} is not listed", endpoint="search"
            )
        return leg

    def straddle_contracts(self, expiry: date, strike: float) -> tuple[Contract, Contract]:
        return self.contract(expiry, strike, "CE"), self.contract(expiry, strike, "PE")

    # quotes --------------------------------------------------------------

    def index_quote(self) -> Quote:
        return self.client.quotes(self.underlying, self.index_exchange)

    def vix(self) -> float | None:
        try:
            return self.client.quotes(self.vix_symbol, self.index_exchange).ltp
        except OpenAlgoError as exc:
            logger.warning("VIX quote unavailable: %s", exc)
            return None

    def days_to_expiry(self, expiry: date, now: datetime | None = None) -> float:
        moment = now or datetime.now(IST)
        end = datetime(expiry.year, expiry.month, expiry.day, *EXPIRY_TIME_IST, tzinfo=IST)
        return max(0.0, (end - moment).total_seconds() / 86400.0)

    def chain_snapshot(
        self,
        expiry: date | None = None,
        index_ltp: float | None = None,
        strikes_each_side: int = 5,
        vix: float | None = None,
        with_iv: bool = False,
        min_days_to_expiry: int = 0,
    ) -> ChainSnapshot:
        """Quotes for the strikes around the index, the synthetic forward and the ATM straddle.

        One ``multiquotes`` call covers every leg. The forward is computed at the
        listed strike nearest the index; ATM is the strike-step multiple nearest
        the forward. ``with_iv`` adds one ``optiongreeks`` call per leg.
        """
        expiry = expiry or self.current_week(min_days_to_expiry)
        if index_ltp is None or vix is None:
            wanted = []
            if index_ltp is None:
                wanted.append((self.index_exchange, self.underlying))
            if vix is None:
                wanted.append((self.index_exchange, self.vix_symbol))
            for quote in self.client.multiquotes(wanted):
                if quote.symbol == self.underlying and index_ltp is None:
                    index_ltp = quote.ltp
                elif quote.symbol == self.vix_symbol and vix is None:
                    vix = quote.ltp
        if index_ltp is None or index_ltp <= 0:
            raise OpenAlgoError(f"no index quote for {self.underlying}", endpoint="multiquotes")
        chain = self.contracts(expiry)
        if not chain:
            raise OpenAlgoError(f"no listed contracts for {self.underlying} {expiry}", endpoint="search")
        centre = nearest_strike(index_ltp, self.strike_step)
        strikes = [
            centre + k * self.strike_step
            for k in range(-int(strikes_each_side), int(strikes_each_side) + 1)
            if (centre + k * self.strike_step) in chain
        ]
        if not strikes:
            nearest = min(chain, key=lambda s: abs(s - index_ltp))
            strikes = [nearest]
        legs: list[tuple[str, str]] = []
        for strike in strikes:
            row = chain[strike]
            if row.ce:
                legs.append((row.ce.exchange, row.ce.symbol))
            if row.pe:
                legs.append((row.pe.exchange, row.pe.symbol))
        raw = {entry.get("symbol"): entry for entry in self.client.multiquotes_raw(legs)}
        quotes: dict[str, OptionQuote] = {}
        for symbol, entry in raw.items():
            data = entry.get("data") or {}
            if not data or entry.get("error"):
                continue
            quotes[str(symbol)] = OptionQuote(
                symbol=str(symbol),
                ltp=float(data.get("ltp") or 0.0),
                bid=float(data.get("bid") or 0.0),
                ask=float(data.get("ask") or 0.0),
                oi=float(data["oi"]) if data.get("oi") is not None else None,
                volume=float(data["volume"]) if data.get("volume") is not None else None,
            )
        if with_iv:
            quotes = {symbol: self._with_iv(quote) for symbol, quote in quotes.items()}

        rows: list[SnapshotRow] = []
        for strike in strikes:
            row = chain[strike]
            ce = quotes.get(row.ce.symbol) if row.ce else None
            pe = quotes.get(row.pe.symbol) if row.pe else None
            rows.append(SnapshotRow(strike=strike, ce=ce, pe=pe))

        complete = [row for row in rows if row.ce and row.pe and row.ce.ltp > 0 and row.pe.ltp > 0]
        if not complete:
            raise OpenAlgoError(
                f"no strike with both legs quoted for {self.underlying} {expiry}", endpoint="multiquotes"
            )
        near = min(complete, key=lambda row: abs(row.strike - index_ltp))
        forward = near.strike + near.ce.ltp - near.pe.ltp  # type: ignore[union-attr]
        atm_strike = nearest_strike(forward, self.strike_step)
        atm_row = next((row for row in complete if row.strike == atm_strike), None)
        if atm_row is None:
            atm_row = min(complete, key=lambda row: abs(row.strike - forward))
            atm_strike = atm_row.strike
        now = time.time()
        atm = StraddleQuote(
            call=self._leg_quote(atm_row.ce, now),  # type: ignore[arg-type]
            put=self._leg_quote(atm_row.pe, now),  # type: ignore[arg-type]
            strike=atm_strike,
            expiry=expiry,
        )
        lot_size = self.lot_size
        if chain[atm_strike].ce is not None:
            lot_size = chain[atm_strike].ce.lot_size or lot_size  # type: ignore[union-attr]
        return ChainSnapshot(
            underlying=self.underlying,
            expiry=expiry,
            days_to_expiry=self.days_to_expiry(expiry),
            index_ltp=float(index_ltp),
            vix=vix,
            synthetic_forward=float(forward),
            atm_strike=atm_strike,
            lot_size=lot_size,
            rows=tuple(rows),
            atm=atm,
            forward_strike=near.strike,
            timestamp=now,
        )

    def _leg_quote(self, leg: OptionQuote, now: float) -> Quote:
        return Quote(
            symbol=leg.symbol,
            exchange=self.options_exchange,
            ltp=leg.ltp,
            bid=leg.bid,
            ask=leg.ask,
            timestamp=now,
        )

    def _with_iv(self, quote: OptionQuote) -> OptionQuote:
        try:
            greeks = self.greeks(quote.symbol)
        except OpenAlgoError as exc:
            logger.warning("greeks unavailable for %s: %s", quote.symbol, exc)
            return quote
        iv = greeks.get("implied_volatility")
        return OptionQuote(
            symbol=quote.symbol,
            ltp=quote.ltp,
            bid=quote.bid,
            ask=quote.ask,
            iv=float(iv) if iv is not None else None,
            oi=quote.oi,
            volume=quote.volume,
        )

    def straddle_quote(self, expiry: date, strike: float) -> StraddleQuote:
        """Both legs of one straddle in a single multiquotes call."""
        ce_symbol, pe_symbol = self.straddle_symbols(expiry, strike)
        quotes = {
            q.symbol: q
            for q in self.client.multiquotes(
                [(self.options_exchange, ce_symbol), (self.options_exchange, pe_symbol)]
            )
        }
        if ce_symbol not in quotes or pe_symbol not in quotes:
            raise OpenAlgoError(f"straddle {strike:g} {expiry} not fully quoted", endpoint="multiquotes")
        return StraddleQuote(call=quotes[ce_symbol], put=quotes[pe_symbol], strike=float(strike), expiry=expiry)

    # margin and greeks ---------------------------------------------------

    def straddle_margin(
        self,
        contracts: Iterable[Contract],
        lots: int,
        product: str | None = None,
        action: str = "SELL",
    ) -> dict[str, float]:
        """Margin for ``lots`` of each contract on the same side (SELL for a short straddle)."""
        product = product or DEFAULT_SETTINGS["strategy"]["product"]
        positions = []
        for contract in contracts:
            positions.append(
                {
                    "symbol": contract.symbol,
                    "exchange": contract.exchange,
                    "action": action,
                    "product": product,
                    "pricetype": "MARKET",
                    "quantity": int(lots) * int(contract.lot_size),
                }
            )
        return self.client.margin(positions)

    def greeks(self, symbol: str, **extra: Any) -> dict[str, Any]:
        """IV and greeks for one contract, using the index as the underlying for spot."""
        params = {
            "underlying_symbol": self.underlying,
            "underlying_exchange": self.index_exchange,
        }
        params.update(extra)
        return self.client.optiongreeks(symbol, self.options_exchange, **params)
