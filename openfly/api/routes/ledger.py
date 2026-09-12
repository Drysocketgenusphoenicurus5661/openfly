"""GET /api/straddle, /api/ledger/intents, /api/orders, /api/positions."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from openfly.api.context import Ctx

router = APIRouter(tags=["ledger"])


@router.get("/api/straddle")
async def straddle(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(ctx.worker.straddle)


@router.get("/api/ledger/intents")
async def intents(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, Any]:
    run_dir = ctx.worker.current_run_dir()
    try:
        rows = await run_in_threadpool(ctx.worker.intents, limit)
    except Exception as exc:
        return {"intents": [], "run_dir": ctx.relative(run_dir) if run_dir else None, "error": str(exc)}
    return {"intents": rows, "run_dir": ctx.relative(run_dir) if run_dir else None}


@router.get("/api/orders")
async def orders(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(ctx.market.orders)


@router.get("/api/positions")
async def positions(ctx: Ctx) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        try:
            symbols = ctx.worker.ledger_symbols()
        except Exception:
            symbols = set()
        return ctx.market.positions(symbols or None)

    return await run_in_threadpool(load)
