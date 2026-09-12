from __future__ import annotations

from datetime import date

import pytest

from openfly.interfaces import Contract, Quote, StraddleQuote
from openfly.market.chain import ChainResolver, ChainSnapshot, nearest_strike
from openfly.market.client import OpenAlgoError
from openfly.market.session import SessionCalendar

EXPIRIES = [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29), date(2026, 10, 6)]
INDEX = 23398.1


def search_rows(expiry: date, strikes: range) -> list[dict]:
    code = expiry.strftime("%d%b%y").upper()
    rows = []
    for strike in strikes:
        for kind in ("CE", "PE"):
            rows.append(
                {
                    "brexchange": "NFO",
                    "brsymbol": f"NIFTY{code}{strike}{kind}",
                    "exchange": "NFO",
                    "expiry": expiry.strftime("%d-%b-%y").upper(),
                    "freeze_qty": 1800,
                    "instrumenttype": kind,
                    "lotsize": 65,
                    "name": "NIFTY",
                    "strike": float(strike),
                    "symbol": f"NIFTY{code}{strike}{kind}",
                    "tick_size": 0.05,
                    "token": "t",
                }
            )
    return rows


def leg_price(strike: float, kind: str, forward: float = 23463.2) -> float:
    """Toy pricing: intrinsic against the forward plus a symmetric time value."""
    time_value = max(5.0, 70.0 - abs(strike - forward) * 0.4)
    intrinsic = max(0.0, forward - strike) if kind == "CE" else max(0.0, strike - forward)
    return round(intrinsic + time_value, 2)


class StubChainClient:
    def __init__(self):
        self.calls: list[str] = []
        self.margin_positions = None

    def expiry(self, symbol, exchange, instrumenttype="options"):
        self.calls.append("expiry")
        return list(EXPIRIES)

    def search(self, query, exchange):
        self.calls.append(f"search:{query}")
        for expiry in EXPIRIES:
            if query == f"NIFTY{expiry.strftime('%d%b%y').upper()}":
                rows = search_rows(expiry, range(22500, 24550, 50))
                # A futures row and a stray other expiry must be filtered out.
                rows.append({**rows[0], "instrumenttype": "FUT", "symbol": "NIFTY29SEP26FUT", "strike": 0})
                return rows
        return []

    def multiquotes(self, symbols):
        self.calls.append("multiquotes")
        out = []
        for exchange, symbol in symbols:
            if symbol == "NIFTY":
                out.append(Quote(symbol, exchange, INDEX, 0, 0, 1.0))
            elif symbol == "INDIAVIX":
                out.append(Quote(symbol, exchange, 12.29, 0, 0, 1.0))
            else:
                data = self._option(symbol)
                out.append(Quote(symbol, exchange, data["ltp"], data["bid"], data["ask"], 1.0))
        return out

    def multiquotes_raw(self, symbols):
        self.calls.append("multiquotes_raw")
        results = []
        for exchange, symbol in symbols:
            if symbol.endswith("23200PE"):
                results.append({"symbol": symbol, "exchange": exchange, "error": "no quote"})
                continue
            results.append({"symbol": symbol, "exchange": exchange, "data": self._option(symbol)})
        return results

    @staticmethod
    def _option(symbol: str) -> dict:
        kind = symbol[-2:]
        strike = float(symbol[-7:-2])
        ltp = leg_price(strike, kind)
        return {"ltp": ltp, "bid": round(ltp - 0.1, 2), "ask": round(ltp + 0.5, 2), "oi": 100, "volume": 5}

    def quotes(self, symbol, exchange):
        self.calls.append(f"quotes:{symbol}")
        if symbol == "INDIAVIX":
            return Quote(symbol, exchange, 12.29, 0, 0, 1.0)
        return Quote(symbol, exchange, INDEX, 0, 0, 1.0)

    def margin(self, positions):
        self.margin_positions = positions
        return {"total_margin_required": 188700.31, "span_margin": 141125.25, "exposure_margin": 60835.06}

    def optiongreeks(self, symbol, exchange="NFO", **extra):
        self.calls.append(f"greeks:{symbol}")
        return {"symbol": symbol, "implied_volatility": 10.88, "greeks": {"delta": 0.5}, **extra}


@pytest.fixture
def resolver(paths, settings):
    client = StubChainClient()
    calendar = SessionCalendar(None, settings, paths)
    return ChainResolver(client, settings, calendar)


def test_expiries_are_cached_and_shared_with_calendar(resolver):
    assert resolver.expiries() == EXPIRIES
    assert resolver.expiries() == EXPIRIES
    assert resolver.client.calls.count("expiry") == 1
    assert resolver.calendar.expiries() == EXPIRIES
    assert resolver.calendar.is_expiry_day(date(2026, 9, 15))


