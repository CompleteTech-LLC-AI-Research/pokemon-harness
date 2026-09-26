"""Battle-boundary drive helpers for the real-ROM battle module.

Split from ``tests/test_mcp_battle_phase_rom.py`` (#122) with no behavior
change: the admitted-boundary observers and the turn/replacement drives moved
here verbatim.  Shared constants and menu primitives come from
``tests._mcp_battle_phase_rom_support``.
"""

from __future__ import annotations

import asyncio
import base64
import json

from tests._mcp_battle_phase_rom_support import (
    BATTLE_ENTRY_BUDGET,
    BATTLE_KINDS,
    BATTLE_TRANSIENT_FIELDS,
    BOUNDARY_BUDGET,
    BOUNDARY_DRIVE_BUDGET,
    BOUNDARY_INPUT_SPACING,
    BOUNDARY_STEP,
    CANCEL_REASON,
    COLOSSEUM_MAP_ID,
    COLOSSEUM_WARP_BUDGET,
    COMMAND_MENU_MAX_ITEM,
    COMMAND_MENU_WATCHED_KEYS,
    LINK_MENU_BUDGET,
    LINK_MENU_BURST_ATTEMPTS,
    LINK_MENU_CURSOR_BUDGET,
    LINK_MENU_QUIET_FRAMES,
    MENU_WATCHED_A,
    MOVE_MENU_MAX_ITEM,
    MOVE_MENU_WATCHED_KEYS,
    OBSERVATIONAL_MENU_HOOKS,
    OUTSTANDING_CANCEL_DELAY,
    PARTY_MENU_WATCHED_KEYS,
    PHASE_EVIDENCE_SYMBOLS,
    PUBLIC_TOOLS,
    RECEPTIONIST_WALK_FRAMES,
    SETTLEMENT_BUDGET,
    SETTLEMENT_INPUT_SPACING,
    SETTLEMENT_STEP,
    STEP_CHUNK,
    TERMINAL_RETURN_PHASE,
    _active_hp,
    _active_mon,
    _active_pp,
    _at_command_boundary,
    _battle,
    _link_menu_ready,
    _link_step,
    _log,
    _mash_until,
    _menu,
    _menu_awaiting_input,
    _peer_press,
    _pp_decrements,
    _press,
    _press_both,
    _request,
    _states,
    _without_epoch,
)
from tests.test_mcp_timed_stdio import CALL_BOUND

# --- admitted battle-boundary scenarios ------------------------------------
#
# The boundary helpers below drive a *reloaded* pair.  A load resets the
# session's observational menu state to unknown, so the drive cannot use the
# hook-derived ``menu_open`` flag the way the live-entry scenarios do; it uses
# the ROM's own menu geometry (``wCurrentMenuItem``/``wMaxMenuItem``/
# ``wMenuWatchedKeys``) read through the public ``pokered://game-state``
# resource, which is the same ROM-owned evidence the capture producer used to
# snapshot the boundary.


def _menu_fields(state):
    """Return ``(current_item, max_item, watched_keys)`` from the public payload."""
    menu = _menu(state)
    current = menu.get("current_item")
    maximum = menu.get("max_item")
    watched = menu.get("watched_keys")
    if type(current) is int and type(maximum) is int and type(watched) is int:
        return current, maximum, watched
    return None


def _command_menu_ready(state):
    """True when the ROM's battle command menu geometry is live."""
    fields = _menu_fields(state)
    return (
        fields is not None
        and 0 <= fields[0] <= 1
        and fields[1] == COMMAND_MENU_MAX_ITEM
        and fields[2] in COMMAND_MENU_WATCHED_KEYS
    )


def _replacement_slot(state):
    """Index of the first living party slot while this owner is choosing one.

    ``None`` means the ROM is not asking this owner to replace a combatant:
    either its own combatant still has HP, or the battle party has no living
    member left to send out.
    """
    active = _active_mon(state)
    if active is None or active["hp"] != 0:
        return None
    living = [slot for slot, mon in enumerate(state["party"]["mons"]) if mon["hp"] > 0]
    return living[0] if living else None


