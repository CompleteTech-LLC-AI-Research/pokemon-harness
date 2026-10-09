"""Total, side-effect free validation helpers for decoded JSON (#107.3).

Every helper accepts arbitrary decoded JSON and answers instead of raising, so one
malformed artifact can never abort the aggregation of the other jobs' evidence.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Container
from datetime import UTC, datetime
from typing import Any

CLOCK_SKEW_SECONDS = 2.0


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_positive_number(value: object) -> bool:
    return is_finite_number(value) and value > 0  # type: ignore[operator]


def is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def in_set(value: object, allowed: Container[str]) -> bool:
    """Membership that never raises for unhashable or non-text decoded values."""

    return isinstance(value, str) and value in allowed


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in items]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate JSON key")
    return dict(items)


MAX_JSON_CHARS = 64 * 1024 * 1024


def loads_strict(text: str) -> Any:
    """Decode JSON, rejecting NaN/Infinity, duplicate keys, oversize text and runaway nesting.

    Every decoder failure is a ``ValueError`` so no caller has to know the decoder's internals.
    """

    if len(text) > MAX_JSON_CHARS:
        raise ValueError("JSON text exceeds the size bound")
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the decoder's recursion bound") from exc


loads_bounded = loads_strict


def parse_utc(value: object) -> datetime | None:
    """Parse a timezone-aware ISO timestamp; ``None`` for anything else."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def timestamp_problems(
    started: object,
    finished: object,
    window: tuple[datetime, datetime] | None = None,
) -> list[str]:
    """Check ordering and, when given, the parent-observed launch/finish window."""

    first, last = parse_utc(started), parse_utc(finished)
    problems: list[str] = []
    if first is None:
        problems.append("started_utc is not a timezone-aware ISO timestamp")
    if last is None:
        problems.append("finished_utc is not a timezone-aware ISO timestamp")
    if first is None or last is None:
        return problems
    if last < first:
        problems.append("finished_utc precedes started_utc")
    if window is not None:
        opened, closed = window
        if (opened - first).total_seconds() > CLOCK_SKEW_SECONDS:
            problems.append("started_utc precedes the parent's launch of this job (stale evidence)")
        if (last - closed).total_seconds() > CLOCK_SKEW_SECONDS:
            problems.append("finished_utc is later than the parent observed the job finish")
    return problems


def guarded(label: str, function: Callable[..., list[str]], *args: Any, **kwargs: Any) -> list[str]:
    """Run a validator and turn any unexpected exception into a failed-closed problem."""

    try:
        return list(function(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - a validator must fail closed, never raise
        return [f"{label}: validator failed closed on malformed input ({type(exc).__name__})"]
