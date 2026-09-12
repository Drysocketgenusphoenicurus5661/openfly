"""GET /api/brain/circuits, /api/brain/state, /api/brain/stimulus.png."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from openfly.api.context import Ctx, error

router = APIRouter(tags=["brain"])


@router.get("/api/brain/circuits")
async def brain_circuits(ctx: Ctx) -> dict[str, Any]:
    try:
        return await run_in_threadpool(ctx.brain.circuits)
    except FileNotFoundError as exc:
        raise error(503, str(exc)) from exc


@router.get("/api/brain/state")
async def brain_state(ctx: Ctx) -> dict[str, Any]:
    state = await run_in_threadpool(ctx.brain.state)
    if state is None:
        raise error(404, "no observation yet: start the worker or run a replay")
    return state


@router.get("/api/brain/stimulus.png")
async def brain_stimulus(ctx: Ctx) -> FileResponse:
    path = await run_in_threadpool(ctx.brain.stimulus_png)
    if path is None:
        raise error(404, "no stimulus image yet: replays render one per step under runs/replays/<id>/stimulus")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