def _replacement_button(state):
    """Return the next button for a live battle party menu, or ``None``.

    ``wMenuWatchedKeys`` is the key mask the ROM itself is watching, and it is
    what tells the two menus apart after a reload: ``3`` (A|B) is the battle
    party menu and ``195`` (UP|DOWN|A|B) is the move menu.  A party menu needs a
    *party* target rather than the move slot :func:`_boundary_button` steers to:
    confirming the move slot selects a fainted combatant, which the ROM refuses,
    and the side then never leaves the menu.  The living slot the capture
    producer used is derivable from public data alone (``party.mons[].hp``), so
    the drive stays on the public surface.
    """
    fields = _menu_fields(state)
    if fields is None or fields[2] != PARTY_MENU_WATCHED_KEYS:
        return None
    target = _replacement_slot(state)
    if target is None:
        return None
    current = fields[0]
    if current == target:
        return "a"
    return "down" if current < target else "up"


def _boundary_button(state, target_slot):
    """Return the next public button to inject for one owner, or ``None``.

    The cursor geometry is the ROM's own: FIGHT is entry 0 of the command menu
    (entry 1 is ITEM) and the move menu cursor is one past the move slot the
    ROM confirms.  The move branch additionally requires the move menu's own
    ``MOVE_MENU_WATCHED_KEYS`` mask, because the geometry above also matches the
    leftovers of a battle party menu that has already taken its input; without
    that witness the drive would press A on a closed menu and the stray input
    would be consumed by the next command menu.  The command branch requires
    the command menu's own mask (``COMMAND_MENU_WATCHED_KEYS``) for the same
    reason: a closed battle party menu of a two-mon party leaves command-menu
    geometry behind, and only the mask separates the two.  ``None`` means no
    menu is currently taking A input, so nothing is injected and the ROM keeps
    running its own animation or text.
    """
    fields = _menu_fields(state)
    if fields is None or not fields[2] & MENU_WATCHED_A:
        return None
    current, maximum, watched = fields
    if (
        0 <= current <= 1
        and maximum == COMMAND_MENU_MAX_ITEM
        and watched in COMMAND_MENU_WATCHED_KEYS
    ):
        # Never confirm ITEM: the admitted pair is replayed as a FIGHT turn.
        return "a" if current == 0 else "up"
    if watched == MOVE_MENU_WATCHED_KEYS and 1 <= current < maximum <= MOVE_MENU_MAX_ITEM:
        target = target_slot + 1
        if current == target:
            return "a"
        return "down" if current < target else "up"
    return None


async def _drive_effect_turn(client, slots, *, budget=SETTLEMENT_BUDGET):
    """Drive both owners through one shared turn that selects ``slots``.

    The buttons and every decision come from the public ``press`` /
    ``link_peer_press`` / ``link_step`` tools and the public
    ``pokered://game-state`` resource, exactly like the boundary drive.  The
    loop stops when each owner's own read shows the selected slot's PP
    decremented *and* a live command menu, which is the ROM-owned return
    boundary the resolution observation must not contradict.
    """
    before = await _states(client)
    pp_before = [_active_pp(state) for state in before]
    hp_before = [_active_hp(state) for state in before]
    assert len(slots) == len(before), (slots, before)
    assert all(value is not None for value in pp_before), before
    assert all(value is not None for value in hp_before), before

    consumed = [False] * len(before)
    phases_seen = set()
    next_input = [0] * len(before)
    boundary = None
    frames = 0
    while frames < budget:
        states = await _states(client)
        for index, state in enumerate(states):
            battle = _battle(state)
            if battle["phase_valid"] is True:
                phases_seen.add(battle["phase"])
            if consumed[index]:
                continue
            after = _active_pp(state)
            if after is not None and after[slots[index]] < pp_before[index][slots[index]]:
                consumed[index] = True
        if all(consumed) and all(
            _command_menu_ready(state) and _battle(state)["menu_open"] is True for state in states
        ):
            boundary = states
            break
        for index, state in enumerate(states):
            if consumed[index] or frames < next_input[index]:
                continue
            button = _boundary_button(state, slots[index])
            if button is None:
                continue
            if index == 0:
                await _press(client, button)
            else:
                await _peer_press(client, button)
            next_input[index] = frames + BOUNDARY_INPUT_SPACING
        await _link_step(client, BOUNDARY_STEP)
        frames += BOUNDARY_STEP
    if boundary is None:
        states = await _states(client)
        raise AssertionError(
            "the selected turn never returned both owners to a command menu "
            f"within {budget} paired frames: " + json.dumps([state["battle"] for state in states])
        )
    return {
        "frames": frames,
        "pp_before": pp_before,
        "hp_before": hp_before,
        "phases_seen": tuple(sorted(value for value in phases_seen if value is not None)),
        "boundary": boundary,
    }


