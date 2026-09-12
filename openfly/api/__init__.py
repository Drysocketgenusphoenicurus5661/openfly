"""FastAPI backend: the REST contract of docs/api-spec.md, the event websocket and the web UI.

    from openfly.api import create_app
    app = create_app()
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from openfly.api.app import create_app

        return create_app
    raise AttributeError(name)
