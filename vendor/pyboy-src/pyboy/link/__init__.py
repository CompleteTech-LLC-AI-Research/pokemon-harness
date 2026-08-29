"""PyBoy link-cable support.

See ``pyboy/core/serial.py`` for the bit-accurate serial core, and
this package's modules for coordinator, session, and network transport
layers. The core types a user imports:

    from pyboy.link import LinkSession

    a = PyBoy("red.gb", window="null")
    b = PyBoy("blue.gb", window="null")
    link = LinkSession.local()
    link.attach(a)
    link.attach(b)
    while not done:
        link.step()

Design notes live in ``docs/link-cable.md`` (to be added alongside
this PR).
"""

from pyboy.core.serial import (
    CYCLES_PER_BYTE_DMG,
    CYCLES_PER_EDGE_DMG,
    LocalBackend,
    NullBackend,
    SerialBackend,
    SerialCore,
)
from pyboy.link.coordinator import (
    CoordinatedBackend,
    LockstepCoordinator,
)
from pyboy.link.network import (
    NetworkBackend,
    NetworkBackendError,
)
from pyboy.link.session import LinkSession

__all__ = [
    "CoordinatedBackend",
    "CYCLES_PER_BYTE_DMG",
    "CYCLES_PER_EDGE_DMG",
    "LinkSession",
    "LocalBackend",
    "LockstepCoordinator",
    "NetworkBackend",
    "NetworkBackendError",
    "NullBackend",
    "SerialBackend",
    "SerialCore",
]
