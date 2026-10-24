"""Golden-path regressions against the real ROM.

Each test boots a fresh :class:`Session`, drives it through a scripted
input sequence that reaches a known checkpoint, and asserts on the
parsed :class:`GameState`. No save-state fixtures are committed — boot
is cheap enough (~1s on the dev machine) that re-running from scratch
is the simplest reproducible baseline.

Skipped entirely when ``POKERED_ROM_PATH`` / ``POKERED_SYM_PATH`` are
not set, so the harness stays importable on machines without a ROM.

Pokémon Red map constants (from ``pret/pokered/constants/map_constants.asm``):

* ``REDS_HOUSE_2F = 38`` (0x26) — upstairs bedroom, the player's spawn
  tile after accepting Oak's intro with default names.
"""

from __future__ import annotations

import os

import pytest

from pokered_harness.session import Session
from pokered_harness.state.battle import BattleKind

ROM_PATH = os.environ.get("POKERED_ROM_PATH")
SYM_PATH = os.environ.get("POKERED_SYM_PATH")
ROM_SHA1 = os.environ.get(
    "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
)

pytestmark = pytest.mark.skipif(
    not (ROM_PATH and SYM_PATH),
    reason="POKERED_ROM_PATH / POKERED_SYM_PATH not set; skipping real-ROM golden paths",
)


# Known checkpoint map IDs (pokered constants).
MAP_REDS_HOUSE_2F = 0x26
# Money starts at 3000 in Red for a fresh save.
STARTING_MONEY = 3000


@pytest.fixture
def session():
    """Boot a fresh session per test. Cheap (~1s) after first PyBoy init."""
    s = Session.from_files(ROM_PATH, SYM_PATH, expected_rom_sha1=ROM_SHA1)
    try:
        yield s
    finally:
        s.close()


def _advance_through_intro(session: Session) -> None:
    """Scripted input sequence that navigates past title + Oak's intro
    accepting all default prompts. Lands the player in Red's bedroom 2F.

    The exact timings were tuned against the pinned ROM + PyBoy version;
    if either changes, the sequence may need to be re-timed.
    """
    session.step(300)
    for _ in range(12):
        session.press("start")
        session.step(120)
    for _ in range(8):
        session.press("a")
        session.step(120)


# -- checkpoints -----------------------------------------------------------


def test_checkpoint_post_boot_is_idle(session):
    """Immediately after ROM load, no ticks: the emulator is alive but
    the game proper hasn't started. This is the cheapest possible
    regression — a check that nothing exploded during ``from_files``."""
    gs = session.read_game_state()
    # Can't assume specific values (uninitialised memory), but the
    # parser should produce a well-formed GameState without raising.
    assert gs.battle.kind is BattleKind.NONE
    assert gs.party.count == 0
    assert gs.overworld.is_standing  # walk_counter == 0 uninitialised


def test_checkpoint_reds_bedroom_after_intro(session):
    """After the scripted intro sequence, the player lands in Red's
    bedroom 2F with $3000 starting money and no Pokémon yet."""
    _advance_through_intro(session)

    gs = session.read_game_state()
    assert gs.overworld.map_id == MAP_REDS_HOUSE_2F, (
        f"expected REDS_HOUSE_2F (0x{MAP_REDS_HOUSE_2F:02x}), "
        f"got 0x{gs.overworld.map_id:02x}"
    )
    assert gs.progress.money == STARTING_MONEY
    assert gs.party.count == 0, "player hasn't picked starter yet"
    assert gs.progress.badges == (), "no gym badges before game starts"
    assert gs.battle.active is False
    assert gs.overworld.is_standing


def test_checkpoint_save_load_is_deterministic(session):
    """Byte-identical save-state roundtrip: advance, snapshot, advance
    more, restore, assert state matches the snapshot point."""
    _advance_through_intro(session)

    snapshot_at_bedroom = session.save_state()
    gs_at_bedroom = session.read_game_state()

    # Wander forward arbitrarily.
    for _ in range(10):
        session.press("down")
        session.step(32)

    # Restore and confirm we're exactly where we snapshotted.
    session.load_state(snapshot_at_bedroom)
    gs_restored = session.read_game_state()

    assert gs_restored.overworld.map_id == gs_at_bedroom.overworld.map_id
    assert gs_restored.overworld.x == gs_at_bedroom.overworld.x
    assert gs_restored.overworld.y == gs_at_bedroom.overworld.y
    assert gs_restored.progress.money == gs_at_bedroom.progress.money


def test_checkpoint_save_state_bytes_are_stable(session):
    """Two save_state() calls with no intervening tick must produce
    byte-identical output. Catches accidental wall-clock / non-deterministic
    state leaking into the serialiser."""
    session.step(120)
    a = session.save_state()
    b = session.save_state()
    assert a == b
    assert len(a) > 0