async def _drive_through_live_replacement(client, slots, *, budget=BOUNDARY_DRIVE_BUDGET):
    """Drive the admitted pair through one live knockout and its replacement.

    The drive stops as soon as every owner that opened the ROM's battle party
    menu has answered it and is back in the live battle with a healthy
    combatant, so the regression covers the nonterminal knockout path (the
    fainted combatant is replaced and the battle continues) instead of the
    whole terminal drive.  ``replacements`` holds one row per read of a live
    battle party menu; ``answered[index]`` is the first read after that owner
    answered its menu, and stays ``None`` for an owner that never opened one.
    """
    replacements = []
    answered = [None] * len(slots)
    opened = [False] * len(slots)
    next_input = [0] * len(slots)
    frames = 0
    while frames < budget:
        states = await _states(client)
        for index, state in enumerate(states):
            battle = _battle(state)
            live = battle["raw_is_in_battle"] in BATTLE_KINDS
            if live and _replacement_button(state) is not None:
                opened[index] = True
                replacements.append(
                    {
                        "side": index,
                        "frames": frames,
                        "tick": state["epoch"]["tick"],
                        "phase": battle["phase"],
                        "phase_valid": battle["phase_valid"],
                        "resolution_open": battle["resolution_open"],
                        "raw_is_in_battle": battle["raw_is_in_battle"],
                        "active_hp": _active_hp(state),
                        "party_hp": [mon["hp"] for mon in state["party"]["mons"]],
                        "menu_evidence": battle["menu_evidence"],
                        "phase_evidence": battle["phase_evidence"],
                        "resolution_evidence": battle["resolution_evidence"],
                    }
                )
            elif (
                opened[index] and answered[index] is None and live and (_active_hp(state) or 0) > 0
            ):
                answered[index] = {
                    "side": index,
                    "frames": frames,
                    "tick": state["epoch"]["tick"],
                    "phase": battle["phase"],
                    "phase_valid": battle["phase_valid"],
                    "resolution_open": battle["resolution_open"],
                    "raw_is_in_battle": battle["raw_is_in_battle"],
                    "active_hp": _active_hp(state),
                    "party_hp": [mon["hp"] for mon in state["party"]["mons"]],
                    "phase_evidence": battle["phase_evidence"],
                }
        if any(opened) and all(
            answered[index] is not None for index, flag in enumerate(opened) if flag
        ):
            break
        for index, state in enumerate(states):
            if opened[index] and answered[index] is not None:
                continue
            if frames < next_input[index]:
                continue
            button = _replacement_button(state)
            if button is None:
                button = _boundary_button(state, slots[index])
            if button is None:
                continue
            if index == 0:
                await _press(client, button)
            else:
                await _peer_press(client, button)
            next_input[index] = frames + BOUNDARY_INPUT_SPACING
        await _link_step(client, BOUNDARY_STEP)
        frames += BOUNDARY_STEP
    return {
        "frames": frames,
        "opened": tuple(opened),
        "replacements": replacements,
        "answered": answered,
    }


