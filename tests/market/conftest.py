"""Shared fixtures for the market tests: mock transports, temp paths, bar builders."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import pytest

from openfly.config import DEFAULT_SETTINGS, Paths
from openfly.market.client import IST, OpenAlgoClient
from openfly.market.store import BarStore

TEST_KEY = "k-test-not-a-real-key"


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> OpenAlgoClient:
    """Client whose HTTP goes to ``handler``; sleeps are recorded, never slept."""
    transport = httpx.MockTransport(handler)
    sleeps: list[float] = []
    client = OpenAlgoClient(
        "http://test.local",
        TEST_KEY,
        transport=transport,
        sleep=sleeps.append,
        rng=lambda: 1.0,
        **kwargs,
    )
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def ok(data=None, **extra) -> httpx.Response:
    payload = {"status": "success", **extra}
    if data is not None:
        payload["data"] = data
    return httpx.Response(200, json=payload)


def error(message: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"status": "error", "message": message})


def minute_bars(
    day: date,
    start_price: float = 100.0,
    step: float = 0.1,
    first: tuple[int, int] = (9, 15),
    last: tuple[int, int] = (15, 29),
    volume: float = 10.0,
) -> pd.DataFrame:
    """Synthetic 1 minute bars for one day, prices rising ``step`` per minute."""
    begin = datetime(day.year, day.month, day.day, *first, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, *last, tzinfo=IST)
    stamps = pd.date_range(begin, end, freq="1min")
    n = len(stamps)
    opens = [start_price + i * step for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": stamps.as_unit("us"),
            "open": opens,
            "high": [o + 0.5 for o in opens],
            "low": [o - 0.5 for o in opens],
            "close": [o + 0.1 for o in opens],
            "volume": [volume] * n,
            "oi": [1000.0] * n,
        }
    )


def rows_from_frame(frame: pd.DataFrame, epoch: bool = False) -> list[dict]:
    """OpenAlgo-shaped history rows (ISO strings or epoch seconds), alphabetical keys."""
    rows = []
    for r in frame.itertuples(index=False):
        ts = int(r.timestamp.timestamp()) if epoch else r.timestamp.strftime("%Y-%m-%d %H:%M:%S%z")
        if not epoch:
            ts = ts[:-2] + ":" + ts[-2:]
        rows.append(
            {
                "close": r.close,
                "high": r.high,
                "low": r.low,
                "oi": r.oi,
                "open": r.open,
                "timestamp": ts,
                "volume": r.volume,
            }
        )
    return rows


@pytest.fixture
def settings() -> dict:
    return copy.deepcopy(DEFAULT_SETTINGS)


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(root=tmp_path, data=tmp_path / "data", runs=tmp_path / "runs")
    p.ensure()
    return p


@pytest.fixture
def bar_store(paths) -> BarStore:
    return BarStore(paths.market_db, history_root=paths.history)


def weekdays_between(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
