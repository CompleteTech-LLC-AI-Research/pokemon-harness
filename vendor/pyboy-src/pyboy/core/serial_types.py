#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""Python serial backends and immutable owner-boundary records.

The compiled Serial extension type and its .pxd stay in serial.py. This
module deliberately has no dependency on serial.py, avoiding an import
cycle while the facade re-exports these compatibility types.
"""

from typing import NamedTuple, Protocol


class SerialBackend(Protocol):
    """Peer side of the cable.

    Called by :class:`Serial` on each master-mode edge to fetch the
    peer's outgoing bit. Slave-mode edges bypass the backend and are
    driven via :meth:`Serial.apply_external_edge` by the coordinator /
    peer.
    """

    def on_edge(self, our_bit: int, our_role: int) -> int: ...


class NullBackend:
    """Disconnected cable.

    Pan Docs: the master's RX line is pulled high when no cable is
    attached, so every received bit is ``1`` and the full byte reads as
    ``0xFF``. Slave with no peer never edges, so :meth:`on_edge` is
    never called in slave mode.
    """

    def on_edge(self, our_bit: int, our_role: int) -> int:
        return 1


class SerialBackendError(RuntimeError):
    """An edge failed; the local emulator is quarantined until replaced."""


class OwnerBoundaryPre(NamedTuple):
    """Immutable metadata for a pre-commit owner boundary.

    ``event`` is the unchanged 12-field tuple delivered to the existing
    owner-pump callback.  The separate metadata object keeps that callback
    backwards compatible while giving a new observer named raw/physical
    times and the parent token for nested catch-up edges.
    """

    boundary_seq: int
    kind: int
    observed_cycles: int
    effective_cycles: int
    physical_epoch: int | None
    effective_physical_units: int | None
    parent_boundary_seq: int | None
    event: tuple[object, ...]


class OwnerBoundarySnapshot(NamedTuple):
    """Complete serial state captured at a post-commit boundary."""

    generation: int
    sb: int
    sc: int
    shift_register: int
    bits_remaining: int
    transfer_enabled: bool
    clock: int
    clock_target: int


class OwnerBoundaryPost(NamedTuple):
    """Immutable owner-thread post-commit record keyed by ``boundary_seq``."""

    boundary_seq: int
    kind: int
    observed_cycles: int
    effective_cycles: int
    physical_epoch: int | None
    effective_physical_units: int | None
    parent_boundary_seq: int | None
    committed: bool
    snapshot: OwnerBoundarySnapshot


class _OwnerBoundaryToken(NamedTuple):
    boundary_seq: int
    kind: int
    observed_cycles: int
    effective_cycles: int
    address: int
    value: int
    parent_boundary_seq: int | None


class LocalBackend:
    """Two in-process backends bridged bit-at-a-time.

    Use :meth:`pair` to build a linked pair. Correct behavior requires
    the coordinator to drive both cores to the same edge cycle before
    reading — under strict lockstep, each side's :meth:`on_edge` call
    deposits into the peer and drains the peer's previous deposit, so
    both sides see the other's real bit.

    Without a coordinator (first caller gets pull-up), that asymmetry
    is the documented limitation of the raw backend; use
    :class:`LockstepCoordinator` (a later milestone) to make it
    symmetric.
    """

    def __init__(self) -> None:
        self._peer: "LocalBackend | None" = None
        # Bit the peer deposited into us, waiting for us to consume.
        self._inbox: int | None = None

    @classmethod
    def pair(cls) -> tuple["LocalBackend", "LocalBackend"]:
        a, b = cls(), cls()
        a._peer = b
        b._peer = a
        return a, b

    @property
    def peer_ready(self) -> bool:
        """``True`` iff the peer has already deposited a bit this round."""
        return self._inbox is not None

    def on_edge(self, our_bit: int, our_role: int) -> int:
        if self._peer is None:
            # Unpaired LocalBackend behaves like NullBackend.
            return 1
        # Deposit our bit for the peer.
        self._peer._inbox = our_bit & 1
        # Drain any bit the peer left for us.
        if self._inbox is None:
            return 1  # pull-up default; lockstep will eliminate this path
        bit, self._inbox = self._inbox, None
        return bit

# Keep historical import/pickle paths for every relocated class, including
# the private token. The facade explicitly imports these exact objects.
for _exported_type in (
    SerialBackend,
    NullBackend,
    SerialBackendError,
    OwnerBoundaryPre,
    OwnerBoundarySnapshot,
    OwnerBoundaryPost,
    _OwnerBoundaryToken,
    LocalBackend,
):
    _exported_type.__module__ = "pyboy.core.serial"
del _exported_type

__all__ = [
    "SerialBackend",
    "NullBackend",
    "SerialBackendError",
    "OwnerBoundaryPre",
    "OwnerBoundarySnapshot",
    "OwnerBoundaryPost",
    "_OwnerBoundaryToken",
    "LocalBackend",
]
