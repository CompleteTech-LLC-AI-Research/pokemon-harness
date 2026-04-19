"""Paired-session orchestration for the link-cable feature.

:class:`LinkPair` owns two :class:`Session` instances, a shared
:class:`LinkTransport`, and (once paired) a :class:`SerialBridge`. The
serial bridge itself is an opaque collaborator — this module wires the
pair together and drives both sides in lockstep.
"""

from __future__ import annotations

from typing import Callable, Iterable, TYPE_CHECKING

from pokered_harness.link.symbols import LinkRole, symbols_for_role
from pokered_harness.link.transport import LinkTransport
from pokered_harness.session import RunUntilResult

if TYPE_CHECKING:
    from pokered_harness.session import Session


def _default_bridge_factory(*args, **kwargs):
    # Lazy import: SerialBridge is owned by another agent and may not be
    # importable in every environment (notably unit tests that inject a
    # fake factory). Keeping the import inside the function defers the
    # failure to the moment it actually matters.
    from pokered_harness.link.serial_bridge import SerialBridge

    return SerialBridge.from_sessions(*args, **kwargs)


class LinkPair:
    """Owns two paired Sessions, a transport, and (when paired) a SerialBridge.

    ``step(count=N)`` advances *each* side by N ticks (not 2N combined),
    interleaved in :data:`CHUNK_SIZE`-sized slices so serial hooks on the
    two sides can fire near-simultaneously.
    """

    #: Chunk size (in ticks) used to interleave the two sides during
    #: :meth:`step`. Smaller values produce finer interleaving at the
    #: cost of more Python overhead per emulated frame; 4 is a reasonable
    #: default — each side advances 4 frames before swapping.
    CHUNK_SIZE: int = 4

    def __init__(
        self,
        primary: "Session",
        peer: "Session",
        *,
        version_primary: str,
        version_peer: str,
        bridge_factory: Callable[..., object] | None = None,
    ) -> None:
        self._primary = primary
        self._peer = peer
        self._version_primary = version_primary
        self._version_peer = version_peer
        self._transport = LinkTransport()
        self._bridge_factory = bridge_factory or _default_bridge_factory
        self._bridge: object | None = None

    # --- properties ----------------------------------------------------

    @property
    def primary(self) -> "Session":
        return self._primary

    @property
    def peer(self) -> "Session":
        return self._peer

    @property
    def transport(self) -> LinkTransport:
        return self._transport

    @property
    def paired(self) -> bool:
        return self._bridge is not None

    # --- pair / unpair -------------------------------------------------

    def pair(self) -> None:
        """Build and install the bridge and register PROGRESS-role hooks.

        Raises :class:`RuntimeError` if already paired. PROGRESS hooks
        missing from a given ROM are silently skipped — they are
        non-required diagnostics.
        """
        if self._bridge is not None:
            raise RuntimeError("LinkPair is already paired; call unpair() first")

        bridge = self._bridge_factory(
            self._primary,
            self._peer,
            self._transport,
            version_a=self._version_primary,
            version_b=self._version_peer,
        )
        bridge.install()
        self._bridge = bridge

        for link_sym in symbols_for_role(LinkRole.PROGRESS):
            if link_sym.event_name is None:
                continue
            for session, version in (
                (self._primary, self._version_primary),
                (self._peer, self._version_peer),
            ):
                label = link_sym.per_version.get(version)
                if label is None:
                    continue
                try:
                    session.register_hook(label, link_sym.event_name)
                except (KeyError, LookupError):
                    pass

    def unpair(self) -> None:
        """Drop the bridge reference and clear the transport.

        PyBoy 2.7.0 has no ``hook_deregister``, so the hook callbacks that
        ``install()`` registered on the underlying PyBoy remain in place —
        but they closed over the now-dropped bridge object, so they become
        effectively no-ops once nothing else holds a reference. After
        unpair, :meth:`pair` may be called again; it will install a NEW
        bridge with NEW callbacks.
        """
        self._bridge = None
        self._transport.reset()

    # --- stepping ------------------------------------------------------

    #: Hardware-level serial IO register addresses (Game Boy common).
    _RSB_ADDR = 0xFF01      # serial data
    _RSC_ADDR = 0xFF02      # serial control (bit 7 = START, bit 0 = INTERNAL)
    _IF_ADDR = 0xFF0F       # interrupt flag; bit 3 = serial
    _SC_START = 0x80
    _IF_SERIAL = 0x08

    def _hardware_serial_tick(self) -> None:
        """Emulate one cycle of a physical link cable between the two peers.

        If either side has SC_START set in its serial-control register, we:
          1. Swap the outgoing rSB bytes between sides.
          2. Clear SC_START on both (signalling transfer complete).
          3. Raise the serial interrupt-flag bit on both so each side's
             own Serial ISR fires, consumes the byte, and updates
             hSerialReceiveData / hSerialConnectionStatus via the game's
             own code — no symbol hooks required.

        This is the cheapest faithful emulation of a GB link cable: PyBoy
        2.7.0 has no native serial peering, so we supply the missing
        hardware integration at the HRAM layer.
        """
        mem_a = self._primary._pyboy.memory
        mem_b = self._peer._pyboy.memory
        sc_a = mem_a[self._RSC_ADDR]
        sc_b = mem_b[self._RSC_ADDR]
        if not ((sc_a & self._SC_START) or (sc_b & self._SC_START)):
            return
        sb_a = mem_a[self._RSB_ADDR]
        sb_b = mem_b[self._RSB_ADDR]
        mem_a[self._RSB_ADDR] = sb_b
        mem_b[self._RSB_ADDR] = sb_a
        mem_a[self._RSC_ADDR] = sc_a & ~self._SC_START
        mem_b[self._RSC_ADDR] = sc_b & ~self._SC_START
        mem_a[self._IF_ADDR] = mem_a[self._IF_ADDR] | self._IF_SERIAL
        mem_b[self._IF_ADDR] = mem_b[self._IF_ADDR] | self._IF_SERIAL

    def step(self, count: int = 1, *, render: bool = False) -> None:
        """Advance BOTH sessions by ``count`` ticks each, interleaved.

        Semantics: ``step(N)`` leaves primary.current_tick() and
        peer.current_tick() each advanced by N, with the two sides
        interleaved in :data:`CHUNK_SIZE`-sized slices so hooks can fire
        near-simultaneously. (It is NOT 2N ticks combined.)

        When paired, each tick runs the hardware serial exchange so the
        two peers' serial ports behave like a physical link cable.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        remaining = count
        paired = self._bridge is not None
        while remaining > 0:
            slice_len = min(self.CHUNK_SIZE, remaining)
            if paired:
                # Per-frame interleave so the hardware-serial tick can run
                # between each emulator frame on both sides.
                for _ in range(slice_len):
                    self._primary.step(1, render=render)
                    self._peer.step(1, render=render)
                    self._hardware_serial_tick()
            else:
                # Unpaired: chunk ticks for less per-frame Python overhead.
                self._primary.step(slice_len, render=render)
                self._peer.step(slice_len, render=render)
            remaining -= slice_len

    def run_until_event_pair(
        self,
        event_names: str | Iterable[str],
        *,
        side: str = "primary",
        max_ticks: int,
        chunk: int = 16,
    ) -> RunUntilResult:
        """Step both sides until ``event_names`` fires on ``side``."""
        if max_ticks <= 0:
            raise ValueError(f"max_ticks must be positive, got {max_ticks}")
        if chunk <= 0:
            raise ValueError(f"chunk must be positive, got {chunk}")
        if side == "primary":
            watched = self._primary
        elif side == "peer":
            watched = self._peer
        else:
            raise ValueError(f"side must be 'primary' or 'peer', got {side!r}")

        wanted = (
            {event_names} if isinstance(event_names, str) else set(event_names)
        )
        if not wanted:
            raise ValueError("event_names must be non-empty")

        start_tick = watched.current_tick()
        deadline = start_tick + max_ticks

        while watched.current_tick() < deadline:
            for name in wanted:
                evt = watched.events.latest(name)
                if evt is not None and evt.tick > start_tick:
                    return RunUntilResult(
                        event=evt,
                        ticks_spent=watched.current_tick() - start_tick,
                    )
            ticks_left = deadline - watched.current_tick()
            self.step(min(chunk, ticks_left))

        for name in wanted:
            evt = watched.events.latest(name)
            if evt is not None and evt.tick > start_tick:
                return RunUntilResult(
                    event=evt,
                    ticks_spent=watched.current_tick() - start_tick,
                )
        return RunUntilResult(
            event=None, ticks_spent=watched.current_tick() - start_tick
        )


__all__ = ["LinkPair"]
