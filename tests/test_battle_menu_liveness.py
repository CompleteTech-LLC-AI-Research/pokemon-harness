"""ROM-free contracts for the battle menu liveness predicates.

The battle command menu, the battle move menu and the battle party menu all
own ``wCurrentMenuItem``/``wMaxMenuItem``/``wMenuWatchedKeys``, and those bytes
are leftovers once their menu closes, so geometry alone cannot name a live
menu.  ``wMenuWatchedKeys`` is the ROM's own per-menu key mask and is what
separates them.  The capture producer
(``scripts/produce_battle_state_fixtures.py``) therefore requires the mask that
belongs to the menu it is about to answer.  The consumer-side boundary driver
(``_boundary_button`` in ``tests/test_mcp_battle_phase_rom.py``) mirrors these
predicates byte for byte, because a recorded pair is only replayable while the
two agree.

These tests are contracts over the predicates, not gameplay proof; the byte
values are quoted from the vendored disassembly under ``asset-build-pokeyellow``:

* command menu, left column  ``PAD_RIGHT | PAD_A`` with ``wMaxMenuItem == 1``
  (``engine/battle/core.asm:2177``),
* command menu, right column ``PAD_LEFT | PAD_A`` with ``wMaxMenuItem == 1``
  (``engine/battle/core.asm:2210``),
* battle party menu ``PAD_A | PAD_B`` with ``wMaxMenuItem == wPartyCount - 1``
  (``home/pokemon.asm:225-240``),
* move menu ``PAD_UP | PAD_DOWN | PAD_A | PAD_B``
  (``engine/battle/core.asm:2655-2672``).
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from scripts import produce_battle_state_fixtures as producer

LIVE_COMMAND_LEFT = (0, 1, 0x11)
LIVE_COMMAND_RIGHT = (1, 1, 0x21)
CLOSED_TWO_MON_PARTY = (1, 1, 0x03)
LIVE_MOVE_MENU = (1, 5, 0xC3)

_NAMES = (
    "wCurrentMenuItem",
    "wMaxMenuItem",
    "wMenuWatchedKeys",
    "wPartyCount",
    "wPartyMenuTypeOrMessageID",
    "wPartyMenuAnimMonEnabled",
    "wPartyMons",
    "wPartySpecies",
    "wBattleMonHP",
    "wBattleMonMaxHP",
    "wIsInBattle",
    "wBattleResult",
    "wInHandlePlayerMonFainted",
)


class _Symbols:
    def __init__(self):
        self.addresses = {name: 0xD000 + index for index, name in enumerate(_NAMES)}

    def addr_of(self, name):
        return self.addresses[name]


class _PyBoy:
    def __init__(self, memory):
        self.memory = memory


class _Session:
    def __init__(self, memory):
        self.symbols = _Symbols()
        self._pyboy = _PyBoy(memory)


class _BankedMemory:
    """PyBoy's WRAM read contract for ``0xC000``-``0xDFFF``.

    ``0xC000``-``0xCFFF`` is fixed bank 0 and reads back directly.  Every
    ``0xD000``-``0xDFFF`` address follows the CGB ``SVBK`` register, while the
    bank-indexed form ``memory[bank, addr]`` returns that bank's own byte
    whatever ``SVBK`` names.  Modelling both halves is what lets a contract
    test prove the producer's battle reads do not depend on the window.
    """

    def __init__(self):
        self.banks = defaultdict(lambda: defaultdict(int))
        self.fixed = defaultdict(int)
        self.svbk = 0

    def __setitem__(self, key, value):
        if isinstance(key, tuple):
            bank, address = key
            self.banks[bank][address] = value
        elif key == producer.WRAM_BANK_PORT:
            self.svbk = value
        else:
            self.fixed[key] = value

    def __getitem__(self, key):
        if isinstance(key, tuple):
            bank, address = key
            return self.banks[bank][address]
        if producer.WRAM_SWITCHABLE_START <= key < producer.WRAM_SWITCHABLE_END:
            return self.banks[self.svbk or 1][key]
        if key == producer.WRAM_BANK_PORT:
            return self.svbk
        return self.fixed[key]


def _producer_session(fields, *, party_count=6, party_type=0x00, anim=0, bank=0):
    """A session whose battle bytes live in WRAM bank 1, as the linker places them.

    The values are written to the bank-1 view, and the ``SVBK`` register is set
    independently, so a test can hold the ROM's own bytes fixed while moving the
    mapped window -- exactly the ``SVBK == 2`` situation that starved the pair.
    """
    current, maximum, watched = fields
    symbols = _Symbols()
    memory = _BankedMemory()
    memory[producer.WRAM_BANK_PORT] = bank
    memory[1, symbols.addr_of("wCurrentMenuItem")] = current
    memory[1, symbols.addr_of("wMaxMenuItem")] = maximum
    memory[1, symbols.addr_of("wMenuWatchedKeys")] = watched
    memory[1, symbols.addr_of("wPartyCount")] = party_count
    memory[1, symbols.addr_of("wPartyMenuTypeOrMessageID")] = party_type
    memory[1, symbols.addr_of("wPartyMenuAnimMonEnabled")] = anim
    return _Session(memory)


@pytest.mark.parametrize("fields", (LIVE_COMMAND_LEFT, LIVE_COMMAND_RIGHT))
def test_live_command_menu_is_answered_by_both_drivers(fields):
    session = _producer_session(fields)
    assert producer._battle_menu_input_ready(session)
    assert producer._menu_awaiting_a(session) == "command"


def test_closed_two_mon_party_menu_is_not_a_command_menu():
    """``wMaxMenuItem == wPartyCount - 1 == 1`` is the command menu's geometry.

    A battle party menu that has already taken its input leaves exactly that
    behind, so geometry alone named a closed menu live and the A intended for it
    would be consumed by the *next* command menu.
    """
    fields = CLOSED_TWO_MON_PARTY
    session = _producer_session(fields, party_count=2)
    assert not producer._party_menu_ready(session)
    assert not producer._battle_menu_input_ready(session)
    assert producer._menu_awaiting_a(session) is None


def test_live_two_mon_party_menu_is_named_party_not_command():
    """The same bytes with the ROM's own liveness witness are a live party menu."""
    fields = CLOSED_TWO_MON_PARTY
    session = _producer_session(
        fields,
        party_count=2,
        party_type=producer.BATTLE_PARTY_MENU,
        anim=producer.PARTY_MENU_ANIM_WITNESS,
    )
    assert producer._party_menu_ready(session)
    assert producer._menu_awaiting_a(session) == "party"


