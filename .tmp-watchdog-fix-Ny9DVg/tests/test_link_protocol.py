"""Protocol-level (Tier 2) tests — drive Pokémon byte patterns through
a :class:`SerialCore` pair under :class:`LockstepCoordinator`.

These tests use no ROM. They prove the bit-accurate transport relays
Pokémon's exact constants and block structures byte-for-byte in the
order the game emits them. Constants come straight out of
``pret/pokered/constants/serial_constants.asm`` and
``pret/pokered/engine/link/cable_club.asm`` per the design doc's
Tier-2 plan (:doc:`docs/pyboy_serial_overhaul_design.md`).

Scope
-----

* Trade payload shapes: 7×``0xFD`` preambles; 10 random-number bytes
  all ``< 0xFD``; player/enemy data blocks with ``0xFE`` filler and
  ``0xFF`` patch-list terminators.
* Nibble exchange for the trade menu: cancel = ``0x1``, confirm =
  ``0x2``, selection-cancel = ``0xF``.
* Battle action nibble domain: move slots ``0x0-0x3``; switch-party
  slots ``0x4-0x9`` (encoded as ``wWhichPokemon + 4``); ``0xD`` no
  action; ``0xE`` struggle; ``0xF`` run.
* Pattern-level correctness: master/slave role swap between bytes,
  interleaved byte streams, and a full synthetic trade preamble block
  end-to-end.
"""

from __future__ import annotations

import pytest

from pokered_harness.link.serial_coordinator import LockstepCoordinator
from pokered_harness.link.serial_core import CYCLES_PER_BYTE_DMG, SerialCore

# --- Pokémon Gen I protocol constants (pret-sourced) -----------------------

SERIAL_PREAMBLE_BYTE = 0xFD
SERIAL_NO_DATA_BYTE = 0xFE
PATCH_LIST_TERMINATOR = 0xFF
TRADE_CANCEL_NIBBLE = 0x1
TRADE_CONFIRM_NIBBLE = 0x2
TRADE_SELECTION_CANCEL_NIBBLE = 0xF
LINKBATTLE_NO_ACTION = 0xD
LINKBATTLE_STRUGGLE = 0xE
LINKBATTLE_RUN = 0xF


# --- helpers ---------------------------------------------------------------


def _make_pair() -> tuple[SerialCore, SerialCore, LockstepCoordinator]:
    a = SerialCore()
    b = SerialCore()
    coord = LockstepCoordinator(a, b)
    return a, b, coord


def _exchange_byte(
    master: SerialCore,
    slave: SerialCore,
    master_byte: int,
    slave_byte: int,
) -> tuple[int, int]:
    """Arm both sides for one byte and advance the master's internal
    clock through the full 8-edge transfer. Returns
    ``(received_by_master, received_by_slave)``."""
    master.set_SB(master_byte)
    slave.set_SB(slave_byte)
    master.set_SC(0x81)  # transfer enable + internal clock
    slave.set_SC(0x80)   # transfer enable + external clock
    target = master.last_cycles + CYCLES_PER_BYTE_DMG
    completed = master.tick(target)
    assert completed, "master did not complete the byte transfer"
    return master.SB, slave.SB


def _exchange_stream(
    master: SerialCore,
    slave: SerialCore,
    master_bytes: bytes | list[int],
    slave_bytes: bytes | list[int],
) -> tuple[bytes, bytes]:
    """Exchange a whole stream of bytes, master→slave + slave→master."""
    assert len(master_bytes) == len(slave_bytes)
    recv_m, recv_s = bytearray(), bytearray()
    for mb, sb in zip(master_bytes, slave_bytes):
        got_m, got_s = _exchange_byte(master, slave, mb, sb)
        recv_m.append(got_m)
        recv_s.append(got_s)
    return bytes(recv_m), bytes(recv_s)


# ---------------------------------------------------------------------------
# Trade preamble and random-number block
# ---------------------------------------------------------------------------


def test_preamble_block_relays_exactly():
    """Cable-Club trade setup sends 7 × 0xFD preamble bytes; both sides
    see the peer's preamble intact."""
    a, b, _ = _make_pair()
    preamble = bytes([SERIAL_PREAMBLE_BYTE] * 7)
    recv_a, recv_b = _exchange_stream(a, b, preamble, preamble)
    assert recv_a == preamble
    assert recv_b == preamble


