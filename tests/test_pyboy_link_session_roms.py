from __future__ import annotations

"""Canonical real-ROM trade/battle acceptance matrix under :class:`PyBoyLinkSession`.

This module keeps the ordered Red/Blue/Yellow acceptance entrypoints that the
production gate and coverage catalogs reference by node ID, so those IDs are
stable across the #132 split. The session/variant helpers, the trade and battle
drivers, and the remaining diagnostic milestones now live in the sibling
``_pyboy_link_session_roms_*`` support modules and
``test_pyboy_link_session_roms_{serial,diagnostics}.py``.

The ``_drive_two_sessions_to_link_menu`` / ``_drive_past_link_menu_to_colosseum``
drivers stay in this module because ``test_local_acceptance_driver_contract``
monkeypatches them by module attribute; keeping every caller and callee in one
namespace preserves that contract unchanged.

Run the strict local acceptance cases with::

    python -m pytest -q \\
        tests/test_pyboy_link_session_roms.py::test_red_yellow_trade_swaps_real_party_records \\
        tests/test_pyboy_link_session_roms.py::test_red_yellow_battle_turn_is_resolved
"""

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from tests._pyboy_link_session_roms_battle_support import (
    _drive_complete_battle_turn,
    _install_battle_diag_counters,
    _wait_for_settled_battle_evidence,
)
from tests._pyboy_link_session_roms_support import (
    _LINK_CHUNK_CYCLES,
    _LINK_MENU_CURSOR_BUDGET_FRAMES,
    _LINK_MENU_SETTLE_FRAMES,
    _RECEPTIONIST_A_STAGGER_FRAMES,
    _ROM_PATHS,
    COLOSSEUM_MAP_ID,
    TRADE_CENTER_MAP_ID,
    _assert_battle_fixture_is_legal,
    _battle_state_path,
    _close_linked_pair,
    _fixtures_available,
    _install_hook_counter,
    _move_link_menu_cursors_to_colosseum,
    _open_session,
    _open_session_pair,
    _open_session_variant,
    _party_raw_summary,
    _require_real_rom_link_runtime,
    _rom_variant_pairs,
    _state_path,
    _variant_state_path,
)
from tests._pyboy_link_session_roms_trade_support import (
    _drive_complete_trade,
    _install_trade_diag_counters,
)

__all__ = [
    "_LINK_CHUNK_CYCLES",
    "_ROM_PATHS",
    "_close_linked_pair",
    "_drive_complete_trade",
    "_install_hook_counter",
    "_install_trade_diag_counters",
    "_move_link_menu_cursors_to_colosseum",
    "_open_session",
    "_open_session_pair",
    "_require_real_rom_link_runtime",
    "_state_path",
]

# Namespace-coupled acceptance drivers (keep with the tests the
# acceptance contract monkeypatches).



