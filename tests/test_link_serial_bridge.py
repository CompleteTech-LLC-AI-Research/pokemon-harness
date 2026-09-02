from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.serial_bridge import (
    HRAM_SERIAL_RECEIVE,
    HRAM_SERIAL_SEND,
    HRAM_SERIAL_STATUS,
    BridgeEndpoint,
    SerialBridge,
)
from pokered_harness.link.transport import LinkTransport
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy

# Realistic-shape HRAM + link-symbol table. Addresses and banks are chosen to
# be plausible (HRAM lives at 0xFFxx) but the tests only care about labels
# resolving and the bridge writing the right bytes to the right cells.
_SYM_TEXT = """
01:6000 Serial_ExchangeBytes
01:6020 Serial_TryEstablishingExternallyClockedConnection
01:6040 Serial_SendZeroByte
00:FFA1 hSerialConnectionStatus
00:FFA2 hSerialSendData
00:FFA3 hSerialReceiveData
"""

SERIAL_EXCHANGE_BANK = 0x01
SERIAL_EXCHANGE_ADDR = 0x6000
SERIAL_HANDSHAKE_BANK = 0x01
SERIAL_HANDSHAKE_ADDR = 0x6020

HRAM_STATUS = 0xFFA1
HRAM_SEND = 0xFFA2
HRAM_RECEIVE = 0xFFA3


class FailingHookPyBoy(FakePyBoy):
    """Fail the first hook registration to exercise bridge rollback."""

    def __init__(self, memory) -> None:
        super().__init__(memory)
        self.fail_register = False

    def hook_register(self, bank: int, addr: int, callback, context) -> None:
        if self.fail_register:
            raise RuntimeError("injected hook registration failure")
        super().hook_register(bank, addr, callback, context)


def _make_session() -> tuple[Session, FakePyBoy, DictMemory]:
    mem = DictMemory()
    pb = FakePyBoy(mem)
    sym = load_sym_text(_SYM_TEXT)
    return Session(pyboy=pb, symbols=sym, event_bus=EventBus()), pb, mem


def _make_pair() -> tuple[Session, FakePyBoy, DictMemory, Session, FakePyBoy, DictMemory]:
    sa, pa, ma = _make_session()
    sb, pb, mb = _make_session()
    return sa, pa, ma, sb, pb, mb


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


def test_install_registers_hooks_on_both_sessions():
    sa, pa, _, sb, pb, _ = _make_pair()
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )
    assert bridge.installed is False

    bridge.install()

    assert bridge.installed is True
    # Each side gets one hook per BRIDGE/HANDSHAKE symbol present in the
    # SymbolTable. With our sym text that's: exchange_bytes, send_zero_byte,
    # handshake → 3 hooks per side.
    assert len(pa._hooks[(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR)]) == 1
    assert len(pa._hooks[(SERIAL_HANDSHAKE_BANK, SERIAL_HANDSHAKE_ADDR)]) == 1
    assert len(pb._hooks[(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR)]) == 1
    assert len(pb._hooks[(SERIAL_HANDSHAKE_BANK, SERIAL_HANDSHAKE_ADDR)]) == 1


def test_install_twice_raises():
    sa, _, _, sb, _, _ = _make_pair()
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )
    bridge.install()
    with pytest.raises(RuntimeError):
        bridge.install()


def test_uninstall_removes_only_bridge_callbacks_and_is_idempotent():
    sa, pa, _, sb, pb, _ = _make_pair()
    unrelated_calls: list[str] = []

    def unrelated(_ctx: object) -> None:
        unrelated_calls.append("unrelated")

    pa.hook_register(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR, unrelated, None)
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )
    bridge.install()

    bridge.uninstall()
    bridge.uninstall()

    assert bridge.installed is False
    assert pa.fire(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR) == 1
    assert (SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR) not in pb._hooks
    assert unrelated_calls == ["unrelated"]
    assert sa._serial_hooks == []
    assert sb._serial_hooks == []


def test_install_rolls_back_direct_bridge_on_partial_failure():
    sa_mem = DictMemory()
    sb_mem = DictMemory()
    pa = FakePyBoy(sa_mem)
    pb = FailingHookPyBoy(sb_mem)
    sym_a = load_sym_text(_SYM_TEXT)
    sym_b = load_sym_text(_SYM_TEXT)
    sa = Session(pyboy=pa, symbols=sym_a, event_bus=EventBus())
    sb = Session(pyboy=pb, symbols=sym_b, event_bus=EventBus())
    pb.fail_register = True
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )

    with pytest.raises(RuntimeError, match="injected hook registration failure"):
        bridge.install()

    assert bridge.installed is False
    assert pa._hooks == {}
    assert pb._hooks == {}
    assert sa._serial_hooks == []
    assert sb._serial_hooks == []

    # The failed transaction does not poison a later retry once the runtime
    # registration fault is removed.
    pb.fail_register = False
    bridge.install()
    assert bridge.installed is True
    bridge.uninstall()


# ---------------------------------------------------------------------------
# exchange_bytes callback — the core behavior
# ---------------------------------------------------------------------------


def test_exchange_from_side_a_swaps_bytes_and_sets_status():
    sa, pa, ma, sb, _, mb = _make_pair()
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )
    bridge.install()

    ma[HRAM_SEND] = 0xAA
    mb[HRAM_SEND] = 0x55
    # Sanity: status starts at 0.
    assert ma[HRAM_STATUS] == 0
    assert mb[HRAM_STATUS] == 0

    pa.fire(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR)

    # A's receive cell gets B's send byte; B's receive cell gets A's.
    assert ma[HRAM_RECEIVE] == 0x55
    assert mb[HRAM_RECEIVE] == 0xAA
    # Both sides' connection-status bytes flip to the "linked" marker.
    # Primary gets 0x01 (EXTERNAL clock / slave), peer gets 0x02
    # (INTERNAL clock / master). See pret/pokered serial_constants.asm.
    assert ma[HRAM_STATUS] == 0x01
    assert mb[HRAM_STATUS] == 0x02


