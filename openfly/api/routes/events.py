"""WS /api/events: the in-process bus plus the running worker's steps and the active replay's steps."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from openfly.api.context import ApiContext
from openfly.api.events import now_iso
from openfly.api.responses import dumps_text

router = APIRouter(tags=["events"])
logger = logging.getLogger("openfly.api")

BACKLOG_LIMIT = 2000


def backlog(ctx: ApiContext) -> list[dict[str, Any]]:
    """Today's steps so far: the worker's events.jsonl and the active replay's steps."""
    messages: list[dict[str, Any]] = []
    try:
        messages.extend(ctx.worker.steps_so_far())
    except Exception as exc:
        logger.info("worker backlog unavailable: %s", exc)
    active = ctx.replays.active
    if active is not None:
        for step in list(active["steps"]):
            messages.append(
                {
                    "type": "replay.progress",
                    "at": step.get("t") or now_iso(),
                    "data": {"id": active["id"], "date": active["date"], "state": active["state"], **active["progress"], "step": step},
                }
            )
    recent = ctx.bus.recent(types={"data.progress", "experiment.progress", "worker", "log"}, limit=100)
    messages.extend(recent)
    return messages[-BACKLOG_LIMIT:]


@router.websocket("/api/events")
async def events(websocket: WebSocket) -> None:
    ctx: ApiContext = websocket.app.state.ctx
    await websocket.accept()
    queue = ctx.bus.subscribe()
    try:
        await websocket.send_text(dumps_text({"type": "hello", "at": now_iso(), "data": {"worker": ctx.worker.status(), "subscribers": ctx.bus.subscribers}}))
        for message in await run_in_threadpool(backlog, ctx):
            await websocket.send_text(dumps_text(message))

        async def sender() -> None:
            while True:
                message = await queue.get()
                await websocket.send_text(dumps_text(message))

        async def receiver() -> None:
            while True:
                text = await websocket.receive_text()
                try:
                    payload = json.loads(text) if text else {}
                except ValueError:
                    payload = {}
                action = payload.get("action") if isinstance(payload, dict) else None
                if action == "ping":
                    await queue.put({"type": "pong", "at": now_iso(), "data": {}})
                elif action == "status":
                    await queue.put({"type": "worker", "at": now_iso(), "data": await run_in_threadpool(ctx.worker.status)})

        tasks = [asyncio.ensure_future(sender()), asyncio.ensure_future(receiver())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect | RuntimeError):
                    logger.info("events socket closed: %s", exc)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass
    finally:
        ctx.bus.unsubscribe(queue)
