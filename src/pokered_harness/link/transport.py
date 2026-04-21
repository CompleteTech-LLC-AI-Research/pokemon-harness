"""Byte-queue transport between two paired Game Boys.

Pure data structure: no PyBoy, no session coupling. The serial bridge drives
this object when the game's serial routines fire.
"""

from __future__ import annotations

from collections import deque


def _validate_byte(byte: int) -> int:
    # bool is an int subclass; reject it explicitly to avoid silent True→1 coercion.
    if not isinstance(byte, int) or isinstance(byte, bool):
        raise ValueError(f"byte must be int in 0..255, got {type(byte).__name__}")
    if byte < 0 or byte > 0xFF:
        raise ValueError(f"byte must be in 0..255, got {byte}")
    return byte


class LinkTransport:
    """Two FIFO byte queues modeling the full-duplex GB link cable."""

    def __init__(self) -> None:
        self._a_to_b: deque[int] = deque()
        self._b_to_a: deque[int] = deque()

    def push_a_to_b(self, byte: int) -> None:
        self._a_to_b.append(_validate_byte(byte))

    def push_b_to_a(self, byte: int) -> None:
        self._b_to_a.append(_validate_byte(byte))

    def pull_for_a(self, default: int = 0x00) -> int:
        """Pop next byte destined for A (from the B→A queue)."""
        _validate_byte(default)
        if self._b_to_a:
            return self._b_to_a.popleft()
        return default

    def pull_for_b(self, default: int = 0x00) -> int:
        _validate_byte(default)
        if self._a_to_b:
            return self._a_to_b.popleft()
        return default

    def exchange(self, from_a: int, from_b: int) -> tuple[int, int]:
        """Symmetric push-then-pull: each side gets the other's oldest queued byte.

        Push order before pop matters when both queues were previously empty:
        pulling for A sees the just-pushed byte from B, and vice versa.
        """
        self.push_a_to_b(from_a)
        self.push_b_to_a(from_b)
        to_a = self.pull_for_a()
        to_b = self.pull_for_b()
        return to_a, to_b

    def reset(self) -> None:
        self._a_to_b.clear()
        self._b_to_a.clear()

    @property
    def pending_a_to_b(self) -> int:
        return len(self._a_to_b)

    @property
    def pending_b_to_a(self) -> int:
        return len(self._b_to_a)

    def snapshot(self) -> dict:
        return {
            "a_to_b": list(self._a_to_b),
            "b_to_a": list(self._b_to_a),
        }