def test_exchange_from_side_b_uses_same_mapping():
    sa, _, ma, sb, pb, mb = _make_pair()
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )
    bridge.install()

    ma[HRAM_SEND] = 0x11
    mb[HRAM_SEND] = 0x22

    pb.fire(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR)

    assert ma[HRAM_RECEIVE] == 0x22
    assert mb[HRAM_RECEIVE] == 0x11
    # Primary gets 0x01 (EXTERNAL clock / slave), peer gets 0x02
    # (INTERNAL clock / master). See pret/pokered serial_constants.asm.
    assert ma[HRAM_STATUS] == 0x01
    assert mb[HRAM_STATUS] == 0x02


def test_exchange_drives_the_transport():
    sa, pa, ma, sb, _, mb = _make_pair()
    transport = LinkTransport()
    bridge = SerialBridge.from_sessions(
        sa, sb, transport, version_a="red", version_b="red"
    )
    bridge.install()

    ma[HRAM_SEND] = 0x42
    mb[HRAM_SEND] = 0x7F
    pa.fire(SERIAL_EXCHANGE_BANK, SERIAL_EXCHANGE_ADDR)

    # exchange() pushes then pulls, so both queues drain to empty.
    snap = transport.snapshot()
    assert snap == {"a_to_b": [], "b_to_a": []}


# ---------------------------------------------------------------------------
# handshake callback
# ---------------------------------------------------------------------------


def test_handshake_writes_connected_marker_on_both_sides():
    sa, pa, ma, sb, _, mb = _make_pair()
    bridge = SerialBridge.from_sessions(
        sa, sb, LinkTransport(), version_a="red", version_b="red"
    )
    bridge.install()

    ma[HRAM_STATUS] = 0x00
    mb[HRAM_STATUS] = 0x00
    # Populate send cells to confirm the handshake does NOT touch receive.
    ma[HRAM_RECEIVE] = 0xEE
    mb[HRAM_RECEIVE] = 0xEE

    pa.fire(SERIAL_HANDSHAKE_BANK, SERIAL_HANDSHAKE_ADDR)

    # Primary gets 0x01 (EXTERNAL clock / slave), peer gets 0x02
    # (INTERNAL clock / master). See pret/pokered serial_constants.asm.
    assert ma[HRAM_STATUS] == 0x01
    assert mb[HRAM_STATUS] == 0x02
    # Receive cells untouched — handshake doesn't exchange bytes.
    assert ma[HRAM_RECEIVE] == 0xEE
    assert mb[HRAM_RECEIVE] == 0xEE


# ---------------------------------------------------------------------------
# from_sessions validation
# ---------------------------------------------------------------------------


def test_from_sessions_missing_hram_label_raises_lookup_error():
    # Same as the normal sym text minus hSerialReceiveData.
    sym_missing = """
01:6000 Serial_ExchangeBytes
01:6020 Serial_TryEstablishingExternallyClockedConnection
00:FFA1 hSerialConnectionStatus
00:FFA2 hSerialSendData
"""
    sa_mem = DictMemory()
    sb_mem = DictMemory()
    sa = Session(
        pyboy=FakePyBoy(sa_mem), symbols=load_sym_text(sym_missing)
    )
    sb = Session(
        pyboy=FakePyBoy(sb_mem), symbols=load_sym_text(_SYM_TEXT)
    )

    with pytest.raises(LookupError) as exc:
        SerialBridge.from_sessions(
            sa, sb, LinkTransport(), version_a="red", version_b="red"
        )
    assert HRAM_SERIAL_RECEIVE in str(exc.value)


def test_from_sessions_missing_required_link_symbol_propagates():
    # No Serial_ExchangeBytes label → resolve_link_symbols raises.
    sym_no_exchange = """
01:6020 Serial_TryEstablishingExternallyClockedConnection
00:FFA1 hSerialConnectionStatus
00:FFA2 hSerialSendData
00:FFA3 hSerialReceiveData
"""
    sa = Session(
        pyboy=FakePyBoy(DictMemory()), symbols=load_sym_text(sym_no_exchange)
    )
    sb = Session(
        pyboy=FakePyBoy(DictMemory()), symbols=load_sym_text(_SYM_TEXT)
    )
    with pytest.raises(LookupError) as exc:
        SerialBridge.from_sessions(
            sa, sb, LinkTransport(), version_a="red", version_b="red"
        )
    assert "exchange_bytes" in str(exc.value)


# ---------------------------------------------------------------------------
# BridgeEndpoint shape — cheap dataclass sanity check
# ---------------------------------------------------------------------------


def test_bridge_endpoint_is_frozen_dataclass():
    sa, _, _ = _make_session()
    ep = BridgeEndpoint(session=sa, send_addr=1, receive_addr=2, status_addr=3)
    with pytest.raises(FrozenInstanceError):
        ep.send_addr = 99  # type: ignore[misc]


def test_hram_constants_match_expected_labels():
    # Guards against accidental renames in the module constants.
    assert HRAM_SERIAL_SEND == "hSerialSendData"
    assert HRAM_SERIAL_RECEIVE == "hSerialReceiveData"
    assert HRAM_SERIAL_STATUS == "hSerialConnectionStatus"