async def _drive_boundary_turn(client, plan, *, budget=BOUNDARY_DRIVE_BUDGET):
    """Drive the reloaded pre-terminal pair until both owners leave the battle.

    Both owners are folded back into their own ROM command/move menus with the
    public ``press`` / ``link_peer_press`` / ``link_step`` tools only, and every
    decision is taken from a public ``pokered://game-state`` read.  An owner
    stops being driven as soon as its own read reports that ``wIsInBattle``
    left the live battle kinds, so that read stays the observed falling edge of
    the battle the session tracks; the other owner keeps being driven until its
    own read shows the same.

    A knockout that opens a forced replacement is folded in rather than
    abandoned: while the ROM is watching its party menu, the cursor is steered
    onto the first living slot (``_replacement_button``), which is the same
    target the capture producer drove the recorded turn with.  The drive
    therefore covers every ROM turn between the admitted boundary and the
    terminal return, bounded by ``budget``.
    """
    slots = [entry["slot"] for entry in plan]
    terminal = [None, None]
    injected = [[], []]
    frames = 0
    realign = [0, 0]
    while frames < budget and any(entry is None for entry in terminal):
        states = await _states(client)
        for index, state in enumerate(states):
            if terminal[index] is not None:
                continue
            if _battle(state)["raw_is_in_battle"] not in BATTLE_KINDS:
                terminal[index] = {
                    "frames": frames,
                    "state": state,
                    "buttons": tuple(injected[index]),
                }
        for index, state in enumerate(states):
            if terminal[index] is not None or frames < realign[index]:
                continue
            button = _replacement_button(state)
            if button is None:
                button = _boundary_button(state, slots[index])
            if button is None:
                continue
            if index == 0:
                await _press(client, button)
            else:
                await _peer_press(client, button)
            injected[index].append(button)
            realign[index] = frames + BOUNDARY_INPUT_SPACING
        if all(entry is not None for entry in terminal):
            break
        await _link_step(client, BOUNDARY_STEP)
        frames += BOUNDARY_STEP
    if any(entry is None for entry in terminal):
        states = await _states(client)
        raise AssertionError(
            "the admitted pre-terminal pair never reached the terminal return "
            f"within {budget} paired frames: " + json.dumps([state["battle"] for state in states])
        )
    return {"terminal": terminal, "frames": frames, "injected": injected}


def _assert_terminal_return(state, *, label):
    """Assert the documented terminal-return derivation for one owner."""
    battle = _battle(state)
    assert battle["raw_is_in_battle"] == 0, (label, battle)
    assert battle["kind"] == 0, (label, battle)
    assert battle["phase"] == TERMINAL_RETURN_PHASE, (label, battle)
    assert battle["phase_valid"] is True, (label, battle)
    assert {"wIsInBattle", "wBattleResult"} <= set(battle["phase_evidence"]), (label, battle)
    raw = battle["raw_battle_result"]
    assert type(raw) is int, (label, battle)
    expected = raw if raw in (1, 2) else None
    assert battle["terminal_result"] == expected, (label, battle)
    return battle


