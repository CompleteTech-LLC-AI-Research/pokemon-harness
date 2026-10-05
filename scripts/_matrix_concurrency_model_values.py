from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any


def _hash_text(text: str) -> str:
    """Return a stable digest of ``text`` for evidence identity."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_int(value: Any) -> int:
    """Return ``value`` as a non-negative int, treating anything else as zero."""

    return value if type(value) is int and value >= 0 else 0


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Return a nearest-rank percentile, or ``None`` for an empty sample.

    Nearest-rank is deliberate: the reported p95 is always an observed
    measurement, never a synthetic value interpolated between two samples.
    """

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000)))
    return ordered[rank - 1]
