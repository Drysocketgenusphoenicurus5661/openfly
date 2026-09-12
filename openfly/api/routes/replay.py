"""GET /api/replay/dates, POST /api/replay/run, GET /api/replay, GET /api/replay/{id}, GET /api/replay/{id}/stimulus/{i}.png."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from openfly.api.context import Ctx, error

router = APIRouter(tags=["replay"])


@router.get("/api/replay/dates")
async def replay_dates(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(ctx.replays.dates)


@router.post("/api/replay/run")
async def replay_run(ctx: Ctx, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise error(400, "body must be the replay config object")
    if not ctx.data.summary()["ready"]:
        raise error(503, "the compiled connectome graph is missing; prepare the data first")
    try:
        replay_id = await run_in_threadpool(ctx.replays.start, body)
    except ValueError as exc:
        raise error(400, str(exc)) from exc
    return {"id": replay_id}


@router.get("/api/replay")
async def replay_list(ctx: Ctx) -> dict[str, Any]:
    return {"replays": await run_in_threadpool(ctx.replays.list)}


@router.get("/api/replay/{replay_id}")
async def replay_get(ctx: Ctx, replay_id: str) -> dict[str, Any]:
    item = await run_in_threadpool(ctx.replays.get, replay_id)
    if item is None:
        raise error(404, f"no replay {replay_id}")
    return item


@router.get("/api/replay/{replay_id}/stimulus/{i}.png")
async def replay_stimulus(ctx: Ctx, replay_id: str, i: int) -> FileResponse:
    path = await run_in_threadpool(ctx.replays.png_path, replay_id, i)
    if path is None:
        raise error(404, f"no stimulus image for step {i} of {replay_id}")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
