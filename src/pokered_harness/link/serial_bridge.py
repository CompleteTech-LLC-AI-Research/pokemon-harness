"""Serial-routine interception bridge between two paired Pokemon sessions.

PyBoy 2.7.0 does not expose SB/SC or the serial interrupt, so instead of
intercepting at the hardware layer we install PyBoy execution hooks at
pret symbol labels. Each BRIDGE-role label corresponds to a short serial
helper in ``home/serial.asm``; when the game enters that helper the bridge
short-circuits the real transfer by reading the outgoing byte from each
side's HRAM, exchanging through a :class:`LinkTransport`, and writing the
results back into both sides' HRAM receive cells plus a success status.

HANDSHAKE-role labels just flip the "connected" status byte to 0x01 on
both sides so the game proceeds as if the handshake protocol succeeded.

The three HRAM labels (:data:`HRAM_SERIAL_SEND`, :data:`HRAM_SERIAL_RECEIVE`,
:data:`HRAM_SERIAL_STATUS`) are required on every ROM this bridge runs
against and are validated at construction time by
:meth:`SerialBridge.from_sessions`.

Teardown note: :meth:`uninstall` disables every callback installed by this
bridge and removes the physical callback when the PyBoy runtime exposes an
identity-preserving hook table. On runtimes without callback enumeration or
``hook_deregister``, the guarded callback is still made inert; callers must
still tear down both underlying sessions before reusing their emulator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

from pokered_harness.events.hooks import (
    RawHookRegistration,
    hooks_added_since,
    snapshot_hooks,
)
from pokered_harness.link.serial_link import validate_rom_version
from pokered_harness.link.symbols import (
    LINK_SYMBOLS,
    LinkRole,
    LinkSymbol,
    resolve_link_symbols,
)
from pokered_harness.link.transport import LinkTransport

if TYPE_CHECKING:
    from pokered_harness.session import Session


HRAM_SERIAL_SEND = "hSerialSendData"
HRAM_SERIAL_RECEIVE = "hSerialReceiveData"
HRAM_SERIAL_STATUS = "hSerialConnectionStatus"

# pret/pokered/constants/serial_constants.asm:
#   USING_EXTERNAL_CLOCK  = 0x01  (slave - peer drives the clock)
#   USING_INTERNAL_CLOCK  = 0x02  (master - drives the clock)
# In pokered's link protocol the two peers must have opposite clock
# roles so the sync-nybble counter logic converges. Our bridge assigns
# the primary as external clock (slave) and the peer as internal clock
# (master); this matches the real Game Boy convention where the cable
# is symmetrically connected but the two carts negotiate roles.
_STATUS_PRIMARY_EXTERNAL = 0x01
_STATUS_PEER_INTERNAL = 0x02


@dataclass(frozen=True, slots=True)
class BridgeEndpoint:
    """One side of the bridge — a Session plus its resolved HRAM addresses."""

    session: Session
    send_addr: int
    receive_addr: int
    status_addr: int


def _require_hram(session: Session, label: str) -> int:
    sym = session.symbols.get(label)
    if sym is None:
        raise LookupError(f"required HRAM label missing: {label!r}")
    return sym.addr


def _label_on(session: Session, link_sym: LinkSymbol, version: str | None = None) -> str | None:
    """Return whichever per_version label for ``link_sym`` exists in this
    session's SymbolTable, or None if none of them do.

    A version is preferred when available. This matters for registries whose
    candidate labels differ between ROM families; falling through the mapping
    order can otherwise install a valid hook for the wrong ROM variant.
    """
    if version is not None:
        label = link_sym.per_version.get(version)
        if label is None or session.symbols.get(label) is None:
            return None
        return label
    for label in link_sym.per_version.values():
        if session.symbols.get(label) is not None:
            return label
    return None


class SerialBridge:
    """Bridges two Pokemon Sessions via pret serial-routine hooks.

    Construct via :meth:`from_sessions` for the typical case; the raw
    constructor takes pre-resolved symbol dicts so tests and custom
    orchestrators can supply their own resolution.
    """

    def __init__(
        self,
        *,
        endpoint_a: BridgeEndpoint,
        endpoint_b: BridgeEndpoint,
        transport: LinkTransport,
        resolved_a: dict[str, tuple[int, int]],
        resolved_b: dict[str, tuple[int, int]],
        version_a: str | None = None,
        version_b: str | None = None,
    ) -> None:
        self._ea = endpoint_a
        self._eb = endpoint_b
        self._transport = transport
        self._resolved_a = dict(resolved_a)
        self._resolved_b = dict(resolved_b)
        self._version_a = validate_rom_version(version_a) if version_a is not None else None
        self._version_b = validate_rom_version(version_b) if version_b is not None else None
        self._installed = False
        self._lifecycle_lock = RLock()
        self._owned_serial_hooks: list[tuple[Session, tuple[object, int, int, str]]] = []
        self._owned_raw_hooks: list[RawHookRegistration] = []

    @classmethod
    def from_sessions(
        cls,
        session_a: Session,
        session_b: Session,
        transport: LinkTransport,
        *,
        version_a: str,
        version_b: str,
    ) -> SerialBridge:
        version_a = validate_rom_version(version_a)
        version_b = validate_rom_version(version_b)
        resolved_a = resolve_link_symbols(session_a.symbols, version_a)
        resolved_b = resolve_link_symbols(session_b.symbols, version_b)
        endpoint_a = BridgeEndpoint(
            session=session_a,
            send_addr=_require_hram(session_a, HRAM_SERIAL_SEND),
            receive_addr=_require_hram(session_a, HRAM_SERIAL_RECEIVE),
            status_addr=_require_hram(session_a, HRAM_SERIAL_STATUS),
        )
        endpoint_b = BridgeEndpoint(
            session=session_b,
            send_addr=_require_hram(session_b, HRAM_SERIAL_SEND),
            receive_addr=_require_hram(session_b, HRAM_SERIAL_RECEIVE),
            status_addr=_require_hram(session_b, HRAM_SERIAL_STATUS),
        )
        return cls(
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            transport=transport,
            resolved_a=resolved_a,
            resolved_b=resolved_b,
            version_a=version_a,
            version_b=version_b,
        )

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> None:
        with self._lifecycle_lock:
            if self._installed:
                raise RuntimeError("SerialBridge.install called twice")
            if self._owned_serial_hooks or self._owned_raw_hooks:
                raise RuntimeError(
                    "SerialBridge.install cannot proceed while callback cleanup "
                    "is pending"
                )
            baselines = self._serial_hook_baselines()
            hook_baselines = self._physical_hook_baselines()
            try:
                self._install_side(
                    self._ea,
                    self._resolved_a,
                    self._eb,
                    is_a=True,
                    version=self._version_a,
                )
                self._install_side(
                    self._eb,
                    self._resolved_b,
                    self._ea,
                    is_a=False,
                    version=self._version_b,
                )
                self._capture_owned_hooks(baselines, hook_baselines)
            except BaseException as exc:
                self._capture_owned_hooks(baselines, hook_baselines)
                cleanup_errors = self._uninstall_locked()
                for cleanup_error in cleanup_errors:
                    exc.add_note(f"bridge install rollback cleanup failed: {cleanup_error!r}")
                raise
            self._installed = True

    def uninstall(self) -> None:
        """Disable and remove callbacks owned by this bridge.

        The method is idempotent and safe to call after a failed install. It
        intentionally removes only callback identities captured during this
        bridge's install transaction, preserving unrelated hooks at the same
        address when the runtime permits inspection.
        """
        with self._lifecycle_lock:
            cleanup_errors = self._uninstall_locked()
            if cleanup_errors:
                raise RuntimeError(
                    "one or more serial bridge callbacks could not be removed"
                ) from cleanup_errors[0]

    # --- install / callback helpers ------------------------------------

    def _install_side(
        self,
        side: BridgeEndpoint,
        resolved: dict[str, tuple[int, int]],
        peer: BridgeEndpoint,
        *,
        is_a: bool,
        version: str | None,
    ) -> None:
        for link_sym in LINK_SYMBOLS:
            if link_sym.key not in resolved:
                continue
            if link_sym.role is LinkRole.BRIDGE:
                cb = self._make_bridge_cb(side, peer, is_a=is_a)
            elif link_sym.role is LinkRole.HANDSHAKE:
                cb = self._make_handshake_cb(side, peer, is_a=is_a)
            else:
                continue
            label = _label_on(side.session, link_sym, version)
            if label is None:
                # resolve_link_symbols put the key in resolved, so some
                # label must exist — belt-and-braces for an unreachable case.
                raise LookupError(
                    f"no per_version label for {link_sym.key!r} resolves on this session"
                )
            side.session.serial_hook(label, cb)

    @staticmethod
    def _deactivate_serial_hooks(
        owned: list[tuple[Session, tuple[object, int, int, str]]],
    ) -> None:
        """Make raw callbacks inert and remove their private records."""
        grouped: dict[int, tuple[Session, list[tuple[object, int, int, str]]]] = {}
        for session, record in owned:
            grouped.setdefault(id(session), (session, []))[1].append(record)

        for session, records in grouped.values():
            lock = getattr(session, "_lock", None)

            def _deactivate(
                records: list[tuple[object, int, int, str]] = records,
                session: Session = session,
            ) -> None:
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

    def _serial_hook_baselines(self) -> tuple[tuple[Session, int], ...]:
        return tuple(
            (session, len(getattr(session, "_serial_hooks", ())))
            for session in (self._ea.session, self._eb.session)
        )

    def _physical_hook_baselines(self) -> tuple[tuple[object, object], ...]:
        return tuple(
            (endpoint.session._pyboy, snapshot_hooks(endpoint.session._pyboy))
            for endpoint in (self._ea, self._eb)
        )

    def _capture_owned_hooks(
        self,
        serial_baselines: tuple[tuple[Session, int], ...],
        physical_baselines: tuple[tuple[object, object], ...],
    ) -> None:
        for session, start in serial_baselines:
            serial_hooks = getattr(session, "_serial_hooks", ())
            for record in serial_hooks[start:]:
                if len(record) != 4:
                    continue
                if any(
                    existing_session is session and existing_record[0] is record[0]
                    for existing_session, existing_record in self._owned_serial_hooks
                ):
                    continue
                self._owned_serial_hooks.append((session, record))

        for pyboy, before in physical_baselines:
            after = snapshot_hooks(pyboy)
            for bank, addr, callback, context in hooks_added_since(before, after):
                if any(
                    registration.pyboy is pyboy
                    and registration.bank == bank
                    and registration.addr == addr
                    and registration.callback is callback
                    and registration.context is context
                    for registration in self._owned_raw_hooks
                ):
                    continue
                self._owned_raw_hooks.append(
                    RawHookRegistration(
                        pyboy=pyboy,
                        bank=bank,
                        addr=addr,
                        callback=callback,
                        context=context,
                    )
                )

    def _uninstall_locked(self) -> list[Exception]:
        owned_serial_hooks = self._owned_serial_hooks
        owned_raw_hooks = self._owned_raw_hooks
        self._owned_serial_hooks = []
        self._owned_raw_hooks = []
        self._installed = False
        self._deactivate_serial_hooks(owned_serial_hooks)
        errors: list[Exception] = []
        failed_raw_hooks: list[RawHookRegistration] = []
        for handle in reversed(owned_raw_hooks):
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001 - cleanup continues
                errors.append(exc)
                # Keep failed ownership records for a later uninstall retry.
                # Clearing these before close would make a transient physical
                # deregistration failure permanently unrecoverable.
                failed_raw_hooks.append(handle)
        self._owned_raw_hooks = list(reversed(failed_raw_hooks))
        self._transport.reset()
        return errors

    def _make_bridge_cb(
        self,
        side: BridgeEndpoint,
        peer: BridgeEndpoint,
        *,
        is_a: bool,
    ) -> Callable[[object], None]:
        """Callback that fires when the game enters a serial-byte-exchange
        routine (e.g. ``Serial_ExchangeBytes``).

        Exchanges a pair of bytes via the transport and writes the
        receive cells so the game sees the transport's peer-byte even
        if PyBoy's interrupt-driven serial transfer doesn't complete.
        Also reinforces the clock-role status (primary = external slave,
        peer = internal master) which the nybble-sync protocol reads.

        ``LinkPair.step()`` also runs a per-frame hardware-serial tick
        that drives the real Serial ISR path via IF-bit manipulation;
        this symbol-level callback is a cooperating belt-and-braces
        layer so the transport still sees byte traffic even before the
        ISR cycle completes.
        """
        transport = self._transport
        side_status = _STATUS_PRIMARY_EXTERNAL if is_a else _STATUS_PEER_INTERNAL
        peer_status = _STATUS_PEER_INTERNAL if is_a else _STATUS_PRIMARY_EXTERNAL

        def _cb(_ctx: object) -> None:
            side_mem = side.session._pyboy.memory
            peer_mem = peer.session._pyboy.memory
            this_send = int(side_mem[side.send_addr]) & 0xFF
            peer_send = int(peer_mem[peer.send_addr]) & 0xFF
            if is_a:
                from_a, from_b = this_send, peer_send
            else:
                from_a, from_b = peer_send, this_send
            to_a, to_b = transport.exchange(from_a, from_b)
            if is_a:
                side_mem[side.receive_addr] = to_a
                peer_mem[peer.receive_addr] = to_b
            else:
                side_mem[side.receive_addr] = to_b
                peer_mem[peer.receive_addr] = to_a
            side_mem[side.status_addr] = side_status
            peer_mem[peer.status_addr] = peer_status

        return _cb

    @staticmethod
    def _make_handshake_cb(
        side: BridgeEndpoint, peer: BridgeEndpoint, *, is_a: bool
    ) -> Callable[[object], None]:
        side_status = _STATUS_PRIMARY_EXTERNAL if is_a else _STATUS_PEER_INTERNAL
        peer_status = _STATUS_PEER_INTERNAL if is_a else _STATUS_PRIMARY_EXTERNAL

        def _cb(_ctx: object) -> None:
            side.session._pyboy.memory[side.status_addr] = side_status
            peer.session._pyboy.memory[peer.status_addr] = peer_status

        return _cb


__all__ = [
    "HRAM_SERIAL_RECEIVE",
    "HRAM_SERIAL_SEND",
    "HRAM_SERIAL_STATUS",
    "BridgeEndpoint",
    "SerialBridge",
]
