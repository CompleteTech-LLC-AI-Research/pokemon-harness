"""Paired-session orchestration for the link-cable feature.

:class:`LinkPair` owns two :class:`Session` instances, a shared
:class:`LinkTransport`, and (once paired) a :class:`SerialBridge`. The
serial bridge itself is an opaque collaborator — this module wires the
pair together and drives both sides in lockstep.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from threading import RLock
from typing import TYPE_CHECKING

from pokered_harness.events.hooks import (
    HookRegistration,
    RawHookRegistration,
    hooks_added_since,
    snapshot_hooks,
)
from pokered_harness.link.serial_link import validate_rom_version
from pokered_harness.link.symbols import LinkRole, symbols_for_role
from pokered_harness.link.transport import LinkTransport
from pokered_harness.session import RunUntilResult

_LOGGER = logging.getLogger(__name__)

_HookSnapshot = dict[
    tuple[int, int],
    tuple[tuple[Callable[[object], None], object], ...],
] | None
_SerialHookRecord = tuple[object, int, int, str]


def _validate_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value

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
        self._version_primary = validate_rom_version(version_primary)
        self._version_peer = validate_rom_version(version_peer)
        self._transport = LinkTransport()
        self._bridge_factory = bridge_factory or _default_bridge_factory
        self._bridge: object | None = None
        self._owned_hooks: list[HookRegistration] = []
        self._owned_raw_hooks: list[RawHookRegistration] = []
        self._owned_serial_hooks: list[tuple[object, tuple[object, int, int, str]]] = []
        self._lifecycle_lock = RLock()

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
        """Build and install the bridge and progress hooks atomically."""
        with self._lifecycle_lock:
            self._pair_locked()

    def _pair_locked(self) -> None:
        if self._bridge is not None:
            raise RuntimeError("LinkPair is already paired; call unpair() first")

        hook_baselines = self._hook_baselines()
        serial_baselines = self._serial_baselines()
        bridge = self._bridge_factory(
            self._primary,
            self._peer,
            self._transport,
            version_a=self._version_primary,
            version_b=self._version_peer,
        )
        owned_hooks: list[HookRegistration] = []
        owned_raw_hooks: list[RawHookRegistration] = []
        owned_serial_hooks: list[tuple[object, tuple[object, int, int, str]]] = []
        bridge_hook_addresses: list[tuple[object, int, int]] = []
        try:
            # Resolve before install so a partially installing bridge can be
            # cleaned up from its known role addresses if install raises.
            bridge_hook_addresses = self._bridge_hook_addresses_for(bridge)
            bridge.install()
            # SerialBridge installs its own raw PyBoy hooks through
            # Session.serial_hook(). Track their addresses as well: otherwise
            # a real PyBoy instance rejects the next pair() because those
            # breakpoints survive the old unpair(). A test/custom bridge that
            # exposes an explicit uninstall() is also given that opportunity
            # during rollback.
            self._capture_bridge_hooks(
                hook_baselines,
                bridge_hook_addresses,
                owned_raw_hooks,
            )
            self._capture_serial_hooks(serial_baselines, owned_serial_hooks)

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
                        handle = self._register_owned_event_hook(
                            session,
                            label,
                            link_sym.event_name,
                        )
                    except (KeyError, LookupError, ValueError):
                        # Optional progress labels can be absent or already
                        # owned by unrelated code. Leave the existing hook
                        # alone rather than replacing it.
                        continue
                    owned_hooks.append(handle)

            self._install_semantic_bridges(owned_hooks, owned_raw_hooks)
        except BaseException as exc:
            # Preserve the original installation exception while making the
            # observable state safe for a retry.
            self._capture_bridge_hooks(
                hook_baselines,
                bridge_hook_addresses,
                owned_raw_hooks,
            )
            self._capture_serial_hooks(serial_baselines, owned_serial_hooks)
            cleanup_errors = self._close_owned_hooks(owned_hooks)
            cleanup_errors.extend(self._close_owned_raw_hooks(owned_raw_hooks))
            self._deactivate_serial_hooks(owned_serial_hooks)
            self._uninstall_bridge(bridge)
            for cleanup_error in cleanup_errors:
                exc.add_note(f"pair rollback cleanup failed: {cleanup_error!r}")
            self._bridge = None
            self._owned_hooks = []
            self._owned_raw_hooks = []
            self._owned_serial_hooks = []
            self._transport.reset()
            raise

        self._owned_hooks = owned_hooks
        self._owned_raw_hooks = owned_raw_hooks
        self._owned_serial_hooks = owned_serial_hooks
        self._bridge = bridge

    @staticmethod
    def _uninstall_bridge(bridge: object) -> None:
        uninstall = getattr(bridge, "uninstall", None)
        if callable(uninstall):
            try:
                uninstall()
            except Exception:
                # Physical address cleanup below is still attempted. The
                # original pair() failure is more useful than teardown noise.
                _LOGGER.debug("bridge uninstall failed during rollback", exc_info=True)

    @staticmethod
    def _close_owned_hooks(handles: Iterable[HookRegistration]) -> list[Exception]:
        """Close all handles, continuing after an individual cleanup error."""
        errors: list[Exception] = []
        for handle in reversed(tuple(handles)):
            try:
                handle.close()
            except Exception as exc:
                # EventBus makes callbacks inert before attempting physical
                # removal. A failed physical deregistration must not prevent
                # the remaining owned callbacks from being closed.
                errors.append(exc)
                _LOGGER.debug(
                    "owned link hook close failed",
                    exc_info=True,
                )
        return errors

    @staticmethod
    def _close_owned_raw_hooks(
        handles: Iterable[RawHookRegistration],
    ) -> list[Exception]:
        """Close raw callbacks, continuing after an individual failure."""
        errors: list[Exception] = []
        for handle in reversed(tuple(handles)):
            try:
                handle.close()
            except Exception as exc:
                errors.append(exc)
                _LOGGER.debug(
                    "owned raw link hook close failed",
                    exc_info=True,
                )
        return errors

    def _hook_baselines(
        self,
    ) -> tuple[
        tuple[
            object,
            dict[
                tuple[int, int],
                tuple[tuple[Callable[[object], None], object], ...],
            ]
            | None,
        ],
        ...,
    ]:
        return tuple(
            (session._pyboy, snapshot_hooks(session._pyboy))
            for session in (self._primary, self._peer)
        )

    def _serial_baselines(self) -> tuple[tuple[object, int], ...]:
        return tuple(
            (session, len(getattr(session, "_serial_hooks", ())))
            for session in (self._primary, self._peer)
        )

    @staticmethod
    def _capture_bridge_hooks(
        baselines: tuple[
            tuple[
                object,
                dict[
                    tuple[int, int],
                    tuple[tuple[Callable[[object], None], object], ...],
                ]
                | None,
            ],
            ...,
        ],
        addresses: Iterable[tuple[object, int, int]],
        owned: list[RawHookRegistration],
    ) -> None:
        """Capture only callbacks added by bridge installation."""
        wanted = {(id(pyboy), bank, addr) for pyboy, bank, addr in addresses}
        for pyboy, before in baselines:
            after = snapshot_hooks(pyboy)
            for bank, addr, callback, context in hooks_added_since(before, after):
                # A real SerialBridge exposes its resolved role map, which
                # narrows ownership to BRIDGE/HANDSHAKE addresses. A custom
                # bridge may not; immediately after install every callback
                # newly visible in the transaction is then bridge-owned.
                if wanted and (id(pyboy), bank, addr) not in wanted:
                    continue
                if any(
                    existing.pyboy is pyboy
                    and existing.bank == bank
                    and existing.addr == addr
                    and existing.callback is callback
                    and existing.context is context
                    for existing in owned
                ):
                    continue
                owned.append(
                    RawHookRegistration(
                        pyboy=pyboy,
                        bank=bank,
                        addr=addr,
                        callback=callback,
                        context=context,
                    )
                )

    @staticmethod
    def _capture_serial_hooks(
        baselines: tuple[tuple[object, int], ...],
        owned: list[tuple[object, tuple[object, int, int, str]]],
    ) -> None:
        """Record guarded serial callbacks added by a bridge."""
        for session, start in baselines:
            serial_hooks = getattr(session, "_serial_hooks", ())
            for record in serial_hooks[start:]:
                if len(record) != 4:
                    continue
                if any(
                    existing_session is session and existing_record[0] is record[0]
                    for existing_session, existing_record in owned
                ):
                    continue
                owned.append((session, record))

    @staticmethod
    def _deactivate_serial_hooks(
        owned: Iterable[tuple[object, tuple[object, int, int, str]]],
    ) -> None:
        """Disable and forget only the bridge's guarded serial records."""
        grouped: dict[int, tuple[object, list[tuple[object, int, int, str]]]] = {}
        for session, record in owned:
            grouped.setdefault(id(session), (session, []))[1].append(record)

        for session, records in grouped.values():
            lock = getattr(session, "_lock", None)

            def _deactivate() -> None:
                states = {id(record[0]) for record in records}
                for record in records:
                    state = record[0]
                    if hasattr(state, "active"):
                        state.active = False
                serial_hooks = getattr(session, "_serial_hooks", None)
                if isinstance(serial_hooks, list):
                    serial_hooks[:] = [
                        candidate
                        for candidate in serial_hooks
                        if not candidate or id(candidate[0]) not in states
                    ]

            if lock is None:
                _deactivate()
            else:
                # Session.serial_hook guards callback execution with this
                # same lock. Waiting here prevents an in-flight callback
                # from touching memory or transport after teardown returns.
                with lock:
                    _deactivate()

    @staticmethod
    def _close_owned_raw_hooks_at(
        owned_raw_hooks: Iterable[RawHookRegistration],
        session: Session,
        bank: int,
        addr: int,
    ) -> list[Exception]:
        """Release pair-owned raw hooks before an intentional replacement."""
        errors: list[Exception] = []
        for handle in reversed(tuple(owned_raw_hooks)):
            if handle.pyboy is not session._pyboy or handle.bank != bank or handle.addr != addr:
                continue
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001 - cleanup continues per handle
                errors.append(exc)
        return errors

    def _bridge_hook_addresses_for(
        self, bridge: object
    ) -> list[tuple[object, int, int]]:
        """Return addresses occupied by the bridge's Session.serial_hook calls.

        ``SerialBridge`` keeps its resolved map privately, so use it when
        available for exact ownership. The symbol-derived fallback supports
        injected bridge implementations and remains limited to BRIDGE and
        HANDSHAKE symbols.
        """
        addresses: list[tuple[object, int, int]] = []
        resolved_pairs = (
            (self._primary, getattr(bridge, "_resolved_a", None)),
            (self._peer, getattr(bridge, "_resolved_b", None)),
        )
        bridge_roles = (
            *symbols_for_role(LinkRole.BRIDGE),
            *symbols_for_role(LinkRole.HANDSHAKE),
        )
        for session, resolved in resolved_pairs:
            if isinstance(resolved, dict):
                for link_sym in bridge_roles:
                    location = resolved.get(link_sym.key)
                    if location is not None:
                        bank, addr = location
                        addresses.append((session._pyboy, bank, addr))
                continue

            # A custom bridge that does not expose resolved hook ownership is
            # deliberately not guessed at: removing an address merely because
            # its symbol exists could destroy an unrelated callback.
        return addresses

    def _register_owned_symbol_hook(
        self,
        session: Session,
        symbol_name: str,
        callback: Callable[[object], None],
    ) -> HookRegistration:
        return session.register_hook_at(symbol_name, callback)

    def _register_owned_event_hook(
        self,
        session: Session,
        symbol_name: str,
        event_name: str,
    ) -> HookRegistration:
        return session.register_hook(symbol_name, event_name)

    def _register_owned_address_hook(
        self,
        session: Session,
        bank: int,
        addr: int,
        callback: Callable[[object], None],
    ) -> HookRegistration:
        return session.register_hook_at_address(bank, addr, callback)

    def _install_semantic_bridges(
        self,
        owned_hooks: list[HookRegistration],
        owned_raw_hooks: list[RawHookRegistration],
    ) -> None:
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

        def _buffer_addresses(
            session: Session, key: str
        ) -> tuple[int, int] | None:
            send_tags = (
                f"wSerial{key}SendData",
                f"wLink{key}SendBuffer",
            )
            receive_tags = (
                f"wSerial{key}ReceiveData",
                f"wLink{key}ReceiveBuffer",
            )
            for send_tag, receive_tag in zip(send_tags, receive_tags):
                if send_tag in session.symbols and receive_tag in session.symbols:
                    return (
                        session.symbols.addr_of(send_tag),
                        session.symbols.addr_of(receive_tag),
                    )
            return None

        def _install_pair(label: str, widths: dict[str, int]) -> None:
            """Install a 1:1 send→receive copy for a pret serial routine."""
            for key, width in widths.items():
                addresses_a = _buffer_addresses(pa, key)
                addresses_b = _buffer_addresses(pb, key)
                if addresses_a is None or addresses_b is None:
                    continue
                send_a, recv_a = addresses_a
                send_b, recv_b = addresses_b

                def _copy_to_a(_ctx, _w=width, _s=send_b, _r=recv_a):
                    for i in range(_w):
                        mem_a[_r + i] = mem_b[_s + i]

                def _copy_to_b(_ctx, _w=width, _s=send_a, _r=recv_b):
                    for i in range(_w):
                        mem_b[_r + i] = mem_a[_s + i]

                for session, callback in (
                    (pa, _copy_to_a),
                    (pb, _copy_to_b),
                ):
                    try:
                        handle = self._register_owned_symbol_hook(
                            session, label, callback
                        )
                    except (KeyError, LookupError, ValueError):
                        # These semantic bridges are optional for a ROM whose
                        # symbol set differs. Do not disturb another hook at
                        # an occupied address.
                        continue
                    owned_hooks.append(handle)

        _install_pair("Serial_ExchangeNybble", {"ExchangeNybble": 1})
        _install_pair("Serial_ExchangeLinkMenuSelection", {"MenuSelection": 2})

        self._install_linkmenu_autoselect_trade(owned_hooks)
        self._install_exchange_bytes_skip(owned_hooks, owned_raw_hooks)

    def _install_exchange_bytes_skip(
        self,
        owned_hooks: list[HookRegistration],
        owned_raw_hooks: list[RawHookRegistration],
    ) -> None:
        """Short-circuit ``Serial_ExchangeBytes`` to copy peer's send buffer
        into this side's receive buffer and RET immediately.

        The function's parameters are ``hl`` = local send-buffer addr,
        ``de`` = local receive-buffer addr, ``bc`` = byte count. The
        peer's send pointer is translated by the shared buffer symbol name
        when the ROM families use different WRAM layouts.

        Without this skip ``Serial_ExchangeBytes`` spin-waits in its
        byte-by-byte serial loop, which PyBoy's silent rSB-write
        rejection makes impossible to complete. Skipping and
        pre-populating the recv buffer lets the game's trade-data-block
        exchange (party data, random numbers, patch lists) complete.
        """
        pa, pb = self._primary, self._peer
        mem_a, mem_b = pa._pyboy.memory, pb._pyboy.memory
        pba, pbb = pa._pyboy, pb._pyboy

        candidate_names = (
            "wSerialPlayerDataBlock",
            "wSerialRandomNumberListBlock",
            "wSerialPartyMonsPatchList",
            "wSerialEnemyDataBlock",
            "wSerialOtherGameboyRandomNumberListBlock",
            "wSerialEnemyMonsPatchList",
        )

        def _buffer_name(session: Session, address: int) -> str | None:
            for name in candidate_names:
                if name in session.symbols and session.symbols.addr_of(name) == address:
                    return name
            try:
                names = session.symbols.names_at(0, address)
            except (AttributeError, KeyError):
                return None
            return next((name for name in names if name in candidate_names), None)

        def _peer_send_address(
            local_session: Session, peer_session: Session, address: int
        ) -> int:
            """Translate a local serial buffer pointer to the peer's address."""
            name = _buffer_name(local_session, address)
            if name is None or name not in peer_session.symbols:
                # Custom/test symbol tables and same-layout ROMs may not carry
                # the curated buffer labels. Preserve the legacy same-address
                # behavior for those inputs; known cross-version buffers use
                # the symbol-specific address below.
                return address
            return peer_session.symbols.addr_of(name)

        def skip(this_pb, this_mem, peer_mem, local_session, peer_session):
            rf = this_pb.register_file
            hl = rf.HL
            de = (rf.D << 8) | rf.E
            bc = (rf.B << 8) | rf.C
            peer_hl = _peer_send_address(local_session, peer_session, hl)
            for i in range(bc):
                this_mem[(de + i) & 0xFFFF] = peer_mem[(peer_hl + i) & 0xFFFF]
            sp = rf.SP
            rf.PC = (
                this_mem[(sp + 1) & 0xFFFF] << 8
            ) | this_mem[sp & 0xFFFF]
            rf.SP = (sp + 2) & 0xFFFF
            rf.HL = (hl + bc) & 0xFFFF
            new_de = (de + bc) & 0xFFFF
            rf.D = new_de >> 8
            rf.E = new_de & 0xFF
            rf.B = 0
            rf.C = 0
            # Serial_ExchangeBytes ends with `xor a; ret` in pokered. Keep
            # the flags observable by the caller identical to a successful
            # hardware exchange instead of leaking the prior hook state.
            rf.A = 0
            rf.F = 0x80

        for session, peer_session, this_pb, this_mem, peer_mem in (
            (pa, pb, pba, mem_a, mem_b),
            (pb, pa, pbb, mem_b, mem_a),
        ):
            if "Serial_ExchangeBytes" not in session.symbols:
                continue

            def _skip_callback(
                _ctx: object,
                *,
                _this_pb=this_pb,
                _this_mem=this_mem,
                _peer_mem=peer_mem,
                _session=session,
                _peer_session=peer_session,
            ) -> None:
                skip(
                    _this_pb,
                    _this_mem,
                    _peer_mem,
                    _session,
                    _peer_session,
                )

            bank, addr = session.symbols.bank_addr("Serial_ExchangeBytes")
            cleanup_errors = self._close_owned_raw_hooks_at(
                owned_raw_hooks,
                session,
                bank,
                addr,
            )
            if cleanup_errors:
                raise RuntimeError(
                    "could not replace the pair-owned Serial_ExchangeBytes hook"
                ) from cleanup_errors[0]
            try:
                handle = self._register_owned_symbol_hook(
                    session,
                    "Serial_ExchangeBytes",
                    _skip_callback,
                )
            except (KeyError, LookupError, ValueError):
                # An occupied address not owned by this pair is left alone;
                # replacing it would destroy an unrelated callback.
                continue
            owned_hooks.append(handle)

    def _install_linkmenu_autoselect_trade(
        self, owned_hooks: list[HookRegistration]
    ) -> None:
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
        recv_addr_a = pa.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
        recv_addr_b = pb.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")

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

        def force_trade_a(_ctx: object) -> None:
            mem_a[recv_addr_a] = 0xd4
            mem_a[recv_addr_a + 1] = 0xd4

        def force_trade_b(_ctx: object) -> None:
            mem_b[recv_addr_b] = 0xd4
            mem_b[recv_addr_b + 1] = 0xd4

        try:
            owned_hooks.append(
                self._register_owned_address_hook(pa, bank_a, addr_a, force_trade_a)
            )
        except (KeyError, LookupError, ValueError):
            pass
        try:
            owned_hooks.append(
                self._register_owned_address_hook(pb, bank_b, addr_b, force_trade_b)
            )
        except (KeyError, LookupError, ValueError):
            pass

    def unpair(self) -> None:
        """Remove pair-owned hooks and transport state atomically."""
        with self._lifecycle_lock:
            self._unpair_locked()

    def _unpair_locked(self) -> None:
        owned_hooks = self._owned_hooks
        owned_raw_hooks = self._owned_raw_hooks
        owned_serial_hooks = self._owned_serial_hooks
        bridge = self._bridge
        self._owned_hooks = []
        self._owned_raw_hooks = []
        self._owned_serial_hooks = []
        self._bridge = None

        self._deactivate_serial_hooks(owned_serial_hooks)
        cleanup_errors = self._close_owned_hooks(owned_hooks)
        cleanup_errors.extend(self._close_owned_raw_hooks(owned_raw_hooks))
        if bridge is not None:
            self._uninstall_bridge(bridge)
        self._transport.reset()
        if cleanup_errors:
            raise RuntimeError("one or more pair hooks could not be removed") from cleanup_errors[0]

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
        """Advance both sessions by ``count`` ticks under the pair lifecycle lock."""
        count = _validate_positive_int(count, "count")
        with self._lifecycle_lock:
            self._step_locked(count, render=render)

    def _step_locked(self, count: int, *, render: bool) -> None:
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
        max_ticks = _validate_positive_int(max_ticks, "max_ticks")
        chunk = _validate_positive_int(chunk, "chunk")
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
