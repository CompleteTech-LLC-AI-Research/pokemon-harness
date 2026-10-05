from __future__ import annotations


def _parse_counts(raw: str) -> list[int]:
    """Return positive worker counts from a comma-separated argument."""

    counts: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError as exc:
            raise ValueError(f"worker count {piece!r} is not an integer") from exc
        if value <= 0:
            raise ValueError("worker counts must be positive")
        counts.append(value)
    if not counts:
        raise ValueError("at least one worker count is required")
    return list(dict.fromkeys(counts))
