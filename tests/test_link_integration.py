"""End-to-end integration tests that exercise the link-cable pipeline
against real ROMs.

Two tiers:

1. **pair_smoke** — constructs two real PyBoy sessions, builds a LinkPair,
   calls pair()/unpair(). No gameplay progression required. Proves the
   symbol registry, bridge, and hook installation survive a real ROM.
   Skipped if ROMs aren't present.

2. **trade_roundtrip** — loads pre-produced ``cable_club.state`` fixtures
   (which the repo does not ship — they must be produced manually per
   README), pairs the sessions, walks the trade UI, asserts party mons
   swap. Skipped if fixture states are missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pokered_harness.link import LinkPair
from pokered_harness.session import Session


REPO_ROOT = Path(__file__).resolve().parents[1]
for parent in [REPO_ROOT, *REPO_ROOT.parents]:
    if (parent / "rom").is_dir():
        ROM_ROOT = parent / "rom"
        break
else:  # pragma: no cover
    ROM_ROOT = REPO_ROOT / "rom"

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "link"


ROM_PATHS = {
    "blue": (ROM_ROOT / "blue" / "pokemon-blue.gb", ROM_ROOT / "blue" / "pokemon-blue.sym"),
    "yellow": (ROM_ROOT / "yellow" / "pokemon-yellow.gbc", ROM_ROOT / "yellow" / "pokemon-yellow.sym"),
    "red": (ROM_ROOT / "red" / "pokemon-red.gb", ROM_ROOT / "red" / "pokemon-red.sym"),
}


def _roms_present(version: str) -> bool:
    rom, sym = ROM_PATHS[version]
    return rom.exists() and sym.exists()


def _open_session(version: str) -> Session:
    rom, sym = ROM_PATHS[version]
    return Session.from_files(rom, sym)


# --- Tier 1: pair smoke --------------------------------------------------


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("blue", "blue"),
        ("blue", "yellow"),
        ("yellow", "yellow"),
    ],
)
def test_link_pair_installs_hooks_on_real_roms(version_a: str, version_b: str):
    if not (_roms_present(version_a) and _roms_present(version_b)):
        pytest.skip(f"ROMs not present for {version_a}/{version_b}")

    session_a = _open_session(version_a)
    session_b = _open_session(version_b)
    try:
        pair = LinkPair(
            session_a,
            session_b,
            version_primary=version_a,
            version_peer=version_b,
        )
        assert not pair.paired
        pair.pair()
        assert pair.paired

        # Both sides should tick without the bridge itself crashing; no
        # game-level link activity is expected yet since we're on the
        # opening GameFreak screen.
        pair.step(4)
        assert session_a.current_tick() == 4
        assert session_b.current_tick() == 4

        pair.unpair()
        assert not pair.paired
    finally:
        session_a.close()
        session_b.close()


# --- Tier 2: trade round-trip (requires manually-produced fixtures) ------


def _cable_club_state(version: str) -> Path:
    return FIXTURE_ROOT / version / "cable_club.state"


@pytest.mark.parametrize(
    "version_a,version_b",
    [("blue", "blue"), ("blue", "yellow"), ("yellow", "yellow")],
)
def test_link_trade_roundtrip(version_a: str, version_b: str):
    if not (_roms_present(version_a) and _roms_present(version_b)):
        pytest.skip(f"ROMs not present for {version_a}/{version_b}")
    state_a = _cable_club_state(version_a)
    state_b = _cable_club_state(version_b)
    if not (state_a.exists() and state_b.exists()):
        pytest.skip(
            f"Cable Club save states missing; see README 'Link cable' section "
            f"for how to produce {state_a} and {state_b}"
        )

    session_a = _open_session(version_a)
    session_b = _open_session(version_b)
    try:
        session_a.load_state(state_a.read_bytes())
        session_b.load_state(state_b.read_bytes())

        pair = LinkPair(
            session_a,
            session_b,
            version_primary=version_a,
            version_peer=version_b,
        )
        pair.pair()

        # TODO: walk the trade UI. This is stubbed pending the save-state
        # production work (which requires in-game progression to Cerulean
        # Cable Club). For now, the smoke portion (load + pair) succeeding
        # is itself a meaningful check: load_state through the paired
        # bridge does not break anything.
        pytest.skip("trade UI walker not yet implemented; load+pair ok")
    finally:
        session_a.close()
        session_b.close()
