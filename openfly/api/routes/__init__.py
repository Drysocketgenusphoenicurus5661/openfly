"""Route modules, one per area of docs/api-spec.md."""

from fastapi import APIRouter

from openfly.api.routes import (
    brain,
    data,
    events,
    experiments,
    ledger,
    market,
    replay,
    settings,
    status,
    worker,
)

API_ROUTERS: tuple[APIRouter, ...] = (
    status.router,
    data.router,
    market.router,
    brain.router,
    experiments.router,
    worker.router,
    ledger.router,
    settings.router,
    replay.router,
    events.router,
)

__all__ = ["API_ROUTERS"]
