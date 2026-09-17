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


def _producer_session(fields, *, party_count=6, party_type=0x00, anim=0, bank=0):
    current, maximum, watched = fields
    symbols = _Symbols()
    memory = defaultdict(int)
    memory[0xFF70] = bank
    memory[symbols.addr_of("wCurrentMenuItem")] = current
    memory[symbols.addr_of("wMaxMenuItem")] = maximum
    memory[symbols.addr_of("wMenuWatchedKeys")] = watched
    memory[symbols.addr_of("wPartyCount")] = party_count
    memory[symbols.addr_of("wPartyMenuTypeOrMessageID")] = party_type
    memory[symbols.addr_of("wPartyMenuAnimMonEnabled")] = anim
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
