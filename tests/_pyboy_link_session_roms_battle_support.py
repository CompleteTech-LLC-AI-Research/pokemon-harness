from __future__ import annotations

"""Battle-phase drivers for the pyboy-link real-ROM tests.

Split from ``tests/test_pyboy_link_session_roms.py`` (#132) with no behavior
change.
"""

from tests._battle_turn_evidence import (
    EVIDENCE_EVENTS,
    BattleTurnObserver,
    install_continuation_hooks,
    verify_battle_turns,
)
from tests._pyboy_link_session_roms_support import (
    _LINK_CHUNK_CYCLES,
    _assert_active_battle_state_is_legal,
    _install_hook_counter,
    _read_active_battle_moves,
)

_BATTLE_DIAG_SYMBOLS = (
    "CableClub_DoBattleOrTrade",
    "DisplayLinkBattleVersusTextBox",
    "BattleTransition",
    "MainInBattleLoop",
    "DisplayBattleMenu",
    "DisplayBattleMenu.leftColumn_WaitForInput",
    "DisplayBattleMenu.rightColumn_WaitForInput",
    "MoveSelectionMenu",
    "MoveSelectionMenu.menuset",
    "MainInBattleLoop.selectEnemyMove",
    "LinkBattleExchangeData",
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
    "PlayerCalcMoveDamage",
)


def _install_battle_diag_counters(
    a, b, *, versions: tuple[str, str] = ("yellow", "yellow")
) -> dict:
    """Install existing counters and read-only settled-turn observers."""
    symbols = tuple(dict.fromkeys((*_BATTLE_DIAG_SYMBOLS, *EVIDENCE_EVENTS)))
    counters = {sym: [0, 0] for sym in symbols}
    observers = []
    for idx, (sess, version) in enumerate(zip((a, b), versions, strict=True)):
        observer = BattleTurnObserver(sess, role=f"local-{idx}", version=version)
        observers.append(observer)
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx, observer)
        install_continuation_hooks(sess, observer, version=version)
    counters["_battle_evidence"] = observers
    return counters


def _assert_settled_battle_evidence(counters: dict) -> None:
    observers = counters.get("_battle_evidence")
    assert isinstance(observers, list) and len(observers) == 2
    rows = [observer.snapshot() for observer in observers]
    errors = verify_battle_turns(rows)
    print(f"  local battle settlement: {rows}")
    assert not errors, f"battle settlement evidence failed: {errors}; rows={rows}"


def _wait_for_settled_battle_evidence(link, counters: dict, *, budget_frames: int = 2400) -> None:
    """Let the ROM pass the exchange into an immutable later-turn boundary."""
    observers = counters.get("_battle_evidence")
    assert isinstance(observers, list) and len(observers) == 2
    for _ in range(0, budget_frames, 20):
        if all(observer.snapshot()["settled"] for observer in observers):
            break
        link.step_interleaved(20, chunk_cycles=_LINK_CHUNK_CYCLES)
    _assert_settled_battle_evidence(counters)