async def _settle_selected_move(client, *, before, budget=SETTLEMENT_BUDGET):
    """Drive the paired ROMs through one move selection -> resolution cycle.

    Both combatants must select a move before the link exchange can complete,
    so each side is advanced toward its own menu with at most one A press per
    ``SETTLEMENT_INPUT_SPACING`` frames.  The loop stops as soon as *both*
    active mons show a PP decrement (the ROM executes ``DecrementPP`` before
    damage, so this is a strict settlement signal) and then waits, input-free,
    for the primary to return to a command/next input boundary.

    Returns the pre/post PP tuples, the decremented slots, the selected move
    bytes, every valid phase and action-flag value observed during the turn,
    and both states captured at the final boundary.
    """
    labels = len(before)
    if labels != 2:
        raise ValueError("settlement expects the primary and peer states")
    pp_before = tuple(_active_pp(state) for state in before)
    decremented = [None] * labels
    selected = [None] * labels
    phases_seen = set()
    actions_seen = set()
    resolution_evidence = set()
    terminal_observations = []
    raw_values = set()
    spent = 0
    next_input = [-SETTLEMENT_INPUT_SPACING] * labels
    while spent < budget:
        states = await _states(client)
        for index, state in enumerate(states):
            battle = _battle(state)
            raw_values.add(battle.get("raw_is_in_battle"))
            if battle.get("phase_valid") is True:
                phases_seen.add(battle.get("phase"))
            if battle.get("phase") == TERMINAL_RETURN_PHASE and not terminal_observations:
                terminal_observations.append(
                    {
                        "side": index,
                        "raw_is_in_battle": battle.get("raw_is_in_battle"),
                        "raw_battle_result": battle.get("raw_battle_result"),
                        "terminal_result": battle.get("terminal_result"),
                        "tick": state["epoch"]["tick"],
                    }
                )
            actions_seen.add(battle.get("action_result_or_took_turn"))
            resolution_evidence.update(battle.get("resolution_evidence") or ())
            if decremented[index] is None:
                changed = _pp_decrements(pp_before[index], _active_pp(state))
                if changed:
                    decremented[index] = changed
                    selected[index] = battle.get("player_selected_move")
        if all(changed is not None for changed in decremented):
            break
        for index, state in enumerate(states):
            if decremented[index] is not None:
                continue
            if not _menu_awaiting_input(state):
                continue
            if spent < next_input[index]:
                continue
            if index == 0:
                await _press(client, "a", duration=4)
            else:
                await _peer_press(client, "a", duration=4)
            next_input[index] = spent + SETTLEMENT_INPUT_SPACING
        await _link_step(client, SETTLEMENT_STEP)
        spent += SETTLEMENT_STEP
    if not all(changed is not None for changed in decremented):
        raise AssertionError(
            "selected move never settled (PP decrement not observed): "
            f"before={pp_before} frames={spent}"
        )

    boundary = None
    boundary_frames = 0
    while boundary_frames < BOUNDARY_BUDGET:
        states = await _states(client)
        if _battle(states[0]).get("raw_is_in_battle") not in BATTLE_KINDS:
            boundary = states
            break
        if _at_command_boundary(states[0]):
            boundary = states
            break
        await _link_step(client, SETTLEMENT_STEP)
        boundary_frames += SETTLEMENT_STEP
    if boundary is None:
        states = await _states(client)
        raise AssertionError(
            "settled turn never returned to a command/next boundary: "
            f"{json.dumps([_battle(state) for state in states])}"
        )
    return {
        "frames": spent,
        "boundary_frames": boundary_frames,
        "pp_before": pp_before,
        "pp_after": tuple(_active_pp(state) for state in boundary),
        "decremented": tuple(decremented),
        "selected": tuple(selected),
        "phases_seen": tuple(sorted(value for value in phases_seen if value is not None)),
        "actions_seen": tuple(sorted(value for value in actions_seen if value is not None)),
        "resolution_evidence": tuple(sorted(resolution_evidence)),
        "raw_values": tuple(sorted(value for value in raw_values if value is not None)),
        "terminal_observations": terminal_observations,
        "boundary": boundary,
    }


async def _send_raw_request(client, method, params):
    """Send one JSON-RPC request and return its id without awaiting a reply."""
    client.sequence += 1
    request_id = client.sequence
    await client.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    return request_id


async def _read_response_for(client, request_id, *, bound):
    """Read stdout until the response for ``request_id`` arrives (or fail)."""
    async with asyncio.timeout(bound):
        while True:
            line = await client.process.stdout.readline()
            assert line, f"server EOF while awaiting response {request_id}"
            text = line.decode("utf-8", "replace").strip()
            if not text or not text.startswith("{"):
                # Ignore blank or non-protocol diagnostic lines; JSON-RPC is
                # line-delimited and only objects carry responses.
                continue
            response = json.loads(text)
            assert response.get("jsonrpc") == "2.0", response
            if "id" not in response:
                continue
            if response["id"] == request_id:
                return response


async def _read_responses_for(client, request_ids, *, bound):
    """Read stdout until every request id in ``request_ids`` has a reply.

    Requests are written before any reply is read, so the server handles them
    concurrently; replies are matched by id rather than arrival order.
    """
    pending = set(request_ids)
    responses = {}
    async with asyncio.timeout(bound):
        while pending:
            line = await client.process.stdout.readline()
            assert line, f"server EOF while awaiting responses {sorted(pending)}"
            text = line.decode("utf-8", "replace").strip()
            if not text or not text.startswith("{"):
                continue
            response = json.loads(text)
            assert response.get("jsonrpc") == "2.0", response
            if "id" not in response or response["id"] not in pending:
                continue
            pending.discard(response["id"])
            responses[response["id"]] = response
    return responses