def test_missing_party_menu_witness_never_names_a_party_menu():
    fields = CLOSED_TWO_MON_PARTY
    session = _producer_session(
        fields, party_count=2, party_type=producer.BATTLE_PARTY_MENU, anim=0
    )
    assert not producer._party_menu_ready(session)
    assert producer._menu_awaiting_a(session) is None


def test_move_menu_geometry_is_not_named_a_command_menu():
    fields = LIVE_MOVE_MENU
    session = _producer_session(fields)
    assert producer._move_menu_input_ready(session)
    assert not producer._battle_menu_input_ready(session)
    assert producer._menu_awaiting_a(session) == "move"


def _counters(main):
    return {"MainInBattleLoop": list(main)}


def _counters_with_selection(main, select_enemy):
    return {
        "MainInBattleLoop": list(main),
        "MainInBattleLoop.selectEnemyMove": list(select_enemy),
    }


def test_committed_move_tracks_the_roms_own_move_selection_entry():
    """``MainInBattleLoop.selectEnemyMove`` proves a side moved past its menu.

    ``MainInBattleLoop`` is entered once per turn too, but ~20 frames before
    that side's command menu is live, so it cannot prove the side is done with
    its menu.  The selection label is only reached after the side answers (or
    skips) its command menu, so only its increment makes the peer's menu safe
    to answer.
    """

    baseline = {"select_enemy": [7, 9]}
    counters = _counters_with_selection([20, 20], [7, 9])
    assert producer._committed_move(counters, 0, baseline) is False
    assert producer._committed_move(counters, 1, baseline) is False
    # The predicate is per side, so the caller can always say which peer has
    # committed; one side advancing must never open the gate for its own menu.
    counters = _counters_with_selection([20, 20], [8, 9])
    assert producer._committed_move(counters, 0, baseline) is True
    assert producer._committed_move(counters, 1, baseline) is False
    counters = _counters_with_selection([20, 20], [8, 10])
    assert producer._committed_move(counters, 1, baseline) is True


