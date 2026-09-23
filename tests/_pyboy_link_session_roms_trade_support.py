from __future__ import annotations

"""Trade-phase drivers for the pyboy-link real-ROM tests.

Split from ``tests/test_pyboy_link_session_roms.py`` (#132) with no behavior
change.
"""

from tests._pyboy_link_session_roms_support import _LINK_CHUNK_CYCLES, _install_hook_counter

_TRADE_DIAG_SYMBOLS = (
    "CableClub_DoBattleOrTrade",
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
)


def _install_trade_diag_counters(a, b) -> dict:
    """Installs hooks for the trade-phase diagnostic symbols on both
    sides and returns a shared counters dict. Call once, before any
    driving, so hooks catch events that fire during the warp itself
    (e.g. ``CableClub_DoBattleOrTrade``)."""
    counters = {sym: [0, 0] for sym in _TRADE_DIAG_SYMBOLS}
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)
    return counters


def _drive_complete_trade(
    a, b, link, *, counters: dict,
    trade_budget_frames: int = 4000, step_frames: int = 20,
) -> dict:
    """Drive a full Pokémon trade from fixture start to completion.

    Sequence (after both sides warp to TRADE_CENTER):

    1. Press A — selects the first player-party mon (cursor is already
       on slot 0 since ``TradeCenter_SelectMon`` initializes it there).
       Opens the STATS/TRADE sub-menu with the cursor on "STATS".
    2. Press RIGHT — moves the sub-menu cursor to "TRADE".
    3. Press A — chooses TRADE. Fires
       ``Serial_PrintWaitingTextAndSyncAndExchangeNybble`` which sends
       the selected player-mon index to the peer.
    4. Peer does the same on its side.
    5. Both sides see each other's selections, enter
       ``TradeCenter_ConfirmMonSelection`` → "WILL TRADE X FOR Y?" YES/NO
       prompt (default cursor on YES).
    6. Press A on both sides — confirms the trade.
    7. ``TradeCenter_Trade`` runs; trade animation plays; both sides'
       ``_AddEnemyMonToPlayerParty`` fires and the new mon lands in the
       party.

    Between presses we run ``link.step_interleaved`` so the busy serial
    exchange under nibble-sync and patch-list transfer stays aligned.
    """
    # ``counters`` is expected pre-populated by
    # :func:`_install_trade_diag_counters` before the warp driver ran,
    # so fire events during CableClub_DoBattleOrTrade are caught too.
    add_mon = counters["_AddEnemyMonToPlayerParty"]
    trade_center_trade = counters["TradeCenter_Trade"]

    def tick_interleaved(frames: int) -> None:
        """Sub-frame interleaved — for serial-heavy phases."""
        link.step_interleaved(frames, chunk_cycles=_LINK_CHUNK_CYCLES)

    def tick_per_frame(frames: int) -> None:
        """Per-frame via the paired owner — for overworld/menu navigation
        where input handling is what matters, not byte-level serial
        sync. The coordinator still handles any incidental serial
        transfers via its CoordinatedBackend."""
        link.step(frames)

    # After warp, both players must walk onto their hidden-event
    # trigger tiles to flip wLinkState = LINK_STATE_START_TRADE and
    # cause CableClub_Run (which fires in WaitForTextScrollButtonPress)
    # to kick off CableClub_DoBattleOrTrade.
    #   - Master (internal clock): spawn (3, 4), walk RIGHT to (4, 4).
    #   - Slave  (external clock): spawn (6, 4), walk LEFT to (5, 4).
    # See data/events/hidden_events.asm:283-286.
    # Resolve directions based on hSerialConnectionStatus so the pair
    # works regardless of which PyBoy took the master role.
    conn_a_now = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
    conn_b_now = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
    INTERNAL = 0x02
    dir_a = "right" if conn_a_now == INTERNAL else "left"
    dir_b = "right" if conn_b_now == INTERNAL else "left"

    # Per-frame stepping during the walk — button-event handling is
    # sensitive to tick discipline and step_interleaved's singlestep
    # path was losing some inputs.
    for _ in range(4):
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        # Press the walk direction a few times — spawn facing may
        # need the first press to rotate, the second to walk.
        a.press(dir_a, duration=8)
        b.press(dir_b, duration=8)
        # Do not batch the trigger crossing.  One side can enter
        # CableClub_DoBattleOrTrade during this call; ticking twenty
        # complete frames on that side before advancing its peer lets the
        # first few serial transfers use stale handshake bytes.
        for _ in range(step_frames):
            tick_per_frame(1)
            if counters["CableClub_DoBattleOrTrade"][0] > 0 or counters["CableClub_DoBattleOrTrade"][1] > 0:
                break

    # Now A-mash to dismiss "JUST A MOMENT!" dialog on each side,
    # which causes WaitForTextScrollButtonPress -> CableClub_Run to
    # fire and launch the big pre-trade exchange.
    settle_frames = 0
    while settle_frames < 1800:
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        # The first call into CableClub_Run can begin the large trainer/
        # party exchange before the entry hook is observed on both sides.
        # Keep the CPUs interleaved for this whole post-warp dialog loop;
        # switching from sequential frames only after the hook fires lets
        # the first side get ahead and corrupt the byte stream.
        tick_interleaved(step_frames)
        settle_frames += step_frames

    # State-aware trade navigation — reactive to the *deepest* hook
    # that just fired, not a forward phase counter. The game can
    # escape back to outer menus (A in STATS sub-menu displays stats
    # then returns to playerMonMenu), so we can't assume linear
    # progression.
    #
    # Rules per tick:
    #   - If selectTradeMenuItem just ticked      → press A (confirm TRADE)
    #   - Elif selectStatsMenuItem just ticked    → press RIGHT (STATS→TRADE)
    #   - Elif playerMonMenu_HandleInput ticked   → press A (enter sub-menu)
    #   - Elif TradeCenter_Trade ticked           → press A (advance YES/NO)
    #   - Otherwise                               → press A (dismiss dialogs)

    stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
    trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
    menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
    tct_key = "TradeCenter_Trade"

    prev = {
        k: list(counters[k])
        for k in (stats_key, trade_key, menu_key, tct_key)
    }
    # Per-side "press RIGHT for N more iterations" counter. Set when
    # selectStatsMenuItem ticks; decremented each tick. Reset when
    # selectTradeMenuItem ticks (cursor already moved).
    right_pending = [0, 0]
    RIGHT_PRESS_ITERATIONS = 5

    extra_frames = 0
    attempts = trade_budget_frames // step_frames
    for _ in range(attempts):
        if add_mon[0] > 0 and add_mon[1] > 0:
            break
        # Tick first so hook counters reflect what happened during
        # the just-run frames. Then snapshot, compare to prev (last
        # iteration's snapshot), and issue keys based on what fired.
        # Switch to sub-frame interleaving as soon as either side
        # enters CableClub_DoBattleOrTrade — that function drives
        # the big ~200-byte block exchange via Serial_ExchangeBytes,
        # which is too serial-heavy for per-frame interleaving
        # (peer misses bytes as IRQs pile up while its CPU is frozen).
        cct_key = "CableClub_DoBattleOrTrade"
        if counters[cct_key][0] > 0 or counters[cct_key][1] > 0:
            tick_interleaved(step_frames)
        else:
            tick_per_frame(step_frames)
        extra_frames += step_frames

        now = {k: list(counters[k]) for k in (stats_key, trade_key, menu_key, tct_key)}
        for idx, sess in enumerate((a, b)):
            def ticked(key, *, _now=now, _idx=idx, _prev=prev):
                return _now[key][_idx] > _prev[key][_idx]

            if ticked(trade_key):
                # Cursor is on TRADE — confirm.
                sess.press("a", duration=4)
                right_pending[idx] = 0
            elif ticked(stats_key):
                # Entered STATS/TRADE sub-menu — move cursor to TRADE.
                # Keep RIGHT held for a few iterations to be robust
                # against HandleMenuInput's poll cadence.
                right_pending[idx] = RIGHT_PRESS_ITERATIONS
                sess.press("right", duration=12)
            elif right_pending[idx] > 0:
                sess.press("right", duration=12)
                right_pending[idx] -= 1
            elif ticked(menu_key):
                # Player-mon menu waiting for input — pick the lead.
                sess.press("a", duration=4)
            elif ticked(tct_key):
                # TradeCenter_Trade animation + YES/NO dialog — A-mash.
                sess.press("a", duration=4)
            else:
                # Pre-menu dialog or between transitions — A-mash.
                sess.press("a", duration=4)
        prev = now

    # The execution hook fires on function entry, before
    # ``_AddEnemyMonToPlayerParty`` has copied the received record into
    # the party array. Advance both emulators past the function body so
    # callers inspect completed game state rather than an entry snapshot.
    post_hook_frames = 0
    if add_mon[0] > 0 and add_mon[1] > 0:
        post_hook_frames = 120
        tick_interleaved(post_hook_frames)

    return {
        "add_mon": add_mon,
        "trade_center_trade": trade_center_trade,
        "trade_phase_frames": extra_frames + step_frames * 7 + post_hook_frames,
        "counters": counters,
    }