def test_random_number_block_preserves_constraint():
    """Pokémon's RNG block is 10 bytes all < 0xFD. The transport must
    relay them without corruption."""
    a, b, _ = _make_pair()
    # Synthetic RNGs; real game produces these from battle-RNG state.
    rng_a = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0, 0x0F, 0xAC])
    rng_b = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA])
    # Preconditions — asserting the test-data meets the protocol invariant.
    assert all(v < SERIAL_PREAMBLE_BYTE for v in rng_a)
    assert all(v < SERIAL_PREAMBLE_BYTE for v in rng_b)

    recv_a, recv_b = _exchange_stream(a, b, rng_a, rng_b)
    assert recv_a == rng_b
    assert recv_b == rng_a


# ---------------------------------------------------------------------------
# Player / enemy data block with 0xFE filler + 0xFF patch-list terminator
# ---------------------------------------------------------------------------


def test_data_block_with_FE_filler_and_patch_list_terminator():
    """Pokémon trade code builds the player-data block, replaces 0xFE
    "no-data" bytes with 0xFF before transmit, and appends a patch-list
    ending in 0xFF. The transport is byte-agnostic but must not corrupt
    0xFE/0xFF in either direction."""
    a, b, _ = _make_pair()
    # Synthetic block: a few payload bytes, some FE fillers, then FF terminator.
    block = bytes([0x01, 0x02, 0x03, SERIAL_NO_DATA_BYTE, 0x04,
                   SERIAL_NO_DATA_BYTE, SERIAL_NO_DATA_BYTE, 0x05,
                   PATCH_LIST_TERMINATOR])
    peer_block = bytes(reversed(block))
    recv_a, recv_b = _exchange_stream(a, b, block, peer_block)
    assert recv_a == peer_block
    assert recv_b == block


def test_200_byte_block_preserves_all_bytes():
    """Trade's patch-list block is ~200 bytes; sanity-check nothing
    desyncs across a long stream."""
    a, b, _ = _make_pair()
    block_a = bytes(i & 0xFF for i in range(200))
    block_b = bytes((0xFF - i) & 0xFF for i in range(200))
    recv_a, recv_b = _exchange_stream(a, b, block_a, block_b)
    assert recv_a == block_b
    assert recv_b == block_a


# ---------------------------------------------------------------------------
# Nibble exchange — trade menu
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "my_nibble, their_nibble",
    [
        (TRADE_CANCEL_NIBBLE, TRADE_CONFIRM_NIBBLE),
        (TRADE_CONFIRM_NIBBLE, TRADE_CONFIRM_NIBBLE),
        (TRADE_CANCEL_NIBBLE, TRADE_CANCEL_NIBBLE),
        (TRADE_SELECTION_CANCEL_NIBBLE, 0x3),  # party-index 3
        (0x0, TRADE_SELECTION_CANCEL_NIBBLE),
    ],
)
def test_trade_menu_nibble_round_trip(my_nibble, their_nibble):
    """Pokémon packs the trade-menu selection as one nibble per byte
    (high nibble zero). Verify the transport relays each documented
    nibble value."""
    a, b, _ = _make_pair()
    # Low-nibble-only byte encoding matches Pokémon's
    # Serial_ExchangeLinkMenuSelection format.
    recv_a, recv_b = _exchange_byte(a, b, my_nibble & 0x0F, their_nibble & 0x0F)
    assert recv_a == their_nibble & 0x0F
    assert recv_b == my_nibble & 0x0F


def test_mutual_confirm_after_mutual_cancel():
    """First round both sides cancel (nibble 1); second round both
    confirm (nibble 2). Verify the transport doesn't leak state
    between rounds."""
    a, b, _ = _make_pair()

    recv_a, recv_b = _exchange_byte(a, b, TRADE_CANCEL_NIBBLE, TRADE_CANCEL_NIBBLE)
    assert recv_a == TRADE_CANCEL_NIBBLE
    assert recv_b == TRADE_CANCEL_NIBBLE

    recv_a, recv_b = _exchange_byte(a, b, TRADE_CONFIRM_NIBBLE, TRADE_CONFIRM_NIBBLE)
    assert recv_a == TRADE_CONFIRM_NIBBLE
    assert recv_b == TRADE_CONFIRM_NIBBLE