def _drive_two_sessions_to_link_menu(
    a, b, link, *, total_frames: int = 2400, frames_per_attempt: int = 20
) -> dict:
    """Interleave per-frame ticks on ``a`` and ``b`` while pressing UP
    then A to engage the Cable Club receptionist and reach ``LinkMenu``.

    Returns a diagnostics dict with hook counts for each key milestone
    plus the actual frames consumed — useful when the test fails so
    the error message can pinpoint where the flow stalled.

    The receptionist sits one tile north of the fixture's starting
    position. Pressing UP three times walks the player to the counter;
    pressing A talks to the receptionist, which triggers
    ``CableClubNPC`` → ``CableClub_DoBattleOrTradeAgain`` → preamble
    handshake → ``SaveGameData`` → nibble sync → ``LinkMenu``.
    """
    if (
        isinstance(frames_per_attempt, bool)
        or not isinstance(frames_per_attempt, int)
        or frames_per_attempt <= _RECEPTIONIST_A_STAGGER_FRAMES
    ):
        raise ValueError(
            "frames_per_attempt must be an integer leaving a post-B frame after the "
            f"{_RECEPTIONIST_A_STAGGER_FRAMES}-frame A stagger"
        )

    counters = {
        "CableClubNPC": [0, 0],
        "SaveGameData": [0, 0],
        "Serial_SyncAndExchangeNybble": [0, 0],
        "Serial_ExchangeBytes": [0, 0],
        "LinkMenu": [0, 0],
    }
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)

    def tick_both_coarse(frames: int) -> None:
        """Use the pair owner for overworld/dialog phases.

        ``step`` and ``step_interleaved`` share the same all-phase
        instruction scheduler; this helper only names the less serial-heavy
        part of the ROM flow. It must never advance either attached Session
        independently while the pair is owned by ``link``.
        """
        link.step(frames)

    def tick_both_fine(frames: int) -> None:
        """Sub-frame interleaved via :meth:`PyBoyLinkSession.step_interleaved`.
        Needed during ``Serial_SyncAndExchangeNybble`` so A and B's
        CPUs stay cycle-aligned enough for nibble-sync to converge."""
        link.step_interleaved(frames, chunk_cycles=_LINK_CHUNK_CYCLES)

    frames_used = 0

    # Walk up to the receptionist with coarse (per-frame) interleaving.
    for _ in range(3):
        a.press("up", duration=6)
        b.press("up", duration=6)
        tick_both_coarse(20)
        frames_used += 20

    # Press A and advance. Once Serial_SyncAndExchangeNybble fires on
    # both sides, switch to fine-grained interleaving so the game's
    # tight master/slave-alternation loop can synchronize. Keep this
    # choice fixed for the whole attempt: changing scheduler modes
    # between the two halves would make the public-input skew itself
    # affect the serial scheduler.
    attempts = (total_frames - frames_used) // frames_per_attempt
    for _attempt in range(attempts):
        if counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0:
            break

        # Select the phase before injecting either A press and reuse the
        # result for both pair-owner advances below.
        in_serial_phase = (
            counters["SaveGameData"][0] > 0 or counters["SaveGameData"][1] > 0
        )

        # Deliberately stagger only the ordinary public input. Endpoint A
        # receives A, then the attached pair owner advances four frames;
        # endpoint B receives A, then the owner advances the remaining
        # sixteen frames of the existing 20-frame attempt.
        a.press("a", duration=4)
        if in_serial_phase:
            tick_both_fine(_RECEPTIONIST_A_STAGGER_FRAMES)
        else:
            tick_both_coarse(_RECEPTIONIST_A_STAGGER_FRAMES)
        b.press("a", duration=4)
        if in_serial_phase:
            tick_both_fine(frames_per_attempt - _RECEPTIONIST_A_STAGGER_FRAMES)
        else:
            tick_both_coarse(frames_per_attempt - _RECEPTIONIST_A_STAGGER_FRAMES)
        frames_used += frames_per_attempt

    return {"counters": counters, "frames_used": frames_used}


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("yellow", "yellow"),
        ("blue", "blue"),
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "yellow"),
        ("yellow", "blue"),
    ],
)
def test_pair_reaches_link_menu_via_pyboy_link_session(version_a, version_b):
    """Milestone 7 flagship, parameterized over every R/B/Y pairing.

    Drive two instances under :class:`PyBoyLinkSession` through the
    Cable Club receptionist dialog to ``LinkMenu``.

    The hard assertion: ``LinkMenu`` fires on *both* sides. That
    means the preamble handshake and nibble exchange have actually
    converged through our bit-accurate :class:`SerialCore` on real
    ROMs — end-to-end proof of the new architecture.

    Pairings where either side's fixture is missing skip rather than
    fail, so partial fixture coverage still exercises the available
    pairs.
    """
    if not (_fixtures_available(version_a) and _fixtures_available(version_b)):
        pytest.skip(
            f"Cable Club fixture(s) missing for {version_a}/{version_b} — "
            f"produce with scripts/produce_cable_club_fixture.py"
        )

    a, b = _open_session_pair(
        lambda: _open_session(version_a),
        lambda: _open_session(version_b),
    )
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_two_sessions_to_link_menu(a, b, link)
        counters = diag["counters"]

        print(f"\n{version_a}<->{version_b} diagnostic counters (per-side [a, b]):")
        for sym, cnt in counters.items():
            print(f"  {sym}: {cnt}")
        print(f"  frames_used: {diag['frames_used']}")

        sg = counters["SaveGameData"]
        lm = counters["LinkMenu"]
        assert sg[0] > 0 and sg[1] > 0, (
            f"{version_a}<->{version_b}: SaveGameData never fired on "
            f"both sides; {counters}. Preamble handshake failed."
        )
        assert lm[0] > 0 and lm[1] > 0, (
            f"{version_a}<->{version_b}: LinkMenu never reached on both "
            f"sides; {counters}, frames={diag['frames_used']}. "
            f"Preamble converged but nibble-sync did not."
        )
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def _drive_past_link_menu_to_trade_center(
    a, b, link, *, post_link_menu_frames: int = 1200, frames_per_attempt: int = 20
) -> dict:
    """Continue from ``LinkMenu`` by pressing A on both sides (default
    cursor is on "Trade Center" — item 0).

    After both sides exchange matching ``0xD4`` selections via
    ``Serial_ExchangeLinkMenuSelection``, the game warps each player
    to map ``TRADE_CENTER`` (``0xEF``). We keep pressing A every
    ``frames_per_attempt`` frames to dismiss any subsequent dialog
    and poll ``read_game_state().overworld.map_id`` to detect the
    warp.

    Returns a dict with per-side map-id progression and the frames
    consumed after LinkMenu fired.
    """
    # Reuse the existing helper to reach LinkMenu.
    diag = _drive_two_sessions_to_link_menu(a, b, link)
    counters = diag["counters"]
    assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
        "precondition: both sides must have reached LinkMenu before "
        "attempting TRADE_CENTER warp"
    )

    extra_frames = 0
    attempts = post_link_menu_frames // frames_per_attempt
    for _ in range(attempts):
        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        if map_a == TRADE_CENTER_MAP_ID and map_b == TRADE_CENTER_MAP_ID:
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        # Sub-frame interleaving stays on — still in serial-heavy phase
        # (LinkMenu exchange, then the big trainer-data block exchange
        # during warp setup).
        link.step_interleaved(
            frames_per_attempt, chunk_cycles=_LINK_CHUNK_CYCLES
        )
        extra_frames += frames_per_attempt

    map_a = a.read_game_state().overworld.map_id
    map_b = b.read_game_state().overworld.map_id
    return {
        "counters": counters,
        "frames_to_link_menu": diag["frames_used"],
        "extra_frames": extra_frames,
        "final_map_a": map_a,
        "final_map_b": map_b,
    }


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("yellow", "yellow"),
        ("blue", "blue"),
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "yellow"),
        ("yellow", "blue"),
    ],
)
def test_pair_completes_trade_end_to_end(version_a, version_b):
    """Flagship: two instances actually trade a Pokémon.

    This closes the "two agents trade a Pokémon" loop the design doc
    set out to unblock. The hard assertion: ``_AddEnemyMonToPlayerParty``
    fires on both sides — meaning each one received and installed the
    peer's mon into its own party.
    """
    if not (_fixtures_available(version_a) and _fixtures_available(version_b)):
        pytest.skip(
            f"Cable Club fixture(s) missing for {version_a}/{version_b}"
        )

    a, b = _open_session_pair(
        lambda: _open_session(version_a),
        lambda: _open_session(version_b),
    )
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        pre_a = a.read_game_state().party
        pre_b = b.read_game_state().party
        pre_a_lead = pre_a.lead.species if pre_a.lead else None
        pre_b_lead = pre_b.lead.species if pre_b.lead else None
        before_a_raw = _party_raw_summary(a)
        before_b_raw = _party_raw_summary(b)

        diag_counters = _install_trade_diag_counters(a, b)

        warp = _drive_past_link_menu_to_trade_center(a, b, link)
        assert warp["final_map_a"] == TRADE_CENTER_MAP_ID
        assert warp["final_map_b"] == TRADE_CENTER_MAP_ID

        trade_diag = _drive_complete_trade(a, b, link, counters=diag_counters)

        after_a_raw = _party_raw_summary(a)
        after_b_raw = _party_raw_summary(b)
        post_a_state = a.read_game_state()
        post_b_state = b.read_game_state()
        link_state_a = a._pyboy.memory[a.symbols.addr_of("wLinkState")]
        link_state_b = b._pyboy.memory[b.symbols.addr_of("wLinkState")]
        ptr_idx_a = a._pyboy.memory[
            a.symbols.addr_of("wTradeCenterPointerTableIndex")
        ]
        ptr_idx_b = b._pyboy.memory[
            b.symbols.addr_of("wTradeCenterPointerTableIndex")
        ]
        # hSerialConnectionStatus lives in HRAM; read via addr.
        conn_a = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
        conn_b = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
        # Player X/Y so we can see if walking actually happened.
        ow_a = post_a_state.overworld
        ow_b = post_b_state.overworld
        post_a_lead = post_a_state.party.lead.species if post_a_state.party.lead else None
        post_b_lead = post_b_state.party.lead.species if post_b_state.party.lead else None

        print(
            f"\n{version_a}<->{version_b} trade diagnostic:\n"
            f"  pre party leads:  A=#{pre_a_lead}, B=#{pre_b_lead}\n"
            f"  post party leads: A=#{post_a_lead}, B=#{post_b_lead}\n"
            f"  warp finals: map_a=0x{warp['final_map_a']:02x}, "
            f"map_b=0x{warp['final_map_b']:02x} "
            f"(after {warp['extra_frames']} frames past LinkMenu)\n"
            f"  post-trade maps: "
            f"map_a=0x{post_a_state.overworld.map_id:02x}, "
            f"map_b=0x{post_b_state.overworld.map_id:02x}\n"
            f"  post-trade wLinkState: "
            f"A=0x{link_state_a:02x}, B=0x{link_state_b:02x}\n"
            f"  post-trade wTradeCenterPointerTableIndex: "
            f"A=0x{ptr_idx_a:02x}, B=0x{ptr_idx_b:02x}\n"
            f"  hSerialConnectionStatus: "
            f"A=0x{conn_a:02x}, B=0x{conn_b:02x} "
            f"(01=EXTERNAL slave, 02=INTERNAL master)\n"
            f"  player pos: A=({ow_a.x}, {ow_a.y}), B=({ow_b.x}, {ow_b.y})\n"
            f"  trade phase frames: {trade_diag['trade_phase_frames']}"
        )
        print(f"  raw party A: {_party_raw_summary(a)}")
        print(f"  raw party B: {_party_raw_summary(b)}")
        for label, core in zip(("A", "B"), link.cores):
            backend = getattr(core, "backend", None)
            print(
                f"  coordinator {label}: edges={getattr(backend, 'edge_count', None)} "
                f"unarmed={getattr(backend, 'peer_unarmed_edges', None)} "
                f"peer_master={getattr(backend, 'peer_master_edges', None)} "
                f"rearm_attempts={getattr(backend, 'peer_rearm_attempts', None)} "
                f"rearm_successes={getattr(backend, 'peer_rearm_successes', None)}"
            )
        for sym, cnt in trade_diag["counters"].items():
            print(f"  {sym}: {cnt}")

        am = trade_diag["add_mon"]
        assert am[0] > 0, (
            f"A never ran _AddEnemyMonToPlayerParty; trade didn't complete "
            f"on side A. diagnostic={trade_diag}"
        )
        assert am[1] > 0, (
            f"B never ran _AddEnemyMonToPlayerParty; trade didn't complete "
            f"on side B. diagnostic={trade_diag}"
        )
        assert after_a_raw["count"] == before_a_raw["count"]
        assert after_b_raw["count"] == before_b_raw["count"]
        assert after_a_raw["species"][0] == before_b_raw["mon_species"][0]
        assert after_b_raw["species"][0] == before_a_raw["mon_species"][0]
        assert after_a_raw["mon_records"][0] == before_b_raw["mon_records"][0]
        assert after_b_raw["mon_records"][0] == before_a_raw["mon_records"][0]
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def test_red_yellow_trade_swaps_real_party_records():
    """Release acceptance: a natural Red/Yellow trade swaps both leads.

    The broad matrix above is intentionally diagnostic and only proves the
    ROM reached the trade routine. This case is the strict gate: it starts
    from two untouched, ROM-matched Cable Club fixtures and checks the
    game-owned species list plus each received party-mon record after the
    real ``_AddEnemyMonToPlayerParty`` path completes.
    """
    if not (_fixtures_available("red") and _fixtures_available("yellow")):
        pytest.skip("Red and Yellow Cable Club fixtures are required")

    a, b = _open_session_pair(
        lambda: _open_session("red"),
        lambda: _open_session("yellow"),
    )
    try:
        before_a = _party_raw_summary(a)
        before_b = _party_raw_summary(b)
        expected_a = before_b["mon_species"][0]
        expected_b = before_a["mon_species"][0]
        assert expected_a != expected_b, (
            f"strict fixture leads must differ: A={before_a} B={before_b}"
        )

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        counters = _install_trade_diag_counters(a, b)

        warp = _drive_past_link_menu_to_trade_center(a, b, link)
        assert warp["final_map_a"] == TRADE_CENTER_MAP_ID
        assert warp["final_map_b"] == TRADE_CENTER_MAP_ID
        diag = _drive_complete_trade(a, b, link, counters=counters)

        after_a = _party_raw_summary(a)
        after_b = _party_raw_summary(b)
        assert diag["add_mon"][0] > 0 and diag["add_mon"][1] > 0, (
            f"trade hook did not fire on both sides: {diag}"
        )
        assert after_a["count"] == before_a["count"]
        assert after_b["count"] == before_b["count"]
        assert after_a["species"][0] == expected_a
        assert after_a["mon_species"][0] == expected_a
        assert after_a["mon_records"][0] == before_b["mon_records"][0]
        assert after_b["species"][0] == expected_b
        assert after_b["mon_species"][0] == expected_b
        assert after_b["mon_records"][0] == before_a["mon_records"][0]
        assert after_a["species"][-1] == 0xFF
        assert after_b["species"][-1] == 0xFF
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def _drive_past_link_menu_to_colosseum(
    a, b, link, *, post_link_menu_frames: int = 1800, frames_per_attempt: int = 20
) -> dict:
    """Continue from ``LinkMenu`` by selecting COLOSSEUM with ordinary input.

    After both sides exchange matching selections via
    ``Serial_ExchangeLinkMenuSelection``, the game warps each player to
    map ``COLOSSEUM`` (``0xF1``) — provided each party has ≥3 mons.
    Caller is responsible for loading a pre-generated battle fixture with a
    legal party.
    """
    diag = _drive_two_sessions_to_link_menu(a, b, link)
    counters = diag["counters"]
    assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
        "precondition: both sides must have reached LinkMenu"
    )

    # LinkMenu's hook fires at function entry, before HandleMenuInput
    # begins polling keys. Preserve the existing 60-frame owner settle, then
    # use bounded, ordinary per-endpoint input. A cross-version peer can be
    # ready later; observe each cursor before every event and do not press a
    # side that already reached COLOSSEUM.
    link.step_interleaved(_LINK_MENU_SETTLE_FRAMES)
    cursor_diag = _move_link_menu_cursors_to_colosseum(
        a,
        b,
        link,
        budget_frames=_LINK_MENU_CURSOR_BUDGET_FRAMES,
        frames_per_attempt=frames_per_attempt,
    )
    cur_a = cursor_diag["cursor_a"]
    cur_b = cursor_diag["cursor_b"]
    # Sanity: both cursors should now be on item 1 (COLOSSEUM). Keep this
    # strict assertion: a LinkMenu milestone without matching user-visible
    # menu selections is not valid battle setup.
    assert cur_a == 1 and cur_b == 1, (
        f"cursor didn't land on COLOSSEUM: a={cur_a}, b={cur_b}"
    )

    extra_frames = 0
    attempts = post_link_menu_frames // frames_per_attempt
    for _ in range(attempts):
        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        if map_a == COLOSSEUM_MAP_ID and map_b == COLOSSEUM_MAP_ID:
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        link.step_interleaved(frames_per_attempt)
        extra_frames += frames_per_attempt

    return {
        "counters": counters,
        "frames_to_link_menu": diag["frames_used"],
        "cursor_frames": _LINK_MENU_SETTLE_FRAMES + cursor_diag["frames_used"],
        "extra_frames": extra_frames,
        "final_map_a": a.read_game_state().overworld.map_id,
        "final_map_b": b.read_game_state().overworld.map_id,
    }


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("yellow", "yellow"),
        ("blue", "blue"),
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "yellow"),
        ("yellow", "blue"),
    ],
)
def test_pair_completes_battle_turn(version_a, version_b):
    """Flagship battle test: two instances resolve one link-battle turn.

    The hard assertion: both sides complete one full turn of the battle
    loop — ``ExecutePlayerMove`` and ``ExecuteEnemyMove`` each fire on
    both sides (each instance processes its own move and simulates the
    peer's move locally), and ``LinkBattleExchangeData`` fires on both
    sides (the move-selection nibble exchange went through the link).

    ``PlayerCalcMoveDamage`` is not the right acceptance hook because
    it only fires on damaging moves that go through the canonical
    damage-calc path. Gen I battles have plenty of non-canonical paths
    (status moves, OHKO moves, trapping moves like Bind/Wrap whose
    damage is done via :asm:`DoMultiHitTrappingMove`, missing, fainting
    before it runs, etc.). The acceptance criterion is that the link
    protocol carries the move exchange and both sides advance through
    the turn in lockstep — that's what Execute* + LinkBattleExchangeData
    assert.
    """
    if not (_fixtures_available(version_a) and _fixtures_available(version_b)):
        pytest.skip(
            f"Cable Club fixture(s) missing for {version_a}/{version_b}"
        )

    a, b = _open_session_pair(
        lambda: _open_session(
            version_a, state_path=_battle_state_path(version_a)
        ),
        lambda: _open_session(
            version_b, state_path=_battle_state_path(version_b)
        ),
    )
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        counters = _install_battle_diag_counters(a, b, versions=(version_a, version_b))
        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID

        diag = _drive_complete_battle_turn(a, b, link, counters=counters)

        print(f"\n{version_a}<->{version_b} battle-turn diagnostic:")
        for sym, cnt in counters.items():
            print(f"  {sym}: {cnt}")
        print(f"  battle_phase_frames: {diag['battle_phase_frames']}")
        print(
            "  battle handoff: "
            f"menu_fields={diag['final_menu_fields']} "
            f"remaining_budget={diag['remaining_budget']} "
            f"move_menu_ready={diag['move_menu_ready']} "
            f"move_input_attempts={diag['move_input_attempts']}"
        )

        epm = counters["ExecutePlayerMove"]
        eem = counters["ExecuteEnemyMove"]
        lbe = counters["LinkBattleExchangeData"]
        assert lbe[0] > 0 and lbe[1] > 0, (
            f"LinkBattleExchangeData didn't fire on both sides; moves "
            f"were never exchanged via the link. counters={counters} "
            f"diag={diag}"
        )
        # Either ExecutePlayerMove or ExecuteEnemyMove (or both) must
        # fire on each side. In heavily-mismatched pairings (L54 vs
        # L16 Pikachu) the weaker side's mon can faint before its own
        # turn executes — skipping ExecutePlayerMove on that side while
        # its ExecuteEnemyMove counterpart still fires — and vice-versa
        # on the peer (where only their own ExecutePlayerMove ran
        # before the opponent fainted on their machine's simulation).
        # Requiring ≥1 execute-path fire per side is the cleanest
        # "this side advanced through some portion of the turn"
        # predicate that survives the fainting race.
        assert epm[0] + eem[0] > 0, (
            f"Side A never fired ExecutePlayerMove or ExecuteEnemyMove; "
            f"the turn didn't advance on A. counters={counters}"
        )
        assert epm[1] + eem[1] > 0, (
            f"Side B never fired ExecutePlayerMove or ExecuteEnemyMove; "
            f"the turn didn't advance on B. counters={counters}"
        )
        _wait_for_settled_battle_evidence(link, counters)
    finally:
        _close_linked_pair(locals().get("link"), a, b)


