"""Experiments: list and read result.json files, launch `openfly experiment run` as a subprocess.

The subprocess writes ``runs/experiments/<id>/result.json`` as it goes; a
watcher thread polls that file and publishes ``experiment.progress`` events.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfly.experiments.runner import list_experiments, load_experiment, new_experiment_id

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")

STATE_MAP = {"created": "queued", "running": "running", "done": "done", "error": "failed", "failed": "failed", "cancelled": "cancelled"}


def public_state(state: Any) -> str:
    return STATE_MAP.get(str(state or "").lower(), "queued")


def _window(value: Any) -> str:
    if isinstance(value, list | tuple) and len(value) == 2:
        return f"{value[0]}:{value[1]}"
    text = str(value)
    if ":" not in text:
        raise ValueError(f"window must be [start, end] or 'start:end', got {value!r}")
    return text


class ExperimentService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self._processes: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _summary(self, result: dict[str, Any]) -> dict[str, Any]:
        progress = result.get("progress") or {}
        return {
            "id": result.get("id"),
            "name": result.get("name"),
            "state": public_state(result.get("state")),
            "created_at": result.get("created_at"),
            "finished_at": result.get("finished_at"),
            "config": result.get("config"),
            "progress": {"done": int(progress.get("done") or 0), "total": int(progress.get("total") or 0), "stage": progress.get("stage")} if progress else None,
            "passed": result.get("passed"),
            "verdict": result.get("verdict"),
        }

    def list(self) -> list[dict[str, Any]]:
        rows = [self._summary(r) for r in list_experiments(self.ctx.paths)]
        seen = {r["id"] for r in rows}
        with self._lock:
            pending = [(eid, info) for eid, info in self._processes.items() if eid not in seen]
        for eid, info in pending:
            rows.append(
                {
                    "id": eid,
                    "name": info["config"].get("name"),
                    "state": "queued" if info["process"].poll() is None else "failed",
                    "created_at": info["created_at"],
                    "finished_at": None,
                    "config": info["config"],
                    "progress": {"done": 0, "total": 0, "stage": "starting"},
                    "passed": None,
                    "verdict": None,
                }
            )
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        if not _safe_id(experiment_id):
            return None
        try:
            result = load_experiment(experiment_id, self.ctx.paths)
        except FileNotFoundError:
            with self._lock:
                info = self._processes.get(experiment_id)
            if info is None:
                return None
            return {
                "id": experiment_id,
                "name": info["config"].get("name"),
                "config": info["config"],
                "state": "queued" if info["process"].poll() is None else "failed",
                "created_at": info["created_at"],
                "progress": {"done": 0, "total": 0, "stage": "starting"},
                "metrics": {},
                "controls": {},
                "curves": {},
                "passed": None,
                "verdict": "starting",
            }
        result = dict(result)
        result["state"] = public_state(result.get("state"))
        progress = result.get("progress") or {}
        result["progress"] = {"done": int(progress.get("done") or 0), "total": int(progress.get("total") or 0), "stage": progress.get("stage")} if progress else None
        return result

    def passed_ids(self) -> list[str]:
        return [r["id"] for r in list_experiments(self.ctx.paths) if r.get("passed") is True and public_state(r.get("state")) == "done"]

    def start(self, config: dict[str, Any]) -> str:
        cfg = dict(config or {})
        readout = str(cfg.get("readout") or "reservoir").lower()
        plastic = bool(cfg.get("plastic", False))
        if readout == "plastic":
            readout, plastic = "reservoir", True
        if readout not in ("fixed", "reservoir"):
            raise ValueError(f"unknown readout {readout!r}; use fixed or reservoir")
        encoder = str(cfg.get("encoder") or "B").upper()
        if encoder not in ("A", "B", "C"):
            raise ValueError(f"unknown encoder {encoder!r}; use A, B or C")
        for key in ("train", "validation", "test"):
            if key not in cfg:
                raise ValueError(f"config needs a {key} window [start, end]")
        neural_ms = float(cfg.get("neural_ms") or self.ctx.settings().get("neural", {}).get("neural_ms", 100.0))
        normalized = {**cfg, "encoder": encoder, "readout": readout, "plastic": plastic, "neural_ms": neural_ms}
        experiment_id = new_experiment_id(normalized)
        cmd = [
            sys.executable,
            "-m",
            "openfly.cli",
            "experiment",
            "run",
            "--id",
            experiment_id,
            "--encoder",
            encoder,
            "--readout",
            readout,
            "--neural-ms",
            str(neural_ms),
            "--train",
            _window(cfg["train"]),
            "--validation",
            _window(cfg["validation"]),
            "--test",
            _window(cfg["test"]),
        ]
        if plastic:
            cmd.append("--plastic")
        if cfg.get("limit_days") is not None:
            cmd += ["--limit-days", str(int(cfg["limit_days"]))]
        if cfg.get("interval"):
            cmd += ["--interval", str(cfg["interval"])]
        if cfg.get("horizon_minutes") is not None:
            cmd += ["--horizon", str(int(cfg["horizon_minutes"]))]
        if cfg.get("seed") is not None:
            cmd += ["--seed", str(int(cfg["seed"]))]
        if cfg.get("name"):
            cmd += ["--name", str(cfg["name"])]
        if cfg.get("fake_brain"):
            cmd.append("--fake-brain")
        directory = self.ctx.paths.experiments / experiment_id
        directory.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("OPENFLY_DATA", str(self.ctx.paths.data))
        env.setdefault("OPENFLY_RUNS", str(self.ctx.paths.runs))
        log = open(directory / "launch.log", "a", encoding="utf-8")
        process = subprocess.Popen(cmd, cwd=str(self.ctx.root), stdout=log, stderr=subprocess.STDOUT, env=env)
        info = {"process": process, "config": normalized, "created_at": self.ctx.now().isoformat(), "log": log}
        with self._lock:
            self._processes[experiment_id] = info
        self.ctx.bus.publish("experiment.progress", {"id": experiment_id, "state": "queued", "stage": "starting", "done": 0, "total": 0})
        threading.Thread(target=self._watch, args=(experiment_id, info), name=f"experiment-{experiment_id}", daemon=True).start()
        return experiment_id

    def _watch(self, experiment_id: str, info: dict[str, Any]) -> None:
        process: subprocess.Popen = info["process"]
        result_path = self.ctx.paths.experiments / experiment_id / "result.json"
        last: tuple | None = None
        while True:
            code = process.poll()
            progress = self._read_progress(result_path)
            if progress is not None:
                key = (progress.get("state"), progress.get("stage"), progress.get("done"), progress.get("total"))
                if key != last:
                    last = key
                    self.ctx.bus.publish("experiment.progress", {"id": experiment_id, **progress})
            if code is not None:
                break
            time.sleep(2.0)
        final = self._read_progress(result_path) or {"state": "failed" if code else "done", "stage": "exit", "done": 0, "total": 0}
        if code and final.get("state") not in ("failed",):
            final["state"] = "failed"
        final["exit_code"] = code
        self.ctx.bus.publish("experiment.progress", {"id": experiment_id, **final})
        try:
            info["log"].close()
        except OSError:
            pass
        with self._lock:
            self._processes.pop(experiment_id, None)

    @staticmethod
    def _read_progress(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        progress = result.get("progress") or {}
        return {
            "state": public_state(result.get("state")),
            "stage": progress.get("stage"),
            "done": int(progress.get("done") or 0),
            "total": int(progress.get("total") or 0),
            "passed": result.get("passed"),
            "verdict": result.get("verdict") if result.get("state") in ("done", "error") else None,
        }

    def shutdown(self) -> None:
        return None


def _safe_id(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")
