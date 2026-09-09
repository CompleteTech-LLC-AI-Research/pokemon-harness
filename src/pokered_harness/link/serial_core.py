"""Thin re-export shim for PyBoy's native bit-accurate serial core.

Historically this module shipped its own ``SerialCore`` class that was
swapped into ``pyboy.mb.serial`` at attach time. After the upstream
PyBoy fork absorbed the bit-accurate logic directly into
:class:`pyboy.core.serial.Serial` (with a runtime-settable ``backend``
attribute and the ``NullBackend``/``LocalBackend``/``SerialBackend``
helpers co-located), the harness no longer needs its own class: the
existing ``pyboy.mb.serial`` instance is reused and only its
``backend`` is wired up.

This module now exists purely as a back-compat re-export so callers
(and tests) that import :class:`SerialCore`, :class:`NullBackend`, etc.
keep working. The constants ``ROLE_INTERNAL`` / ``ROLE_EXTERNAL`` /
``CYCLES_PER_EDGE_DMG`` / ``SC_TRANSFER_ENABLE`` / ``SC_CLOCK_SOURCE``
/ ``SC_CLOCK_SPEED`` / ``IF_SERIAL`` / ``CYCLES_PER_BYTE_DMG`` /
``MAX_CYCLES`` are imported from PyBoy where available and fall back
to local definitions when PyBoy isn't importable (pure-Python unit
test runs).

See :doc:`docs/pyboy_serial_overhaul_design.md` for the full design.
"""

from __future__ import annotations

from typing import Protocol

# Local fallback constants — match the PyBoy values exactly. Used when
# PyBoy isn't importable (e.g. sdist-only unit tests) and shadowed by
# the PyBoy-side constants below when it is.
CYCLES_PER_EDGE_DMG: int = 512
CYCLES_PER_BYTE_DMG: int = 8 * CYCLES_PER_EDGE_DMG
SC_TRANSFER_ENABLE: int = 0x80
SC_CLOCK_SPEED: int = 0x02
SC_CLOCK_SOURCE: int = 0x01
IF_SERIAL: int = 0x08
ROLE_INTERNAL: int = 1
ROLE_EXTERNAL: int = 0
MAX_CYCLES: int = 1 << 31


# Try to pull the real implementations out of PyBoy. When the fork has
# Agent A's changes merged in, ``pyboy.core.serial`` exposes the
# bit-accurate ``Serial`` class plus the backend helpers. When PyBoy
# is missing entirely, fall back to local stubs so imports still work
# for non-integration unit tests.
try:
    from pyboy.utils import MAX_CYCLES as _PYBOY_MAX_CYCLES  # noqa: F401
    MAX_CYCLES = _PYBOY_MAX_CYCLES
except ImportError:  # pragma: no cover - exercised only without PyBoy
    pass


_PYBOY_AVAILABLE = False
try:
    from pyboy.core import serial as _pyboy_serial  # type: ignore
    _PYBOY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without PyBoy
    _pyboy_serial = None  # type: ignore


if _PYBOY_AVAILABLE:
    # --- Re-exports from the PyBoy native module ----------------------------

    # ``Serial`` is the bit-accurate class post-Agent-A. Alias it as
    # ``SerialCore`` for back-compat.
    _PyBoySerial = getattr(_pyboy_serial, "Serial")

    # Prefer PyBoy's own ``SerialCore`` alias if Agent A exported one;
    # otherwise use ``Serial`` directly.
    SerialCore = getattr(_pyboy_serial, "SerialCore", _PyBoySerial)  # type: ignore

    # Backend helpers — importable from the same module post-Agent-A.
    # If Agent A's fork isn't fully merged yet these may be absent;
    # fall through to the local stubs defined below.
    NullBackend = getattr(_pyboy_serial, "NullBackend", None)  # type: ignore
    LocalBackend = getattr(_pyboy_serial, "LocalBackend", None)  # type: ignore
    SerialBackend = getattr(_pyboy_serial, "SerialBackend", None)  # type: ignore

    # Constants — prefer PyBoy's values when exposed.
    CYCLES_PER_EDGE_DMG = getattr(
        _pyboy_serial, "CYCLES_PER_EDGE_DMG", CYCLES_PER_EDGE_DMG
    )
    CYCLES_PER_BYTE_DMG = getattr(
        _pyboy_serial, "CYCLES_PER_BYTE_DMG", CYCLES_PER_BYTE_DMG
    )
    SC_TRANSFER_ENABLE = getattr(
        _pyboy_serial, "SC_TRANSFER_ENABLE", SC_TRANSFER_ENABLE
    )
    SC_CLOCK_SPEED = getattr(_pyboy_serial, "SC_CLOCK_SPEED", SC_CLOCK_SPEED)
    SC_CLOCK_SOURCE = getattr(_pyboy_serial, "SC_CLOCK_SOURCE", SC_CLOCK_SOURCE)
    IF_SERIAL = getattr(_pyboy_serial, "IF_SERIAL", IF_SERIAL)
    ROLE_INTERNAL = getattr(_pyboy_serial, "ROLE_INTERNAL", ROLE_INTERNAL)
    ROLE_EXTERNAL = getattr(_pyboy_serial, "ROLE_EXTERNAL", ROLE_EXTERNAL)
else:
    # No PyBoy available — define no-op stubs so imports don't crash.
    SerialCore = None  # type: ignore
    NullBackend = None  # type: ignore
    LocalBackend = None  # type: ignore
    SerialBackend = None  # type: ignore


# If PyBoy didn't expose the backend helpers (partial Agent-A merge
# or PyBoy missing entirely), provide local fallback implementations so
# ``from pokered_harness.link.serial_core import NullBackend`` still
# works and the harness can exercise its own code paths.

if SerialBackend is None:
    class SerialBackend(Protocol):  # type: ignore[no-redef]
        """Peer side of the cable.

        Called by :class:`SerialCore` on each master-mode edge to fetch
        the peer's outgoing bit. Slave-mode edges bypass the backend
        and are driven via ``apply_external_edge`` by the coordinator /
        peer.
        """

        def on_edge(self, our_bit: int, our_role: int) -> int: ...


if NullBackend is None:
    class NullBackend:  # type: ignore[no-redef]
        """Disconnected cable — every received bit reads as ``1``."""

        def on_edge(self, our_bit: int, our_role: int) -> int:
            return 1


if LocalBackend is None:
    class LocalBackend:  # type: ignore[no-redef]
        """Two in-process backends bridged bit-at-a-time."""

        def __init__(self) -> None:
            self._peer: "LocalBackend | None" = None
            self._inbox: int | None = None

        @classmethod
        def pair(cls) -> tuple["LocalBackend", "LocalBackend"]:
            a, b = cls(), cls()
            a._peer = b
            b._peer = a
            return a, b

        @property
        def peer_ready(self) -> bool:
            return self._inbox is not None

        def on_edge(self, our_bit: int, our_role: int) -> int:
            if self._peer is None:
                return 1
            self._peer._inbox = our_bit & 1
            if self._inbox is None:
                return 1
            bit, self._inbox = self._inbox, None
            return bit


__all__ = [
    "CYCLES_PER_BYTE_DMG",
    "CYCLES_PER_EDGE_DMG",
    "IF_SERIAL",
    "LocalBackend",
    "MAX_CYCLES",
    "NullBackend",
    "ROLE_EXTERNAL",
    "ROLE_INTERNAL",
    "SC_CLOCK_SOURCE",
    "SC_CLOCK_SPEED",
    "SC_TRANSFER_ENABLE",
    "SerialBackend",
    "SerialCore",
]
