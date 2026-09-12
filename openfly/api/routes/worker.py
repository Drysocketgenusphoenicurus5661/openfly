"""POST /api/worker/start, /api/worker/stop, /api/worker/squareoff."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body
from starlette.concurrency import run_in_threadpool

from openfly.api.context import ApiContext, Ctx, error

router = APIRouter(tags=["worker"])

LIVE_HINT = "set OPENFLY_LIVE=I_ACCEPT_REAL_TRADES in the environment that runs the API"


def _check_and_start(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    mode = str(body.get("mode") or "paper").lower()
    if mode not in ("paper", "live"):
        raise error(400, "mode must be paper or live")
    settings = ctx.settings()
    strategy = settings.get("strategy", {})
    risk = settings.get("risk", {})
    lots = int(body.get("lots") or strategy.get("lots") or 1)
    if lots < 1:
        raise error(400, "lots must be at least 1")
    max_lots = int(risk.get("max_lots", 0) or 0)
    if max_lots and lots > max_lots:
        raise error(400, f"lots {lots} is above risk.max_lots {max_lots}")
    trading_date = None
    if body.get("date"):
        try:
            trading_date = date.fromisoformat(str(body["date"])[:10])
        except ValueError as exc:
            raise error(400, "date must be YYYY-MM-DD") from exc
    if ctx.worker.is_running():
        raise error(409, "a worker is already running; stop it first")
    analyzer, probe_error = ctx.market.analyzer_mode()
    if analyzer is None:
        raise error(503, f"OpenAlgo is unreachable, cannot start the worker: {probe_error}")
    if mode == "paper":
        if not analyzer:
            raise error(
                409,
                "OpenAlgo analyzer mode is off. Paper trading sends orders to the analyzer sandbox, so turn the "
                "analyzer on first (Settings, analyzer toggle; it is global to the OpenAlgo installation).",
            )
    else:
        if not ctx.store.live_enabled:
            raise error(403, f"live mode refused: {LIVE_HINT}")
        passed = ctx.experiments.passed_ids()
        if not passed:
            raise error(403, "live mode refused: no experiment in runs/experiments has passed the protocol (passed: true)")
        if analyzer:
            raise error(409, "OpenAlgo analyzer mode is on; live orders would go to the sandbox. Turn the analyzer off before trading live.")
    try:
        return ctx.worker.start(mode, lots, body.get("run_dir"), trading_date)
    except RuntimeError as exc:
        raise error(409, str(exc)) from exc
    except OSError as exc:
        raise error(500, f"could not launch the worker: {exc}") from exc


@router.post("/api/worker/start")
async def worker_start(ctx: Ctx, body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
    return await run_in_threadpool(_check_and_start, ctx, body or {})


@router.post("/api/worker/stop")
async def worker_stop(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(ctx.worker.stop)


@router.post("/api/worker/squareoff")
async def worker_squareoff(ctx: Ctx) -> dict[str, Any]:
    try:
        return await run_in_threadpool(ctx.worker.squareoff)
    except RuntimeError as exc:
        raise error(409, str(exc)) from exc
