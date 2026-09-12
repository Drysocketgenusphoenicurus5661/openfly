"""GET and POST /api/experiments, GET /api/experiments/{id}."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from openfly.api.context import Ctx, error

router = APIRouter(tags=["experiments"])


@router.get("/api/experiments")
async def list_experiments(ctx: Ctx) -> dict[str, Any]:
    return {"experiments": await run_in_threadpool(ctx.experiments.list)}


@router.post("/api/experiments")
async def create_experiment(ctx: Ctx, config: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise error(400, "body must be the experiment config object")
    try:
        experiment_id = await run_in_threadpool(ctx.experiments.start, config)
    except ValueError as exc:
        raise error(400, str(exc)) from exc
    except OSError as exc:
        raise error(500, f"could not launch the experiment: {exc}") from exc
    return {"id": experiment_id}


@router.get("/api/experiments/{experiment_id}")
async def get_experiment(ctx: Ctx, experiment_id: str) -> dict[str, Any]:
    result = await run_in_threadpool(ctx.experiments.get, experiment_id)
    if result is None:
        raise error(404, f"no experiment {experiment_id}")
    return result
