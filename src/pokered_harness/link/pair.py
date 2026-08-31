"""Paired-session orchestration for the link-cable feature.

:class:`LinkPair` owns two :class:`Session` instances, a shared
:class:`LinkTransport`, and (once paired) a :class:`SerialBridge`. The
serial bridge itself is an opaque collaborator — this module wires the
pair together and drives both sides in lockstep.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

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
        primary: Session,
        peer: Session,
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
    def primary(self) -> Session:
        return self._primary

    @property
    def peer(self) -> Session:
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

        self._install_semantic_bridges()

    def _install_semantic_bridges(self) -> None:
        """Hook pret-level link protocol routines and bridge their WRAM
        data cells between the two peers.

        PyBoy 2.7.0 doesn't let us write the serial-data register rSB
        (0xFF01) — writes at the memory interface are silently dropped —
        so we cannot emulate the hardware exchange. Instead, for each
        protocol function we know about, we intercept the entry and
        copy the *peer*'s send cell into *this* side's receive cell.
        The game code then reads its receive cell and proceeds as
        though the real serial exchange completed.

        Cells covered:

        - ``Serial_ExchangeNybble`` →
          ``wSerialExchangeNybbleSendData`` /
          ``wSerialExchangeNybbleReceiveData``
        - ``Serial_ExchangeLinkMenuSelection`` → two-byte
          ``wLinkMenuSelectionSendBuffer`` /
          ``wLinkMenuSelectionReceiveBuffer``

        Missing symbols on either side are silently skipped.
        """
        pa, pb = self._primary, self._peer
        mem_a, mem_b = pa._pyboy.memory, pb._pyboy.memory

        def _install_pair(label: str, widths: dict[str, int]) -> None:
            """Install a 1:1 send→receive copy for a pret serial routine."""
            for key, width in widths.items():
                send_tag = f"wSerial{key}SendData"
                recv_tag = f"wSerial{key}ReceiveData"
                alt_send = f"wLink{key}SendBuffer"
                alt_recv = f"wLink{key}ReceiveBuffer"
                send = None
                recv = None
                for s_tag, r_tag in ((send_tag, recv_tag), (alt_send, alt_recv)):
                    if s_tag in pa.symbols and r_tag in pa.symbols:
                        send = pa.symbols.addr_of(s_tag)
                        recv = pa.symbols.addr_of(r_tag)
                        break
                if send is None or recv is None:
                    continue
                send_b, recv_b = send, recv  # same addr on both versions

                def _copy_to_a(_ctx, _w=width, _s=send_b, _r=recv_b):
                    for i in range(_w):
                        mem_a[_r + i] = mem_b[_s + i]

                def _copy_to_b(_ctx, _w=width, _s=send_b, _r=recv_b):
                    for i in range(_w):
                        mem_b[_r + i] = mem_a[_s + i]

                try:
                    pa.serial_hook(label, _copy_to_a)
                    pb.serial_hook(label, _copy_to_b)
                except (KeyError, LookupError):
                    pass

        _install_pair("Serial_ExchangeNybble", {"ExchangeNybble": 1})
        _install_pair("Serial_ExchangeLinkMenuSelection", {"MenuSelection": 2})

        self._install_linkmenu_autoselect_trade()
        self._install_exchange_bytes_skip()

    def _install_exchange_bytes_skip(self) -> None:
        """Short-circuit ``Serial_ExchangeBytes`` to copy peer's send buffer
        into this side's receive buffer and RET immediately.

        The function's parameters are ``hl`` = send-buffer addr,
        ``de`` = receive-buffer addr, ``bc`` = byte count. Both peers
        have identical WRAM layouts so ``peer_memory[hl..hl+bc]`` is
        the peer's send bytes for the same logical buffer.

        Without this skip ``Serial_ExchangeBytes`` spin-waits in its
        byte-by-byte serial loop, which PyBoy's silent rSB-write
        rejection makes impossible to complete. Skipping and
        pre-populating the recv buffer lets the game's trade-data-block
        exchange (party data, random numbers, patch lists) complete.
        """
        pa, pb = self._primary, self._peer
        mem_a, mem_b = pa._pyboy.memory, pb._pyboy.memory
        pba, pbb = pa._pyboy, pb._pyboy

        if "Serial_ExchangeBytes" not in pa.symbols:
            return
        bank, addr = pa.symbols.bank_addr("Serial_ExchangeBytes")

        def skip(this_pb, this_mem, peer_mem):
            rf = this_pb.register_file
            hl = rf.HL
            de = (rf.D << 8) | rf.E
            bc = (rf.B << 8) | rf.C
            for i in range(bc):
                this_mem[de + i] = peer_mem[hl + i]
            sp = rf.SP
            rf.PC = (this_mem[sp + 1] << 8) | this_mem[sp]
            rf.SP = (sp + 2) & 0xFFFF
            rf.HL = (hl + bc) & 0xFFFF
            new_de = (de + bc) & 0xFFFF
            rf.D = new_de >> 8
            rf.E = new_de & 0xFF
            rf.B = 0
            rf.C = 0

        deregister_a = getattr(pba, "hook_deregister", None)
        if callable(deregister_a):
            try:
                deregister_a(bank, addr)
            except ValueError:
                # PyBoy raises when no callback is registered at the
                # address.  That is a normal case for a ROM/session whose
                # legacy hook was never installed.
                pass
        deregister_b = getattr(pbb, "hook_deregister", None)
        if callable(deregister_b):
            try:
                deregister_b(bank, addr)
            except ValueError:
                pass
        pba.hook_register(bank, addr, lambda _: skip(pba, mem_a, mem_b), None)
        pbb.hook_register(bank, addr, lambda _: skip(pbb, mem_b, mem_a), None)

    def _install_linkmenu_autoselect_trade(self) -> None:
        """Auto-select TRADE in the Cable Club LinkMenu.

        LinkMenu's ``.exchangeMenuSelectionLoop`` calls
        ``Serial_ExchangeLinkMenuSelection`` then reads
        ``wLinkMenuSelectionReceiveBuffer``. The handshake byte format
        is ``0xd0 | (keys_A_B << 2) | menu_item``; a value of ``0xd4``
        means "enemy pressed A while cursor was on TRADE" which takes
        the game through
        ``.enemyPressedAOrB`` → ``.useEnemyMenuSelection`` →
        ``.doneChoosingMenuSelection`` → ``.updateCursorPosition`` →
        ``wCableClubDestinationMap = TRADE_CENTER`` → ``SpecialEnterMap``.
        Both peers follow this path in parallel and warp into
        ``TRADE_CENTER`` (map 0xef).

        Hook location: ``LinkMenu.exchangeMenuSelectionLoop + 3`` bytes
        (past the ``call`` instruction) — the first instruction that
        reads the receive buffer after the serial exchange returns.
        Writing ``0xd4`` there races ahead of every other game read.

        If the game is driven via some other path that needs BATTLE
        instead of TRADE this hook would select wrong; a future
        knob could expose per-pair menu-item choice.
        """
        pa, pb = self._primary, self._peer
        mem_a, mem_b = pa._pyboy.memory, pb._pyboy.memory

        for session, mem, label in (
            (pa, mem_a, "wLinkMenuSelectionReceiveBuffer"),
            (pb, mem_b, "wLinkMenuSelectionReceiveBuffer"),
        ):
            if label not in session.symbols:
                return
        recv_addr = pa.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")

        # Locate the post-exchange read instruction. Both Blue and Yellow
        # label ``LinkMenu.exchangeMenuSelectionLoop`` — the first
        # instruction at that label is the ``call``, so +3 bytes is the
        # following ``ld a, [wLinkMenuSelectionReceiveBuffer]``.
        label = "LinkMenu.exchangeMenuSelectionLoop"
        if label not in pa.symbols or label not in pb.symbols:
            return
        bank_a, addr_a = pa.symbols.bank_addr(label)
        bank_b, addr_b = pb.symbols.bank_addr(label)
        addr_a += 3
        addr_b += 3

        def force_trade_a(_ctx):
            mem_a[recv_addr] = 0xd4
            mem_a[recv_addr + 1] = 0xd4

        def force_trade_b(_ctx):
            mem_b[recv_addr] = 0xd4
            mem_b[recv_addr + 1] = 0xd4

        try:
            pa._pyboy.hook_register(bank_a, addr_a, force_trade_a, None)
        except ValueError:
            pass
        try:
            pb._pyboy.hook_register(bank_b, addr_b, force_trade_b, None)
        except ValueError:
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
    _HRAM_STATUS_ADDR = 0xFFAA  # hSerialConnectionStatus
    _SC_START = 0x80
    _IF_SERIAL = 0x08
    _STATUS_NOT_ESTABLISHED = 0xFF
    _STATUS_EXTERNAL = 0x01  # slave (peer drives the clock)
    _STATUS_INTERNAL = 0x02  # master (drives the clock)
    _ESTABLISH_INTERNAL = 0x01
    _ESTABLISH_EXTERNAL = 0x02

    def _hardware_serial_tick(self) -> None:
        """Clear SC_START and raise the serial-interrupt flag so the game's
        serial interrupt handler doesn't permanently block on an
        unserviced transfer.

        Historical note: an earlier version of this method also swapped
        the rSB (0xFF01) bytes between the two peers to emulate the
        full hardware serial exchange. That approach does not work:
        PyBoy 2.7.0 silently rejects writes to 0xFF01 at the memory
        interface, so the swap was cosmetic. The actual byte bridging
        happens at the semantic WRAM layer: callers install hooks on
        Serial_ExchangeNybble / Serial_ExchangeLinkMenuSelection that
        write the peer's send buffer into this side's receive buffer.
        See the scripts/link_trade_demo.py walker for an example.

        What this method still contributes: without clearing SC_START
        and raising IF-bit 3, the game's Serial:: ISR never fires and
        hSerialReceivedNewData stays 0 — which breaks the sync-nybble
        protocol's "received-new-data" short-circuit.
        """
        mem_a = self._primary._pyboy.memory
        mem_b = self._peer._pyboy.memory
        sc_a = mem_a[self._RSC_ADDR]
        sc_b = mem_b[self._RSC_ADDR]
        if not ((sc_a & self._SC_START) or (sc_b & self._SC_START)):
            return
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
