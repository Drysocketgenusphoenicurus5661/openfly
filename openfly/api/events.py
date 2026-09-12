"""In-process event bus (thread safe, asyncio consumers) and a JSON lines file tailer.

Producers run in worker threads (replays, data jobs, subprocess watchers) and
call ``bus.publish``; websocket handlers subscribe from the event loop. The
bus keeps a bounded history so a client connecting late sees recent events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger("openfly.api")


def now_iso() -> str:
    return datetime.now(IST).isoformat()


class EventBus:
    def __init__(self, history: int = 1000, queue_size: int = 4000):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._queue_size = queue_size

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, type_: str, data: Any = None, at: str | None = None) -> dict[str, Any]:
        message = {"type": type_, "at": at or now_iso(), "data": data if data is not None else {}}
        with self._lock:
            self._history.append(message)
            subscribers = list(self._subscribers)
            loop = self._loop
        if loop is None or loop.is_closed() or not subscribers:
            return message
        for queue in subscribers:
            try:
                loop.call_soon_threadsafe(_offer, queue, message)
            except RuntimeError:
                pass
        return message

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def recent(self, types: set[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if types:
            items = [m for m in items if m["type"] in types]
        if limit is not None:
            items = items[-limit:]
        return items

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)


def _offer(queue: asyncio.Queue, message: dict[str, Any]) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(message)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Every parseable JSON object in a JSON lines file (the last ``limit`` when given)."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        return out
    return out[-limit:] if limit is not None else out


class FileTailer(threading.Thread):
    """Follows a JSON lines file and hands every new record to ``on_record``."""

    def __init__(
        self,
        path: Path,
        on_record: Callable[[dict[str, Any]], None],
        poll_seconds: float = 0.5,
        from_start: bool = False,
        on_idle: Callable[[], bool] | None = None,
    ):
        super().__init__(name=f"tail:{path.name}", daemon=True)
        self.path = Path(path)
        self.on_record = on_record
        self.poll_seconds = poll_seconds
        self.from_start = from_start
        self.on_idle = on_idle
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        position = 0
        started = self.from_start
        while not self._stop.is_set():
            if not self.path.exists():
                if self.on_idle is not None and self.on_idle():
                    return
                time.sleep(self.poll_seconds)
                continue
            try:
                size = self.path.stat().st_size
                if not started:
                    position = size
                    started = True
                if size < position:
                    position = 0
                if size == position:
                    if self.on_idle is not None and self.on_idle():
                        return
                    time.sleep(self.poll_seconds)
                    continue
                with open(self.path, encoding="utf-8") as fh:
                    fh.seek(position)
                    for line in fh:
                        if not line.endswith("\n"):
                            break
                        position += len(line.encode("utf-8"))
                        text = line.strip()
                        if not text:
                            continue
                        try:
                            record = json.loads(text)
                        except ValueError:
                            continue
                        if isinstance(record, dict):
                            try:
                                self.on_record(record)
                            except Exception:
                                logger.exception("tailer callback failed")
            except OSError as exc:
                logger.info("tailer: %s", exc)
                time.sleep(self.poll_seconds)
