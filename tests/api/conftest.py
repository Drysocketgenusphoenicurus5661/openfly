"""Fixtures for the API tests: temp paths, a temp settings database, a fake OpenAlgo client, TestClient."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from openfly.api.app import create_app
from openfly.config import Paths, SettingsStore
from openfly.market.client import OpenAlgoError
from openfly.market.store import BarStore

IST = ZoneInfo("Asia/Kolkata")
DAY1 = date(2026, 9, 10)
DAY2 = date(2026, 9, 11)


class FakeClient:
    """The slice of OpenAlgoClient the API touches. Never talks to a network."""

    def __init__(self, analyzer: bool = True, reachable: bool = True):
        self.analyzer = analyzer
        self.reachable = reachable
        self.toggled: list[bool] = []
        self.orders: list[dict[str, Any]] = [
            {"orderid": "1", "symbol": "NIFTY15SEP2623400CE", "exchange": "NFO", "action": "SELL", "quantity": "65", "strategy": "openfly", "order_status": "complete"},
            {"orderid": "2", "symbol": "RELIANCE", "exchange": "NSE", "action": "BUY", "quantity": "1", "strategy": "manual", "order_status": "open"},
        ]
        self.positions: list[dict[str, Any]] = [
            {"symbol": "NIFTY15SEP2623400CE", "exchange": "NFO", "product": "NRML", "quantity": "-65", "average_price": "101.2", "ltp": "95.0", "pnl": "403.0"},
            {"symbol": "RELIANCE", "exchange": "NSE", "product": "CNC", "quantity": "1", "average_price": "2900", "ltp": "2910", "pnl": "10"},
        ]
        self.closed = 0

    def _check(self) -> None:
        if not self.reachable:
            raise OpenAlgoError("analyzer: gave up after 1 tries: ConnectError", None, "analyzer")

    def analyzer_status(self) -> dict[str, Any]:
        self._check()
        return {"analyze_mode": self.analyzer, "mode": "analyze" if self.analyzer else "live", "total_logs": 0}

    def analyzer_toggle(self, mode: bool) -> dict[str, Any]:
        self._check()
        self.toggled.append(bool(mode))
        self.analyzer = bool(mode)
        return {"analyze_mode": self.analyzer, "mode": "analyze" if self.analyzer else "live"}

    def orderbook(self) -> dict[str, Any]:
        self._check()
        return {"orders": list(self.orders), "statistics": {"total_orders": len(self.orders)}}

    def positionbook(self) -> list[dict[str, Any]]:
        self._check()
        return list(self.positions)

    def _post(self, endpoint: str, body: dict | None = None) -> dict[str, Any]:
        self._check()
        if endpoint == "ping":
            return {"status": "success", "data": {"broker": "testbroker", "message": "pong"}}
        raise OpenAlgoError(f"unexpected endpoint {endpoint}", None, endpoint)

    def close(self) -> None:
        self.closed += 1


def minute_bars(day: date, start_price: float = 23300.0, step: float = 0.1) -> pd.DataFrame:
    begin = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 29, tzinfo=IST)
    stamps = pd.date_range(begin, end, freq="1min")
    opens = [start_price + i * step for i in range(len(stamps))]
    return pd.DataFrame(
        {
            "timestamp": stamps.as_unit("us"),
            "open": opens,
            "high": [o + 0.5 for o in opens],
            "low": [o - 0.5 for o in opens],
            "close": [o + 0.1 for o in opens],
            "volume": [0.0] * len(stamps),
            "oi": [0.0] * len(stamps),
        }
    )


def write_index_bars(paths: Paths, days: tuple[date, ...] = (DAY1, DAY2)) -> None:
    store = BarStore(paths.market_db, history_root=paths.history)
    for i, day in enumerate(days):
        store.upsert_bars("NSE_INDEX", "NIFTY", "1m", minute_bars(day, 23300.0 + 50 * i))


def write_trace(paths: Paths, replay_id: str = "rp_20260911_120000") -> Path:
    directory = paths.replays / replay_id
    (directory / "stimulus").mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    (directory / "stimulus" / "0.png").write_bytes(png)
    step = {
        "i": 0,
        "t": "2026-09-11T09:16:00+05:30",
        "trigger": "observation",
        "index": 23300.1,
        "vix": 12.29,
        "premium": 204.0,
        "days_to_expiry": 3.6,
        "stimulus_hash": "sha256:abc",
        "stimulus_png": "stimulus/0.png",
        "rates_hz": {"KC": 1.2, "DN": 2.1},
        "fixed_decoder": {"left_hz": 0.0, "right_hz": 0.0, "difference_hz": 0.0, "gate_spikes": 0, "side": "HOLD"},
        "prediction": {"realized_over_implied": 1.0, "confidence": 0.5, "decision": "HOLD", "tau": 0.1},
        "guard": None,
        "action": "HOLD",
        "straddle": {"in_position": False, "premium_source": "recorded"},
        "fills": [],
        "pnl_day": 0.0,
        "compute_seconds": 0.9,
        "narrative": "09:16. Holding.",
        "technical": {"encoder": "B", "readout": "reservoir", "neural_ms": 100.0, "sim_ms": 100.0},
        "premium_source": "recorded",
    }
    trace = {
        "date": "2026-09-11",
        "config": {"encoder": "B", "readout": "reservoir", "neural_ms": 100.0, "lots": 1, "stop_pct": 25.0, "target_pct": 40.0},
        "summary": {"pnl": -125.44, "trades": 1, "stop_hits": 0, "target_hits": 0, "stop_hits_leg": 0, "observations": 1, "steps": 1, "premium_source": "recorded", "synthetic_fraction": 0.0, "closed": []},
        "steps": [step],
    }
    (directory / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    return directory


def write_experiment(paths: Paths, experiment_id: str = "exp_20260912_060339_b_reservoir", state: str = "done", passed: bool = False) -> Path:
    directory = paths.experiments / experiment_id
    directory.mkdir(parents=True, exist_ok=True)
    stamp = experiment_id.split("_")[2] if experiment_id.count("_") >= 3 else "120000"
    result = {
        "id": experiment_id,
        "name": "encoder B, reservoir, frozen",
        "state": state,
        "created_at": f"2026-09-12T{stamp[:2]}:{stamp[2:4]}:{stamp[4:6]}+05:30",
        "config": {"encoder": "B", "readout": "reservoir", "plastic": False, "neural_ms": 100.0, "train": ["2025-08-08", "2026-03-31"], "validation": ["2026-04-01", "2026-06-30"], "test": ["2026-07-01", "2026-09-11"]},
        "progress": {"stage": "done", "done": 225, "total": 225},
        "metrics": {"test": {"net_pnl_per_lot": 12.0, "sharpe": 0.0, "max_drawdown": 0.0, "trades": 1, "stop_hits": 0, "target_hits": 0, "accuracy": 0.5, "accuracy_ci": [0.4, 0.6]}},
        "controls": {},
        "curves": {},
        "passed": passed,
        "verdict": "smoke run",
    }
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return directory


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path, data=tmp_path / "data", runs=tmp_path / "runs")
    p.ensure()
    return p


@pytest.fixture
def store(paths: Paths) -> SettingsStore:
    return SettingsStore(paths.db)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def app(paths: Paths, store: SettingsStore, fake_client: FakeClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENALGO_API_KEY", raising=False)
    monkeypatch.delenv("OPENFLY_LIVE", raising=False)
    return create_app(paths=paths, store=store, client_factory=lambda **overrides: fake_client)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
