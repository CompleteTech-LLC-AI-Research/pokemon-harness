from __future__ import annotations

"""Real-ROM diagnostic milestone tests for :class:`PyBoyLinkSession`.

Split from ``tests/test_pyboy_link_session_roms.py`` (#132) with no behavior
change: the non-acceptance trade/battle warp milestones moved here verbatim.
"""

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from tests._pyboy_link_session_roms_battle_support import (
    _drive_complete_battle_turn,
    _install_battle_diag_counters,
)
from tests._pyboy_link_session_roms_support import (
    COLOSSEUM_MAP_ID,
    TRADE_CENTER_MAP_ID,
    _assert_battle_fixture_is_legal,
    _battle_state_path,
    _close_linked_pair,
    _fixtures_available,
    _open_session,
    _open_session_pair,
    _require_real_rom_link_runtime,
)
from tests.test_pyboy_link_session_roms import (
    _drive_past_link_menu_to_colosseum,
    _drive_past_link_menu_to_trade_center,
)

__all__ = [
    "_drive_past_link_menu_to_colosseum",
    "_drive_past_link_menu_to_trade_center",
    "_require_real_rom_link_runtime",
]

def test_yellow_pair_warps_to_trade_center():
    """Drive past LinkMenu to the TRADE_CENTER map warp on both sides.

    This is the next real milestone after the LinkMenu reach: it
    proves the big (~200-byte) trainer + party data exchange that
    happens inside ``CableClub_DoBattleOrTradeAgain`` also converges
    through :class:`SerialCore`. The map warp is the observable
    side-effect — both sides end up on map ``0xEF`` (TRADE_CENTER).
    """
    if not _fixtures_available("yellow"):
        pytest.skip("Yellow Cable Club fixture missing")

    a, b = _open_session_pair(
        lambda: _open_session("yellow"),
        lambda: _open_session("yellow"),
    )
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_past_link_menu_to_trade_center(a, b, link)

        print(
            f"\nyellow<->yellow TRADE_CENTER warp diagnostic:\n"
            f"  LinkMenu counters: {diag['counters']}\n"
            f"  frames_to_link_menu: {diag['frames_to_link_menu']}\n"
            f"  extra_frames: {diag['extra_frames']}\n"
            f"  final_map_a: 0x{diag['final_map_a']:02x}\n"
            f"  final_map_b: 0x{diag['final_map_b']:02x}"
        )

        assert diag["final_map_a"] == TRADE_CENTER_MAP_ID, (
            f"A didn't warp to TRADE_CENTER; "
            f"final_map_a=0x{diag['final_map_a']:02x}"
        )
        assert diag["final_map_b"] == TRADE_CENTER_MAP_ID, (
            f"B didn't warp to TRADE_CENTER; "
            f"final_map_b=0x{diag['final_map_b']:02x}"
        )
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def test_yellow_pair_warps_to_colosseum():
    """Drive past LinkMenu to the COLOSSEUM map warp on both sides.

    The battle-path analogue of :func:`test_yellow_pair_warps_to_trade_center`.
    Proves that LinkMenu's DOWN+A navigation works, that ``Serial_
    ExchangeLinkMenuSelection`` carries the BATTLE selection, and that
    both sides satisfy the Colosseum party-size gate.
    """
    if not _fixtures_available("yellow"):
        pytest.skip("Yellow Cable Club fixture missing")

    a, b = _open_session_pair(
        lambda: _open_session("yellow", state_path=_battle_state_path("yellow")),
        lambda: _open_session("yellow", state_path=_battle_state_path("yellow")),
    )
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_past_link_menu_to_colosseum(a, b, link)

        print(
            f"\nyellow<->yellow COLOSSEUM warp diagnostic:\n"
            f"  LinkMenu counters: {diag['counters']}\n"
            f"  frames_to_link_menu: {diag['frames_to_link_menu']}\n"
            f"  extra_frames: {diag['extra_frames']}\n"
            f"  final_map_a: 0x{diag['final_map_a']:02x}\n"
            f"  final_map_b: 0x{diag['final_map_b']:02x}"
        )

        assert diag["final_map_a"] == COLOSSEUM_MAP_ID, (
            f"A didn't warp to COLOSSEUM; "
            f"final_map_a=0x{diag['final_map_a']:02x}"
        )
        assert diag["final_map_b"] == COLOSSEUM_MAP_ID, (
            f"B didn't warp to COLOSSEUM; "
            f"final_map_b=0x{diag['final_map_b']:02x}"
        )
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def test_yellow_pair_starts_link_battle():
    """Yellow pair enters Colosseum and triggers the battle VS splash.

    The milestone between "warps to COLOSSEUM" and "completes turn":
    both sides run ``DisplayLinkBattleVersusTextBox``, meaning the
    big pre-battle trainer+party block exchange converged through
    our :class:`SerialCore`.
    """
    if not _fixtures_available("yellow"):
        pytest.skip("Yellow Cable Club fixture missing")

    a, b = _open_session_pair(
        lambda: _open_session("yellow", state_path=_battle_state_path("yellow")),
        lambda: _open_session("yellow", state_path=_battle_state_path("yellow")),
    )
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        counters = _install_battle_diag_counters(a, b)
        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID

        diag = _drive_complete_battle_turn(
            a,
            b,
            link,
            counters=counters,
            battle_budget_frames=2400,
            completion="versus",
        )

        print("\nyellow<->yellow battle-start diagnostic:")
        for sym, cnt in counters.items():
            print(f"  {sym}: {cnt}")
        print(f"  battle_phase_frames: {diag['battle_phase_frames']}")

        assert diag["vs"][0] > 0, (
            f"A never ran DisplayLinkBattleVersusTextBox; "
            f"battle didn't start. counters={counters}"
        )
        assert diag["vs"][1] > 0, (
            f"B never ran DisplayLinkBattleVersusTextBox; "
            f"battle didn't start. counters={counters}"
        )
    finally:
        _close_linked_pair(locals().get("link"), a, b)