def test_current_week_respects_min_days_to_expiry(resolver):
    assert resolver.current_week(0, today=date(2026, 9, 12)) == date(2026, 9, 15)
    assert resolver.current_week(0, today=date(2026, 9, 15)) == date(2026, 9, 15)
    assert resolver.current_week(1, today=date(2026, 9, 15)) == date(2026, 9, 22)
    assert resolver.current_week(2, today=date(2026, 9, 14)) == date(2026, 9, 22)
    assert resolver.current_week(1, today=date(2026, 9, 14)) == date(2026, 9, 15)
    assert resolver.trading_days_to(date(2026, 9, 15), today=date(2026, 9, 11)) == 2
    with pytest.raises(OpenAlgoError):
        resolver.current_week(99, today=date(2026, 9, 12))


def test_contracts_resolved_via_search_and_filtered(resolver):
    chain = resolver.contracts(date(2026, 9, 15))
    assert resolver.client.calls[-1] == "search:NIFTY15SEP26"
    assert len(chain) == 41 and 23400.0 in chain
    row = chain[23400.0]
    assert isinstance(row.ce, Contract) and row.ce.symbol == "NIFTY15SEP2623400CE"
    assert row.pe.symbol == "NIFTY15SEP2623400PE" and row.pe.lot_size == 65 and row.pe.freeze_qty == 1800
    assert resolver.symbol_for(date(2026, 9, 15), 23450, "CE") == "NIFTY15SEP2623450CE"
    assert resolver.straddle_symbols(date(2026, 9, 15), 23450) == ("NIFTY15SEP2623450CE", "NIFTY15SEP2623450PE")
    ce, pe = resolver.straddle_contracts(date(2026, 9, 15), 23450)
    assert ce.strike == pe.strike == 23450.0
    resolver.contracts(date(2026, 9, 15))
    assert sum(1 for c in resolver.client.calls if c.startswith("search")) == 1
    with pytest.raises(OpenAlgoError):
        resolver.contract(date(2026, 9, 15), 99999, "CE")


def test_chain_snapshot_atm_by_synthetic_forward(resolver):
    snapshot = resolver.chain_snapshot(index_ltp=INDEX, strikes_each_side=5, vix=12.29)
    assert isinstance(snapshot, ChainSnapshot)
    assert snapshot.expiry == date(2026, 9, 15)
    assert [row.strike for row in snapshot.rows] == [23150.0 + 50 * k for k in range(11)]
    # Forward is computed at the strike nearest the index (23400), giving 23463.2: ATM 23450, not 23400.
    assert snapshot.forward_strike == 23400.0
    assert snapshot.synthetic_forward == pytest.approx(23463.2, abs=0.01)
    assert snapshot.atm_strike == 23450.0
    assert nearest_strike(INDEX, 50) == 23400.0
    assert isinstance(snapshot.atm, StraddleQuote)
    assert snapshot.atm.strike == 23450.0
    assert snapshot.atm.call.symbol == "NIFTY15SEP2623450CE"
    assert snapshot.atm.combined_ltp == pytest.approx(leg_price(23450, "CE") + leg_price(23450, "PE"))
    assert snapshot.lot_size == 65
    assert snapshot.row(23200.0).pe is None  # the leg with an error is left out
    payload = snapshot.to_dict()
    assert payload["expiry"] == "2026-09-15" and payload["atm_strike"] == 23450.0
    assert payload["rows"][5]["ce"]["symbol"] == "NIFTY15SEP2623400CE"
    assert set(payload) >= {"underlying", "days_to_expiry", "index_ltp", "vix", "synthetic_forward", "lot_size", "rows"}
    assert "multiquotes_raw" in resolver.client.calls and "multiquotes" not in resolver.client.calls


def test_chain_snapshot_fetches_index_and_vix_when_missing(resolver):
    snapshot = resolver.chain_snapshot(expiry=date(2026, 9, 22), strikes_each_side=2)
    assert snapshot.index_ltp == INDEX and snapshot.vix == 12.29
    assert len(snapshot.rows) == 5
    assert resolver.client.calls.count("multiquotes") == 1


def test_chain_snapshot_with_iv_and_greeks(resolver):
    snapshot = resolver.chain_snapshot(index_ltp=INDEX, strikes_each_side=1, vix=12.0, with_iv=True)
    assert snapshot.rows[0].ce.iv == 10.88
    greeks = resolver.greeks("NIFTY15SEP2623400CE")
    assert greeks["underlying_symbol"] == "NIFTY" and greeks["underlying_exchange"] == "NSE_INDEX"


def test_straddle_margin_and_quote(resolver):
    ce, pe = resolver.straddle_contracts(date(2026, 9, 15), 23450)
    margin = resolver.straddle_margin([ce, pe], lots=2)
    assert margin["total_margin_required"] == 188700.31
    positions = resolver.client.margin_positions
    assert [p["action"] for p in positions] == ["SELL", "SELL"]
    assert positions[0]["quantity"] == 130 and positions[0]["product"] == "NRML"
    quote = resolver.straddle_quote(date(2026, 9, 15), 23450)
    assert quote.synthetic_forward == pytest.approx(23450 + quote.call.ltp - quote.put.ltp)
