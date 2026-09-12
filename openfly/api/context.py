"""The objects every route shares: paths, settings, the event bus, the OpenAlgo client factory and the services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request

from openfly.api.events import IST, EventBus
from openfly.config import PATHS, Paths, SettingsStore

if TYPE_CHECKING:
    from openfly.api.services.brain import BrainService
    from openfly.api.services.data import DataService
    from openfly.api.services.experiments import ExperimentService
    from openfly.api.services.market import MarketService
    from openfly.api.services.replay import ReplayService
    from openfly.api.services.worker import WorkerService


def default_client_factory(store: SettingsStore) -> Callable[..., Any]:
    def factory(**overrides: Any) -> Any:
        from openfly.market.client import OpenAlgoClient

        return OpenAlgoClient.from_settings(store, **overrides)

    return factory


@dataclass
class ApiContext:
    root: Path
    paths: Paths
    store: SettingsStore
    bus: EventBus
    client_factory: Callable[..., Any]
    started_at: datetime = field(default_factory=lambda: datetime.now(IST))
    market: MarketService = field(init=False, repr=False)
    data: DataService = field(init=False, repr=False)
    worker: WorkerService = field(init=False, repr=False)
    replays: ReplayService = field(init=False, repr=False)
    experiments: ExperimentService = field(init=False, repr=False)
    brain: BrainService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        from openfly.api.services.brain import BrainService
        from openfly.api.services.data import DataService
        from openfly.api.services.experiments import ExperimentService
        from openfly.api.services.market import MarketService
        from openfly.api.services.replay import ReplayService
        from openfly.api.services.worker import WorkerService

        self.market = MarketService(self)
        self.data = DataService(self)
        self.worker = WorkerService(self)
        self.replays = ReplayService(self)
        self.experiments = ExperimentService(self)
        self.brain = BrainService(self)

    def settings(self) -> dict[str, Any]:
        return self.store.get()

    def client(self, **overrides: Any) -> Any:
        """A fresh OpenAlgo client; raises ValueError when the API key is not set."""
        return self.client_factory(**overrides)

    def probe_client(self) -> Any:
        """A client for quick reachability checks: short timeout, no retries."""
        try:
            return self.client(timeout=4.0, max_tries=1)
        except TypeError:
            return self.client()

    def relative(self, path: Path | str) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(p)

    def now(self) -> datetime:
        return datetime.now(IST)

    def shutdown(self) -> None:
        self.worker.shutdown()
        self.replays.shutdown()
        self.experiments.shutdown()
        self.data.shutdown()


def build_context(
    paths: Paths | None = None,
    store: SettingsStore | None = None,
    client_factory: Callable[..., Any] | None = None,
    root: Path | None = None,
) -> ApiContext:
    paths = paths or PATHS
    paths.ensure()
    store = store or SettingsStore(paths.db)
    return ApiContext(
        root=Path(root) if root else paths.root,
        paths=paths,
        store=store,
        bus=EventBus(),
        client_factory=client_factory or default_client_factory(store),
    )


def get_context(request: Request) -> ApiContext:
    return request.app.state.ctx


Ctx = Annotated[ApiContext, Depends(get_context)]


def error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)
