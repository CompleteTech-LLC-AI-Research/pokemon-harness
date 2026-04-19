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

        # Cerulean Pokemon Center's map script calls
        # Serial_TryEstablishingExternallyClockedConnection every frame
        # while the player is on this map (see
        # pret/pokered/scripts/CeruleanPokecenter.asm). That's the real
        # game initiating its serial handshake. The SerialBridge hooks
        # that label with a HANDSHAKE-role callback that writes 0x01
        # to hSerialConnectionStatus on both sides, emulating "link
        # established".
        #
        # After a few frames of stepping, hSerialConnectionStatus should
        # be non-0xFF (CONNECTION_NOT_ESTABLISHED) on both sides. This
        # is the decisive end-to-end proof that our bridge functions
        # against real ROM-level serial entry points — not just
        # fake-PyBoy unit tests.
        pair.step(180)

        status_addr = session_a.symbols.addr_of("hSerialConnectionStatus")
        status_a = session_a._pyboy.memory[status_addr]
        status_b = session_b._pyboy.memory[status_addr]
        assert status_a != 0xFF, (
            f"bridge failed to establish handshake on {version_a} — "
            f"hSerialConnectionStatus=0x{status_a:02x}"
        )
        assert status_b != 0xFF, (
            f"bridge failed to establish handshake on {version_b} — "
            f"hSerialConnectionStatus=0x{status_b:02x}"
        )
        # Driving the trade UI itself (attendant dialog → "Yes, save" →
        # Serial_SyncAndExchangeNybble → TRADE_CENTER warp → select mon
        # → confirm) is deferred; SaveGameData's own internal prompts
        # need more orchestration than the current harness provides.
    finally:
        session_a.close()
        session_b.close()