def _bank1_battle_session(*, bank, party_count=6):
    """A live both-side battle menu, with the mapped window pointed elsewhere.

    ``wIsInBattle``/``wPartyCount``/``wPartyMons``/the party-menu witness are
    written into WRAM bank 1 -- where the linker places them -- while ``bank``
    decides what ``0xD000``-``0xDFFF`` maps to.  Bank 2 is a real ROM state: the
    color build banks its own scratch region there mid-battle.
    """
    symbols = _Symbols()
    memory = _BankedMemory()
    memory[producer.WRAM_BANK_PORT] = bank
    memory[1, symbols.addr_of("wIsInBattle")] = 2
    memory[1, symbols.addr_of("wPartyCount")] = party_count
    memory[1, symbols.addr_of("wCurrentMenuItem")] = 1
    memory[1, symbols.addr_of("wMaxMenuItem")] = party_count - 1
    memory[1, symbols.addr_of("wMenuWatchedKeys")] = 0x03
    memory[1, symbols.addr_of("wPartyMenuTypeOrMessageID")] = producer.BATTLE_PARTY_MENU
    memory[1, symbols.addr_of("wPartyMenuAnimMonEnabled")] = producer.PARTY_MENU_ANIM_WITNESS
    memory[1, symbols.addr_of("wBattleMonHP")] = 0
    memory[1, symbols.addr_of("wBattleMonHP") + 1] = 0
    for slot, hp in enumerate((0, 152, 152, 152, 152, 152)):
        base = symbols.addr_of("wPartyMons") + slot * producer.PARTY_MON_SIZE
        memory[1, base + 1] = hp >> 8
        memory[1, base + 2] = hp & 0xFF
    return _Session(memory), symbols


@pytest.mark.parametrize("bank", (0, 1, 2, 3, 7))
def test_battle_reads_follow_wram_bank_1_not_the_svbk_window(bank):
    """The ROM's battle bytes stay readable while another bank is mapped.

    Before the fix every one of these reads went through the ``SVBK`` window and
    ``_banked_readable`` failed closed for ``bank not in (0, 1)``, so a live
    party menu behind an ``SVBK == 2`` window was invisible: no press was ever
    issued and the pair starved.  The reads now resolve bank 1 directly.
    """
    session, _ = _bank1_battle_session(bank=bank)
    assert producer._wram_bank(session) == bank
    assert producer._banked_readable(session)
    assert producer._battle_active(session)
    assert not producer._battle_ended(session)
    assert producer._byte(session, "wIsInBattle") == 2
    assert producer._byte(session, "wPartyCount") == 6
    assert producer._word(session, "wBattleMonHP") == 0
    assert producer._party_hp(session) == [0, 152, 152, 152, 152, 152]
    assert producer._party_menu_live(session)
    assert producer._party_menu_ready(session)
    assert producer._menu_awaiting_a(session) == "party"


def test_unqualified_banked_read_would_miss_the_live_party_menu():
    """The pre-fix read path is proven wrong rather than merely disbelieved.

    With ``SVBK == 2`` the unqualified ``memory[addr]`` read returns bank 2's
    bytes, which here are all zero -- the exact false "not in battle / no party
    menu" reading that starved the blue capture.  Bank 1 still holds the truth,
    and that is the read the producer now performs.
    """
    session, symbols = _bank1_battle_session(bank=2)
    address = symbols.addr_of("wPartyMenuAnimMonEnabled")
    mapped = int(session._pyboy.memory[address])
    assert mapped == 0
    assert mapped != int(session._pyboy.memory[1, address])
    assert producer._byte(session, "wPartyMenuAnimMonEnabled") == (producer.PARTY_MENU_ANIM_WITNESS)
    # The battle/party reads the replacement path uses read zero through the
    # mapped window too, while bank 1 carries the live values.  (The active
    # mon's HP is genuinely 0 here -- that is the faint being recovered from.)
    for name in ("wIsInBattle", "wPartyCount", "wBattleMonHP"):
        assert int(session._pyboy.memory[symbols.addr_of(name)]) == 0
    assert producer._byte(session, "wIsInBattle") == 2
    assert producer._byte(session, "wPartyCount") == 6


def test_fixed_wram_bank_0_keeps_its_ordinary_read():
    """``0xC000``-``0xCFFF`` is not banked, so it must not be resolved as bank 1."""
    symbols = _Symbols()
    memory = _BankedMemory()
    memory[0xC123] = 0x5A
    memory[1, 0xC123] = 0xA5
    session = _Session(memory)
    assert producer._wram_byte(session, 0xC123) == 0x5A
    assert symbols is not None
