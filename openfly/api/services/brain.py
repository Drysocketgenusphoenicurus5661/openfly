"""Brain introspection for the API without loading a Brain in-process.

Circuits (population sizes) are computed once from the compiled graph's
metadata arrays and cached in ``data/circuits.json``. The last observation
comes from the running worker's ``state.json`` or the latest replay step.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")


class BrainService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self._circuits: dict[str, Any] | None = None
        self._lock = threading.Lock()

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

    def state(self) -> dict[str, Any] | None:
        """Last observation of the running worker, else of the active or latest replay."""
        worker_status = self.ctx.worker.status()
        step = None
        source = None
        replay_id = None
        if worker_status["state"] in ("running", "starting", "halted"):
            step = self.ctx.worker.last_step()
            source = "worker"
        if step is None:
            step, replay_id = self.ctx.replays.last_step()
            source = "replay" if step is not None else None
        if step is None:
            step = self.ctx.worker.last_step()
            source = "worker" if step is not None else None
        if step is None:
            return None
        technical = step.get("technical") or {}
        return {
            "observed_at": step.get("t"),
            "neural_ms": technical.get("neural_ms"),
            "sim_ms": technical.get("sim_ms"),
            "compute_seconds": step.get("compute_seconds"),
            "rates_hz": step.get("rates_hz") or {},
            "fixed_decoder": step.get("fixed_decoder") or {},
            "prediction": step.get("prediction"),
            "stimulus_hash": step.get("stimulus_hash"),
            "action": step.get("action"),
            "narrative": step.get("narrative"),
            "premium_source": step.get("premium_source"),
            "source": source,
            "replay_id": replay_id,
            "step_i": step.get("i"),
            "plastic": technical.get("plastic"),
        }

    def stimulus_png(self) -> Path | None:
        state = self.state()
        if state is None:
            return None
        replay_id = state.get("replay_id")
        if replay_id and state.get("step_i") is not None:
            path = self.ctx.replays.png_path(replay_id, int(state["step_i"]))
            if path is not None:
                return path
            item = self.ctx.replays.get(replay_id)
            if item:
                for step in reversed(item.get("steps") or []):
                    png = step.get("stimulus_png")
                    if png:
                        candidate = self.ctx.replays.png_path(replay_id, int(step.get("i", 0)))
                        if candidate is not None:
                            return candidate
        return None
