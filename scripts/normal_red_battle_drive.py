"""Ordinary public-MCP battle controls shared by local and TCP Red rows."""

from __future__ import annotations

import json

from tests._mcp_battle_phase_rom_drive_support import (
    _assert_terminal_return,
    _boundary_button,
    _enter_battle,
    _replacement_button,
)

# Damaging moves naturally present in this admitted party, verified against
# pinned pokered move data: Tackle, Vine Whip, Poison Sting, String Shot is NOT
# damaging, and Gust. Reject an exhausted/unknown menu rather than guess.
DAMAGING_MOVES = frozenset({33, 22, 40, 16})


class PairBattleClient:
    """Map existing public battle driver operations to public owner tools.

    TCP has no in-process peer tool; the existing advertised-surface-checked
    pair routes it to that owner's public press/step/resource operations.
    """

    def __init__(self, pair):
        self.pair = pair

    async def request(self, method, arguments):
        if method != "resources/read":
            raise ValueError("battle adapter permits only resource reads")
        uri = arguments["uri"]
        if uri not in ("pokered://game-state", "pokered://peer-game-state"):
            raise ValueError("battle adapter permits only game-state resources")
        state = await self.pair.state(int(uri == "pokered://peer-game-state"))
        return {"contents": [{"text": json.dumps(state)}]}

    async def tool(self, name, arguments=None):
        arguments = arguments or {}
        if name == "link_step":
            await self.pair.step(arguments["count"])
        elif name in ("press", "link_peer_press"):
            await self.pair.press(
                int(name == "link_peer_press"),
                arguments["button"],
                duration=arguments.get("duration", 4),
            )
        else:
            raise ValueError(f"undocumented adapter tool: {name}")
        return {"ok": True}


def available_damaging_slot(state):
    active = state.get("party", {}).get("active_mon")
    if not isinstance(active, dict):
        return None
    moves, pp = active.get("moves"), active.get("pp")
    if not isinstance(moves, (list, tuple)) or not isinstance(pp, (list, tuple)):
        return None
    for slot, (move, amount) in enumerate(zip(moves, pp, strict=True)):
        if move in DAMAGING_MOVES and type(amount) is int and amount & 0x3F:
            return slot
    return None


async def _drive_to_link_menu(client):
    """Stop dialogue A pulses at the fresh public save-choice observation.

    The pinned CableClubNPC has one YesNoChoice before SaveGameData. An old
    menu descriptor cannot admit input: require a new server yes_no_prompt
    event, both known save menus, then staggered confirmations and guarded serial progress.
    """
    pair = client.pair

    async def choices(owner):
        events = await pair.rom_events(owner)
        return sum(event.get("name") == "yes_no_prompt" for event in events)

    baseline = [await choices(owner) for owner in range(2)]
    for _ in range(3):
        for owner in range(2):
            await pair.press(owner, "up", duration=6)
        await pair.step(60)
    ready = False
    for _ in range(1200):
        fresh = [await choices(owner) > baseline[owner] for owner in range(2)]
        states = [await pair.state(owner) for owner in range(2)]
        assert all(state["overworld"]["map_id"] == 64 for state in states), states
        if all(fresh):
            ready = True
            break
        for owner in range(2):
            if not await choices(owner) > baseline[owner]:
                await pair.press(owner, "a", duration=1)
                await pair.step(2)
    assert ready, "fresh public Cable Club save-choice events never reached both owners"
    await pair.release_buttons()
    await pair.step(8)
    states = [await pair.state(owner) for owner in range(2)]
    assert all(
        state["menu"]["current_item"] == 0
        and state["menu"]["max_item"] == 1
        and state["menu"]["watched_keys"] == 3
        for state in states
    ), ("fresh save-choice event lacks known Yes/No menu", states)
    text_baseline = [
        sum(event.get("name") == "text_shown" for event in await pair.rom_events(owner))
        for owner in range(2)
    ]
    # Stagger the normal confirmations as in the working public trade path.
    await pair.press(0, "a", duration=4)
    await pair.step(4)
    await pair.press(1, "a", duration=4)
    await pair.step(16)
    await pair.release_buttons()
    # No further A during SaveGameData/serial setup: repeated dialogue pulses
    # can survive the blocked TCP preamble and commit the default TRADE vote.
    for attempt in range(600):
        states = [await pair.state(owner) for owner in range(2)]
        assert all(state["overworld"]["map_id"] == 64 for state in states), states
        if all(
            state["menu"]["max_item"] == 2 and state["menu"]["watched_keys"] == 3
            for state in states
        ):
            return states
        # Menu geometry is written before its draw finishes, so the first
        # confirmation can be lost. The next PrintText in this pinned NPC is
        # PleaseWait, after SaveGameData (whose callees do not print text).
        # Stop pulses on that fresh event, before serial/LinkMenu processing.
        for owner, state in enumerate(states):
            texts = sum(event.get("name") == "text_shown" for event in await pair.rom_events(owner))
            menu = state["menu"]
            if texts == text_baseline[owner] and attempt < 64 and attempt % 2 == 0:
                assert (menu["current_item"], menu["max_item"], menu["watched_keys"]) == (0, 1, 3)
                await pair.press(owner, "a", duration=1)
        await pair.step(4)
    raise AssertionError("passive LinkMenu entry frame bound exhausted")


