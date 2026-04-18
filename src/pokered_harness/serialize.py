"""JSON serialization for the state dataclasses and event records.

Avoids a pydantic dependency for state types — the state parsers are hot
per-tick, frozen dataclasses are faster to construct, and the serializer
only runs when a caller actually asks for JSON.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses / enums / tuples to JSON-ready types.

    Unknown objects fall through as-is and will blow up at ``json.dumps``
    time with a clear TypeError — that's intentional: silent ``repr()``
    fallbacks hide bugs.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(to_jsonable(v) for v in value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        import base64

        return {"__b64__": base64.b64encode(value).decode("ascii")}
    return value
