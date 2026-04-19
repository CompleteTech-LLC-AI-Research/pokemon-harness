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

Teardown note: PyBoy 2.7.0 has no ``hook_deregister``, so there is no way
to unbind callbacks. Drop the :class:`SerialBridge` reference only when
tearing down both underlying sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

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

_STATUS_CONNECTED = 0x01


@dataclass(frozen=True, slots=True)
class BridgeEndpoint:
    """One side of the bridge — a Session plus its resolved HRAM addresses."""

    session: "Session"
    send_addr: int
    receive_addr: int
    status_addr: int


def _require_hram(session: "Session", label: str) -> int:
    sym = session.symbols.get(label)
    if sym is None:
        raise LookupError(f"required HRAM label missing: {label!r}")
    return sym.addr


def _label_on(session: "Session", link_sym: LinkSymbol) -> str | None:
    """Return whichever per_version label for ``link_sym`` exists in this
    session's SymbolTable, or None if none of them do."""
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
    ) -> None:
        self._ea = endpoint_a
        self._eb = endpoint_b
        self._transport = transport
        self._resolved_a = dict(resolved_a)
        self._resolved_b = dict(resolved_b)
        self._installed = False

    @classmethod
    def from_sessions(
        cls,
        session_a: "Session",
        session_b: "Session",
        transport: LinkTransport,
        *,
        version_a: str,
        version_b: str,
    ) -> "SerialBridge":
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
        )

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> None:
        if self._installed:
            raise RuntimeError("SerialBridge.install called twice")
        self._install_side(self._ea, self._resolved_a, self._eb, is_a=True)
        self._install_side(self._eb, self._resolved_b, self._ea, is_a=False)
        self._installed = True

    # --- install / callback helpers ------------------------------------

    def _install_side(
        self,
        side: BridgeEndpoint,
        resolved: dict[str, tuple[int, int]],
        peer: BridgeEndpoint,
        *,
        is_a: bool,
    ) -> None:
        for link_sym in LINK_SYMBOLS:
            if link_sym.key not in resolved:
                continue
            if link_sym.role is LinkRole.BRIDGE:
                cb = self._make_bridge_cb(side, peer, is_a=is_a)
            elif link_sym.role is LinkRole.HANDSHAKE:
                cb = self._make_handshake_cb(side, peer)
            else:
                continue
            label = _label_on(side.session, link_sym)
            if label is None:
                # resolve_link_symbols put the key in resolved, so some
                # label must exist — belt-and-braces for an unreachable case.
                raise LookupError(
                    f"no per_version label for {link_sym.key!r} resolves on this session"
                )
            side.session.serial_hook(label, cb)

    def _make_bridge_cb(
        self,
        side: BridgeEndpoint,
        peer: BridgeEndpoint,
        *,
        is_a: bool,
    ) -> Callable[[object], None]:
        transport = self._transport

        def _cb(_ctx: object) -> None:
            # Access via session._pyboy.memory — Session has no public memory
            # accessor and adding one just for the bridge would be scope creep.
            side_mem = side.session._pyboy.memory
            peer_mem = peer.session._pyboy.memory
            this_send = int(side_mem[side.send_addr]) & 0xFF
            peer_send = int(peer_mem[peer.send_addr]) & 0xFF
            if is_a:
                from_a, from_b = this_send, peer_send
            else:
                from_a, from_b = peer_send, this_send
            to_a, to_b = transport.exchange(from_a, from_b)
            # Write A's result to A's receive cell, B's to B's — regardless
            # of which side fired the hook.
            if is_a:
                side_mem[side.receive_addr] = to_a
                peer_mem[peer.receive_addr] = to_b
            else:
                side_mem[side.receive_addr] = to_b
                peer_mem[peer.receive_addr] = to_a
            side_mem[side.status_addr] = _STATUS_CONNECTED
            peer_mem[peer.status_addr] = _STATUS_CONNECTED

        return _cb

    @staticmethod
    def _make_handshake_cb(
        side: BridgeEndpoint, peer: BridgeEndpoint
    ) -> Callable[[object], None]:
        def _cb(_ctx: object) -> None:
            side.session._pyboy.memory[side.status_addr] = _STATUS_CONNECTED
            peer.session._pyboy.memory[peer.status_addr] = _STATUS_CONNECTED

        return _cb


__all__ = [
    "BridgeEndpoint",
    "SerialBridge",
    "HRAM_SERIAL_SEND",
    "HRAM_SERIAL_RECEIVE",
    "HRAM_SERIAL_STATUS",
]
