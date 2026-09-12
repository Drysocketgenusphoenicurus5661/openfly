"""GET /api/status."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from openfly.api.context import ApiContext, Ctx

router = APIRouter(tags=["status"])

VERSION_FALLBACK = "0.1.0"


def project_version() -> str:
    try:
        from importlib.metadata import version

        return version("openfly")
    except Exception:
        return VERSION_FALLBACK


def numba_version() -> str | None:
    try:
        import numba

        return str(numba.__version__)
    except Exception:
        return None


def build_status(ctx: ApiContext) -> dict[str, Any]:
    probe = ctx.market.probe()
    try:
        session = ctx.market.session()
    except Exception as exc:
        session = {"error": str(exc), "now": ctx.now().isoformat()}
    try:
        chains = ctx.market.chains_summary()
    except Exception as exc:
        chains = {"days": 0, "coverage": [], "error": str(exc)}
    worker = ctx.worker.status()
    data = ctx.data.summary()
    active = ctx.replays.active
    return {
        "version": project_version(),
        "python": platform.python_version(),
        "numba": numba_version(),
        "data": data,
        "openalgo": {
            "reachable": bool(probe.get("reachable")),
            "host": probe.get("host"),
            "analyzer_mode": bool(probe.get("analyzer_mode")) if probe.get("analyzer_mode") is not None else False,
            "broker": probe.get("broker"),
            "error": probe.get("error"),
        },
        "worker": worker,
        "session": session,
        "chains": chains,
        "replay": {
            "active": active["id"] if active else None,
            "progress": dict(active["progress"]) if active else None,
        },
        "data_job": ctx.data.busy,
        "live_allowed": ctx.store.live_enabled,
        "live_env_set": bool(os.environ.get("OPENFLY_LIVE")),
        "executable": sys.executable,
        "root": str(ctx.root),
        "started_at": ctx.started_at.isoformat(),
        "now": ctx.now().isoformat(),
    }


@router.get("/api/status")
async def get_status(ctx: Ctx) -> dict[str, Any]:
    return await run_in_threadpool(build_status, ctx)
