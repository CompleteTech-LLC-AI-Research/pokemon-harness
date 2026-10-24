"""Minimal PyBoy-shaped protocol used by the session and hook layers.

Keeping the protocol tight means the session logic is unit-testable against
an in-process fake, and any upstream PyBoy method rename surfaces as a
protocol violation at the seam rather than deep inside a run.

The real :class:`pyboy.PyBoy` class satisfies this protocol structurally
without any declared inheritance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO, Protocol

from pokered_harness.symbols.loader import MemoryLike

HookCallback = Callable[[object], None]


class PyBoyLike(Protocol):
    """Subset of ``pyboy.PyBoy`` the harness depends on.

    PyBoy 2.7.0 API reference:
    https://docs.pyboy.dk/
    """

    memory: MemoryLike

    def tick(self, count: int = 1, render: bool = False) -> bool: ...

    def button(self, name: str, duration: int = 1) -> None: ...

    def button_press(self, name: str) -> None: ...

    def button_release(self, name: str) -> None: ...

    def save_state(self, file_like: BinaryIO) -> None: ...

    def load_state(self, file_like: BinaryIO) -> None: ...

    def hook_register(
        self, bank: int, addr: int, callback: HookCallback, context: object
    ) -> None: ...

    def stop(self, save: bool = False) -> None: ...
