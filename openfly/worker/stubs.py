"""Stand-ins used when the neural, sensory, readout or experiments packages are missing.

They keep `openfly replay-day` and the worker runnable on a partial checkout:
a NullBrain with the required population names and zero spikes, a null
encoder, a readout that says ENTER at the trade start, an approximate ATM
straddle pricer and a synthetic index path. None of them is a model of
anything; every trace produced with them says so in its config.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from openfly.interfaces import (
    REQUIRED_POPULATIONS,
    Bar,
    Decision,
    MarketObservation,
    ObservationResult,
    Prediction,
    Quote,
    Stimulus,
    StraddleQuote,
)

IST = ZoneInfo("Asia/Kolkata")

_POPULATION_SIZES = {
    "R1-R6": 64,
    "R8p": 8,
    "R8y": 8,
    "lamina": 16,
    "KC": 32,
    "PAM11": 4,
    "PPL101": 2,
    "MBON07": 2,
    "MBON11": 2,
    "MBON": 8,
    "DNp20_L": 1,
    "DNp20_R": 1,
    "DNpe017": 1,
    "DN": 8,
    "central_complex": 16,
    "random2000": 32,
}


class NullBrain:
    """BrainProtocol stand-in: the required populations, zero spikes, no state."""

    def __init__(self) -> None:
        self.populations: dict[str, np.ndarray] = {}
        start = 0
        for name in REQUIRED_POPULATIONS:
            size = _POPULATION_SIZES.get(name, 4)
            self.populations[name] = np.arange(start, start + size, dtype=np.int32)
            start += size
        self.n = start
        self.sim_ms = 0.0
        self.observations = 0

    def observe(self, stimulus: Stimulus, neural_ms: float) -> ObservationResult:
        self.sim_ms += float(neural_ms)
        self.observations += 1
        return ObservationResult(
            counts=np.zeros(self.n, dtype=np.int32),
            neural_ms=float(neural_ms),
            sim_ms=self.sim_ms,
            compute_seconds=0.0,
        )

    def checkpoint(self, path: str) -> None:
        return None

    def restore(self, path: str) -> None:
        return None

    def provenance(self) -> dict:
        return {"kind": "NullBrain", "n": self.n, "note": "stand-in, zero spikes"}


class NullEncoder:
    """EncoderProtocol stand-in: a dark field, no pulses."""

    name = "null"

    def encode(self, observation: MarketObservation, brain) -> Stimulus:
        pops = getattr(brain, "populations", {})
        n16 = int(np.asarray(pops.get("R1-R6", np.zeros(0))).size)
        n8 = int(np.asarray(pops.get("R8p", np.zeros(0))).size) + int(np.asarray(pops.get("R8y", np.zeros(0))).size)
        return Stimulus(r16=np.zeros(n16, dtype=np.float64), r8=np.zeros(n8, dtype=np.float64))

    def config_hash(self) -> str:
        return "null"


class FixedTimeReadout:
    """ReadoutProtocol stand-in: ENTER on the observation at `enter_at`, HOLD otherwise."""

    name = "fixed_time"

    def __init__(self, enter_at: str | time = "09:20", realized_over_implied: float = 0.9):
        if isinstance(enter_at, str):
            hh, mm = enter_at.split(":")[:2]
            enter_at = time(int(hh), int(mm))
        self.enter_at = enter_at
        self.roi = realized_over_implied

    @classmethod
    def from_settings(cls, settings: dict) -> FixedTimeReadout:
        return cls(settings.get("strategy", {}).get("trade_start", "09:20"))

    def predict(self, counts: np.ndarray, brain, observation: MarketObservation) -> Prediction:
        when = observation.timestamp.time().replace(second=0, microsecond=0)
        if when == self.enter_at and observation.position_lots == 0:
            return Prediction(self.roi, 0.5, Decision.ENTER, {"readout": self.name})
        return Prediction(1.0, 0.5, Decision.HOLD, {"readout": self.name})

    def config_hash(self) -> str:
        return f"fixed_time:{self.enter_at.strftime('%H:%M')}"


def nearest_strike(value: float, step: float = 50.0) -> float:
    return float(round(value / step) * step)


def simple_minute_quotes(
    day: date,
    bars1m: Sequence[Bar],
    vix: float,
    expiry: date,
    *,
    strike_step: float = 50.0,
    exchange: str = "NFO",
    underlying: str = "NIFTY",
    spread: float = 0.10,
) -> Callable[..., StraddleQuote | None]:
    """An approximate ATM straddle pricer from the index path and a VIX level.

    Time value of the straddle is 0.8 x S x sigma x sqrt(T) split evenly between
    the legs, plus intrinsic value per leg. It captures decay and gamma cost only
    roughly; it is a stand-in for the experiments package's calibrated pricer.
    """
    closes: dict[datetime, float] = {}
    ordered = sorted((b for b in bars1m if b.timestamp.date() == day), key=lambda b: b.timestamp)
    for b in ordered:
        closes[b.timestamp + timedelta(minutes=1)] = b.close
    stamps = sorted(closes)
    sigma = max(float(vix), 1.0) / 100.0
    expiry_close = datetime.combine(expiry, time(15, 30), IST)
    code = expiry.strftime("%d%b%y").upper()

    def level_at(when: datetime) -> float | None:
        last = None
        for ts in stamps:
            if ts <= when:
                last = closes[ts]
            else:
                break
        if last is None and ordered:
            return ordered[0].open
        return last

    def quote(when: datetime, strike: float | None = None) -> StraddleQuote | None:
        s = level_at(when)
        if s is None:
            return None
        k = float(strike) if strike is not None else nearest_strike(s, strike_step)
        years = max((expiry_close - when).total_seconds(), 60.0) / (365.0 * 86400.0)
        tv = 0.8 * s * sigma * math.sqrt(years)
        call = max(s - k, 0.0) + tv / 2.0
        put = max(k - s, 0.0) + tv / 2.0
        call = round(max(call, 0.05) / 0.05) * 0.05
        put = round(max(put, 0.05) / 0.05) * 0.05
        ts = when.timestamp()
        ce = f"{underlying}{code}{int(k)}CE"
        pe = f"{underlying}{code}{int(k)}PE"
        return StraddleQuote(
            call=Quote(ce, exchange, round(call, 2), round(call - spread / 2, 2), round(call + spread / 2, 2), ts),
            put=Quote(pe, exchange, round(put, 2), round(put - spread / 2, 2), round(put + spread / 2, 2), ts),
            strike=k,
            expiry=expiry,
        )

    return quote


def synthetic_index_bars(day: date, seed: int = 0, start_level: float = 23400.0, minutes: int = 375, std_pct: float = 0.0426) -> list[Bar]:
    """A deterministic random walk of 1 minute bars from 09:15 with the measured 1m return std."""
    rng = np.random.default_rng(seed + day.toordinal())
    level = start_level
    bars: list[Bar] = []
    t = datetime.combine(day, time(9, 15), IST)
    for _ in range(minutes):
        o = level
        path = o * np.exp(np.cumsum(rng.normal(0.0, std_pct / 100.0 / 2.0, 4)))
        c = float(path[-1])
        h = float(max(o, path.max()))
        lo = float(min(o, path.min()))
        bars.append(Bar(t, round(o, 2), round(h, 2), round(lo, 2), round(c, 2), 0.0))
        level = c
        t += timedelta(minutes=1)
    return bars


def aggregate_bars(bars1m: Sequence[Bar], interval_min: int) -> list[Bar]:
    """Aggregate 1 minute bars into `interval_min` bars aligned to the day's first bar."""
    out: list[Bar] = []
    bucket: list[Bar] = []
    bucket_start: datetime | None = None
    for b in sorted(bars1m, key=lambda x: x.timestamp):
        if bucket_start is None or b.timestamp.date() != bucket_start.date() or (b.timestamp - bucket_start) >= timedelta(minutes=interval_min):
            if bucket:
                out.append(_merge(bucket, bucket_start))
            bucket, bucket_start = [], b.timestamp
        bucket.append(b)
    if bucket and bucket_start is not None:
        out.append(_merge(bucket, bucket_start))
    return out


def _merge(bucket: list[Bar], start: datetime) -> Bar:
    return Bar(
        timestamp=start,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
    )


def next_weekly_expiry(day: date, weekday: int = 1) -> date:
    """The next Tuesday on or after `day` (NIFTY weekly expiry weekday)."""
    delta = (weekday - day.weekday()) % 7
    return day + timedelta(days=delta)
