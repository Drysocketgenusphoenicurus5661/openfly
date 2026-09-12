"""GET /api/market/bars, /api/market/chain, /api/market/session."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from openfly.api.context import Ctx, error

router = APIRouter(tags=["market"])


@router.get("/api/market/bars")
async def market_bars(
    ctx: Ctx,
    symbol: str | None = None,
    exchange: str | None = None,
    interval: str = "1m",
    days: Annotated[int, Query(ge=1, le=400)] = 1,
) -> dict[str, Any]:
    default_exchange, default_symbol = ctx.market.index_symbol()
    try:
        return await run_in_threadpool(ctx.market.bars, symbol or default_symbol, exchange or default_exchange, interval, days)
    except ValueError as exc:
        raise error(400, str(exc)) from exc
    except Exception as exc:
        raise error(503, f"bar store unavailable: {exc}") from exc


@router.get("/api/market/chain")
async def market_chain(ctx: Ctx, strikes: Annotated[int, Query(ge=1, le=30)] = 5, iv: bool = False) -> dict[str, Any]:
    try:
        return await run_in_threadpool(ctx.market.chain, strikes, iv)
    except ValueError as exc:
        raise error(400, f"OpenAlgo client unavailable: {exc}") from exc
    except Exception as exc:
        raise error(503, f"chain unavailable: {exc}") from exc


@router.get("/api/market/session")
async def market_session(ctx: Ctx, date_: Annotated[str | None, Query(alias="date")] = None) -> dict[str, Any]:
    day: date | None = None
    if date_:
        try:
            day = date.fromisoformat(date_[:10])
        except ValueError as exc:
            raise error(400, "date must be YYYY-MM-DD") from exc
    try:
        return await run_in_threadpool(ctx.market.session, day)
    except Exception as exc:
        raise error(503, f"session calendar unavailable: {exc}") from exc
