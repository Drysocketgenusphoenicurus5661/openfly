"""On-demand replays: one trading day through the real brain, in a background thread.

A replay writes ``runs/replays/<id>/trace.json``, ``meta.json`` and
``stimulus/<i>.png``. Progress goes out as ``replay.progress`` events carrying
the finished step, so the websocket streams the day as it is replayed.
"""

from __future__ import annotations

import gc
import json
import logging
import threading
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfly.api.events import IST
from openfly.config import deep_merge
from openfly.execution.brokers import ReplayBroker
from openfly.execution.costs import load_cost_model

if TYPE_CHECKING:
    from openfly.api.context import ApiContext

logger = logging.getLogger("openfly.api")

REPLAY_STATES = ("queued", "running", "done", "failed")
CONFIG_KEYS = ("encoder", "readout", "neural_ms", "lots", "stop_pct", "target_pct", "experiment_id", "plastic", "interval")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    from openfly.api.responses import dumps

    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(dumps(payload))
    tmp.replace(path)


def normalize_replay_config(body: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    neural = settings.get("neural", {})
    strategy = settings.get("strategy", {})
    readout = str(body.get("readout") or neural.get("readout", "reservoir")).lower()
    plastic = bool(body.get("plastic", neural.get("plastic", False)))
    if readout == "plastic":
        readout, plastic = "reservoir", True
    if readout not in ("fixed", "reservoir"):
        raise ValueError(f"unknown readout {readout!r}; use fixed or reservoir")
    encoder = str(body.get("encoder") or neural.get("encoder", "B")).upper()
    if encoder not in ("A", "B", "C"):
        raise ValueError(f"unknown encoder {encoder!r}; use A, B or C")
    date_text = str(body.get("date") or "")
    try:
        day = date.fromisoformat(date_text[:10])
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    return {
        "date": day.isoformat(),
        "encoder": encoder,
        "readout": readout,
        "plastic": plastic,
        "neural_ms": float(body.get("neural_ms") or neural.get("neural_ms", 100.0)),
        "lots": int(body.get("lots") or strategy.get("lots", 1) or 1),
        "stop_pct": float(body.get("stop_pct") if body.get("stop_pct") is not None else strategy.get("stop_pct", 25.0)),
        "target_pct": float(body.get("target_pct") if body.get("target_pct") is not None else strategy.get("target_pct", 40.0)),
        "experiment_id": body.get("experiment_id") or None,
        "interval": str(body.get("interval") or neural.get("replay_interval", "1m")),
        "render_png": bool(body.get("render_png", True)),
    }


class ReplayService:
    def __init__(self, ctx: ApiContext):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.active: dict[str, Any] | None = None
        self._queue: list[dict[str, Any]] = []
        self._threads: list[threading.Thread] = []

    # -------------------------------------------------------------- reads

    def dates(self) -> dict[str, Any]:
        exchange, symbol = self.ctx.market.index_symbol()
        error = None
        try:
            dates = self.ctx.market.available_dates(exchange, symbol, "1m")
        except Exception as exc:
            logger.info("replay dates unavailable: %s", exc)
            dates = []
            error = str(exc)
        return {
            "dates": [d.isoformat() for d in reversed(dates)],
            "source": f"BarStore {exchange}:{symbol} 1m",
            "symbol": symbol,
            "exchange": exchange,
            "error": error,
        }

    def url_for_png(self, replay_id: str, i: int) -> str:
        return f"/api/replay/{replay_id}/stimulus/{i}.png"

    def _with_png_urls(self, replay_id: str, steps: list[dict[str, Any]], pngs_on_disk: bool) -> list[dict[str, Any]]:
        out = []
        for step in steps:
            item = dict(step)
            png = item.get("stimulus_png")
            if pngs_on_disk and png and not str(png).startswith("/api/"):
                item["stimulus_png"] = self.url_for_png(replay_id, int(item.get("i", 0)))
            elif not png:
                item["stimulus_png"] = None
            out.append(item)
        return out

    @staticmethod
    def _summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
        if not summary:
            return None
        out = dict(summary)
        out.pop("closed", None)
        out.setdefault("leg_stop_hits", out.get("stop_hits_leg", 0))
        return out

    def _item(self, directory: Path, with_steps: bool) -> dict[str, Any] | None:
        replay_id = directory.name
        with self._lock:
            active = self.active if self.active and self.active["id"] == replay_id else None
            queued = next((q for q in self._queue if q["id"] == replay_id), None)
        if active is not None:
            item = {
                "id": replay_id,
                "date": active["date"],
                "state": active["state"],
                "config": active["config"],
                "summary": self._summary(active.get("summary")),
                "progress": dict(active["progress"]),
                "created_at": active["created_at"],
                "error": active.get("error"),
            }
            if with_steps:
                item["steps"] = self._with_png_urls(replay_id, list(active["steps"]), False)
            return item
        if queued is not None:
            item = {"id": replay_id, "date": queued["date"], "state": "queued", "config": queued["config"], "summary": None, "progress": {"done": 0, "total": 0}, "created_at": queued["created_at"]}
            if with_steps:
                item["steps"] = []
            return item
        meta = _read_json(directory / "meta.json") or {}
        trace_path = directory / "trace.json"
        trace = _read_json(trace_path) if (with_steps or not meta) and trace_path.exists() else None
        if trace is None and not meta:
            return None
        config = meta.get("config") or (trace or {}).get("config") or {}
        summary = meta.get("summary") or (trace or {}).get("summary")
        state = meta.get("state") or ("done" if trace_path.exists() else "failed")
        created = meta.get("created_at") or datetime.fromtimestamp(directory.stat().st_mtime, IST).isoformat()
        item = {
            "id": replay_id,
            "date": meta.get("date") or (trace or {}).get("date") or _date_from_id(replay_id),
            "state": state,
            "config": config,
            "summary": self._summary(summary),
            "progress": meta.get("progress") or ({"done": summary.get("observations", 0), "total": summary.get("observations", 0)} if summary else None),
            "created_at": created,
            "error": meta.get("error"),
        }
        if with_steps:
            steps = list((trace or {}).get("steps") or [])
            item["steps"] = self._with_png_urls(replay_id, steps, (directory / "stimulus").exists())
        return item

    def list(self) -> list[dict[str, Any]]:
        root = self.ctx.paths.replays
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        if root.exists():
            for directory in root.iterdir():
                if not directory.is_dir():
                    continue
                item = self._item(directory, with_steps=False)
                if item is not None:
                    out.append(item)
                    seen.add(directory.name)
        with self._lock:
            pending = [q for q in self._queue if q["id"] not in seen]
            active = self.active if self.active and self.active["id"] not in seen else None
        for q in pending:
            out.append({"id": q["id"], "date": q["date"], "state": "queued", "config": q["config"], "summary": None, "progress": {"done": 0, "total": 0}, "created_at": q["created_at"]})
        if active is not None:
            out.append({"id": active["id"], "date": active["date"], "state": active["state"], "config": active["config"], "summary": None, "progress": dict(active["progress"]), "created_at": active["created_at"]})
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return out

    def get(self, replay_id: str) -> dict[str, Any] | None:
        directory = self.ctx.paths.replays / replay_id
        if not _safe_child(self.ctx.paths.replays, directory):
            return None
        return self._item(directory, with_steps=True)

    def png_path(self, replay_id: str, i: int) -> Path | None:
        directory = self.ctx.paths.replays / replay_id
        if not _safe_child(self.ctx.paths.replays, directory):
            return None
        path = directory / "stimulus" / f"{int(i)}.png"
        return path if path.exists() else None

    def last_step(self) -> tuple[dict[str, Any] | None, str | None]:
        """The latest observation step of the active or most recent replay, with its id."""
        with self._lock:
            active = self.active
            if active and active["steps"]:
                for step in reversed(active["steps"]):
                    if step.get("trigger", "observation") == "observation":
                        return dict(step), active["id"]
        for item in self.list():
            if item["state"] != "done":
                continue
            full = self.get(item["id"])
            if not full or not full.get("steps"):
                continue
            for step in reversed(full["steps"]):
                if step.get("trigger", "observation") == "observation":
                    return dict(step), item["id"]
        return None, None

    # -------------------------------------------------------------- start

    def start(self, body: dict[str, Any]) -> str:
        settings = self.ctx.settings()
        config = normalize_replay_config(body, settings)
        day = config["date"]
        replay_id = f"rp_{day.replace('-', '')}_{self.ctx.now().strftime('%H%M%S_%f')[:10]}"
        entry = {"id": replay_id, "date": day, "config": config, "created_at": self.ctx.now().isoformat()}
        directory = self.ctx.paths.replays / replay_id
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / "meta.json", {**entry, "state": "queued", "progress": {"done": 0, "total": 0}})
        with self._lock:
            self._queue.append(entry)
        thread = threading.Thread(target=self._run, args=(entry,), name=f"replay-{replay_id}", daemon=True)
        self._threads.append(thread)
        thread.start()
        self.ctx.bus.publish("replay.progress", {"id": replay_id, "date": day, "state": "queued", "done": 0, "total": 0})
        return replay_id

    def _run(self, entry: dict[str, Any]) -> None:
        with self._run_lock:
            with self._lock:
                if entry in self._queue:
                    self._queue.remove(entry)
                self.active = {**entry, "state": "running", "steps": [], "progress": {"done": 0, "total": 0}, "summary": None, "error": None}
            directory = self.ctx.paths.replays / entry["id"]
            try:
                self._execute(entry, directory)
            except Exception as exc:
                logger.error("replay %s failed: %s\n%s", entry["id"], exc, traceback.format_exc())
                with self._lock:
                    self.active["state"] = "failed"
                    self.active["error"] = str(exc)
                    snapshot = dict(self.active)
                _write_json(
                    directory / "meta.json",
                    {"id": entry["id"], "date": entry["date"], "config": entry["config"], "created_at": entry["created_at"], "state": "failed", "error": str(exc), "progress": snapshot["progress"]},
                )
                self.ctx.bus.publish("replay.progress", {"id": entry["id"], "date": entry["date"], "state": "failed", "error": str(exc), **snapshot["progress"]})
            finally:
                with self._lock:
                    self.active = None
                gc.collect()

    def _execute(self, entry: dict[str, Any], directory: Path) -> None:
        from openfly.straddle.replay import run_day
        from openfly.worker.factories import (
            load_bars,
            load_brain,
            load_encoder,
            load_minute_quotes,
            load_readout,
            load_session_window,
            load_vix,
        )

        config = entry["config"]
        replay_id = entry["id"]
        day = date.fromisoformat(entry["date"])
        settings = deep_merge(
            self.ctx.settings(),
            {
                "neural": {"encoder": config["encoder"], "readout": config["readout"], "neural_ms": config["neural_ms"], "plastic": config["plastic"]},
                "strategy": {"lots": config["lots"], "stop_pct": config["stop_pct"], "target_pct": config["target_pct"]},
            },
        )
        paths = self.ctx.paths

        def announce(message: str) -> None:
            self.ctx.bus.publish("log", {"message": message, "replay_id": replay_id})

        window, is_trading, window_note = load_session_window(day, settings, paths=paths)
        if not is_trading:
            raise ValueError(f"{day.isoformat()} is not a trading day ({window_note})")
        bars, bars_note = load_bars(day, settings, "1m", paths=paths)
        vix, vix_note = load_vix(day, settings, paths=paths)
        quotes, quotes_note = load_minute_quotes(day, bars, vix, settings, paths=paths)
        announce(f"replay {replay_id}: {bars_note} ({len(bars)} bars), VIX {vix:.2f} ({vix_note}), quotes {quotes_note}")
        announce(f"replay {replay_id}: loading the brain")
        brain, brain_note = load_brain(settings)
        encoder, enc_note = load_encoder(config["encoder"], settings)
        readout, rd_note = load_readout(config["readout"], settings, experiment_id=config.get("experiment_id"), paths=paths, brain=brain)
        announce(f"replay {replay_id}: {brain_note}; {enc_note}; {rd_note}")
        cost_model = load_cost_model(settings)
        broker = ReplayBroker(cost_model)

        def on_step(obs_i: int, total: int, step: dict[str, Any]) -> None:
            with self._lock:
                if self.active is not None and self.active["id"] == replay_id:
                    self.active["steps"].append(step)
                    self.active["progress"] = {"done": int(obs_i) + (1 if step.get("trigger", "observation") == "observation" else 0), "total": int(total)}
                    progress = dict(self.active["progress"])
                else:
                    progress = {"done": obs_i, "total": total}
            self.ctx.bus.publish(
                "replay.progress",
                {"id": replay_id, "date": entry["date"], "state": "running", **progress, "step": step},
                at=step.get("t"),
            )

        try:
            trace = run_day(
                day,
                bars,
                quotes,
                brain,
                encoder,
                readout,
                settings,
                broker,
                window,
                interval=config["interval"],
                vix=vix,
                cost_model=cost_model,
                on_step=on_step,
                render_png=config.get("render_png", True),
            )
        finally:
            del brain
        trace.config.update(
            {
                "bars": bars_note,
                "quotes": quotes_note,
                "brain": brain_note,
                "session": window_note,
                "experiment_id": config.get("experiment_id"),
                "encoder": config["encoder"],
                "readout": config["readout"],
                "neural_ms": config["neural_ms"],
                "lots": config["lots"],
                "stop_pct": config["stop_pct"],
                "target_pct": config["target_pct"],
            }
        )
        trace.save(directory)
        summary = trace.summary
        meta = {
            "id": replay_id,
            "date": entry["date"],
            "config": trace.config,
            "created_at": entry["created_at"],
            "finished_at": self.ctx.now().isoformat(),
            "state": "done",
            "summary": {k: v for k, v in summary.items() if k != "closed"},
            "progress": {"done": summary.get("observations", 0), "total": summary.get("observations", 0)},
        }
        _write_json(directory / "meta.json", meta)
        with self._lock:
            if self.active is not None and self.active["id"] == replay_id:
                self.active["state"] = "done"
                self.active["summary"] = summary
                self.active["config"] = trace.config
        self.ctx.bus.publish(
            "replay.progress",
            {"id": replay_id, "date": entry["date"], "state": "done", "done": summary.get("observations", 0), "total": summary.get("observations", 0), "summary": meta["summary"]},
        )
        announce(
            f"replay {replay_id} done: {summary.get('trades', 0)} trades, net P&L INR {summary.get('pnl', 0):,.0f}, "
            f"premiums {summary.get('premium_source')} (synthetic fraction {summary.get('synthetic_fraction')})"
        )

    def shutdown(self) -> None:
        return None


def _safe_child(root: Path, path: Path) -> bool:
    try:
        return path.resolve().parent == root.resolve() and path.name not in ("", ".", "..")
    except OSError:
        return False


def _date_from_id(replay_id: str) -> str | None:
    parts = replay_id.split("_")
    if len(parts) >= 2 and len(parts[1]) == 8 and parts[1].isdigit():
        d = parts[1]
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return None
