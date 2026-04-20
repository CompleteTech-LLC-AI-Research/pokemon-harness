"""In-process fakes used by the event bus and session test suites."""

from __future__ import annotations

from io import BytesIO
from typing import BinaryIO, Callable


HookKey = tuple[int, int]


class _FakeRegisterFile:
    """Minimal stand-in for ``pyboy.PyBoy.register_file``.

    The real PyBoy exposes the CPU's 8/16-bit registers as attributes
    so hooks can read call-convention arguments (``hl``, ``de``, ``bc``)
    and write ``PC``/``SP`` to simulate a ``ret``. The remote link
    endpoint relies on this; tests need the same shape."""

    __slots__ = ("A", "B", "C", "D", "E", "F", "HL", "SP", "PC")

    def __init__(self) -> None:
        self.A = 0
        self.B = 0
        self.C = 0
        self.D = 0
        self.E = 0
        self.F = 0
        self.HL = 0
        self.SP = 0
        self.PC = 0


class FakePyBoy:
    """Minimum viable stand-in for ``pyboy.PyBoy``.

    Supports manual trigger of registered hooks (``fire(symbol_bank_addr)``),
    records button presses, and gives tests direct control over ``memory``
    through the ``DictMemory`` passed in at construction.
    """

    def __init__(self, memory) -> None:
        self.memory = memory
        self._hooks: dict[HookKey, list[tuple[Callable[[object], None], object]]] = {}
        self.tick_calls: list[tuple[int, bool]] = []
        self.button_calls: list[tuple[str, int]] = []
        self.button_press_calls: list[str] = []
        self.button_release_calls: list[str] = []
        self.stopped = False
        self._saved_state: bytes = b""
        self.register_file = _FakeRegisterFile()

    def tick(self, count: int = 1, render: bool = False) -> bool:
        self.tick_calls.append((count, render))
        return True

    def button(self, name: str, duration: int = 1) -> None:
        self.button_calls.append((name, duration))

    def button_press(self, name: str) -> None:
        self.button_press_calls.append(name)

    def button_release(self, name: str) -> None:
        self.button_release_calls.append(name)

    def save_state(self, file_like: BinaryIO) -> None:
        file_like.write(self._saved_state or b"STATE")

    def load_state(self, file_like: BinaryIO) -> None:
        self._saved_state = file_like.read()

    def hook_register(self, bank: int, addr: int, callback, context) -> None:
        self._hooks.setdefault((bank, addr), []).append((callback, context))

    def hook_deregister(self, bank: int, addr: int) -> None:
        """Remove every hook at ``(bank, addr)``. Real PyBoy 2.7 only
        supports removing by address — we mirror that."""
        self._hooks.pop((bank, addr), None)

    def stop(self, save: bool = False) -> None:
        self.stopped = True

    # --- test helpers (not part of PyBoyLike) ---

    def fire(self, bank: int, addr: int) -> int:
        """Invoke every callback registered at the given bank/addr pair.
        Returns how many ran.
        """
        hooks = self._hooks.get((bank, addr), [])
        for cb, ctx in hooks:
            cb(ctx)
        return len(hooks)
