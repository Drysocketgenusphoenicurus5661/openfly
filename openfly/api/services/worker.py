"""The worker subprocess: start, stop, square off, and the state the API reads from its run directory.

The worker owns the brain, the engine, the ledger and the broker connection
in its own process (``openfly worker``). The API launches it, tails its
``events.jsonl`` onto the event bus and reads ``state.json`` for status. A
worker started elsewhere is recognised through its ``worker.lock``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import FileLock, Timeout

from openfly.api.events import IST, FileTailer, read_jsonl
from openfly.api.responses import (
    json_default,  # noqa: F401  (re-exported for callers serialising state)
)
from openfly.execution.ledger import Ledger

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")

WORKER_STATES = ("stopped", "starting", "running", "halted")
STOP_FILE = "STOP"
SQUAREOFF_FILE = "SQUAREOFF"
PREMIUM_LIVE = "live"

FLAT_STRADDLE: dict[str, Any] = {
    "in_position": False,
    "expiry": None,
    "strike": None,
    "lots": None,
    "legs": None,
    "entry_credit": None,
    "combined_ltp": None,
    "stop_level": None,
    "target_level": None,
    "pnl": None,
    "entered_at": None,
    "square_off_at": None,
}


def _read_json(path: Path) -> dict[str, Any] | None:
    import json

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lock_held(run_dir: Path) -> bool:
    """True when a worker process holds this run directory's lock."""
    path = run_dir / "worker.lock"
    if not path.exists():
        return False
    lock = FileLock(str(path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    except OSError:
        return False
    lock.release()
    return False


class WorkerService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self.process: subprocess.Popen | None = None
        self.run_dir: Path | None = None
        self.mode: str | None = None
        self.lots: int | None = None
        self.started_at: datetime | None = None
        self.exit_code: int | None = None
        self._tailer: FileTailer | None = None
        self._log_handle: Any = None
        self._lock = threading.Lock()
        self._last_state: str | None = None

    # ------------------------------------------------------------ discovery

    def current_run_dir(self) -> Path | None:
        if self.run_dir is not None:
            return self.run_dir
        runs = self.ctx.paths.runs
        if not runs.exists():
            return None
        candidates = [d for d in runs.iterdir() if d.is_dir() and (d.name.startswith("paper-") or d.name.startswith("live-")) and (d / "state.json").exists()]
        if not candidates:
            return None
        return max(candidates, key=lambda d: (d / "state.json").stat().st_mtime)

    def state_json(self, run_dir: Path | None = None) -> dict[str, Any] | None:
        run_dir = run_dir or self.current_run_dir()
        if run_dir is None:
            return None
        return _read_json(run_dir / "state.json")

    def process_alive(self) -> bool:
        proc = self.process
        if proc is None:
            return False
        code = proc.poll()
        if code is None:
            return True
        self.exit_code = code
        return False

    def is_running(self) -> bool:
        if self.process_alive():
            return True
        run_dir = self.current_run_dir()
        return run_dir is not None and lock_held(run_dir)

    def status(self) -> dict[str, Any]:
        run_dir = self.current_run_dir()
        state_json = self.state_json(run_dir) or {}
        worker = state_json.get("worker") or {}
        file_state = str(worker.get("state") or "stopped")
        alive = self.process_alive()
        external = run_dir is not None and self.process is None and lock_held(run_dir)
        if alive or external:
            state = file_state if file_state in WORKER_STATES and file_state != "stopped" else "starting"
        elif file_state == "halted":
            state = "halted"
        elif self.process is not None and self.exit_code not in (None, 0):
            state = "halted"
        else:
            state = "stopped"
        mode = worker.get("mode") or self.mode
        started = worker.get("started_at") or (self.started_at.isoformat() if self.started_at else None)
        payload = {
            "state": state,
            "mode": mode if mode in ("paper", "live") else None,
            "run_dir": self.ctx.relative(run_dir) if run_dir else None,
            "started_at": started,
            "last_event_at": worker.get("last_event_at"),
            "halt_reason": worker.get("halt_reason"),
            "trading_date": worker.get("trading_date"),
            "legs": worker.get("legs"),
            "strike": worker.get("strike"),
            "expiry": worker.get("expiry"),
            "steps": worker.get("steps"),
            "pid": self.process.pid if alive and self.process else None,
            "exit_code": None if alive else self.exit_code,
            "managed": self.process is not None,
        }
        if state != self._last_state:
            self._last_state = state
        return payload

    # ------------------------------------------------------------- control

    def start(self, mode: str, lots: int, run_dir: str | None, trading_date: date | None = None) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                current = self.current_run_dir()
                raise RuntimeError(f"a worker is already running in {self.ctx.relative(current) if current else 'another run directory'}")
            day = trading_date or self.ctx.now().date()
            target = Path(run_dir) if run_dir else self.ctx.paths.runs / f"{mode}-{day.isoformat()}"
            if not target.is_absolute():
                target = self.ctx.root / target
            target.mkdir(parents=True, exist_ok=True)
            for name in (STOP_FILE, SQUAREOFF_FILE):
                stale = target / name
                if stale.exists():
                    stale.unlink()
            cmd = [
                sys.executable,
                "-m",
                "openfly.cli",
                "worker",
                "--mode",
                mode,
                "--lots",
                str(int(lots)),
                "--run-dir",
                str(target),
                "--date",
                day.isoformat(),
            ]
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env.setdefault("OPENFLY_DATA", str(self.ctx.paths.data))
            env.setdefault("OPENFLY_RUNS", str(self.ctx.paths.runs))
            self._log_handle = open(target / "worker.log", "a", encoding="utf-8")
            self.process = subprocess.Popen(cmd, cwd=str(self.ctx.root), stdout=self._log_handle, stderr=subprocess.STDOUT, env=env)
            self.run_dir = target
            self.mode = mode
            self.lots = int(lots)
            self.started_at = self.ctx.now()
            self.exit_code = None
            self._start_tailer(target)
        self.ctx.bus.publish("worker", {"state": "starting", "mode": mode, "run_dir": self.ctx.relative(target), "pid": self.process.pid})
        threading.Thread(target=self._watch, args=(self.process,), name="worker-watch", daemon=True).start()
        return {"state": "starting", "run_dir": self.ctx.relative(target)}

    def _watch(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        self.exit_code = code
        state = self.status()
        self.ctx.bus.publish("worker", {**state, "exit_code": code})
        time.sleep(1.0)
        self._stop_tailer()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def attach(self) -> None:
        """Follow a worker started outside the API when its lock is held."""
        run_dir = self.current_run_dir()
        if run_dir is not None and lock_held(run_dir) and self._tailer is None:
            self.run_dir = run_dir
            self._start_tailer(run_dir, from_start=False)

    def stop(self, timeout: float = 45.0) -> dict[str, Any]:
        run_dir = self.current_run_dir()
        if run_dir is None:
            return {"ok": True, "detail": "no worker run directory"}
        was_running = self.is_running()
        (run_dir / STOP_FILE).write_text(self.ctx.now().isoformat(), encoding="utf-8")
        self.ctx.bus.publish("worker", {"state": "stopping", "run_dir": self.ctx.relative(run_dir)})
        if not was_running:
            return {"ok": True, "detail": "worker was not running; STOP file written"}
        deadline = time.monotonic() + timeout
        proc = self.process
        while time.monotonic() < deadline:
            if proc is not None:
                if proc.poll() is not None:
                    break
            elif not lock_held(run_dir):
                break
            time.sleep(0.5)
        else:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return {"ok": True, "detail": "stop requested; the worker squares off any open straddle and exits"}

    def squareoff(self) -> dict[str, Any]:
        run_dir = self.current_run_dir()
        if run_dir is None or not self.is_running():
            raise RuntimeError("no running worker to square off")
        (run_dir / SQUAREOFF_FILE).write_text(self.ctx.now().isoformat(), encoding="utf-8")
        self.ctx.bus.publish("worker", {"state": "squareoff_requested", "run_dir": self.ctx.relative(run_dir)})
        return {"ok": True, "detail": "square-off requested; the worker exits the open straddle at the next tick"}

    # -------------------------------------------------------------- events

    def _start_tailer(self, run_dir: Path, from_start: bool = True) -> None:
        self._stop_tailer()
        self._tailer = FileTailer(run_dir / "events.jsonl", self._on_record, from_start=from_start)
        self._tailer.start()

    def _stop_tailer(self) -> None:
        if self._tailer is not None:
            self._tailer.stop()
            self._tailer = None

    @staticmethod
    def event_for(record: dict[str, Any]) -> tuple[str, Any, str | None] | None:
        kind = record.get("type")
        if kind == "step":
            step = {k: v for k, v in record.items() if k != "type"}
            step.setdefault("premium_source", PREMIUM_LIVE)
            if isinstance(step.get("straddle"), dict):
                step["straddle"].setdefault("premium_source", PREMIUM_LIVE)
            trigger = step.get("trigger")
            type_ = "observation" if trigger == "observation" else "straddle"
            return type_, step, step.get("t")
        if kind == "log":
            return "log", {"message": record.get("message"), "t": record.get("t")}, record.get("t")
        return None

    def _on_record(self, record: dict[str, Any]) -> None:
        mapped = self.event_for(record)
        if mapped is None:
            return
        type_, data, at = mapped
        self.ctx.bus.publish(type_, data, at=at)

    def steps_so_far(self, run_dir: Path | None = None) -> list[dict[str, Any]]:
        """Today's step records from events.jsonl as event messages, oldest first."""
        run_dir = run_dir or self.current_run_dir()
        if run_dir is None:
            return []
        out = []
        for record in read_jsonl(run_dir / "events.jsonl"):
            mapped = self.event_for(record)
            if mapped is None or mapped[0] == "log":
                continue
            type_, data, at = mapped
            out.append({"type": type_, "at": at or "", "data": data})
        return out

    # ------------------------------------------------------------- reads

    def straddle(self) -> dict[str, Any]:
        state = self.state_json()
        snapshot = (state or {}).get("straddle")
        if not isinstance(snapshot, dict):
            return dict(FLAT_STRADDLE)
        payload = dict(snapshot)
        payload.setdefault("premium_source", PREMIUM_LIVE if payload.get("in_position") else None)
        if not payload.get("in_position") and not payload.get("legs"):
            payload["legs"] = None
        return payload

    def last_step(self) -> dict[str, Any] | None:
        state = self.state_json()
        step = (state or {}).get("last_step")
        return step if isinstance(step, dict) else None

    def intents(self, limit: int = 100) -> list[dict[str, Any]]:
        run_dir = self.current_run_dir()
        if run_dir is None or not (run_dir / "ledger.db").exists():
            return []
        ledger = Ledger(run_dir)
        try:
            return ledger.intents(limit=limit)
        finally:
            ledger.close()

    def ledger_symbols(self) -> set[str]:
        run_dir = self.current_run_dir()
        if run_dir is None or not (run_dir / "ledger.db").exists():
            return set()
        ledger = Ledger(run_dir)
        try:
            return {row["symbol"] for row in ledger.fills()}
        finally:
            ledger.close()

    def shutdown(self) -> None:
        self._stop_tailer()


def now_ist() -> datetime:
    return datetime.now(IST)
