"""ROM-owned observation and turn-drive helpers for the battle-state producer.

Split out of ``scripts/produce_battle_state_fixtures.py`` for the #122
file-size contract with no behavior change: the read-only WRAM/menu
observations and the turn/replacement drives moved here verbatim.
"""

from __future__ import annotations

from scripts.produce_battle_state_fixtures_model import (
    BATTLE_PARTY_MENU,
    COMMAND_MENU_MAX_ITEM,
    COMMAND_MENU_WATCHED_KEYS,
    MENU_WATCHED_A,
    MOVE_MENU_MAX_ITEM,
    MOVE_MENU_WATCHED_KEYS,
    PARTY_MENU_ANIM_WITNESS,
    PARTY_MON_SIZE,
    REPLACEMENT_END_GRACE_FRAMES,
)

# ---------------------------------------------------------------------------
# ROM-owned observation helpers (read-only; never write RAM)
# ---------------------------------------------------------------------------


def _byte(session, name: str) -> int:
    return _wram_byte(session, session.symbols.addr_of(name))


def _word(session, name: str) -> int:
    address = session.symbols.addr_of(name)
    return (_wram_byte(session, address) << 8) | _wram_byte(session, address + 1)


# The battle WRAM reads below resolve WRAM bank 1 rather than the ``SVBK``
# window; see ``pokered_harness.symbols.loader.read_wram_u8`` for why that is
# the ROM's own battle state.  ``SVBK == 2`` is a legitimate ROM state, not a
# degenerated one, and reading the bank-1 bytes directly is what keeps the
# driver able to see a live battle party menu behind it instead of failing
# closed and starving the pair.
WRAM_BANK_PORT = 0xFF70
WRAM_SWITCHABLE_START = 0xD000
WRAM_SWITCHABLE_END = 0xE000
WRAM_BATTLE_BANK = 1


def _wram_byte(session, address: int) -> int:
    """One byte of the ROM's own battle WRAM, independent of the ``SVBK`` window.

    ``0xD000``-``0xDFFF`` is resolved from WRAM bank 1, the bank the linker
    assigns the battle WRAM section to, so the read survives the ROM banking
    another region into that window.  The fixed ``0xC000``-``0xCFFF`` range has
    no bank register and keeps its ordinary mapped read.
    """
    from pokered_harness.symbols.loader import read_wram_u8

    return read_wram_u8(session._pyboy.memory, address)


def _wram_bank(session) -> int | None:
    """The CGB WRAM bank ``0xD000``-``0xDFFF`` currently maps to."""
    try:
        return int(session._pyboy.memory[WRAM_BANK_PORT]) & 0x07
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return None


def _banked_readable(session) -> bool:
    """True while the ROM's battle WRAM bank is readable.

    The battle observations are taken from WRAM bank 1 directly rather than
    through the ``SVBK`` window, so the bank register no longer decides whether
    they are trustworthy and there is no window to fail closed on.  The
    predicate is retained as the named admission point for those reads and to
    keep the recorded ``wram_bank`` meaningful as a window annotation.
    """
    return True


