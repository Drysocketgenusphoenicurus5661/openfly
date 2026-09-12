"""Zero-config settings and paths.

Everything the user can change lives in one SQLite database under `data/`.
There is no required .env. Environment variables are optional overrides for
automation (OPENALGO_HOST, OPENALGO_WS_URL, OPENALGO_API_KEY, OPENFLY_LIVE,
OPENFLY_DATA, OPENFLY_RUNS).

Usage:

    from openfly.config import PATHS, SettingsStore
    store = SettingsStore()          # opens data/openfly.db, creates tables
    s = store.get()                  # merged dict: defaults <- stored <- env
    store.set({"strategy": {"lots": 2}})
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _dir(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    path = Path(value).expanduser() if value else default
    if not path.is_absolute():
        path = (_ROOT / path).resolve()
    return path


@dataclass(frozen=True)
class Paths:
    root: Path
    data: Path
    runs: Path

    @property
    def malecns(self) -> Path:
        return self.data / "malecns"

    @property
    def graph(self) -> Path:
        return self.data / "graph.npz"

    @property
    def history(self) -> Path:
        return self.data / "history"

    @property
    def features(self) -> Path:
        return self.data / "features"

    @property
    def market_db(self) -> Path:
        """DuckDB file holding every bar ever fetched from the broker (bars, coverage, chains)."""
        return self.data / "market.duckdb"

    @property
    def db(self) -> Path:
        return self.data / "openfly.db"

    @property
    def experiments(self) -> Path:
        return self.runs / "experiments"

    @property
    def replays(self) -> Path:
        return self.runs / "replays"

    @property
    def frontend_dist(self) -> Path:
        return self.root / "frontend" / "dist"

    def ensure(self) -> None:
        for p in (self.data, self.runs, self.malecns, self.history, self.features, self.experiments, self.replays):
            p.mkdir(parents=True, exist_ok=True)


PATHS = Paths(root=_ROOT, data=_dir("OPENFLY_DATA", _ROOT / "data"), runs=_dir("OPENFLY_RUNS", _ROOT / "runs"))


def history_path(exchange: str, symbol: str, interval: str, paths: Paths = PATHS) -> Path:
    """Parquet location used only for importing and exporting bars.

    The live store is DuckDB at PATHS.market_db; parquet files under data/history are
    imported into it on first open and can be exported for sharing.
    Columns: timestamp (tz-aware Asia/Kolkata), open, high, low, close, volume, oi.
    """
    return paths.history / exchange / symbol / f"{interval}.parquet"


DEFAULT_SETTINGS: dict[str, Any] = {
    "strategy": {
        "underlying": "NIFTY",
        "index_exchange": "NSE_INDEX",
        "options_exchange": "NFO",
        "vix_symbol": "INDIAVIX",
        "lot_size": 65,
        "lots": 1,
        "product": "NRML",
        "strategy_tag": "openfly",
        "leg_stop_pct": 30.0,
        "leg_stop_mode": "broker",
        "on_leg_stop": "hold_other",
        "combined_stop_enabled": True,
        "stop_pct": 25.0,
        "target_pct": 40.0,
        "lock_after_pct": 15.0,
        "trade_start": "09:20",
        "last_entry": "14:30",
        "square_off": "15:15",
        "square_off_lead_seconds": 30,
        "max_entries_per_day": 10,
        "reentry_cooldown_minutes": 5,
        "vix_ceiling": 20.0,
        "min_days_to_expiry": 0,
        "recenter": False,
        "strike_step": 50,
        "chain_strikes_each_side": 12,
        "chain_record_interval": "1m",
    },
    "risk": {
        "capital": 1000000.0,
        "daily_loss_limit_pct": 1.0,
        "risk_budget_pct": 1.0,
        "max_lots": 3,
        "spread_pct_max": 0.5,
        "quote_max_age_s": 5.0,
        "index_move_veto_pct": 0.3,
    },
    "neural": {
        "neural_ms": 100.0,
        "live_interval": "5m",
        "replay_interval": "1m",
        "bar_interval": "5m",
        "encoder": "B",
        "readout": "reservoir",
        "plastic": False,
        "tau": 0.1,
        "horizon_minutes": 60,
        "half_saturation": 0.5,
        "r8_ame12_excitatory": True,
    },
    "costs": {
        # Zerodha NFO options calculator, verified 2026-09-12: buy 100, sell 100, qty 400
        # gives brokerage 40, STT 60, exchange 28.42, GST 12.33, SEBI 0.08, stamp 1, total 141.83.
        "brokerage_per_order": 20.0,
        "brokerage_pct": 0.0,
        "stt_sell_pct": 0.15,
        "exchange_pct": 0.03553,
        "sebi_pct": 0.0001,
        "stamp_buy_pct": 0.003,
        "stamp_round_to_rupee": True,
        "gst_pct": 18.0,
        "gst_on_sebi": True,
    },
    "openalgo": {
        "host": "http://127.0.0.1:5000",
        "ws_url": "ws://127.0.0.1:8765",
        "api_key": "",
    },
    "ui": {
        "port": 8000,
        "open_browser": True,
    },
}

_ENV_OVERRIDES = {
    ("openalgo", "host"): "OPENALGO_HOST",
    ("openalgo", "ws_url"): "OPENALGO_WS_URL",
    ("openalgo", "api_key"): "OPENALGO_API_KEY",
}

SECRET_KEYS = {("openalgo", "api_key")}


def deep_merge(base: dict, update: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class SettingsStore:
    """Settings persisted in SQLite. Thread-safe, one row per top-level section."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else PATHS.db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS settings (section TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def stored(self) -> dict[str, Any]:
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT section, value FROM settings").fetchall()
        return {section: json.loads(value) for section, value in rows}

    def get(self) -> dict[str, Any]:
        merged = deep_merge(DEFAULT_SETTINGS, self.stored())
        for (section, key), env in _ENV_OVERRIDES.items():
            value = os.environ.get(env)
            if value:
                merged.setdefault(section, {})[key] = value
        return merged

    def set(self, update: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(update, dict):
            raise TypeError("settings update must be a dict of sections")
        current = deep_merge(DEFAULT_SETTINGS, self.stored())
        merged = deep_merge(current, update)
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for section in update:
                con.execute(
                    "INSERT INTO settings(section, value) VALUES(?, ?) "
                    "ON CONFLICT(section) DO UPDATE SET value=excluded.value",
                    (section, json.dumps(merged[section])),
                )
            con.execute("COMMIT")
        return self.get()

    def public(self) -> dict[str, Any]:
        """Settings with secrets replaced by a presence flag, safe to return from the API."""
        s = self.get()
        for section, key in SECRET_KEYS:
            value = s.get(section, {}).pop(key, "")
            s.setdefault(section, {})[f"{key}_set"] = bool(value)
        return s

    @property
    def live_enabled(self) -> bool:
        return os.environ.get("OPENFLY_LIVE") == "I_ACCEPT_REAL_TRADES"