def test_red_yellow_battle_turn_is_resolved():
    """Release acceptance: Red/Yellow exchange and resolve one move turn."""
    if not (_fixtures_available("red") and _fixtures_available("yellow")):
        pytest.skip("Red and Yellow battle fixtures are required")

    a, b = _open_session_pair(
        lambda: _open_session("red", state_path=_battle_state_path("red")),
        lambda: _open_session(
            "yellow", state_path=_battle_state_path("yellow")
        ),
    )
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        counters = _install_battle_diag_counters(a, b, versions=("red", "yellow"))
        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID
        _drive_complete_battle_turn(
            a, b, link, counters=counters, completion="damage"
        )

        required_hooks = (
            "DisplayLinkBattleVersusTextBox",
            "BattleTransition",
            "MainInBattleLoop",
            "MoveSelectionMenu",
            "LinkBattleExchangeData",
            "ExecutePlayerMove",
            "ExecuteEnemyMove",
            "PlayerCalcMoveDamage",
        )
        for symbol in required_hooks:
            assert counters[symbol][0] > 0 and counters[symbol][1] > 0, (
                f"{symbol} did not fire on both sides: counters={counters}"
            )
        _wait_for_settled_battle_evidence(link, counters)
    finally:
        _close_linked_pair(locals().get("link"), a, b)