def _drive_complete_battle_turn(
    a, b, link, *, counters: dict,
    battle_budget_frames: int = 6000, step_frames: int = 20,
    completion: str = "turn",
) -> dict:
    """Drive a full link-battle turn from COLOSSEUM warp to damage resolution.

    Sequence:

    1. Walk onto the hidden-event trigger tile (same tiles as Trade
       Center: (4,4) for master, (5,4) for slave).
    2. Wait without input for ``CableClub_DoBattleOrTrade`` to run its big
       trainer+party data block exchange.
    3. ``DisplayLinkBattleVersusTextBox`` + ``BattleTransition`` fire —
       the battle intro animation plays.
    4. Wait for ``MainInBattleLoop``/``DisplayBattleMenu`` before pressing
       A once to choose FIGHT; no input is sent during the intro transition.
    5. Read the ROM-populated active move/PP buffers, move the real menu
       cursor to the first move with PP remaining, and press A once.
    6. ``LinkBattleExchangeData`` nibble-exchanges both sides' moves.
    7. ``ExecutePlayerMove`` / ``ExecuteEnemyMove`` fire as the turn resolves.

    ``completion`` controls the bounded stopping condition. ``"versus"``
    stops after both sides enter the battle intro, ``"turn"`` stops after the
    native ``LinkBattleExchangeData`` move exchange plus at least one execute
    path on each side, and ``"damage"`` additionally requires
    ``PlayerCalcMoveDamage`` on both sides. The broad matrix uses ``"turn"``;
    the focused Red/Yellow release case uses ``"damage"``. This keeps the
    wait aligned with each caller's actual acceptance assertion instead of
    making non-damaging but valid Gen I moves consume the whole frame budget.
    """
    cct = counters["CableClub_DoBattleOrTrade"]
    vs = counters["DisplayLinkBattleVersusTextBox"]
    main = counters["MainInBattleLoop"]
    battle_menu = counters["DisplayBattleMenu"]
    mm = counters["MoveSelectionMenu"]
    select_enemy = counters["MainInBattleLoop.selectEnemyMove"]
    lbe = counters["LinkBattleExchangeData"]
    dmg = counters["PlayerCalcMoveDamage"]
    epm = counters["ExecutePlayerMove"]
    eem = counters["ExecuteEnemyMove"]
    selected_move_slots: list[int] = []
    selected_move_ids: list[int] = []
    active_move_choices: list[tuple[tuple[int, int], ...]] = []

    def tick_interleaved(frames: int) -> None:
        link.step_interleaved(frames, chunk_cycles=_LINK_CHUNK_CYCLES)

    def tick_per_frame(frames: int) -> None:
        link.step(frames)

    if step_frames <= 0:
        raise ValueError("step_frames must be positive")
    if battle_budget_frames < 0:
        raise ValueError("battle_budget_frames must be non-negative")
    if completion not in {"versus", "turn", "damage"}:
        raise ValueError(
            "completion must be one of: versus, turn, damage"
        )

    phase_frames = 0
    remaining_budget = battle_budget_frames

    def tick_bounded(frames: int) -> int:
        """Advance at most the remaining post-CCT budget."""
        nonlocal phase_frames, remaining_budget
        chunk = min(frames, remaining_budget)
        if chunk <= 0:
            return 0
        tick_interleaved(chunk)
        phase_frames += chunk
        remaining_budget -= chunk
        return chunk

    def wait_interleaved(predicate) -> bool:
        """Poll a ROM milestone within one shared finite frame budget."""
        while remaining_budget > 0 and not predicate():
            tick_bounded(step_frames)
        return bool(predicate())

    # Walk onto trigger tiles — same direction rules as trade flow.
    conn_a = int(a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")])
    conn_b = int(b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")])
    INTERNAL = 0x02
    dir_a = "right" if conn_a == INTERNAL else "left"
    dir_b = "right" if conn_b == INTERNAL else "left"

    trigger_frames = 0
    for _ in range(4):
        if cct[0] > 0 and cct[1] > 0:
            break
        a.press(dir_a, duration=8)
        b.press(dir_b, duration=8)
        # Keep the trigger crossing close to simultaneous; the following
        # CableClub_DoBattleOrTrade exchange is bit-level serial traffic.
        for _ in range(step_frames):
            tick_per_frame(1)
            trigger_frames += 1
            if cct[0] > 0 or cct[1] > 0:
                break

    # Dismiss the post-warp "JUST A MOMENT!" prompt until each side enters
    # the serial-heavy function.  Stop sending input to a side immediately
    # after its hook fires: the battle intro is a timed transition, and an A
    # press consumed there can leak into a later menu in a role-dependent
    # way.  Once either side enters the function, use interleaved stepping so
    # the first serial bytes cannot run against a frozen peer.
    if not (cct[0] > 0 and cct[1] > 0):
        remaining = max(0, 1800 - trigger_frames)
        while remaining > 0 and not (cct[0] > 0 and cct[1] > 0):
            if cct[0] == 0:
                a.press("a", duration=4)
            if cct[1] == 0:
                b.press("a", duration=4)
            if cct[0] > 0 or cct[1] > 0:
                chunk = min(step_frames, remaining)
                tick_interleaved(chunk)
                phase_frames += chunk
                remaining -= chunk
            else:
                tick_per_frame(1)
                trigger_frames += 1
                remaining -= 1

    # These waits are bounded and intentionally input-free.  Returning a
    # diagnostic without fabricating input preserves the existing callers'
    # strict milestone assertions while making a scheduler/ROM stall visible.
    if cct[0] > 0 and cct[1] > 0:
        wait_interleaved(lambda: vs[0] > 0 and vs[1] > 0)

    def menu_fields(session) -> tuple[int, int, int] | None:
        try:
            memory = session._pyboy.memory
            symbols = session.symbols
            return (
                int(memory[symbols.addr_of("wCurrentMenuItem")]),
                int(memory[symbols.addr_of("wMaxMenuItem")]),
                int(memory[symbols.addr_of("wMenuWatchedKeys")]),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def battle_menu_input_ready(session) -> bool:
        fields = menu_fields(session)
        if fields is None:
            return False
        current, maximum, watched_keys = fields
        return 0 <= current <= 1 and maximum == 1 and watched_keys & 0x01

    def move_menu_input_ready(session) -> bool:
        fields = menu_fields(session)
        if fields is None:
            return False
        current, maximum, watched_keys = fields
        return (
            # The ROM stores ``wNumMovesMinusOne + 2`` as the menu maximum;
            # that is one greater than the last real move slot.  A four-move
            # mon therefore exposes max=5 while valid move cursors remain
            # 1..4.  This mirrors SelectMenuItem_CursorDown in the cartridge
            # code instead of treating wMaxMenuItem as a move count.
            1 <= current < maximum <= 5
            and watched_keys & 0x01
        )

    if completion != "versus" and vs[0] > 0 and vs[1] > 0:
        wait_interleaved(
            lambda: (
                main[0] > 0
                and main[1] > 0
                and battle_menu[0] > 0
                and battle_menu[1] > 0
            ),
        )
        wait_interleaved(
            lambda: battle_menu_input_ready(a) and battle_menu_input_ready(b)
        )

    menu_ready = (
        completion != "versus"
        and main[0] > 0
        and main[1] > 0
        and battle_menu[0] > 0
        and battle_menu[1] > 0
        and battle_menu_input_ready(a)
        and battle_menu_input_ready(b)
    )
    if menu_ready:
        # Let both ROMs finish drawing/entering HandleMenuInput, then select
        # FIGHT through the real command menu. The input-ready hook/fields
        # are ROM-owned milestones; retrying only while a side still exposes
        # that menu avoids losing a one-frame A event to the intro transition.
        tick_bounded(min(step_frames, 4))
        next_fight_input_frame = [0, 0]
        while remaining_budget > 0 and not (mm[0] > 0 and mm[1] > 0):
            for idx, session in enumerate((a, b)):
                if (
                    mm[idx] == 0
                    and phase_frames >= next_fight_input_frame[idx]
                    and battle_menu_input_ready(session)
                ):
                    session.press("a")
                    next_fight_input_frame[idx] = phase_frames + 8
            tick_bounded(min(step_frames, 2))

    if completion != "versus":
        wait_interleaved(
            lambda: (
                mm[0] > 0
                and mm[1] > 0
                and move_menu_input_ready(a)
                and move_menu_input_ready(b)
            )
        )

    move_menu_ready = (
        completion != "versus"
        and mm[0] > 0
        and mm[1] > 0
        and move_menu_input_ready(a)
        and move_menu_input_ready(b)
    )
    if move_menu_ready:
        # The hook fires at function entry.  Give the ROM enough input-free
        # time to install the menu cursor before inspecting it.
        settle = min(step_frames, 4)
        tick_bounded(settle)

        # ``battle_budget_frames`` covers the normal battle phase, but a
        # ROM can enter MoveSelectionMenu at the exact end of that budget.
        # Reserve a small, separately bounded handoff window so a valid menu
        # cannot be mistaken for a transport failure merely because the
        # final A edge was sampled on the next joypad poll.
        if remaining_budget == 0:
            remaining_budget = 1200

        slot_a, move_a = _assert_active_battle_state_is_legal(a)
        slot_b, move_b = _assert_active_battle_state_is_legal(b)
        active_a = _read_active_battle_moves(a)
        active_b = _read_active_battle_moves(b)
        active_move_choices.extend((active_a, active_b))
        selected_move_slots.extend((slot_a, slot_b))
        selected_move_ids.extend((move_a, move_b))

        def known_move_count(slots: tuple[tuple[int, int], ...]) -> int:
            count = 0
            for move_id, _pp in slots:
                if move_id == 0:
                    break
                count += 1
            assert count > 0
            return count

        def menu_cursor(session, move_count: int) -> int:
            cursor = int(
                session._pyboy.memory[
                    session.symbols.addr_of("wCurrentMenuItem")
                ]
            )
            assert 1 <= cursor <= move_count, (
                f"invalid move-menu cursor {cursor} for {move_count} moves"
            )
            return cursor - 1

        count_a = known_move_count(active_a)
        count_b = known_move_count(active_b)
        # A move-menu direction is sampled by the ROM's input loop.  A
        # single pulse can land before that loop reaches its next poll, so
        # re-read the ROM-owned cursor after every paced attempt instead of
        # assuming each injected pulse was consumed.  This keeps selection
        # entirely menu-driven and bounded while allowing a later qualified
        # slot (for example Blue's Vine Whip after SolarBeam) to be chosen.
        cursor_attempts = 0
        while menu_cursor(a, count_a) != slot_a or menu_cursor(b, count_b) != slot_b:
            if menu_cursor(a, count_a) != slot_a:
                a.press("down", duration=2)
            if menu_cursor(b, count_b) != slot_b:
                b.press("down", duration=2)
            tick_bounded(min(step_frames, 4))
            cursor_attempts += 1
            assert cursor_attempts <= 30, (
                "move-menu cursor did not reach selected supported slots: "
                f"targets={(slot_a, slot_b)} cursors="
                f"{(menu_cursor(a, count_a), menu_cursor(b, count_b))}"
            )

        assert menu_cursor(a, count_a) == slot_a
        assert menu_cursor(b, count_b) == slot_b

        # A legal move is selected through the ROM's own menu handling.  The
        # cartridge's HandleMenuInput waits for a fresh low-sensitivity
        # joypad sample; a single event can be queued just before that poll
        # and be consumed by the surrounding transition instead. Retry only
        # while the ROM still exposes the move menu, and stop per side as
        # soon as the ROM-owned selectEnemyMove label proves that A was
        # consumed. No input is injected once that boundary is crossed.
        select_enemy_before = [select_enemy[0], select_enemy[1]]
        next_move_input_frame = [phase_frames, phase_frames]
        move_input_attempts = [0, 0]
        while remaining_budget > 0 and not (
            select_enemy[0] > select_enemy_before[0]
            and select_enemy[1] > select_enemy_before[1]
        ):
            for idx, session in enumerate((a, b)):
                if (
                    select_enemy[idx] == select_enemy_before[idx]
                    and phase_frames >= next_move_input_frame[idx]
                    and move_menu_input_ready(session)
                ):
                    session.press("a")
                    move_input_attempts[idx] += 1
                    next_move_input_frame[idx] = phase_frames + 8
            tick_bounded(min(step_frames, 2))

    # Keep the original bounded resolution window and acceptance semantics:
    # callers decide whether link exchange, execution, or damage is required
    # for their tier.  Crucially, this loop never sends blind input.
    def completion_reached() -> bool:
        if completion == "versus":
            return vs[0] > 0 and vs[1] > 0
        if completion == "damage":
            return dmg[0] > 0 and dmg[1] > 0
        return (
            lbe[0] > 0
            and lbe[1] > 0
            and epm[0] + eem[0] > 0
            and epm[1] + eem[1] > 0
        )

    while remaining_budget > 0 and not completion_reached():
        tick_bounded(step_frames)

    return {
        "cct": cct,
        "vs": vs,
        "mm": mm,
        "lbe": lbe,
        "dmg": dmg,
        "main": main,
        "battle_menu": battle_menu,
        "select_enemy_move": select_enemy,
        "menu_ready": menu_ready,
        "move_menu_ready": move_menu_ready,
        "move_input_attempts": move_input_attempts if move_menu_ready else [],
        "final_menu_fields": (menu_fields(a), menu_fields(b)),
        "remaining_budget": remaining_budget,
        "active_move_choices": active_move_choices,
        "selected_move_slots": selected_move_slots,
        "selected_move_ids": selected_move_ids,
        "battle_phase_frames": phase_frames,
        "counters": counters,
    }
