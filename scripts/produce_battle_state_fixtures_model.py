"""Declared constants and light helpers for the battle-state fixture producer.

Split out of ``scripts/produce_battle_state_fixtures.py`` for the #122
file-size contract with no behavior change: the declared menu/party constants,
the hashing helpers, and the atomic writer moved here verbatim.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# Cable Club Colosseum map id (pokered/pokeyellow constants name this 0xF0).
COLOSSEUM_MAP_ID = 0xF0
# constants/menu_constants.asm: const BATTLE_PARTY_MENU ; $02
BATTLE_PARTY_MENU = 0x02
PARTY_MON_SIZE = 44

# ``wPartyMenuAnimMonEnabled`` is the ROM-owned witness that the battle party
# menu is live and waiting for input, and it is the only byte that says so.
# ``HandlePartyMenuInput`` (home/pokemon.asm:242-249) stores ``$40`` there for
# exactly the span in which it calls ``HandleMenuInput_`` for the party menu and
# clears it again before returning, and every other menu handler zeroes it
# (``HandleMenuInput`` in home/window.asm:1-3, which the battle command menu and
# the move menu both reach).  The flag survives as a leftover nowhere, so an
# exact equality against ``$40`` answers "is the party menu taking input now?"
# without relying on cursor bytes that stay stale after their menu closes.
PARTY_MENU_ANIM_WITNESS = 0x40

# ROM-owned menu geometry, shared with the consumer-side boundary driver
# (``_boundary_button`` in tests/test_mcp_battle_phase_rom.py).  The battle
# command menu is two entries wide (0 = FIGHT, 1 = ITEM) and the move menu
# cursor sits one past the slot the ROM confirms (1..wNumMovesMinusOne + 1).
COMMAND_MENU_MAX_ITEM = 1
MOVE_MENU_MAX_ITEM = 5
MENU_WATCHED_A = 0x01

# ``wMenuWatchedKeys`` the ROM installs for the battle command menu, both
# columns of it: ``engine/battle/core.asm:2177`` writes ``PAD_RIGHT | PAD_A``
# for the left column and ``:2210`` writes ``PAD_LEFT | PAD_A`` for the right
# column, each with ``wMaxMenuItem == 1``.  The command menu's cursor geometry
# is not unique on its own -- a battle party menu that has already taken its
# input leaves ``wMaxMenuItem == wPartyCount - 1`` and
# ``wMenuWatchedKeys == PAD_A | PAD_B`` behind, which for a two-mon party is
# the command menu's geometry exactly -- so the mask, not the geometry, is what
# proves the command menu is the live one.
COMMAND_MENU_WATCHED_KEYS = (0x11, 0x21)

# ``wMenuWatchedKeys`` is the ROM's own per-menu key mask and the only byte of
# the three menu bytes that differs between the two menus whose cursor geometry
# collides, so it is what separates them.  ``home/pokemon.asm:225-240`` installs
# ``PAD_A | PAD_B`` for the battle party menu (``PAD_A`` alone under
# ``wForcePlayerToChooseMon``) and ``engine/battle/core.asm:2655-2672`` installs
# ``PAD_UP | PAD_DOWN | PAD_A | PAD_B`` for the move menu.  A pressed battle
# party menu leaves ``wCurrentMenuItem``/``wMaxMenuItem`` at the move menu's
# values, so the mask -- not the geometry -- is what proves which menu is live.
MOVE_MENU_WATCHED_KEYS = 0xC3

# A closed battle party menu can clear ``wIsInBattle`` for a few frames before
# the ROM's own replacement dialogue finishes, so only this many frames of
# sustained "no battle" evidence may end a replacement attempt.
REPLACEMENT_END_GRACE_FRAMES = 600

FAINT_STEM = "cable_club-battle-faint"
PRE_TERMINAL_STEM = "cable_club-pre-terminal"
TERMINAL_STEM = "cable_club-terminal"

# Fixture-manifest metadata for the boundary rows this producer emits.  The
# variant names the ROM the state was driven on: Red/Blue use the
# color-patched ROM that ``tests/test_pyboy_link_session_roms._ROM_PATHS``
# opens, Yellow uses the canonical CGB ROM.
PRODUCER_PATH = "scripts/produce_battle_state_fixtures.py"
VARIANT_BY_VERSION = {"red": "color", "blue": "color", "yellow": "cgb"}

CAPTURE_COMMAND_TEMPLATE = (
    "python scripts/produce_battle_state_fixtures.py --version {version} "
    "--rom-root <rom-root> --fixture-root <fixture-root> "
    "--max-turns {max_turns} --emit-manifest-rows <rows.json>"
)


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _runtime_label() -> str:
    """Describe the PyBoy build that is actually driving the link pair.

    The producer may run under either the pure-Python build
    (``PYBOY_NO_CYTHON=1``) or the compiled build.  Both step the same core and
    produce byte-identical save states, but provenance must record the build
    that really ran instead of assuming the source build.
    """

    from pyboy import utils as pyboy_utils

    if bool(getattr(pyboy_utils, "cython_compiled", False)):
        return "cython runtime"
    return "source runtime (PYBOY_NO_CYTHON=1)"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _repo_commit(repo_root: Path) -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


