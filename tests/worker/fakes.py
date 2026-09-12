"""Fakes for the replay and worker tests: brain, encoder, readout, bars, quotes, feed, calendar, chain."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from openfly.config import DEFAULT_SETTINGS, deep_merge
from openfly.interfaces import (
    REQUIRED_POPULATIONS,
    Bar,
    Decision,
    MarketObservation,
    ObservationResult,
    Prediction,
    Quote,
    SessionWindow,
    Stimulus,
    StraddleQuote,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 9, 11)
EXPIRY = date(2026, 9, 29)  # the September monthly expiry
CE = "NIFTY29SEP2623350CE"
PE = "NIFTY29SEP2623350PE"


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), IST)


def settings_with(**overrides) -> dict:
    """DEFAULT_SETTINGS with fixed stops (the adaptive tests opt in) plus section__key overrides."""
    update: dict = {"strategy": {"stop_mode": "fixed"}}
    for key, value in overrides.items():
        section, _, name = key.partition("__")
        update.setdefault(section, {})[name] = value
    return deep_merge(DEFAULT_SETTINGS, update)


def window(day: date = DAY) -> SessionWindow:
    return SessionWindow(day, at(9, 15, day=day), at(15, 30, day=day), at(9, 20, day=day), at(14, 30, day=day), at(15, 15, day=day), False)


class FakeBrain:
    """Required population names, deterministic counts that depend on the observation number."""

    def __init__(self, size_per_population: int = 4):
        self.populations: dict[str, np.ndarray] = {}
        start = 0
        for name in REQUIRED_POPULATIONS:
            self.populations[name] = np.arange(start, start + size_per_population, dtype=np.int32)
            start += size_per_population
        self.n = start
        self.observations = 0
        self.sim_ms = 0.0
        self.pulses_seen: list[tuple] = []

    def observe(self, stimulus: Stimulus, neural_ms: float) -> ObservationResult:
        self.observations += 1
        self.sim_ms += neural_ms
        self.pulses_seen.append(tuple(stimulus.pulses))
        counts = ((np.arange(self.n) * 7 + self.observations) % 5).astype(np.int32)
        counts[self.populations["DNp20_R"]] = 3
        counts[self.populations["DNp20_L"]] = 1
        counts[self.populations["DNpe017"]] = 1
        return ObservationResult(counts=counts, neural_ms=neural_ms, sim_ms=self.sim_ms, compute_seconds=0.001)

    def checkpoint(self, path: str) -> None:
        return None

    def restore(self, path: str) -> None:
        return None

    def provenance(self) -> dict:
        return {"kind": "FakeBrain"}


class FakeEncoder:
    name = "fake"

    def __init__(self, with_png: bool = True):
        self.with_png = with_png
        self.calls = 0

    def encode(self, observation: MarketObservation, brain) -> Stimulus:
        self.calls += 1
        n16 = brain.populations["R1-R6"].size
        n8 = brain.populations["R8p"].size + brain.populations["R8y"].size
        level = observation.index_bars[-1].close / 100000.0
        return Stimulus(r16=np.full(n16, level), r8=np.full(n8, 0.5))

    def config_hash(self) -> str:
        return "fake-encoder"

    def render_png(self, stimulus: Stimulus) -> bytes:
        if not self.with_png:
            raise RuntimeError("no renderer")
        return b"\x89PNG\r\n\x1a\n" + bytes(int(v * 255) % 256 for v in stimulus.r16[:8])


class FakeReadout:
    """ENTER at one time, EXIT at another, HOLD otherwise."""

    name = "fake"

    def __init__(self, enter_at: time = time(10, 0), exit_at: time = time(12, 0)):
        self.enter_at = enter_at
        self.exit_at = exit_at
        self.calls = 0

    def predict(self, counts: np.ndarray, brain, observation: MarketObservation) -> Prediction:
        self.calls += 1
        when = observation.timestamp.time().replace(second=0, microsecond=0)
        if when == self.enter_at:
            return Prediction(0.82, 0.61, Decision.ENTER, {"readout": self.name})
        if when == self.exit_at:
            return Prediction(1.12, 0.7, Decision.EXIT, {"readout": self.name})
        return Prediction(0.95, 0.5, Decision.HOLD, {"readout": self.name})

    def config_hash(self) -> str:
        return "fake-readout"


def synthetic_1m_bars(day: date = DAY, start: float = 23350.0) -> list[Bar]:
    bars = []
    t = at(9, 15, day=day)
    level = start
    for i in range(375):
        drift = 0.4 * np.sin(i / 25.0)
        o = level
        c = round(level + drift, 2)
        bars.append(Bar(t, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 0.0))
        level = c
        t += timedelta(minutes=1)
    return bars


def premium_at(when: datetime, day: date = DAY) -> tuple[float, float]:
    """A calm path: combined premium decays from 200 to about 185 over the day (no level is hit)."""
    minutes = (when - at(9, 15, day=day)).total_seconds() / 60.0
    call = round(100.6 - 0.02 * minutes, 2)
    put = round(99.4 - 0.02 * minutes, 2)
    return call, put


def make_minute_quotes(day: date = DAY, strike: float = 23350.0, expiry: date = EXPIRY):
    def quotes(when: datetime, strike_: float | None = None) -> StraddleQuote:
        k = strike_ if strike_ is not None else strike
        call, put = premium_at(when, day)
        ts = when.timestamp()
        code = expiry.strftime("%d%b%y").upper()
        return StraddleQuote(
            call=Quote(f"NIFTY{code}{int(k)}CE", "NFO", call, call - 0.05, call + 0.05, ts),
            put=Quote(f"NIFTY{code}{int(k)}PE", "NFO", put, put - 0.05, put + 0.05, ts),
            strike=k,
            expiry=expiry,
        )

    return quotes


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeFeed:
    """Serves quotes as a function of the clock; LTP only, like the websocket in mode 1."""

    def __init__(self, clock: FakeClock, bars: list[Bar], vix: float = 12.1):
        self.clock = clock
        self.bars = {b.timestamp: b for b in bars}
        self.vix = vix
        self.subscriptions: list[tuple[str, str]] = []
        self.quotes = make_minute_quotes()

    def subscribe(self, pairs) -> None:
        self.subscriptions.extend(pairs)

    def _index(self, when: datetime) -> float | None:
        minute = when.replace(second=0, microsecond=0)
        bar = self.bars.get(minute)
        if bar is None:
            earlier = [b for t, b in self.bars.items() if t <= minute]
            if not earlier:
                return None
            bar = max(earlier, key=lambda b: b.timestamp)
        return bar.close

    def last(self, symbol: str) -> Quote | None:
        now = self.clock()
        if symbol == "NIFTY":
            level = self._index(now)
            return Quote("NIFTY", "NSE_INDEX", level, 0.0, 0.0, now.timestamp()) if level else None
        if symbol == "INDIAVIX":
            return Quote("INDIAVIX", "NSE_INDEX", self.vix, 0.0, 0.0, now.timestamp())
        q = self.quotes(now)
        if symbol == q.call.symbol:
            return q.call
        if symbol == q.put.symbol:
            return q.put
        return None

    def age_seconds(self, symbol: str) -> float | None:
        return 0.2


class FakeCalendar:
    def __init__(self, day: date = DAY, trading: bool = True):
        self.day = day
        self.trading = trading

    def window_for(self, day) -> SessionWindow:
        return window(day)

    def is_trading_day(self, day) -> bool:
        return self.trading


class FakeChain:
    def __init__(self, strike: float = 23350.0, expiry: date = EXPIRY):
        self.strike = strike
        self.expiry = expiry
        self.calls = 0

    def select_expiry(self, settings: dict) -> date:
        return self.expiry

    def chain_snapshot(self, expiry: date | None = None) -> dict:
        self.calls += 1
        if expiry is not None:
            assert expiry == self.expiry, "the worker must ask for the selected expiry"
        code = self.expiry.strftime("%d%b%y").upper()
        return {
            "underlying": "NIFTY",
            "expiry": self.expiry.isoformat(),
            "atm_strike": self.strike,
            "lot_size": 65,
            "rows": [{"strike": self.strike, "ce": {"symbol": f"NIFTY{code}{int(self.strike)}CE"}, "pe": {"symbol": f"NIFTY{code}{int(self.strike)}PE"}}],
        }
