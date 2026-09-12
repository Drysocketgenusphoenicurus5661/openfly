"""orjson responses and a JSON default that knows the project's value types."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import orjson
from fastapi.responses import JSONResponse

ORJSON_OPTIONS = orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS


def json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def dumps(value: Any) -> bytes:
    return orjson.dumps(value, default=json_default, option=ORJSON_OPTIONS)


def dumps_text(value: Any) -> str:
    return dumps(value).decode("utf-8")


class ORJSONResponse(JSONResponse):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content)