def _menu_fields(session) -> tuple[int, int, int] | None:
    try:
        return (
            _byte(session, "wCurrentMenuItem"),
            _byte(session, "wMaxMenuItem"),
            _byte(session, "wMenuWatchedKeys"),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _battle_menu_input_ready(session) -> bool:
    fields = _menu_fields(session)
    if fields is None:
        return False
    current, maximum, watched_keys = fields
    return (
        0 <= current <= 1
        and maximum == COMMAND_MENU_MAX_ITEM
        and watched_keys in COMMAND_MENU_WATCHED_KEYS
    )


def _move_menu_input_ready(session) -> bool:
    fields = _menu_fields(session)
    if fields is None:
        return False
    current, maximum, watched_keys = fields
    # The ROM stores ``wNumMovesMinusOne + 2`` as the menu maximum; a
    # four-move mon therefore exposes max=5 while cursors stay 1..4.  The
    # watched-key mask must be the move menu's own: a battle party menu that
    # has just confirmed a replacement leaves current=1, max=5 behind, so
    # geometry alone names a closed party menu a live move menu and the A
    # intended for it would be consumed by the *next* command menu instead.
    return watched_keys == MOVE_MENU_WATCHED_KEYS and 1 <= current < maximum <= MOVE_MENU_MAX_ITEM


def _party_menu_live(session) -> bool:
    """True while ``session``'s ROM is inside the party-menu input handler.

    ``wPartyMenuAnimMonEnabled == PARTY_MENU_ANIM_WITNESS`` is set by the ROM
    for exactly the span in which the battle party menu waits for input and is
    cleared by every other menu handler, so it is the liveness half of the
    party-menu test (see ``PARTY_MENU_ANIM_WITNESS``).
    """
    try:
        if not _banked_readable(session):
            return False
        return _byte(session, "wPartyMenuAnimMonEnabled") == PARTY_MENU_ANIM_WITNESS
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _party_menu_ready(session) -> bool:
    """True when the battle party menu is open *and* waiting for input.

    Geometry alone is not enough.  ``ChooseNextMon``
    (engine/battle/core.asm:1086-1088) writes ``wPartyMenuTypeOrMessageID``
    once and never clears it, and ``wCurrentMenuItem``/``wMaxMenuItem``/
    ``wMenuWatchedKeys`` keep their last values after their menu closes, so a
    side that has advanced past the party menu can still satisfy the geometry
    test by coincidence: a link-battle move menu watches
    ``PAD_UP|PAD_DOWN|PAD_A|PAD_B`` (195) and a six-mon party menu watches
    ``PAD_A|PAD_B`` (3), and both expose ``wMaxMenuItem == 5``.  Treating such
    a side as a party menu made the pump press directions that a live move
    menu swallowed, which starved the pair instead of advancing it.

    The ROM-owned witness is therefore required in addition to the geometry,
    so only a party menu that is demonstrably taking input is ever named.
    """
    if not _party_menu_live(session):
        return False
    try:
        count = _byte(session, "wPartyCount")
        if _byte(session, "wPartyMenuTypeOrMessageID") != BATTLE_PARTY_MENU:
            return False
        current, maximum, watched_keys = _menu_fields(session)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return 1 <= count <= 6 and 0 <= current < count and maximum == count - 1 and watched_keys & 0x01


def _party_hp(session) -> list[int]:
    base = session.symbols.addr_of("wPartyMons")
    count = _byte(session, "wPartyCount")
    return [
        (_wram_byte(session, base + slot * PARTY_MON_SIZE + 1) << 8)
        | _wram_byte(session, base + slot * PARTY_MON_SIZE + 2)
        for slot in range(count)
    ]


def _party_hp_or_empty(session) -> list[int]:
    """Party HP, or ``[]`` while the banked window is not the ROM's bank 1."""
    if not _banked_readable(session):
        return []
    return _party_hp(session)


def _active_hp(session) -> tuple[int, int]:
    return _word(session, "wBattleMonHP"), _word(session, "wBattleMonMaxHP")


def _battle_active(session) -> bool:
    if not _banked_readable(session):
        return False
    return _byte(session, "wIsInBattle") != 0


def _battle_ended(session) -> bool:
    """True only when ``wIsInBattle`` reads zero from the ROM's own bank.

    A banked window the ROM is not using reads another region's bytes, which
    can be zero for reasons unrelated to the battle ending, so the check is
    gated on the bank being mapped rather than trusting the byte alone.
    """
    return _banked_readable(session) and _byte(session, "wIsInBattle") == 0


def _party_summary(session) -> dict:
    count = _byte(session, "wPartyCount")
    species_addr = session.symbols.addr_of("wPartySpecies")
    species = [_wram_byte(session, species_addr + slot) for slot in range(count)]
    hp = _party_hp_or_empty(session)
    active_hp, active_max = _active_hp(session)
    return {
        "party_count": count,
        "species": species,
        "party_hp": hp,
        "active_hp": active_hp,
        "active_max_hp": active_max,
        "is_in_battle": _byte(session, "wIsInBattle"),
        "battle_result": _byte(session, "wBattleResult"),
        "in_handle_player_mon_fainted": _byte(session, "wInHandlePlayerMonFainted"),
        "party_menu_type": _byte(session, "wPartyMenuTypeOrMessageID"),
        "wram_bank": _wram_bank(session),
    }


def _replayable_turn_plan(plan) -> bool:
    """True when a recorded turn names one usable move slot per owner.

    The pre-terminal pair is admitted so a consumer can fold both owners back
    into their own FIGHT/move menus and drive the remaining turns to the
    terminal return.  That only holds when the capture recorded a move for
    each owner, so an unusable plan is refused instead of being written into
    the manifest.
    """

    if not isinstance(plan, list) or len(plan) != 2:
        return False
    return all(
        isinstance(entry, dict)
        and type(entry.get("slot")) is int
        and 0 <= entry["slot"] < 4
        and type(entry.get("move")) is int
        and entry["move"] != 0
        for entry in plan
    )


def _active_moves(session) -> list:
    """The live combatant's ``(move, pp)`` pairs, or ``[]`` when unreadable."""
    from tests.test_pyboy_link_session_roms import _read_active_battle_moves

    try:
        return list(_read_active_battle_moves(session))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return []


def _first_usable_move_slot(session) -> int | None:
    """Move-menu cursor value (``slot + 1``) of the first move with PP left."""
    for slot, entry in enumerate(_active_moves(session)):
        try:
            move_id, pp = entry
        except (TypeError, ValueError):
            return None
        if move_id and (int(pp) & 0x3F) > 0:
            return slot + 1
    return None


def _slot_usable(session, cursor: int) -> bool:
    """True when ``cursor`` (one past the slot) names a move with PP left."""
    moves = _active_moves(session)
    if not 0 <= cursor - 1 < len(moves):
        return False
    try:
        move_id, pp = moves[cursor - 1]
    except (TypeError, ValueError):
        return False
    return bool(move_id) and (int(pp) & 0x3F) > 0


def _planned_move_cursor(session, target_slot: int | None, maximum: int) -> int | None:
    """Cursor of the move the pump should confirm, or ``None`` if none is usable.

    A plan is recorded at one boundary and replayed later, so the slot it names
    can outlive the mon that offered it: a forced replacement brings in a mon
    with a shorter move list, or with a different move out of PP.  The planned
    cursor is therefore clamped to the live menu's own extent and only honoured
    while the live combatant can actually select that slot; otherwise the first
    usable slot is steered to instead of confirming a move the ROM would refuse
    and then waiting forever for an A it never consumes.
    """
    limit = maximum - 1 if maximum >= 1 else None
    cursor = None
    if target_slot is not None:
        cursor = target_slot + 1
        if limit is not None:
            cursor = min(cursor, limit)
        if cursor < 1 or not _slot_usable(session, cursor):
            cursor = None
    if cursor is None:
        cursor = _first_usable_move_slot(session)
    if cursor is None:
        return None
    if limit is not None:
        cursor = min(cursor, limit)
    return cursor if cursor >= 1 else None


def _record_confirmed_slot(plan, slots: list, index: int, cursor: int, session) -> None:
    """Record the move slot the pump actually confirmed for ``index``.

    ``slots`` drives the rest of this settlement and ``plan`` is the same list
    object the caller writes into the manifest row as the replayable turn, so
    both are corrected together: a plan naming a cursor the live menu could not
    offer would make the admitted row describe a turn the capture never drove.
    The entry is only rewritten when the replacement move is known, so the
    row never carries a slot paired with another slot's move.
    """
    slot = cursor - 1
    if slots[index] != slot:
        slots[index] = slot
    if not isinstance(plan, list) or index >= len(plan):
        return
    entry = plan[index]
    if not isinstance(entry, dict) or entry.get("slot") == slot:
        return
    moves = _active_moves(session)
    if not 0 <= slot < len(moves):
        return
    try:
        move_id = moves[slot][0]
    except (TypeError, ValueError):
        return
    if not move_id:
        return
    entry["slot"] = slot
    entry["move"] = int(move_id)
    entry["policy"] = "pump-fallback"


# ---------------------------------------------------------------------------
# Multi-turn driver
# ---------------------------------------------------------------------------


def _counters(counters: dict, name: str, index: int) -> int:
    return int(counters[name][index])


def _faint_deltas(counters: dict, index: int, before: dict) -> tuple[int, int]:
    player = _counters(counters, "HandlePlayerMonFainted", index) - before["player"][index]
    enemy = _counters(counters, "HandleEnemyMonFainted", index) - before["enemy"][index]
    return player, enemy


def _committed_move(counters: dict, index: int, baseline: dict) -> bool:
    """True once ``index``'s ROM has moved past its own command menu this turn.

    ``MainInBattleLoop.selectEnemyMove`` (``engine/battle/core.asm:347``) is
    reached once per turn, after the side has answered its command menu -- or
    straight after the skip for a charging, thrashing, recharging or trapped
    side, because that skip jumps to the same label.  An increment is therefore
    the ROM's own witness that this side has committed to the turn's move and
    can no longer be sent backwards to a command menu, which is exactly the
    fact a peer needs before it is safe to answer its own command menu.

    ``MainInBattleLoop`` itself is *not* enough: it is entered at the top of the
    function, ~20 frames before ``DisplayBattleMenu`` makes a side's command
    menu live, so gating on it (as an earlier revision did) opened the gate
    while the peer's menu was still two frames from existing and let a
    just-replaced side run ahead of its peer.  Measured on the blue faint pair:
    the peer's own command menu appeared at t=200 with no input at all, while
    the turn-loop gate opened at t=180 and pressed the other side 20 frames too
    early, wedging the pair (``SVBK == 2``, unreadable party, junk HP).
    """
    return (
        _counters(counters, "MainInBattleLoop.selectEnemyMove", index)
        > baseline["select_enemy"][index]
    )


def _baseline(counters: dict) -> dict:
    return {
        "main": [int(value) for value in counters["MainInBattleLoop"]],
        "battle_menu": [int(value) for value in counters["DisplayBattleMenu"]],
        "move_menu": [int(value) for value in counters["MoveSelectionMenu"]],
        "select_enemy": [int(value) for value in counters["MainInBattleLoop.selectEnemyMove"]],
        "exchange": [int(value) for value in counters["LinkBattleExchangeData"]],
        "execute": [
            int(value)
            for value in (
                counters["ExecutePlayerMove"][0] + counters["ExecuteEnemyMove"][0],
                counters["ExecutePlayerMove"][1] + counters["ExecuteEnemyMove"][1],
            )
        ],
        "player": [int(value) for value in counters["HandlePlayerMonFainted"]],
        "enemy": [int(value) for value in counters["HandleEnemyMonFainted"]],
        "end": [int(value) for value in counters["EndOfBattle"]],
    }


def _choose_supported_or_any(session, moves):
    from tests._battle_turn_evidence import choose_supported_battle_move

    try:
        slot, move_id = choose_supported_battle_move(session, moves)
        return slot, move_id, "supported"
    except ValueError:
        for slot, (move_id, pp) in enumerate(moves):
            if move_id and (pp & 0x3F) > 0:
                return slot, move_id, "fallback"
        raise RuntimeError("active battle mon has no move with PP remaining")


def _menu_awaiting_a(session) -> str | None:
    """Classify the ROM menu currently taking A input for ``session``.

    Returns ``"command"``, ``"move"``, ``"party"`` or ``None``.  Only these
    three geometries are named because they are exactly the ones the accepted
    consumer-side driver (``_boundary_button``) recognises: the pair recorded
    here is replayed by that driver, so an input the replay would never issue
    must never be needed.  Anything else returns ``None`` and is never pressed,
    so no button is injected on geometry that no live menu owns.

    The cursor and watched-key bytes are the ROM's own, but they are leftovers
    once their menu closes, so they cannot tell a live menu from a stale one on
    their own.  The party branch is therefore gated on the ROM's own
    party-menu liveness witness (``_party_menu_ready``); without it a move menu
    whose ``wMaxMenuItem`` happens to equal ``wPartyCount - 1`` was named a
    party menu and driven with the wrong buttons.  The move branch is gated on
    the move menu's own ``MOVE_MENU_WATCHED_KEYS`` mask for the same reason: a
    closed party menu leaves move-menu geometry behind, and only the mask says
    which of the two menus is live.  The command branch is gated on
    ``COMMAND_MENU_WATCHED_KEYS`` for exactly the same reason: the battle party
    menu of a two-mon party leaves ``current=0..1, max=1`` behind, which is the
    command menu's geometry, and only the mask separates the two.
    """
    fields = _menu_fields(session)
    if fields is None:
        return None
    current, maximum, watched_keys = fields
    if not watched_keys & MENU_WATCHED_A:
        return None
    if _party_menu_ready(session):
        return "party"
    if (
        0 <= current <= 1
        and maximum == COMMAND_MENU_MAX_ITEM
        and watched_keys in COMMAND_MENU_WATCHED_KEYS
    ):
        return "command"
    if watched_keys == MOVE_MENU_WATCHED_KEYS and 1 <= current < maximum <= MOVE_MENU_MAX_ITEM:
        return "move"
    return None


def _pump_menu_input(
    session,
    *,
    next_press: int,
    spent: int,
    target_slot: int | None,
    started: bool,
    command_fresh: bool,
):
    """Issue at most one input toward the ROM menu that is waiting for it.

    Mirrors the consumer-side ``_boundary_button`` policy: the command menu is
    answered with A on FIGHT (never ITEM), the move menu is steered to
    ``target_slot + 1`` and then confirmed, the replacement party menu is
    steered onto a living slot, and no other geometry is touched at all.  The
    function only ever *adds* the single press a live menu is already waiting
    for, so it can never drive the battle past a boundary the caller watches.

    A move menu is only driven once this side's own command menu was answered
    by this same pump (``started``): ``wCurrentMenuItem``/``wMaxMenuItem`` keep
    their last values after a menu closes, so the move geometry alone does not
    prove a menu is live, and a stray queued A would be consumed by the *next*
    command menu and silently consume a boundary.  The caller's planned slot is
    only ever a steering *target*; it never opens the move branch.

    The command menu is answered only while ``command_fresh`` is true, which
    the caller sets once *both* sides have re-entered their own turn loop this
    turn.  A command menu can open for one side before the *peer* restarts its
    turn, and answering it at that point sends that side into its move menu
    while the peer is still finishing the previous turn's loss text; the peer
    then walks away from the menu the next exchange needs.  Observed on the
    blue faint pair: answering b's command menu at t=100 -- after b's own turn
    had restarted but ~100 frames before a restarted at t=200 -- left b parked
    in ``(1, 5, 195)`` and settled as a timeout, while the same pair driven
    with no input at all reached a real both-command boundary at t=200 with
    both sides healthy.

    Returns ``(deadline, began_turn, slot)``.  ``began_turn`` is true when the
    press opened a turn's move selection (A on FIGHT); ``slot`` names the move
    slot this press just confirmed, when it confirmed one.
    """
    if spent < next_press:
        return next_press, False, None
    kind = _menu_awaiting_a(session)
    if kind is None:
        return next_press, False, None
    if kind == "party":
        healthy = [slot for slot, value in enumerate(_party_hp_or_empty(session)) if value > 0]
        if not healthy:
            return next_press, False, None
        target = healthy[0]
        current = _byte(session, "wCurrentMenuItem")
        if current == target:
            session.press("a", duration=4)
            return spent + 12, False, None
        session.press("down" if current < target else "up", duration=4)
        return spent + 8, False, None
    if kind == "move":
        if not started:
            return next_press, False, None
        fields = _menu_fields(session)
        maximum = fields[1] if fields is not None else MOVE_MENU_MAX_ITEM
        want = _planned_move_cursor(session, target_slot, maximum)
        if want is None:
            return next_press, False, None
        current = _byte(session, "wCurrentMenuItem")
        if current == want:
            session.press("a", duration=4)
            return spent + 12, False, want - 1
        session.press("down" if current < want else "up", duration=4)
        return spent + 8, False, None
    # command menu: FIGHT is entry 0, ITEM is entry 1 and must never be chosen.
    if not command_fresh:
        return next_press, False, None
    current = _byte(session, "wCurrentMenuItem")
    session.press("a" if current == 0 else "up", duration=4)
    return spent + 12, current == 0, None


def _settle_turn(
    a,
    b,
    link,
    counters: dict,
    *,
    step_frames: int,
    frame_budget: int,
    chunk_cycles: int,
    require_counter: bool = True,
    baseline: dict | None = None,
    plan: list | None = None,
    keep_alive: bool = False,
):
    """Advance until the turn ends in a boundary, faint, or terminal state.

    ``baseline`` may be the counter snapshot taken when the turn started (the
    command menu); when omitted, baselines are read at entry, so only a
    *future* counter increment counts.

    The ROM resolves the turn, but the only input injected here is the single
    press each side's *own* live menu is already waiting for.  That is
    required rather than optional: a pair left with pure zero input deadlocks
    the moment one side is parked on a menu the other never reaches (the
    ``timeout frames=...`` observed on red and blue), because the link
    exchange cannot complete until both owners answer their own menu.  A pair
    that is *both* at a live command menu is a boundary and not an input
    problem, so it is never pumped: the caller snapshots it.

    ``keep_alive`` additionally lets the pump complete a turn the ROM skipped
    ``DisplayBattleMenu`` for (a charging, thrashing, recharging or trapped
    side owns no menu), so its peer's selection is still driven.  Starting such
    a turn is not itself a command boundary -- the ROM skips that side's menu
    outright, so no reloadable both-command pair exists for it.  The pump
    therefore bridges such a turn -- the caller's own plan still names the
    boundary it snapshotted, and the recorded plan is never invented for a
    turn no boundary exists for.
    """
    if baseline is None:
        baseline = _baseline(counters)
    # ``plan`` only supplies move cursors for the adaptive pump; resolution
    # itself is still driven by the ROM once both moves are confirmed.
    slots: list[int | None] = [None, None]
    if isinstance(plan, list):
        for index, entry in enumerate(plan[:2]):
            if isinstance(entry, dict) and type(entry.get("slot")) is int:
                slots[index] = entry["slot"]
    spent = 0
    next_press = [0, 0]
    started = [False, False]
    while spent < frame_budget:
        ended = _counters(counters, "EndOfBattle", 0) > baseline["end"][0] or (
            _counters(counters, "EndOfBattle", 1) > baseline["end"][1]
        )
        if ended and _battle_ended(a) and _battle_ended(b):
            return {"status": "terminal", "spent": spent}

        faint_sides = []
        for index, session in enumerate((a, b)):
            player, enemy = _faint_deltas(counters, index, baseline)
            if player > 0:
                faint_sides.append({"index": index, "kind": "player"})
            elif enemy > 0:
                faint_sides.append({"index": index, "kind": "enemy"})
        if faint_sides:
            return {"status": "faint", "spent": spent, "sides": faint_sides}

        both_command = _battle_menu_input_ready(a) and _battle_menu_input_ready(b)
        # A fresh command-menu boundary is detected from the ROM's own menu
        # geometry rather than from the DisplayBattleMenu counter: the ROM
        # *skips* DisplayBattleMenu for a side that is charging (Solar Beam),
        # thrashing, recharging or trapped, so requiring that counter to
        # increment can never be satisfied for such a turn.
        if require_counter:
            resumed = (
                _counters(counters, "MainInBattleLoop", 0) > baseline["main"][0]
                and _counters(counters, "MainInBattleLoop", 1) > baseline["main"][1]
                and both_command
            )
        else:
            resumed = both_command
        if resumed:
            return {"status": "boundary", "spent": spent}

        # Never starve a side that the ROM has parked on a live input menu.
        # This is the fix for the red/blue deadlock: after a skipped menu one
        # side waits for a single A and, with pure zero input, the pair blocks
        # forever.  Only the two sides' *own* command/move/party menus are
        # pumped, and only while the turn has not resolved, so the
        # boundary/terminal/faint checks above always win.  A pair that is
        # both at a command menu is a boundary and is left untouched for the
        # caller to snapshot.
        if (keep_alive or not require_counter) and not both_command:
            # A side's command menu is answered only once its *peer* has
            # committed to this turn's move.  The turn-loop entry alone is not
            # enough: a side re-enters ``MainInBattleLoop`` ~20 frames before
            # its own command menu is live, so a gate on that let a side whose
            # peer was still walking to its menu be pressed a frame early and
            # wedged (blue turn 9's in-turn replacement).  Requiring the peer's
            # ``MainInBattleLoop.selectEnemyMove`` increment -- reached after
            # the peer answers, or skips, its own command menu -- proves the
            # peer can no longer be sent backwards to a command menu, which is
            # the only state in which answering this side is safe.
            for index, session in enumerate((a, b)):
                peer = 1 - index
                deadline, began, slot = _pump_menu_input(
                    session,
                    next_press=next_press[index],
                    spent=spent,
                    target_slot=slots[index],
                    started=started[index],
                    command_fresh=_committed_move(counters, peer, baseline),
                )
                next_press[index] = deadline
                if began:
                    started[index] = True
                # A confirmed selection hands the turn back to the ROM, so the
                # pump stops offering buttons for this side's now-closed menu.
                if slot is not None:
                    _record_confirmed_slot(plan, slots, index, slot + 1, session)
                    started[index] = False

        chunk = min(step_frames, frame_budget - spent)
        link.step_interleaved(chunk, chunk_cycles=chunk_cycles)
        spent += chunk
    return {"status": "timeout", "spent": spent}


def _wait_for_fainted_side(a, b, link, *, step_frames, frame_budget, chunk_cycles):
    """Step until exactly one side's own active combatant has 0 HP.

    ``wBattleMonHP`` is each session's own combatant, so the fainting side is
    unambiguous even though ``wInHandlePlayerMonFainted`` persists for the
    rest of the battle and a closed party menu leaves stale cursor fields.
    Returns ``(index_or_None, spent)``; ``None`` means both battles ended.
    """
    spent = 0
    while spent < frame_budget:
        fainted = [
            index
            for index, session in enumerate((a, b))
            if _battle_active(session) and _word(session, "wBattleMonHP") == 0
        ]
        if len(fainted) == 1:
            return fainted[0], spent
        if _battle_ended(a) and _battle_ended(b):
            return None, spent
        chunk = min(step_frames, frame_budget - spent)
        link.step_interleaved(chunk, chunk_cycles=chunk_cycles)
        spent += chunk
    return None, spent


def _wait_for_side_menu(session, other, link, *, step_frames, frame_budget, chunk_cycles):
    """Wait until ``session``'s ROM-owned replacement party menu is input-ready.

    The party-menu type byte is rewritten by the ROM for every replacement, so
    an input-ready reading after later faints still names the live menu, and the
    ROM's own ``wPartyMenuAnimMonEnabled`` witness proves the handler is
    actually inside its input loop at the captured instant rather than merely
    leaving the byte pattern behind.
    ``wPartyCount`` follows ``wIsInBattle`` in WRAM and ``EndOfBattle`` zeroes
    both together, so the "both battles ended" exit needs *sustained* evidence
    instead of one transient read.
    """
    spent = 0
    ended_frames = 0
    while spent < frame_budget:
        if _party_menu_ready(session):
            return spent
        chunk = min(step_frames, frame_budget - spent)
        if _battle_ended(session) and _battle_ended(other):
            ended_frames += chunk
            if ended_frames >= REPLACEMENT_END_GRACE_FRAMES:
                return None
        else:
            ended_frames = 0
        link.step_interleaved(chunk, chunk_cycles=chunk_cycles)
        spent += chunk
    return None


def _wait_for_both_ended(a, b, link, *, step_frames, frame_budget, chunk_cycles) -> bool:
    """Step input-free until both sessions report ``wIsInBattle == 0``."""
    spent = 0
    while spent < frame_budget:
        if _battle_ended(a) and _battle_ended(b):
            return True
        chunk = min(step_frames, frame_budget - spent)
        link.step_interleaved(chunk, chunk_cycles=chunk_cycles)
        spent += chunk
    return _battle_ended(a) and _battle_ended(b)


def _choose_healthy_replacement(session, link, *, step_frames, frame_budget, chunk_cycles):
    """Drive the replacement cursor to a living mon and confirm it with A.

    Success is measured only by the ROM-owned ``wBattleMonHP`` becoming
    non-zero, so a transient redraw or stale party-menu cursor bytes can never
    be mistaken for a completed replacement.  The routine keeps issuing
    ordinary directional/A input until a living combatant is out, and treats a
    momentary ``wIsInBattle == 0`` or empty party reading as the teardown
    window it is: the battle is only abandoned after
    ``REPLACEMENT_END_GRACE_FRAMES`` of sustained "ended" evidence, because
    ``EndOfBattle`` zeroes a WRAM block starting at ``wIsInBattle`` (and with
    it ``wPartyCount``) while the ROM is still redrawing.

    Input is only injected while ``_party_menu_ready`` proves a live party menu
    is taking it, so nothing is pressed once the ROM has consumed the
    confirmation and moved on -- a stray direction or A there would be picked
    up by the *next* live menu instead of the one the cursor bytes still
    describe.  Waiting costs nothing: the success test is the ROM's own, so the
    routine returns as soon as the replacement is out whether or not that
    instant arrived with a button.

    ``wBattleMonHP`` lives in WRAM bank 1, and the success reading resolves
    that bank directly rather than through the ``SVBK`` window, so it stays
    authoritative while the ROM has another bank mapped (see ``_wram_byte``).
    The earlier "fail closed on ``SVBK != 0/1``" rule read the wrong bank and
    made a live party menu invisible, which starved the pair; the read
    primitive is now bank-correct instead.
    """
    spent = 0
    next_press = 0
    ended_frames = 0
    while spent < frame_budget:
        if _battle_ended(session):
            ended_frames += step_frames
            if ended_frames >= REPLACEMENT_END_GRACE_FRAMES:
                return False
        else:
            ended_frames = 0
        if _banked_readable(session) and _word(session, "wBattleMonHP") > 0:
            return True
        healthy = [slot for slot, value in enumerate(_party_hp_or_empty(session)) if value > 0]
        if healthy and _party_menu_ready(session):
            target = healthy[0]
            current = _byte(session, "wCurrentMenuItem")
            if current == target:
                if spent >= next_press:
                    session.press("a", duration=4)
                    next_press = spent + 12
            elif current < target:
                session.press("down", duration=4)
            else:
                session.press("up", duration=4)
        chunk = min(step_frames, frame_budget - spent)
        link.step_interleaved(chunk, chunk_cycles=chunk_cycles)
        spent += chunk
    return _banked_readable(session) and _word(session, "wBattleMonHP") > 0


def _drive_next_turn(
    a,
    b,
    link,
    counters: dict,
    *,
    step_frames: int,
    frame_budget: int,
    chunk_cycles: int,
    on_boundary=None,
):
    """Drive FIGHT -> move selection -> resolution from the command menu.

    ``on_boundary`` is called exactly once per call, immediately after both
    ROM command menus are live and before any input is injected, so a caller
    can snapshot the boundary that the next driven turn starts from.  Its
    return value is ignored: the boundary it is offered is always a live
    both-command pair, because step 1 waits for exactly that geometry.
    """
    from tests.test_pyboy_link_session_roms import _read_active_battle_moves

    select_enemy = counters["MainInBattleLoop.selectEnemyMove"]
    spent = 0

    def step(frames: int) -> int:
        nonlocal spent
        chunk = min(frames, frame_budget - spent)
        if chunk <= 0:
            return 0
        link.step_interleaved(chunk, chunk_cycles=chunk_cycles)
        spent += chunk
        return chunk

    # 1. Wait for both command menus to be live and awaiting input.
    while spent < frame_budget and not (
        _battle_menu_input_ready(a) and _battle_menu_input_ready(b)
    ):
        if _battle_ended(a) or _battle_ended(b):
            return {"status": "terminal", "spent": spent, "turn": None}
        step(step_frames)
    if spent >= frame_budget:
        return {"status": "timeout", "spent": spent, "turn": None}

    # The whole turn is measured from the live command menu, so a faint that
    # lands between the execute hook and this function's return is still seen.
    turn_baseline = _baseline(counters)
    if on_boundary is not None:
        on_boundary()

    # 2. Select FIGHT on each side; the ROM opens its own move menu.
    next_press = [0, 0]
    while spent < frame_budget:
        if _move_menu_input_ready(a) and _move_menu_input_ready(b):
            break
        for index, session in enumerate((a, b)):
            if (
                not _move_menu_input_ready(session)
                and _battle_menu_input_ready(session)
                and spent >= next_press[index]
            ):
                # Steer to FIGHT before confirming.  The command menu's cursor
                # is not guaranteed to be on entry 0 at a boundary the caller
                # snapshotted: a boundary only requires the ROM's command
                # geometry to be live, and the cursor keeps its last value
                # across menus, so a pair admitted with the cursor on entry 1
                # would otherwise be confirmed onto ITEM.  That opens the item
                # menu instead of move selection, no move menu ever appears and
                # the drive times out with both links parked (observed on red's
                # turn 8).  This mirrors the consumer-side ``_boundary_button``,
                # which never confirms ITEM either.
                if _byte(session, "wCurrentMenuItem") == 0:
                    session.press("a", duration=4)
                else:
                    session.press("up", duration=4)
                next_press[index] = spent + 8
        step(min(step_frames, 2))
    if spent >= frame_budget or not (_move_menu_input_ready(a) and _move_menu_input_ready(b)):
        return {"status": "timeout", "spent": spent, "turn": None}

    # 3. Choose a move with PP on each side and move the ROM cursor to it.
    chosen = []
    for index, session in enumerate((a, b)):
        moves = _read_active_battle_moves(session)
        slot, move_id, policy = _choose_supported_or_any(session, moves)
        chosen.append({"slot": slot, "move": move_id, "policy": policy})
        attempts = 0
        while spent < frame_budget and _move_menu_input_ready(session):
            current = _byte(session, "wCurrentMenuItem")
            if current == slot + 1:
                break
            session.press("down" if current < slot + 1 else "up", duration=2)
            step(min(step_frames, 4))
            attempts += 1
            if attempts > 30:
                return {"status": "timeout", "spent": spent, "turn": None}
    if spent >= frame_budget:
        return {"status": "timeout", "spent": spent, "turn": None}

    # 4. Confirm the move; stop per side once selectEnemyMove proves the A
    #    edge was consumed by the ROM's menu handler.
    select_before = [int(select_enemy[0]), int(select_enemy[1])]
    next_move = [spent, spent]
    while spent < frame_budget:
        if int(select_enemy[0]) > select_before[0] and int(select_enemy[1]) > select_before[1]:
            break
        for index, session in enumerate((a, b)):
            if (
                int(select_enemy[index]) == select_before[index]
                and spent >= next_move[index]
                and _move_menu_input_ready(session)
            ):
                session.press("a", duration=4)
                next_move[index] = spent + 8
        step(min(step_frames, 2))
    if spent >= frame_budget:
        return {"status": "timeout", "spent": spent, "turn": None}

    result = _settle_turn(
        a,
        b,
        link,
        counters,
        step_frames=step_frames,
        frame_budget=frame_budget - spent,
        chunk_cycles=chunk_cycles,
        baseline=turn_baseline,
        plan=chosen,
        keep_alive=True,
    )
    # The turn this call drove starts from the boundary ``on_boundary`` just
    # snapshotted, so the plan selected here is the one written to the row.
    result.setdefault("turn", chosen)
    result["spent"] += spent
    return result


# ---------------------------------------------------------------------------
# Fixture capture and reload validation
# ---------------------------------------------------------------------------