async def _select_colosseum(client):
    """Observe both real cursors before confirming; decline a stale/warped menu.

    A queued dialogue A can remain pending through the TCP preamble. Release
    ordinary buttons before menu steering and never keep steering after a warp.
    """
    pair = client.pair
    await pair.release_buttons()
    ready = False
    for _ in range(100):
        states = [await pair.state(owner) for owner in range(2)]
        for state in states:
            menu = state["menu"]
            assert state["overworld"]["map_id"] == 64, ("unexpected pre-vote warp", states)
            assert menu["max_item"] == 2 and menu["watched_keys"] == 3, (
                "unproven LinkMenu geometry",
                states,
            )
        if all(state["menu"]["current_item"] == 1 for state in states):
            ready = True
            break
        for owner, state in enumerate(states):
            cursor = state["menu"]["current_item"]
            assert cursor in (0, 1, 2), state
            if cursor != 1:
                await pair.press(owner, "down" if cursor < 1 else "up", duration=2)
        await pair.step(4)
    assert ready, "both public LinkMenu cursors never reached COLOSSEUM"
    await pair.press(0, "a", duration=4)
    await pair.step(4)
    await pair.press(1, "a", duration=4)
    await pair.step(16)
    for attempt in range(600):
        states = [await pair.state(owner) for owner in range(2)]
        assert all(state["overworld"]["map_id"] != 239 for state in states), (
            "TRADE CENTER selected instead of COLOSSEUM",
            states,
        )
        if all(state["overworld"]["map_id"] == 240 for state in states):
            return states
        # A descriptor can precede the ROM's input loop. Retry only while the
        # owner still publishes the exact selected LinkMenu, never in its room.
        if attempt < 64 and attempt % 4 == 0:
            for owner in range(2):
                state = await pair.state(owner)
                if state["overworld"]["map_id"] == 64:
                    menu = state["menu"]
                    assert (menu["current_item"], menu["max_item"], menu["watched_keys"]) == (
                        1,
                        2,
                        3,
                    )
                    await pair.press(owner, "a", duration=1)
                    await pair.step(2)
        await pair.step(4)
    raise AssertionError("COLOSSEUM warp frame bound exhausted")


async def complete_battle(pair, journal, *, budget_frames=18000):
    """Record KO/replacement and terminal edges; room acceptance is separate."""
    client = PairBattleClient(pair)
    # Public one-frame owner stepping needs the documented TCP frame barrier
    # for the guarded save/serial rendezvous. LocalPair ignores this knob.
    await pair.link_up(arm_barrier=True)
    await _drive_to_link_menu(client)
    await _select_colosseum(client)
    await _enter_battle(client)
    terminal = [None, None]
    last_live = [None, None]
    replacement_open = [False, False]
    replacement_completed = [False, False]
    last_input = [-8, -8]
    frames = 0
    while frames < budget_frames:
        states = [await pair.state(owner) for owner in range(2)]
        journal.write(json.dumps({"frames": frames, "states": states}) + "\n")
        journal.flush()
        for owner, state in enumerate(states):
            battle = state["battle"]
            if terminal[owner] is not None:
                continue
            if battle["raw_is_in_battle"] not in (1, 2):
                _assert_terminal_return(state, label=f"normal-red-owner-{owner}")
                terminal[owner] = state
                continue
            last_live[owner] = state
            replacement = _replacement_button(state)
            if replacement is not None:
                replacement_open[owner] = True
            elif replacement_open[owner]:
                active = state.get("party", {}).get("active_mon")
                if active and active["hp"] > 0 and battle["raw_is_in_battle"] in (1, 2):
                    replacement_completed[owner] = True
            if frames - last_input[owner] < 8:
                continue
            slot = available_damaging_slot(state)
            button = replacement
            if button is None and slot is not None:
                button = _boundary_button(state, slot)
            if button is not None:
                journal.write(
                    json.dumps({"frames": frames, "owner": owner, "button": button}) + "\n"
                )
                journal.flush()
                await pair.press(owner, button, duration=4)
                last_input[owner] = frames
        if all(state is not None for state in terminal):
            break
        await pair.step(4)
        frames += 4
    assert all(state is not None for state in terminal), ("battle frame bound", frames)
    assert any(replacement_completed), ("no witnessed nonterminal replacement", replacement_open)
    return {
        "frames": frames,
        "terminal": terminal,
        "last_live": last_live,
        "replacement_open": replacement_open,
        "replacement_completed": replacement_completed,
        "network_frame_barrier_armed": getattr(pair, "network_frame_barrier_armed", False),
    }
