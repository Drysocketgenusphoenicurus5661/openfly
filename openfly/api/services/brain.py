"""Brain introspection for the API without loading a Brain in-process.

Circuits (population sizes) are computed once from the compiled graph's
metadata arrays and cached in ``data/circuits.json``. The last observation
and its history come from the most recently updated source: a running
replay, then the worker (its ``state.json`` and ``events.jsonl``), then the
newest finished replay's trace.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfly.api.events import read_jsonl

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")

HISTORY_LENGTH = 30


def _is_observation(step: dict[str, Any]) -> bool:
    return step.get("trigger", "observation") == "observation"


def _history(steps: list[dict[str, Any]], limit: int = HISTORY_LENGTH) -> list[dict[str, Any]]:
    observations = [s for s in steps if _is_observation(s)]
    return [{"t": s.get("t"), "rates_hz": s.get("rates_hz") or {}, "action": s.get("action")} for s in observations[-limit:]]


class BrainService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self._circuits: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._trace_cache: tuple[tuple[str, int], list[dict[str, Any]]] | None = None

    # ------------------------------------------------------------ circuits

    def _cache_path(self) -> Path:
        return self.ctx.paths.data / "circuits.json"

    def _graph_signature(self) -> dict[str, Any] | None:
        graph = self.ctx.paths.graph
        if not graph.exists():
            return None
        stat = graph.stat()
        return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def circuits(self) -> dict[str, Any]:
        signature = self._graph_signature()
        if signature is None:
            raise FileNotFoundError(f"no compiled graph at {self.ctx.relative(self.ctx.paths.graph)}; run `openfly prepare` or POST /api/data/prepare")
        with self._lock:
            if self._circuits is not None and self._circuits.get("graph") == signature:
                return {k: v for k, v in self._circuits.items() if k != "graph"}
        cached = self._read_cache()
        if cached is not None and cached.get("graph") == signature:
            with self._lock:
                self._circuits = cached
            return {k: v for k, v in cached.items() if k != "graph"}
        from openfly.neural.brain import build_populations, load_graph

        g = load_graph(self.ctx.paths.graph)
        populations = build_populations(g)
        n = int(len(g["ptr"]) - 1)
        edges = int(len(g["post"]))
        payload = {
            "n": n,
            "edges": edges,
            "populations": [{"name": name, "size": int(len(idx))} for name, idx in populations.items()],
            "graph": signature,
            "graph_path": self.ctx.relative(self.ctx.paths.graph),
        }
        del g
        with self._lock:
            self._circuits = payload
        try:
            self._cache_path().write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.info("circuits cache not written: %s", exc)
        return {k: v for k, v in payload.items() if k != "graph"}

    def _read_cache(self) -> dict[str, Any] | None:
        path = self._cache_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "populations" in data else None

    # ------------------------------------------------------------- sources

    def _trace_steps(self, replay_id: str) -> list[dict[str, Any]]:
        path = self.ctx.paths.replays / replay_id / "trace.json"
        if not path.exists():
            return []
        key = (replay_id, path.stat().st_mtime_ns)
        with self._lock:
            if self._trace_cache is not None and self._trace_cache[0] == key:
                return self._trace_cache[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        steps = list(data.get("steps") or []) if isinstance(data, dict) else []
        with self._lock:
            self._trace_cache = (key, steps)
        return steps

    def _worker_steps(self) -> list[dict[str, Any]]:
        run_dir = self.ctx.worker.current_run_dir()
        if run_dir is None:
            return []
        steps = []
        for record in read_jsonl(run_dir / "events.jsonl"):
            if record.get("type") == "step":
                steps.append({k: v for k, v in record.items() if k != "type"})
        return steps

    def _worker_source(self) -> dict[str, Any] | None:
        last = self.ctx.worker.last_step()
        steps = self._worker_steps()
        if last is None:
            observations = [s for s in steps if _is_observation(s)]
            last = observations[-1] if observations else None
        if last is None:
            return None
        return {"source": "worker", "replay_id": None, "step": last, "steps": steps, "pngs_on_disk": False}

    def _replay_source(self, replay_id: str, steps: list[dict[str, Any]]) -> dict[str, Any] | None:
        observations = [s for s in steps if _is_observation(s)]
        if not observations:
            return None
        pngs = (self.ctx.paths.replays / replay_id / "stimulus").exists()
        return {"source": "replay", "replay_id": replay_id, "step": observations[-1], "steps": steps, "pngs_on_disk": pngs}

    def _pick_source(self) -> dict[str, Any] | None:
        """A running replay, then the worker, then the newest finished replay."""
        active = self.ctx.replays.active
        if active is not None and active.get("steps"):
            picked = self._replay_source(active["id"], list(active["steps"]))
            if picked is not None:
                return picked
        worker_status = self.ctx.worker.status()
        worker = self._worker_source() if worker_status["state"] in ("running", "starting", "halted") else None
        if worker is not None:
            return worker
        for item in self.ctx.replays.list():
            if item.get("state") != "done":
                continue
            picked = self._replay_source(item["id"], self._trace_steps(item["id"]))
            if picked is not None:
                return picked
        return self._worker_source()

    # --------------------------------------------------------------- state

    def state(self) -> dict[str, Any] | None:
        picked = self._pick_source()
        if picked is None:
            return None
        step = picked["step"]
        technical = step.get("technical") or {}
        replay_id = picked["replay_id"]
        index = step.get("i")
        stimulus_hash = step.get("stimulus_hash")
        if picked["pngs_on_disk"] and replay_id and index is not None and self.ctx.replays.png_path(replay_id, int(index)) is not None:
            png_url = self.ctx.replays.url_for_png(replay_id, int(index))
        else:
            png_url = "/api/brain/stimulus.png" + (f"?h={str(stimulus_hash).replace('sha256:', '')[:16]}" if stimulus_hash else "")
        return {
            "observed_at": step.get("t"),
            "neural_ms": technical.get("neural_ms"),
            "sim_ms": technical.get("sim_ms"),
            "compute_seconds": step.get("compute_seconds"),
            "rates_hz": step.get("rates_hz") or {},
            "fixed_decoder": step.get("fixed_decoder") or {},
            "prediction": step.get("prediction"),
            "stimulus_hash": stimulus_hash,
            "stimulus_png": png_url,
            "action": step.get("action"),
            "narrative": step.get("narrative"),
            "premium_source": step.get("premium_source"),
            "source": picked["source"],
            "replay_id": replay_id,
            "step": index,
            "history": _history(picked["steps"]),
            "plastic": technical.get("plastic"),
        }

    def stimulus_png(self) -> Path | None:
        """The image the current state points to: the replay step's PNG when it exists on disk."""
        picked = self._pick_source()
        if picked is None or not picked["pngs_on_disk"] or not picked["replay_id"]:
            return None
        replay_id = picked["replay_id"]
        index = picked["step"].get("i")
        if index is not None:
            path = self.ctx.replays.png_path(replay_id, int(index))
            if path is not None:
                return path
        for step in reversed(picked["steps"]):
            if step.get("stimulus_png") and step.get("i") is not None:
                path = self.ctx.replays.png_path(replay_id, int(step["i"]))
                if path is not None:
                    return path
        return None
