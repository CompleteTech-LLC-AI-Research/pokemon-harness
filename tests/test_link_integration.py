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
from tests._rom_assets import fixture_path, rom_path, sym_path


ROM_PATHS = {
    # Blue Cable-Club fixture was captured against the color-patched Blue
    # (pokeblue_color_vanilla.ips). Loading it into stock Blue silently
    # corrupts WRAM on the first tick — the state requires the CGB color
    # ROM as its paired cartridge.
    "blue": (rom_path("blue", color=True), sym_path("blue")),
    "yellow": (rom_path("yellow"), sym_path("yellow")),
    "red": (rom_path("red"), sym_path("red")),
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
    return fixture_path(version)


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
        # to hSerialConnectionStatus on both sides.
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

        # Stronger proof (Blue only — Yellow's fixture uses the weaker
        # EnterMap hook-warp which leaves the player un-walkable, so they
        # can't navigate to the attendant):
        # Press A to talk to the Cable Club attendant. The bridge should
        # take the game through:
        #   attendant dialog → CableClubNPC → Yes → SaveGameData →
        #   Serial_SyncAndExchangeNybble → LinkMenu
        # If the bridge is broken anywhere along this chain, the flow
        # stalls — most commonly at CloseLinkConnection (sync failed)
        # or never reaches LinkMenu (nybble protocol didn't converge).
        if version_a == version_b == "blue":
            counters = {"SaveGameData": [0, 0], "LinkMenu": [0, 0]}
            for name, bucket in counters.items():
                for idx, session in enumerate((session_a, session_b)):
                    if name not in session.symbols:
                        continue
                    bank, addr = session.symbols.bank_addr(name)

                    def _make_cb(b, i=idx):
                        def _cb(_ctx):
                            b[i] += 1

                        return _cb

                    session._pyboy.hook_register(bank, addr, _make_cb(bucket), None)

            TRADE_CENTER = 0xEF
            for _ in range(3):
                session_a.press("up", duration=6)
                session_b.press("up", duration=6)
                pair.step(18)
            reached_trade_center = False
            for _ in range(400):
                session_a.press("a", duration=4)
                session_b.press("a", duration=4)
                pair.step(20)
                if (session_a.read_game_state().overworld.map_id == TRADE_CENTER
                        and session_b.read_game_state().overworld.map_id == TRADE_CENTER):
                    reached_trade_center = True
                    break
            assert (counters["SaveGameData"][0] > 0
                    and counters["SaveGameData"][1] > 0), (
                f"attendant dialog never reached SaveGameData — "
                f"counters={counters}"
            )
            assert (counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0), (
                f"nybble sync didn't converge; LinkMenu not reached — "
                f"counters={counters}."
            )
            assert reached_trade_center, (
                f"LinkMenu's auto-trade selection didn't warp peers to "
                f"TRADE_CENTER. map_a=0x{session_a.read_game_state().overworld.map_id:02x} "
                f"map_b=0x{session_b.read_game_state().overworld.map_id:02x}"
            )

            # Inside TRADE_CENTER: walk A (external/slave) left to face the
            # trade table, B (internal/master) right. Press A on both to
            # trigger the hidden events at (5,4)/(4,4) which call
            # CableClubRightGameboy/CableClubLeftGameboy and jump to
            # CableClub_DoBattleOrTrade.
            pair.step(300)  # let TRADE_CENTER init settle
            for _ in range(2):
                session_a.press("left", duration=6)
                session_b.press("right", duration=6)
                pair.step(20)

            cabble_trade_fired = [0, 0]
            for idx, session in enumerate((session_a, session_b)):
                if "CableClub_DoBattleOrTrade" not in session.symbols:
                    continue
                bank, addr = session.symbols.bank_addr("CableClub_DoBattleOrTrade")

                def _make_cb(b, i=idx):
                    def _cb(_ctx):
                        b[i] += 1

                    return _cb

                try:
                    session._pyboy.hook_register(
                        bank, addr, _make_cb(cabble_trade_fired), None
                    )
                except ValueError:
                    pass

            for _ in range(50):
                session_a.press("a", duration=6)
                session_b.press("a", duration=6)
                pair.step(30)
                if cabble_trade_fired[0] > 0:
                    break
            assert cabble_trade_fired[0] > 0, (
                f"hidden-event trade initiation never fired "
                f"CableClub_DoBattleOrTrade on primary — counts={cabble_trade_fired}"
            )

            # Deepest layer: after CableClub_DoBattleOrTrade runs its
            # three Serial_ExchangeBytes blocks (RNG list + player data
            # + patch list), control flows to CallCurrentTradeCenterFunction
            # which jumps into TradeCenter_SelectMon — the actual mon-
            # selection menu. We detect this by counting
            # TradeCenter_DrawPartyLists (called from inside SelectMon);
            # reaching it means the full trade protocol + UI has spun up.
            draw_counts = [0, 0]
            for idx, session in enumerate((session_a, session_b)):
                if "TradeCenter_DrawPartyLists" not in session.symbols:
                    continue
                bank, addr = session.symbols.bank_addr(
                    "TradeCenter_DrawPartyLists"
                )

                def _mk(b, i=idx):
                    def _cb(_ctx):
                        b[i] += 1

                    return _cb

                try:
                    session._pyboy.hook_register(bank, addr, _mk(draw_counts), None)
                except ValueError:
                    pass

            for _ in range(150):
                session_a.press("a", duration=6)
                session_b.press("a", duration=6)
                pair.step(60)
                if draw_counts[0] > 0 and draw_counts[1] > 0:
                    break
            assert draw_counts[0] > 0 and draw_counts[1] > 0, (
                f"TradeCenter_SelectMon never drew its party list — "
                f"counts={draw_counts}. The protocol didn't reach the UI."
            )
    finally:
        session_a.close()
        session_b.close()
