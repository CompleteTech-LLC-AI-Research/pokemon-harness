"""Input vocabulary shared by the session and the MCP tool surface."""

from __future__ import annotations

from enum import StrEnum


class Button(StrEnum):
    A = "a"
    B = "b"
    START = "start"
    SELECT = "select"
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


ALL_BUTTONS: frozenset[str] = frozenset(b.value for b in Button)


def validate_button(name: str) -> Button:
    try:
        return Button(name)
    except ValueError as e:
        raise ValueError(
            f"unknown button {name!r}; valid: {sorted(ALL_BUTTONS)}"
        ) from e
