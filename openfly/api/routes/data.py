"""GET /api/data/status, POST /api/data/prepare, POST /api/data/record, POST /api/data/backfill-chains."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from openfly.api.context import ApiContext, Ctx, error

router = APIRouter(tags=["data"])

OptionalBody = Annotated[dict[str, Any] | None, Body()]


@router.get("/api/data/status")
async def data_status(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(ctx.data.status)


@router.post("/api/data/prepare")
async def data_prepare(ctx: Ctx) -> dict[str, Any]:
    try:
        await run_in_threadpool(ctx.data.prepare)
    except RuntimeError as exc:
        raise error(409, str(exc)) from exc
    return {"started": True, "job": "prepare"}


def _requires_client(ctx: ApiContext) -> None:
    try:
        client = ctx.probe_client()
    except ValueError as exc:
        raise error(400, f"OpenAlgo API key is not set: {exc}") from exc
    close = getattr(client, "close", None)
    if callable(close):
        close()


@router.post("/api/data/record")
async def data_record(ctx: Ctx, body: OptionalBody = None) -> dict[str, Any]:
    body = body or {}
    day = None
    if body.get("date"):
        try:
            day = date.fromisoformat(str(body["date"])[:10])
        except ValueError as exc:
            raise error(400, "date must be YYYY-MM-DD") from exc
    strikes = body.get("strikes_each_side")
    _requires_client(ctx)
    try:
        await run_in_threadpool(ctx.data.record, day, bool(body.get("force", False)), int(strikes) if strikes is not None else None)
    except RuntimeError as exc:
        raise error(409, str(exc)) from exc
    return {"started": True, "job": "record", "date": day.isoformat() if day else None}


@router.post("/api/data/backfill-chains")
async def data_backfill(ctx: Ctx, body: OptionalBody = None) -> dict[str, Any]:
    body = body or {}
    strategy = ctx.settings().get("strategy", {})
    selection = str(strategy.get("expiry_selection", "monthly"))
    default_days = strategy.get("monthly_backfill_days" if selection == "monthly" else "weekly_backfill_days", 30)
    days = int(body.get("days") or default_days or 30)
    lead = int(body.get("lead") or body.get("listing_lead_days") or 21)
    strikes = body.get("strikes_each_side")
    if days < 1 or days > 400:
        raise error(400, "days must be between 1 and 400")
    _requires_client(ctx)
    try:
        await run_in_threadpool(ctx.data.backfill, days, lead, int(strikes) if strikes is not None else None)
    except RuntimeError as exc:
        raise error(409, str(exc)) from exc
    return {"started": True, "job": "backfill", "days": days, "lead": lead}
