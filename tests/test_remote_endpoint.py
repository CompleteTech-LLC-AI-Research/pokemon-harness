"""Unit tests for :class:`RemoteLinkEndpoint`.

Uses :class:`InProcessSerialLink` to pair two endpoints in-process so
the tests don't depend on sockets. Sessions are built on top of
:class:`FakePyBoy` so hook firing can be simulated without a real ROM.
"""

from __future__ import annotations

import threading

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.remote import (
    STATUS_EXTERNAL,
    STATUS_INTERNAL,
    RemoteLinkEndpoint,
)
from pokered_harness.link.serial_link import InProcessSerialLink
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


# --- symbol tables --------------------------------------------------------
#
# Two sessions with DIFFERENT addresses for the same symbols — proves
# cross-version address translation works.


_BLUE_SYM = """\
00:22fa Serial_TryEstablishingExternallyClockedConnection
00:216f Serial_ExchangeBytes
00:22c3 Serial_ExchangeNybble
00:2247 Serial_ExchangeLinkMenuSelection
00:ffaa hSerialConnectionStatus
00:ffac hSerialSendData
00:ffad hSerialReceiveData
00:cc42 wLinkMenuSelectionSendBuffer
00:cc3d wLinkMenuSelectionReceiveBuffer
00:cc42 wSerialExchangeNybbleSendData
00:cc3e wSerialExchangeNybbleReceiveData
00:d141 wSerialRandomNumberListBlock
00:d173 wSerialPlayerDataBlock
"""

_YELLOW_SYM = """\
00:2156 Serial_TryEstablishingExternallyClockedConnection
00:1fcb Serial_ExchangeBytes
00:20db Serial_ExchangeNybble
00:20a3 Serial_ExchangeLinkMenuSelection
00:ffaa hSerialConnectionStatus
00:ffac hSerialSendData
00:ffad hSerialReceiveData
00:cc71 wLinkMenuSelectionSendBuffer
00:cc6c wLinkMenuSelectionReceiveBuffer
00:cc71 wSerialExchangeNybbleSendData
00:cc6d wSerialExchangeNybbleReceiveData
00:d148 wSerialRandomNumberListBlock
00:d17a wSerialPlayerDataBlock
"""


def _make_session(sym_text: str):
    pb = FakePyBoy(DictMemory())
    sym = load_sym_text(sym_text)
    session = Session(pyboy=pb, symbols=sym, event_bus=EventBus())
    return session, pb, pb.memory  # DictMemory supports __getitem__/__setitem__


# --- handshake -----------------------------------------------------------


def test_handshake_writes_clock_status_byte():
    sa, pa, ma = _make_session(_BLUE_SYM)
    sb, pb, mb = _make_session(_BLUE_SYM)
    la, lb = InProcessSerialLink.pair("blue", "blue")

    ea = RemoteLinkEndpoint.as_listener(sa, la)     # master
    eb = RemoteLinkEndpoint.as_connector(sb, lb)    # slave
    ea.install()
    eb.install()

    # Fire the handshake hook on each side: listener should get 0x02,
    # connector 0x01.
    status_addr = sa.symbols.addr_of("hSerialConnectionStatus")
    pa.fire(0x00, 0x22fa)
    pb.fire(0x00, 0x22fa)
    assert ma[status_addr] == STATUS_INTERNAL
    assert mb[status_addr] == STATUS_EXTERNAL


# --- exchange nybble -----------------------------------------------------


def test_exchange_nybble_exchanges_bytes_between_peers():
    sa, pa, ma = _make_session(_BLUE_SYM)
    sb, pbb, mb = _make_session(_BLUE_SYM)
    la, lb = InProcessSerialLink.pair("blue", "blue")

    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    send_addr = sa.symbols.addr_of("wSerialExchangeNybbleSendData")
    recv_addr = sa.symbols.addr_of("wSerialExchangeNybbleReceiveData")
    # A offers byte 0x61, B offers 0x67. Each should end up with the other's.
    ma[send_addr] = 0x61
    mb[send_addr] = 0x67

    # Each side's hook fires (order doesn't matter — in-process link
    # is threadsafe; we invoke A's hook from a thread so B's can
    # proceed synchronously).
    results: dict[str, Exception | None] = {}

    def fire_a():
        try:
            pa.fire(0x00, 0x22c3)
            results["a"] = None
        except Exception as exc:  # noqa: BLE001
            results["a"] = exc

    t = threading.Thread(target=fire_a, daemon=True)
    t.start()
    pbb.fire(0x00, 0x22c3)
    t.join(timeout=2.0)
    assert results.get("a") is None, results.get("a")
    assert ma[recv_addr] == 0x67
    assert mb[recv_addr] == 0x61


# --- exchange menu selection ---------------------------------------------


def test_exchange_menu_selection_exchanges_two_bytes():
    sa, pa, ma = _make_session(_BLUE_SYM)
    sb, pbb, mb = _make_session(_BLUE_SYM)
    la, lb = InProcessSerialLink.pair("blue", "blue")
    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    send = sa.symbols.addr_of("wLinkMenuSelectionSendBuffer")
    recv = sa.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    ma[send] = 0xD0
    ma[send + 1] = 0xD0
    mb[send] = 0xD4  # peer voted A-press + TRADE
    mb[send + 1] = 0xD4

    def fire_a():
        pa.fire(0x00, 0x2247)

    t = threading.Thread(target=fire_a, daemon=True)
    t.start()
    pbb.fire(0x00, 0x2247)
    t.join(timeout=2.0)

    assert ma[recv] == 0xD4
    assert ma[recv + 1] == 0xD4
    assert mb[recv] == 0xD0
    assert mb[recv + 1] == 0xD0


# --- cross-version exchange bytes ----------------------------------------


