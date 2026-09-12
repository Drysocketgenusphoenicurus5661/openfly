"""GET and PUT /api/settings, POST /api/settings/analyzer."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from openfly.api.context import Ctx, error
from openfly.config import DEFAULT_SETTINGS

router = APIRouter(tags=["settings"])

ALLOWED_SECTIONS = set(DEFAULT_SETTINGS) | {"execution"}
READ_ONLY_KEYS = {("openalgo", "api_key_set")}


@router.get("/api/settings")
async def get_settings(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(ctx.store.public)


@router.put("/api/settings")
async def put_settings(ctx: Ctx, patch: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise error(400, "body must be an object of settings sections")
    update: dict[str, Any] = {}
    for section, values in patch.items():
        if section not in ALLOWED_SECTIONS:
            raise error(400, f"unknown settings section {section!r}; use one of {sorted(ALLOWED_SECTIONS)}")
        if not isinstance(values, dict):
            raise error(400, f"section {section!r} must be an object")
        cleaned = {k: v for k, v in values.items() if (section, k) not in READ_ONLY_KEYS}
        if section == "openalgo" and "api_key" in cleaned and cleaned["api_key"] is None:
            cleaned["api_key"] = ""
        if cleaned:
            update[section] = cleaned
    if update:
        await run_in_threadpool(ctx.store.set, update)
    return await run_in_threadpool(ctx.store.public)


@router.post("/api/settings/analyzer")
async def set_analyzer(ctx: Ctx, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    if not isinstance(body, dict) or "mode" not in body:
        raise error(400, 'body must be {"mode": true | false}')
    mode = bool(body["mode"])
    try:
        value = await run_in_threadpool(ctx.market.analyzer_toggle, mode)
    except ValueError as exc:
        raise error(400, f"OpenAlgo client unavailable: {exc}") from exc
    except Exception as exc:
        raise error(503, f"analyzer toggle failed: {exc}") from exc
    ctx.bus.publish("log", {"message": f"OpenAlgo analyzer mode set to {'on' if value else 'off'}"})
    return {"analyzer_mode": value}