async def _read_state_during_outstanding_step(client, *, frames, label):
    """Read ``pokered://game-state`` while a long ``link_step`` is outstanding.

    Both public requests are written before either reply is read.  The resource
    read serializes on the same session emulator lock the paired step holds, so
    it observes a coherent pre- or post-step snapshot.  The returned ``tick`` is
    asserted to be exactly one of those two boundary values, never a torn
    mid-step value, and the documented battle fields are validated.
    """
    before = await _request(client, "pokered://game-state")
    before_tick = before["epoch"]["tick"]
    step_id = await _send_raw_request(
        client, "tools/call", {"name": "link_step", "arguments": {"count": frames}}
    )
    read_id = await _send_raw_request(client, "resources/read", {"uri": "pokered://game-state"})
    responses = await _read_responses_for(client, (step_id, read_id), bound=CALL_BOUND)
    step_response = responses[step_id]
    read_response = responses[read_id]
    assert "error" not in step_response, step_response
    assert "error" not in read_response, read_response
    step_result = json.loads(step_response["result"]["content"][0]["text"])
    snapshot = json.loads(read_response["result"]["contents"][0]["text"])
    assert isinstance(step_result, dict), step_result
    assert isinstance(snapshot, dict), snapshot
    tick = snapshot["epoch"]["tick"]
    assert tick in (before_tick, before_tick + frames), (before_tick, tick, frames, snapshot)
    _assert_battle_observations(snapshot, label=label)
    _log(
        "concurrent_read",
        f"step_id={step_id} read_id={read_id} frames={frames} tick={before_tick}->{tick}",
    )
    return {
        "step_id": step_id,
        "read_id": read_id,
        "before_tick": before_tick,
        "tick": tick,
        "step_result": step_result,
        "snapshot": snapshot,
    }


async def _server_visible_cancel(client, *, frames, label):
    """Cancel an outstanding public ``link_step`` with ``notifications/cancelled``.

    Protocol messages used (exact JSON-RPC):

    * request: ``{"jsonrpc": "2.0", "id": <id>, "method": "tools/call",
      "params": {"name": "link_step", "arguments": {"count": frames}}}``
    * cancel: ``{"jsonrpc": "2.0", "method": "notifications/cancelled",
      "params": {"requestId": <id>, "reason": <reason>}}``

    The SDK receive loop marks the in-flight request cancelled, cancels the
    ``_call_tool`` task and answers with the bounded JSON-RPC error
    ``{"code": 0, "message": "Request cancelled"}``.  The server's
    ``_await_blocking_task`` shield keeps the thread-backed emulator worker
    alive, so the full finite count still completes; the follow-up read
    serializes behind it and proves the tick advanced by exactly ``frames``.
    """
    before = await _request(client, "pokered://game-state")
    before_tick = before["epoch"]["tick"]
    request_id = await _send_raw_request(
        client, "tools/call", {"name": "link_step", "arguments": {"count": frames}}
    )
    # Let the server admit the request (its SDK request-task is created) before
    # the cancellation notification arrives; the count keeps it outstanding.
    await asyncio.sleep(OUTSTANDING_CANCEL_DELAY)
    await client.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": CANCEL_REASON},
        }
    )
    response = await _read_response_for(client, request_id, bound=CALL_BOUND)
    assert "error" in response, response
    error = response["error"]
    assert error["code"] == 0, error
    assert error["message"] == "Request cancelled", error
    # The read below serializes behind the cancelled-but-shielded worker, so
    # its coherent snapshot proves the worker was not orphaned mid-step.
    after = await _request(client, "pokered://game-state")
    after_tick = after["epoch"]["tick"]
    assert after_tick == before_tick + frames, (before_tick, after_tick, frames)
    _assert_battle_observations(after, label=label)
    _log(
        "server_cancellation",
        f"request_id={request_id} frames={frames} "
        f"tick={before_tick}->{after_tick} error={error['message']}",
    )
    return {
        "request_id": request_id,
        "error": error,
        "before_tick": before_tick,
        "after_tick": after_tick,
        "state": after,
    }