# ---------------------------------------------------------------------------
# Link-battle action nibble domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("move_slot", [0x0, 0x1, 0x2, 0x3])
def test_battle_action_move_slot_nibbles(move_slot):
    a, b, _ = _make_pair()
    recv_a, _ = _exchange_byte(a, b, move_slot, LINKBATTLE_NO_ACTION)
    assert recv_a == LINKBATTLE_NO_ACTION
    # Ensure the peer received the move slot verbatim.
    a2, b2, _ = _make_pair()
    _, recv_b = _exchange_byte(a2, b2, move_slot, LINKBATTLE_NO_ACTION)
    assert recv_b == move_slot


@pytest.mark.parametrize("party_slot_plus_4", [0x4, 0x5, 0x6, 0x7, 0x8, 0x9])
def test_battle_action_switch_slots(party_slot_plus_4):
    """Battle engine sends ``wWhichPokemon + 4`` as the switch encoding,
    which occupies the nibble range 0x4-0x9."""
    a, b, _ = _make_pair()
    _recv_a, recv_b = _exchange_byte(a, b, party_slot_plus_4, LINKBATTLE_NO_ACTION)
    assert recv_b == party_slot_plus_4


@pytest.mark.parametrize(
    "a_action, b_action, desc",
    [
        (LINKBATTLE_RUN, LINKBATTLE_NO_ACTION, "a runs"),
        (LINKBATTLE_STRUGGLE, LINKBATTLE_STRUGGLE, "both struggle"),
        (LINKBATTLE_NO_ACTION, LINKBATTLE_RUN, "b runs"),
        (LINKBATTLE_NO_ACTION, LINKBATTLE_NO_ACTION, "both skip"),
    ],
)
def test_battle_action_sentinel_nibbles(a_action, b_action, desc):
    """The terminal values 0xD, 0xE, 0xF round-trip correctly — these
    are the ones Pokémon's battle core compares against directly."""
    a, b, _ = _make_pair()
    recv_a, recv_b = _exchange_byte(a, b, a_action, b_action)
    assert recv_a == b_action, desc
    assert recv_b == a_action, desc


# ---------------------------------------------------------------------------
# Role swap mid-stream and full synthetic trade block
# ---------------------------------------------------------------------------


def test_role_swap_between_bytes_preserves_byte_order():
    """Real Pokémon code alternates master/slave turns between certain
    exchange phases. Prove that swapping the clock source between bytes
    doesn't corrupt the stream."""
    a, b, _ = _make_pair()

    # First byte: A master.
    recv_a, recv_b = _exchange_byte(a, b, 0x11, 0x22)
    assert recv_a == 0x22 and recv_b == 0x11

    # Second byte: B master.
    b.set_SB(0x33); a.set_SB(0x44)
    b.set_SC(0x81); a.set_SC(0x80)
    target = b.last_cycles + CYCLES_PER_BYTE_DMG
    assert b.tick(target)
    assert b.SB == 0x44
    assert a.SB == 0x33

    # Third byte: A master again.
    recv_a, recv_b = _exchange_byte(a, b, 0x55, 0x66)
    assert recv_a == 0x66 and recv_b == 0x55


def test_full_synthetic_trade_preamble_block():
    """End-to-end: 7 preambles + 10 random bytes + a 16-byte data
    block with FE filler + FF patch-list terminator. Mimics the opening
    of a real trade exchange."""
    a, b, _ = _make_pair()

    preamble = bytes([SERIAL_PREAMBLE_BYTE] * 7)
    rng_a = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A])
    rng_b = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x11, 0x22, 0x33, 0x44, 0x55])
    data_a = bytes([0x10, 0x11, SERIAL_NO_DATA_BYTE, 0x12, 0x13, 0x14,
                    SERIAL_NO_DATA_BYTE, 0x15, 0x16, 0x17, 0x18,
                    SERIAL_NO_DATA_BYTE, 0x19, 0x1A, 0x1B,
                    PATCH_LIST_TERMINATOR])
    data_b = bytes(reversed(data_a))

    stream_a = preamble + rng_a + data_a
    stream_b = preamble + rng_b + data_b

    recv_a, recv_b = _exchange_stream(a, b, stream_a, stream_b)

    # Verify structural shape of what each side received.
    assert recv_a[:7] == preamble
    assert recv_b[:7] == preamble
    assert recv_a[7:17] == rng_b
    assert recv_b[7:17] == rng_a
    assert recv_a[17:] == data_b
    assert recv_b[17:] == data_a
