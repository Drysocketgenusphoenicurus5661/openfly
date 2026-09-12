"""FastAPI application factory.

    from openfly.api.app import create_app
    app = create_app()                       # real paths, data/openfly.db, OpenAlgo from settings
    app = create_app(paths=..., store=..., client_factory=...)   # tests inject everything

Blocking work (DuckDB, SQLite, OpenAlgo calls, file scans) runs in the
threadpool; long jobs (replays, data preparation, recording) run in daemon
threads; the worker and experiments are subprocesses. Responses are orjson.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from openfly.api import static
from openfly.api.context import ApiContext, build_context
from openfly.api.responses import ORJSONResponse
from openfly.api.routes import API_ROUTERS
from openfly.config import Paths, SettingsStore

logger = logging.getLogger("openfly.api")

CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def create_app(
    paths: Paths | None = None,
    store: SettingsStore | None = None,
    client_factory: Callable[..., Any] | None = None,
    root: Path | None = None,
    ctx: ApiContext | None = None,
) -> FastAPI:
    context = ctx or build_context(paths=paths, store=store, client_factory=client_factory, root=root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        context.bus.bind(asyncio.get_running_loop())
        try:
            context.worker.attach()
        except Exception as exc:
            logger.info("worker attach skipped: %s", exc)
        try:
            yield
        finally:
            context.shutdown()

    app = FastAPI(
        title="OpenFly",
        version="0.1.0",
        description="A fruit fly connectome trading an intraday NIFTY straddle through OpenAlgo",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.ctx = context
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return ORJSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return ORJSONResponse({"detail": _validation_text(exc), "errors": exc.errors()}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return ORJSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    for router in API_ROUTERS:
        app.include_router(router)
    app.include_router(static.router)
    return app


def _validation_text(exc: RequestValidationError) -> str:
    parts = []
    for item in exc.errors():
        location = ".".join(str(p) for p in item.get("loc", ()) if p not in ("body", "query"))
        parts.append(f"{location}: {item.get('msg')}" if location else str(item.get("msg")))
    return "; ".join(parts) or "invalid request"