async def _pair(client):
    paired = await client.tool("link_pair")
    assert paired["paired"] is True, paired
    status = await client.tool("link_status")
    assert status["link_backend"] == "bit_accurate", status
    assert status["paired"] is True, status


async def _drive_to_link_menu(client):
    for _ in range(3):
        await _press_both(client, "up", duration=6)
        await _link_step(client, RECEPTIONIST_WALK_FRAMES)
    spent = 0
    while spent < LINK_MENU_BUDGET:
        states = await _states(client)
        if all(_link_menu_ready(state) for state in states):
            _log("link_menu", f"frames={spent}")
            return states
        # Burst: stagger the ordinary public A input exactly as the proven
        # local driver does (primary A, four frames, peer A, sixteen frames).
        for _ in range(LINK_MENU_BURST_ATTEMPTS):
            await _press(client, "a", duration=4)
            await _link_step(client, 4)
            await _peer_press(client, "a", duration=4)
            await _link_step(client, 16)
            spent += 20
        # Quiet window: no public input, so a LinkMenu that finishes drawing
        # here cannot select its default entry.
        for _ in range(LINK_MENU_QUIET_FRAMES // STEP_CHUNK):
            await _link_step(client, STEP_CHUNK)
            spent += STEP_CHUNK
            states = await _states(client)
            if all(_link_menu_ready(state) for state in states):
                _log("link_menu", f"frames={spent}")
                return states
    states = await _states(client)
    raise AssertionError(
        f"LinkMenu never reached on both peers: budget {LINK_MENU_BUDGET} "
        f"frames exhausted; states={json.dumps(states)}"
    )


async def _select_colosseum(client):
    # Let the pair settle on the freshly drawn LinkMenu before moving cursors.
    await _link_step(client, 60)
    spent = 60
    while spent < LINK_MENU_CURSOR_BUDGET:
        states = await _states(client)
        if all(_menu(state)["current_item"] == 1 for state in states):
            break
        primary_cursor = _menu(states[0])["current_item"]
        peer_cursor = _menu(states[1])["current_item"]
        if primary_cursor != 1:
            await _press(client, "down" if primary_cursor < 1 else "up", duration=12)
        if peer_cursor != 1:
            await _peer_press(client, "down" if peer_cursor < 1 else "up", duration=12)
        await _link_step(client, 16)
        spent += 16
    states = await _states(client)
    assert all(_menu(state)["current_item"] == 1 for state in states), states
    await _press_both(client, "a")
    states, spent = await _mash_until(
        client,
        lambda state: state["overworld"]["map_id"] == COLOSSEUM_MAP_ID,
        budget=COLOSSEUM_WARP_BUDGET,
        prompt="COLOSSEUM warp never reached on both peers",
    )
    _log("colosseum", f"frames={spent}")
    return states


async def _enter_battle(client):
    # The first-attached primary is the local-pair clock master, so it walks
    # right onto the hidden-event tile; the peer follows left.  Step one frame
    # at a time across the trigger so the paired CPUs stay aligned.
    for _ in range(4):
        states = await _states(client)
        if all(_battle(state)["raw_is_in_battle"] in BATTLE_KINDS for state in states):
            break
        await _press(client, "right", duration=8)
        await _peer_press(client, "left", duration=8)
        await _link_step(client, 8)
        await _link_step(client, 12)
    states, spent = await _mash_until(
        client,
        lambda state: _battle(state)["raw_is_in_battle"] in BATTLE_KINDS,
        budget=BATTLE_ENTRY_BUDGET,
        prompt="link battle never became active on both peers",
    )
    _log("battle_active", f"frames={spent}")
    return states


def _assert_battle_observations(state, *, label):
    """Assert the documented additive schema and validity for one peer."""
    battle = _battle(state)
    menu = _menu(state)

    # Identity/enemy: the ROM-owned enemy combatant must be present and valid.
    assert battle["raw_is_in_battle"] in BATTLE_KINDS, (label, battle)
    assert battle["kind"] in BATTLE_KINDS, (label, battle)
    enemy = battle["enemy_mon"]
    assert isinstance(enemy, dict), (label, battle)
    assert enemy["species"] not in (0, 0xFF), (label, enemy)
    assert enemy["max_hp"] > 0, (label, enemy)
    assert 0 < enemy["hp"] <= enemy["max_hp"], (label, enemy)
    assert 0 <= enemy["slot"] < 6, (label, enemy)
    assert battle["enemy_mon_valid"] is True, (label, battle)

    # Raw result is always exposed; nothing here proves a terminal outcome.
    assert type(battle["raw_battle_result"]) is int, (label, battle)
    assert battle["terminal_result"] is None, (label, battle)

    # Derived phase: documented validity, and command_selection only from the
    # session's observational menu hooks.
    assert battle["phase"] in (0, 1, 2, 3, 4, 5, 6), (label, battle)
    assert type(battle["phase_valid"]) is bool, (label, battle)
    evidence = battle["phase_evidence"]
    assert isinstance(evidence, list) and evidence, (label, battle)
    assert set(evidence) <= OBSERVATIONAL_MENU_HOOKS | PHASE_EVIDENCE_SYMBOLS, (
        label,
        battle,
    )

    # Menu/transient fields are present with their documented validity.
    assert battle["menu_open"] in (True, False, None), (label, battle)
    assert set(battle["menu_evidence"]) <= OBSERVATIONAL_MENU_HOOKS, (label, battle)
    for field in BATTLE_TRANSIENT_FIELDS:
        assert field in battle, (label, field, battle)
    assert menu["current_item"] is not None, (label, menu)
    assert menu["max_item"] is not None, (label, menu)
    assert isinstance(state["epoch"], dict), (label, state)
    assert state["epoch"]["session_id"], (label, state)


async def _prelink(client, asset):
    """Load the primary battle fixture and prove the public surface is live."""
    await client.initialize()
    listed = await client.request("tools/list", {})
    tool_names = {row["name"] for row in listed["tools"]}
    assert PUBLIC_TOOLS <= tool_names, tool_names
    # No RAM/hash/hook tool exists in the public surface.
    assert not any("hash" in name or "memory" in name or "hook" in name for name in tool_names), (
        tool_names
    )
    resources = await client.request("resources/list", {})
    assert {
        "pokered://game-state",
        "pokered://peer-game-state",
        "pokered://link-status",
    } <= {row["uri"] for row in resources["resources"]}
    loaded = await client.tool(
        "load_state", {"data": base64.b64encode(asset["state"]).decode("ascii")}
    )
    assert loaded == {"ok": True}, loaded
    before = await _states(client)
    for state, label in zip(before, ("primary", "peer"), strict=True):
        battle = _battle(state)
        # The pristine fixture is out of battle; the live enemy-mon bytes are
        # stale and must not be reported as live state.
        assert battle["raw_is_in_battle"] == 0, (label, battle)
        assert battle["kind"] == 0, (label, battle)
        assert battle["enemy_mon"] is None, (label, battle)
        assert battle["phase"] == 0 and battle["phase_valid"] is True, (label, battle)
        assert battle["terminal_result"] is None, (label, battle)
        # A load resets the observational menu state to unknown, never
        # fabricated.
        assert battle["menu_open"] is None, (label, battle)
        assert set(battle["menu_evidence"]) <= OBSERVATIONAL_MENU_HOOKS, (label, battle)
    saved = await client.tool("save_state")
    assert set(saved) == {"data"} and isinstance(saved["data"], str), saved
    assert base64.b64decode(saved["data"], validate=True)
    assert await client.tool("press", {"button": "a", "duration": 1}) == {"ok": True}
    assert await client.tool("release", {"button": "a"}) == {"ok": True}
    await client.tool("step", {"count": 2})
    assert await client.tool("load_state", saved) == {"ok": True}
    restored = await _request(client, "pokered://game-state")
    # The emulated state is restored exactly; the epoch is session
    # bookkeeping, so only the load generation advances (the external tick is
    # deliberately not rewound by load_state).
    assert _without_epoch(restored) == _without_epoch(before[0]), (restored, before[0])
    assert restored["epoch"]["load_generation"] == before[0]["epoch"]["load_generation"] + 1, (
        restored["epoch"],
        before[0]["epoch"],
    )
