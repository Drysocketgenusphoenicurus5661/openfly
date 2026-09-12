"""Connectome data status and the background data jobs: prepare, record, backfill chains.

One job runs at a time in a daemon thread and reports through ``data.progress``
events: ``{"stage", "file", "done_bytes", "total_bytes", "message"}``.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import date
from typing import TYPE_CHECKING, Any

from openfly.connectome import download as dl
from openfly.connectome import verify as vf
from openfly.connectome.compile import compile_graph, read_manifest
from openfly.connectome.sources import SOURCES

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")

STAGES = ("missing", "downloading", "downloaded", "compiling", "compiled", "error")


class DataService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self._job: threading.Thread | None = None
        self._job_name: str | None = None
        self._stage: str | None = None
        self._progress: dict[str, Any] | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._verify_cache: dict[str, Any] = {}
        self._verify_thread: threading.Thread | None = None
        self._last_report: dict[str, Any] | None = None

    # ------------------------------------------------------------ status

    def _signature(self) -> tuple:
        parts = []
        for src in SOURCES:
            path = src.path(self.ctx.paths.malecns)
            parts.append((path.exists(), path.stat().st_size if path.exists() else 0, path.stat().st_mtime_ns if path.exists() else 0))
        graph = self.ctx.paths.graph
        parts.append((graph.exists(), graph.stat().st_size if graph.exists() else 0, graph.stat().st_mtime_ns if graph.exists() else 0))
        return tuple(parts)

    def _ensure_verification(self, signature: tuple) -> dict[str, Any] | None:
        with self._lock:
            cached = self._verify_cache.get("result") if self._verify_cache.get("signature") == signature else None
            running = self._verify_thread is not None and self._verify_thread.is_alive()
            if cached is not None or running:
                return cached
            self._verify_thread = threading.Thread(target=self._verify, args=(signature,), name="verify-data", daemon=True)
            self._verify_thread.start()
        return None

    def _verify(self, signature: tuple) -> None:
        try:
            result = {
                "sources": {row["name"]: bool(row["sha256_ok"]) for row in vf.verify_sources(self.ctx.paths.malecns)},
                "graph": bool(vf.verify_graph(self.ctx.paths.graph)["ok"]),
            }
        except Exception as exc:
            logger.info("verification failed: %s", exc)
            result = {"sources": {}, "graph": False, "error": str(exc)}
        with self._lock:
            self._verify_cache = {"signature": signature, "result": result}

    def status(self) -> dict[str, Any]:
        paths = self.ctx.paths
        signature = self._signature()
        verified = self._ensure_verification(signature)
        files = []
        for src in SOURCES:
            path = src.path(paths.malecns)
            present = path.exists()
            size = path.stat().st_size if present else 0
            files.append(
                {
                    "name": src.name,
                    "present": present,
                    "bytes": size,
                    "expected_bytes": src.bytes,
                    "verified": bool(verified["sources"].get(src.name)) if verified else (present and size == src.bytes),
                }
            )
        manifest = read_manifest(paths.graph) if paths.graph.exists() else None
        counts = (manifest or {}).get("counts", {})
        graph = {
            "present": paths.graph.exists(),
            "neurons": int(counts.get("neurons", 0) or 0),
            "edges": int(counts.get("edges", 0) or 0),
            "verified": bool(verified["graph"]) if verified else (paths.graph.exists() and vf.lock_path(paths.graph).exists()),
            "path": self.ctx.relative(paths.graph),
        }
        with self._lock:
            job_stage = self._stage if self._job is not None and self._job.is_alive() else None
            progress = dict(self._progress) if self._progress else None
            error = self._error
            job = self._job_name if self._job is not None and self._job.is_alive() else None
        if job_stage in ("downloading", "compiling"):
            stage = job_stage
        elif error and not graph["present"]:
            stage = "error"
        elif graph["present"] and (manifest is not None):
            stage = "compiled"
        elif all(f["present"] for f in files):
            stage = "downloaded"
        else:
            stage = "missing"
        return {
            "stage": stage,
            "files": files,
            "graph": graph,
            "progress": progress,
            "error": error,
            "job": job,
            "verification": "done" if verified else "pending",
            "last_report": self._last_report,
        }

    def summary(self) -> dict[str, Any]:
        paths = self.ctx.paths
        manifest = read_manifest(paths.graph) if paths.graph.exists() else None
        counts = (manifest or {}).get("counts", {})
        return {
            "ready": paths.graph.exists() and manifest is not None,
            "neurons": int(counts.get("neurons", 0) or 0),
            "edges": int(counts.get("edges", 0) or 0),
            "graph_path": self.ctx.relative(paths.graph) if paths.graph.exists() else None,
        }

    # -------------------------------------------------------------- jobs

    @property
    def busy(self) -> str | None:
        with self._lock:
            if self._job is not None and self._job.is_alive():
                return self._job_name
        return None

    def _start(self, name: str, target, *args: Any) -> None:
        with self._lock:
            if self._job is not None and self._job.is_alive():
                raise RuntimeError(f"a data job is already running ({self._job_name})")
            self._job_name = name
            self._error = None
            self._progress = {"stage": name, "message": "starting"}
            self._job = threading.Thread(target=self._guard, args=(name, target, *args), name=f"data-{name}", daemon=True)
            self._job.start()

    def _guard(self, name: str, target, *args: Any) -> None:
        try:
            target(*args)
        except Exception as exc:
            logger.error("data job %s failed: %s\n%s", name, exc, traceback.format_exc())
            with self._lock:
                self._error = str(exc)
                self._stage = "error"
            self._emit(name, message=f"{name} failed: {exc}", error=str(exc))

    def _emit(self, stage: str, **fields: Any) -> None:
        progress = {"stage": stage, **fields}
        with self._lock:
            self._progress = progress
        self.ctx.bus.publish("data.progress", progress)

    def prepare(self) -> None:
        self._start("prepare", self._prepare)

    def _prepare(self) -> None:
        paths = self.ctx.paths
        paths.ensure()
        for src in SOURCES:
            if dl.is_verified(src, paths.malecns):
                self._emit("download", file=src.name, done_bytes=src.bytes, total_bytes=src.bytes, message=f"{src.name} present and verified")
                continue
            with self._lock:
                self._stage = "downloading"
            def progress(done: int, total: int, name: str = src.name, last: dict[str, float] = {"t": 0.0}) -> None:  # noqa: B006
                now = time.monotonic()
                if now - last["t"] > 1.0 or done >= total:
                    last["t"] = now
                    self._emit("download", file=name, done_bytes=int(done), total_bytes=int(total), message=f"downloading {name}")

            dl.download_source(src, paths.malecns, progress=progress)
            self._emit("download", file=src.name, done_bytes=src.bytes, total_bytes=src.bytes, message=f"{src.name} downloaded and verified")
        graph_ok = vf.verify_graph(paths.graph)["ok"]
        if not graph_ok:
            with self._lock:
                self._stage = "compiling"
            self._emit("compile", message="compiling the graph")
            compile_graph(progress=lambda msg: self._emit("compile", message=str(msg)))
        report = vf.verify(paths.malecns, paths.graph)
        with self._lock:
            self._stage = "compiled" if report["ok"] else "error"
            self._verify_cache = {}
            if not report["ok"]:
                self._error = "verification failed after prepare"
        self._emit("done", message="connectome ready" if report["ok"] else "verification failed", ok=bool(report["ok"]))

    def record(self, day: date | None = None, force: bool = False, strikes_each_side: int | None = None) -> None:
        self._start("record", self._record, day, force, strikes_each_side)

    def _recorder(self):
        from openfly.market.recorder import Recorder

        client = self.ctx.client()
        return Recorder(client, store=self.ctx.settings()), client

    def _record(self, day: date | None, force: bool, strikes_each_side: int | None) -> None:
        recorder, client = self._recorder()
        try:
            self._emit("record", message=f"recording {day.isoformat() if day else 'today'}")
            report = recorder.record(
                day, strikes_each_side=strikes_each_side, force=force, progress=lambda text: self._emit("record", message=str(text))
            )
        finally:
            _close(client)
        self._finish("record", [report])

    def backfill(self, days: int = 30, listing_lead_days: int = 21, strikes_each_side: int | None = None) -> None:
        self._start("backfill", self._backfill, int(days), int(listing_lead_days), strikes_each_side)

    def _backfill(self, days: int, lead: int, strikes_each_side: int | None) -> None:
        recorder, client = self._recorder()
        try:
            self._emit("backfill", message=f"backfilling chains for the last {days} days")
            reports = recorder.backfill(
                days=days, listing_lead_days=lead, progress=lambda text: self._emit("backfill", message=str(text)), strikes_each_side=strikes_each_side
            )
        finally:
            _close(client)
        self._finish("backfill", reports)

    def _finish(self, name: str, reports: list) -> None:
        rows = sum(getattr(r, "rows", 0) for r in reports)
        symbols = sum(getattr(r, "symbols", 0) for r in reports)
        declined = next((r.declined for r in reports if getattr(r, "declined", None)), None)
        summary = {
            "job": name,
            "days": len(reports),
            "symbols": symbols,
            "rows": rows,
            "declined": declined,
            "reports": [r.to_dict() for r in reports[-5:]],
        }
        self._last_report = summary
        self.ctx.market.chain_coverage(refresh=True)
        self._emit(
            "done",
            job=name,
            message=(f"{name} finished: {len(reports)} day(s), {symbols} symbol-days, {rows} rows" + (f"; broker declined: {declined}" if declined else "")),
            report=summary,
        )

    def shutdown(self) -> None:
        return None


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