def test_exchange_bytes_cross_version_translates_via_symbol():
    """Blue calls Serial_ExchangeBytes with hl = Blue's addr, Yellow
    with hl = Yellow's addr. Symbol resolution should map both to the
    same wire kind so the exchange matches up."""

    sa, pa, ma = _make_session(_BLUE_SYM)
    sb, pbb, mb = _make_session(_YELLOW_SYM)
    la, lb = InProcessSerialLink.pair("blue", "yellow")

    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    # Blue addrs
    blue_send = sa.symbols.addr_of("wSerialPlayerDataBlock")    # 0xD173
    blue_recv = sa.symbols.addr_of("wSerialRandomNumberListBlock")  # 0xD141
    # Yellow addrs (different!)
    yellow_send = sb.symbols.addr_of("wSerialPlayerDataBlock")  # 0xD17A
    yellow_recv = sb.symbols.addr_of("wSerialRandomNumberListBlock")  # 0xD148
    assert blue_send != yellow_send
    assert blue_recv != yellow_recv

    # Populate each side's send buffer with distinguishable patterns.
    blue_bytes = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE])
    yellow_bytes = bytes([0x11, 0x22, 0x33, 0x44, 0x55])
    for i, b in enumerate(blue_bytes):
        ma[blue_send + i] = b
    for i, b in enumerate(yellow_bytes):
        mb[yellow_send + i] = b

    # Set up each side's CPU registers to call Serial_ExchangeBytes
    # with hl = own send addr, de = own recv addr, bc = 5.
    pa.register_file.HL = blue_send
    pa.register_file.D = blue_recv >> 8
    pa.register_file.E = blue_recv & 0xFF
    pa.register_file.B = 0
    pa.register_file.C = 5
    pa.register_file.SP = 0x1000
    # Stack return: dummy return address
    ma[0x1000] = 0x34
    ma[0x1001] = 0x12

    pbb.register_file.HL = yellow_send
    pbb.register_file.D = yellow_recv >> 8
    pbb.register_file.E = yellow_recv & 0xFF
    pbb.register_file.B = 0
    pbb.register_file.C = 5
    pbb.register_file.SP = 0x2000
    mb[0x2000] = 0x78
    mb[0x2001] = 0x56

    def fire_a():
        pa.fire(0x00, 0x216f)  # Blue's Serial_ExchangeBytes

    t = threading.Thread(target=fire_a, daemon=True)
    t.start()
    pbb.fire(0x00, 0x1fcb)  # Yellow's Serial_ExchangeBytes
    t.join(timeout=2.0)

    # Verify cross-version exchange: Blue received Yellow's bytes,
    # Yellow received Blue's bytes — at THEIR OWN addresses, not
    # mixed up.
    assert bytes(ma[blue_recv + i] for i in range(5)) == yellow_bytes
    assert bytes(mb[yellow_recv + i] for i in range(5)) == blue_bytes

    # PC was popped off the stack (RET simulation).
    assert pa.register_file.PC == 0x1234
    assert pbb.register_file.PC == 0x5678


# --- serial_tick is local-only ------------------------------------------


def test_serial_tick_clears_sc_start_and_raises_if():
    session, pb, mem = _make_session(_BLUE_SYM)
    la, _lb = InProcessSerialLink.pair("blue", "blue")
    endpoint = RemoteLinkEndpoint.as_connector(session, la)
    endpoint.install()
    mem[0xFF02] = 0x81  # SC_START | SC_INTERNAL
    mem[0xFF0F] = 0x00
    endpoint.serial_tick()
    assert mem[0xFF02] == 0x01  # SC_START cleared
    assert mem[0xFF0F] & 0x08 == 0x08  # serial IF bit set


def test_serial_tick_noop_when_sc_start_clear():
    session, pb, mem = _make_session(_BLUE_SYM)
    la, _lb = InProcessSerialLink.pair("blue", "blue")
    endpoint = RemoteLinkEndpoint.as_connector(session, la)
    endpoint.install()
    mem[0xFF02] = 0x01
    mem[0xFF0F] = 0x00
    endpoint.serial_tick()
    assert mem[0xFF02] == 0x01
    assert mem[0xFF0F] == 0x00


def test_step_services_serial_tick_after_each_frame():
    session, pb, _mem = _make_session(_BLUE_SYM)
    la, _lb = InProcessSerialLink.pair("blue", "blue")
    endpoint = RemoteLinkEndpoint.as_connector(session, la)
    serial_ticks: list[int] = []
    endpoint.serial_tick = lambda: serial_ticks.append(session.current_tick())  # type: ignore[method-assign]

    endpoint.step(3, render=True)

    assert pb.tick_calls == [(1, True), (1, True), (1, True)]
    assert serial_ticks == [1, 2, 3]


# --- install() guards ----------------------------------------------------


def test_install_twice_raises():
    session, _pb, _mem = _make_session(_BLUE_SYM)
    la, _lb = InProcessSerialLink.pair("blue", "blue")
    endpoint = RemoteLinkEndpoint.as_connector(session, la)
    endpoint.install()
    with pytest.raises(RuntimeError):
        endpoint.install()


def test_clock_status_byte_depends_on_role():
    session, _pb, _mem = _make_session(_BLUE_SYM)
    la, lb = InProcessSerialLink.pair("blue", "blue")
    assert (
        RemoteLinkEndpoint.as_listener(session, la).clock_status_byte
        == STATUS_INTERNAL
    )
    # Create a second session for the other endpoint since install()
    # registers hooks.
    s2, _, _ = _make_session(_BLUE_SYM)
    assert (
        RemoteLinkEndpoint.as_connector(s2, lb).clock_status_byte
        == STATUS_EXTERNAL
    )