@pytest.mark.parametrize("version,rom_a,tag_a,rom_b,tag_b", _rom_variant_pairs())
def test_same_version_variants_reach_link_menu(
    version, rom_a, tag_a, rom_b, tag_b,
):
    """Prove same-version pairings reach ``LinkMenu`` under every ROM
    variant combo (vanilla×vanilla, vanilla×color, color×color).

    The color patch only alters cartridge-header CGB flags + color
    palette code; serial-protocol code is untouched. This test guards
    against any future regression where the patch accidentally affects
    the serial path or Cable Club script.

    Uses the LinkMenu milestone rather than full battle to keep runtime
    reasonable — 4 variants × 9 pairings × 7 min would be ~4 hours. The
    LinkMenu path converges in ~2 min per pairing.

    Each side loads its variant-specific cable_club.state fixture
    (``cable_club.state`` for color, ``cable_club-vanilla.state`` for
    vanilla). Skips when the required fixture is missing — so pairings
    auto-enable once vanilla fixtures are produced.
    """
    if not (rom_a.is_file() and rom_b.is_file()):
        pytest.skip(f"ROM variant missing: {rom_a.name} or {rom_b.name}")
    state_a = _variant_state_path(version, tag_a)
    state_b = _variant_state_path(version, tag_b)
    if not (state_a.is_file() and state_b.is_file()):
        missing = [p.name for p in (state_a, state_b) if not p.is_file()]
        pytest.skip(
            f"cable_club state fixture(s) missing for {version}: "
            f"{missing}. Produce via scripts/produce_cable_club_fixture.py"
        )

    a, b = _open_session_pair(
        lambda: _open_session_variant(version, rom_a, tag_a),
        lambda: _open_session_variant(version, rom_b, tag_b),
    )
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_two_sessions_to_link_menu(a, b, link)
        counters = diag["counters"]

        print(
            f"\n{version} {tag_a}<->{tag_b} LinkMenu reach:\n"
            + "\n".join(f"  {sym}: {cnt}" for sym, cnt in counters.items())
            + f"\n  frames_used: {diag['frames_used']}"
        )

        lm = counters["LinkMenu"]
        assert lm[0] > 0 and lm[1] > 0, (
            f"{version} {tag_a}<->{tag_b}: LinkMenu never reached; {counters}"
        )
    finally:
        _close_linked_pair(locals().get("link"), a, b)
