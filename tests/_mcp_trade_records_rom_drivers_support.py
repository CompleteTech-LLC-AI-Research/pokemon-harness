"""Public-surface navigation and trade drivers for the MCP stdio trade module.

Split from ``tests/test_mcp_trade_records_rom.py`` (#208) with no behavior change:
every helper that reads the public ``menu``/``overworld``/``link_step`` surface or
drives the trade center moved here verbatim."""

from __future__ import annotations

import json

from tests._mcp_trade_records_rom_support import (
    CABLE_CLUB_MAP_ID,
    CONFIRM_MENU_KEYS,
    LINK_MENU_BUDGET,
    LINK_MENU_BURST_ATTEMPTS,
    LINK_MENU_MAX_ITEMS,
    LINK_MENU_QUIET_FRAMES,
    PARTY_MENU_KEYS,
    POST_TRADE_BUDGET,
    RECEPTIONIST_WALK_FRAMES,
    STATS_MENU_KEYS,
    STEP_CHUNK,
    TRADE_BUDGET,
    TRADE_CENTER_MAP_ID,
    TRADE_MENU_KEYS,
    TRADE_RECEIVED_EVENT,
    WALK_ATTEMPTS,
    WALK_SETTLE_BUDGET,
    WALK_SETTLE_SLICE,
    WALK_STABLE_CHECKS,
    WALK_STEP_FRAMES,
    WARP_BUDGET,
    TradeRun,
)


def _overworld(state):
    overworld = state.get("overworld")
    assert isinstance(overworld, dict), state
    return overworld


def _menu(state):
    menu = state.get("menu")
    assert isinstance(menu, dict), state
    return menu


def _menu_ready(state, max_items):
    menu = _menu(state)
    watched = menu["watched_keys"]
    current = menu["current_item"]
    return (
        type(watched) is int
        and watched & 0x01 == 0x01
        and menu["max_item"] in max_items
        and type(current) is int
    )


def _link_menu_ready(state):
    return _overworld(state)["map_id"] == CABLE_CLUB_MAP_ID and _menu_ready(
        state, LINK_MENU_MAX_ITEMS
    )


def _selection_loop_ready(state, party_count):
    """Whether one owner is back on the trade-center party-selection loop."""
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return False
    menu = _menu(state)
    return (
        menu["watched_keys"] == PARTY_MENU_KEYS
        and menu["max_item"] == party_count
        and _menu_ready(state, (party_count,))
    )


def _player_mon_menu_live(state, party_count):
    """Whether one owner is resting on the ROM's live trade party menu."""
    menu = _menu(state)
    return (
        _overworld(state)["map_id"] == TRADE_CENTER_MAP_ID
        and menu["watched_keys"] == PARTY_MENU_KEYS
        and menu["max_item"] == party_count
        and type(menu["current_item"]) is int
        and 0 <= menu["current_item"] < party_count
    )


def _log(phase, detail):
    print(f"MCP_TRADE_RECORDS {phase} {detail}", flush=True)


async def _all_states(pair):
    return [await pair.state(owner) for owner in range(pair.owners)]


async def _all_records(pair):
    return [await pair.records(owner) for owner in range(pair.owners)]


async def _trade_received_counts(pair):
    """Per-owner count of the ROM-owned ``trade_received`` append marker.

    Read from each owner's *own* event log, so the peer's copy is observed
    rather than inferred from the primary's view.
    """
    counts = []
    for owner in range(pair.owners):
        events = await pair.rom_events(owner)
        names = [str(event.get("name")) for event in events]
        counts.append(names.count(TRADE_RECEIVED_EVENT))
    return counts


async def _link_step(pair, frames):
    await pair.step(frames)


async def _press(pair, button, *, duration=4):
    await pair.press(0, button, duration=duration)


async def _peer_press(pair, button, *, duration=4):
    await pair.press(1, button, duration=duration)


async def _press_both(pair, button, *, duration=4):
    await _press(pair, button, duration=duration)
    await _peer_press(pair, button, duration=duration)


async def _press_actions(pair, primary_action, peer_action, *, duration=4):
    """Press each owner's button, treating ``None`` as "send no input".

    The completion driver must leave an owner that has already reached the
    restored trade-center selection loop untouched: ``_trade_action`` would
    return ``"a"`` there and start a *second* trade, which desynchronises the
    pair before its peer arrives at the same milestone.

    An action is either a button name or a ``(button, hold)`` pair.  The hold
    length matters while a menu can still open under the keystroke: the ROM's
    menu loop reads the key state of its first frames, so an ``A`` that is still
    held when the trade party menu appears is consumed as that menu's own
    selection and the offer is confirmed without ever being observed.  Dialogue
    presses therefore use a one-frame hold while menu navigation keeps the
    proven four-frame hold.
    """
    for owner, action in ((0, primary_action), (1, peer_action)):
        if action is None:
            continue
        button, hold = action if isinstance(action, tuple) else (action, duration)
        await pair.press(owner, button, duration=hold)


def _approach_action(state, party_count):
    """Return one owner's approach action on the way to the trade party menu.

    An owner resting on the live ``.playerMonMenu`` receives no input, so its
    peer can catch up.  An owner that is already inside the STATS/TRADE sub-menu
    was driven there by an input the caller aimed at the preceding dialogue (the
    held ``A`` is consumed by the freshly opened menu); ``B`` is the ROM's
    return key for that sub-menu, so backing out restores the live menu the
    caller has to observe instead of pressing on and committing unobserved.
    Everything else is the pre-menu dialogue and the waiting prompt, which
    advance on ``A``; the one-frame hold keeps that press from reaching a menu
    that opens underneath it.
    """
    if _player_mon_menu_live(state, party_count):
        return None
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return ("a", 1)
    if _menu(state)["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS):
        return ("b", 4)
    return ("a", 1)


async def _owner_trade_action(pair, owner, state, *, party_count, target_slot, confirmed):
    """Return one owner's ``(button, hold)`` action and any freshly read offer.

    The ROM's ``.playerMonMenu`` publishes ``wCurrentMenuItem``/``wMaxMenuItem``
    and closes as soon as its entry is confirmed, so an offer is only accepted
    from a *fresh* read taken after the cursor has settled on the intended slot
    and before the confirming press.  A sample that already showed the intended
    slot is not enough on its own: the cursor can move between the sample and
    the press, which is exactly the gap that let an earlier revision of this
    driver confirm owner 0 without ever reading its live menu.

    An owner that reaches the STATS/TRADE sub-menu without a recorded offer is
    returned to the live menu through the ROM's sub-menu back key rather than
    being confirmed unobserved.
    """
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        # Outside the trade center the drive is still in the Cable Club walk.
        return ("a", 1), None
    menu = _menu(state)
    if not confirmed and _player_mon_menu_live(state, party_count):
        item = menu["current_item"]
        if item != target_slot:
            # Player-mon menu: walk the published cursor to the intended slot.
            return (("down" if item < target_slot else "up"), 4), None
        fresh = await pair.state(owner)
        if (
            _player_mon_menu_live(fresh, party_count)
            and _menu(fresh)["current_item"] == target_slot
        ):
            return ("a", 4), item
        # The menu moved between the sample and the confirming press; keep
        # driving from the fresh reading instead of confirming what was not read.
        return _trade_action(fresh, party_count, target_slot), None
    if not confirmed and menu["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS):
        return ("b", 4), None
    return _trade_action(state, party_count, target_slot), None


async def _advance_until(pair, predicate, *, budget, prompt):
    """Advance the pair in bounded chunks until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _all_states(pair)
        if all(predicate(state) for state in states):
            return states, spent
        await _link_step(pair, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _mash_until(pair, predicate, *, budget, prompt):
    """A-mash the pair while advancing until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _all_states(pair)
        if all(predicate(state) for state in states):
            return states, spent
        await _press_both(pair, "a")
        await _link_step(pair, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _prelink(pair, primary_asset, peer_asset):
    """Load the admitted fixtures through the public surface and verify it."""
    await pair.initialize()
    await pair.assert_surface()
    await pair.load_fixture(primary_asset, owner=0)
    if pair.transport == "tcp_pair":
        await pair.load_fixture(peer_asset, owner=1)
    before = await _all_records(pair)
    for payload, owner in zip(before, range(pair.owners), strict=True):
        assert payload["digest_algorithm"] == "sha256", payload
        assert payload["record_size"] == 44, payload
        assert payload["valid"] is True, payload
        assert len(_records_from_payload(payload)) >= 1, payload
        del owner
    return before


async def _drive_to_link_menu(pair):
    for _ in range(3):
        await _press_both(pair, "up", duration=6)
        await _link_step(pair, RECEPTIONIST_WALK_FRAMES)
    spent = 0
    while spent < LINK_MENU_BUDGET:
        states = await _all_states(pair)
        if all(_link_menu_ready(state) for state in states):
            _log("link_menu", f"frames={spent}")
            return states
        # Stagger the ordinary public A input exactly as the proven local
        # driver does: primary A, four frames, peer A, sixteen frames.
        for _ in range(LINK_MENU_BURST_ATTEMPTS):
            await _press(pair, "a", duration=4)
            await _link_step(pair, 4)
            await _peer_press(pair, "a", duration=4)
            await _link_step(pair, 16)
            spent += 20
        # Quiet window: no public input, so a LinkMenu that finishes drawing
        # here cannot select its default entry before the explicit selection.
        for _ in range(LINK_MENU_QUIET_FRAMES // STEP_CHUNK):
            await _link_step(pair, STEP_CHUNK)
            spent += STEP_CHUNK
            states = await _all_states(pair)
            if all(_link_menu_ready(state) for state in states):
                _log("link_menu", f"frames={spent}")
                return states
    states = await _all_states(pair)
    raise AssertionError(
        f"LinkMenu never reached on both owners: budget {LINK_MENU_BUDGET} "
        f"frames exhausted; states={json.dumps(states)}"
    )


async def _select_trade_center(pair):
    """Confirm the default TRADE CENTER entry and wait for the 0xEF warp."""
    spent = 0
    while spent < 400:
        states = await _all_states(pair)
        if all(_menu_ready(state, LINK_MENU_MAX_ITEMS) for state in states):
            break
        await _link_step(pair, 16)
        spent += 16
    states, cursor_frames = await _advance_until(
        pair,
        lambda state: _link_menu_ready(state) and _menu(state)["current_item"] == 0,
        budget=400,
        prompt="LinkMenu cursor never rested on TRADE CENTER on both owners",
    )
    # Staggered A confirms the default TRADE CENTER entry on both owners.
    await _press(pair, "a", duration=4)
    await _link_step(pair, 4)
    await _peer_press(pair, "a", duration=4)
    await _link_step(pair, 16)
    states, warp_frames = await _mash_until(
        pair,
        lambda state: _overworld(state)["map_id"] == TRADE_CENTER_MAP_ID,
        budget=WARP_BUDGET,
        prompt="TRADE_CENTER warp never reached on both owners",
    )
    _log("trade_center", f"cursor={cursor_frames} warp={warp_frames}")
    return states


async def _walk_to_trade_trigger(pair):
    """Turn each owner toward its hidden-event tile and hand off to the trade.

    The internal-clock owner spawns at (3, 4) and the external-clock owner at
    (6, 4); the two hidden-event triggers live at (4, 4) and (5, 4).  The
    public ``overworld.x`` identifies each owner without reading serial
    registers.  Facing is what the ``ANY_FACING`` trigger needs, so each
    attempt presses the direction on its own owner and then advances the pair
    one frame at a time: one owner can enter ``CableClub_DoBattleOrTrade``
    during this loop, and ticking twenty frames on that side before its peer
    advances would let the first serial transfers use stale handshake bytes.

    Before the first press both owners are advanced quietly until the
    ROM-owned trade-center state has stopped changing (see
    ``WALK_STABLE_CHECKS``): the Cable Club entry script keeps the overworld
    input for its own handshake, so a press offered during it is consumed
    instead of walking onto the trigger.  The quiet rendezvous is bounded, so
    an unsettled pair still walks and fails its own budget rather than hanging.
    """
    settle_frames = await _settle_trade_center(pair)
    states = await _all_states(pair)
    directions = ["right" if _overworld(state)["x"] < 5 else "left" for state in states]
    for _ in range(WALK_ATTEMPTS):
        await _press(pair, directions[0], duration=8)
        await _peer_press(pair, directions[1], duration=8)
        for _ in range(WALK_STEP_FRAMES):
            await _link_step(pair, 1)
    pair.walk_settle_frames = settle_frames
    _log("walk", f"directions={directions} settle_frames={settle_frames}")


def _trade_center_fingerprint(state):
    """Return the public trade-center observation used for quiescence.

    Every field is read from a resource the acceptance surface already
    publishes, so the rendezvous is a statement about ROM-owned state rather
    than about host scheduling.
    """
    overworld = _overworld(state)
    menu = _menu(state)
    text = state["text"]
    return (
        overworld["map_id"],
        overworld["x"],
        overworld["y"],
        overworld["direction"],
        overworld["walk_counter"],
        overworld["current_map_script"],
        overworld["map_script_flags"],
        menu["watched_keys"],
        menu["max_item"],
        text["suppress_prompt_wait"],
    )


async def _settle_trade_center(pair):
    """Advance both owners quietly until their trade-center state is stable.

    Returns the number of frames spent.  ``WALK_STABLE_CHECKS`` identical
    consecutive readings of :func:`_trade_center_fingerprint` are required, and
    the whole wait is bounded by ``WALK_SETTLE_BUDGET`` so a pair that never
    settles still walks and fails closed on its own budget.
    """
    spent = 0
    previous = None
    stable = 0
    while spent < WALK_SETTLE_BUDGET and stable < WALK_STABLE_CHECKS:
        states = await _all_states(pair)
        current = tuple(_trade_center_fingerprint(state) for state in states)
        stable = stable + 1 if current == previous else 0
        previous = current
        if stable >= WALK_STABLE_CHECKS:
            break
        await _link_step(pair, WALK_SETTLE_SLICE)
        spent += WALK_SETTLE_SLICE
    return spent


def _trade_action(state, party_count, target_slot=0):
    """Return the public button for one owner from its observed ROM menu context.

    The navigator is reactive rather than phase-counted: the ROM can escape
    back to an outer menu (A in the STATS/TRADE sub-menu displays stats and
    returns to the player-mon menu), so each owner is driven from the mask and
    bounds it currently publishes.  ``wMaxMenuItem`` is retained after a menu
    closes, so the map id gates every branch and only the trade-center masks
    below are treated as live menus.

    ``target_slot`` is the party slot this owner must offer.  On the
    player-mon menu the ROM sets ``wMaxMenuItem = wPartyCount``, so a
    multi-member party can walk the cursor with DOWN; a single-member party
    never issues a DOWN because the cursor is already on the only slot.
    """
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return "a"
    menu = _menu(state)
    keys = menu["watched_keys"]
    item = menu["current_item"]
    maximum = menu["max_item"]
    if keys == PARTY_MENU_KEYS and maximum == party_count and 0 <= item < party_count:
        if item < target_slot:
            # Player-mon menu: move down toward the intended slot.
            return "down"
        if item > target_slot:
            # Overshot (or the ROM clamped); walk back up.
            return "up"
        # Player-mon menu: A selects this mon and opens the STATS/TRADE menu.
        return "a"
    if keys == STATS_MENU_KEYS and maximum == 0:
        # STATS/TRADE sub-menu: RIGHT moves the cursor from STATS to TRADE.
        return "right"
    if keys == TRADE_MENU_KEYS and maximum == 0:
        # TRADE entry: A commits the selection and starts the nibble exchange.
        return "a"
    if keys == CONFIRM_MENU_KEYS and maximum == 1 and item == 0:
        # "WILL TRADE X FOR Y?" defaults to YES.
        return "a"
    # Pre-menu dialogue, the waiting prompt, and the trade animation: A-mash.
    return "a"


def _records_from_payload(payload):
    records = payload["records"]
    assert isinstance(records, list), payload
    return records


def _digests(records):
    return [record["digest"] for record in records]


def _compaction_expected_digests(before, outgoing_slot, incoming_digest):
    """Return the source-defined post-trade digest list for one owner.

    Gen I removes the selected outgoing record, compacts the survivors keeping
    their order, then appends the received 44-byte record (engine/link/
    cable_club.asm via the party compaction path).  The receiving slot is the
    final occupied slot, independent of the outgoing index, so a fixed-slot or
    in-place equality would be wrong for a multi-member party.
    """
    survivors = before[:outgoing_slot] + before[outgoing_slot + 1 :]
    return [record["digest"] for record in survivors] + [incoming_digest]


def _compacted_records(before, outgoing_slot, incoming_record):
    """Return the source-defined post-trade record list for one owner.

    Same remove/compact/append model as ``_compaction_expected_digests`` but at
    the record level, so a fabricated observation carries the received record's
    species and level (which the oracle also checks) instead of a digest alone.
    """
    survivors = before[:outgoing_slot] + before[outgoing_slot + 1 :]
    renumbered = [{**record, "slot": index} for index, record in enumerate(survivors)]
    receiving = len(before) - 1
    return renumbered + [{**incoming_record, "slot": receiving}]


async def _drive_trade(
    pair, *, party_counts, primary_before, peer_before, primary_slot=0, peer_slot=0
):
    """Drive both owners through the ROM trade flow to full completion.

    Success is the exact paired 44-byte record exchange under the ROM's
    remove/compact/append semantics: each owner's final occupied slot must hold
    the peer's selected record after its other records are compacted in order.
    The copy milestone is only the first half: after it the ROM runs the trade
    animation, a forced evolution check, a final serial synchronization, the
    save, and the return to the selection loop, so this continues to that
    later, ROM-owned completion milestone before returning.  Every phase is
    bounded, and a bound that lapses fails closed with the observed state
    instead of spinning.

    The copy is only accepted when *both* signals agree: each owner's party
    digests equal the source-defined compaction prediction *and* each owner's
    own event log holds the ROM's ``trade_received`` marker from
    ``_AddEnemyMonToPlayerParty``.  The digest comparison alone is vacuous for
    an orientation whose two admitted records happen to be byte-identical (Red
    and Yellow cannot supply distinct admitted records, see the module
    docstring), because it already holds before a single input is sent; the
    ROM-owned marker is what makes those rows falsifiable, and a party that
    never appended a received record never emits it.  The records are reread
    after the terminal milestone and required to equal the copy snapshot, so a
    party mutated between the copy and the room return fails this row instead
    of being validated from a stale read.
    """
    primary_incoming = peer_before[peer_slot]["digest"]
    peer_incoming = primary_before[primary_slot]["digest"]
    expected_primary = _compaction_expected_digests(primary_before, primary_slot, primary_incoming)
    expected_peer = _compaction_expected_digests(peer_before, peer_slot, peer_incoming)
    spent = 0
    milestones = []
    swap_records = None
    swap_states = None
    pre_evolution = None
    received: list[int] = []
    back_outs = 0
    # Cursor slot each owner was resting on when it confirmed the offer.  The
    # ROM's ``.playerMonMenu`` publishes ``wCurrentMenuItem``/``wMaxMenuItem``,
    # so the intended slot is observed through the public menu read rather than
    # inferred from the result.  The offer is recorded from the fresh read that
    # immediately precedes the confirming press (``_owner_trade_action``).
    offered: dict[int, int] = {}
    targets = (primary_slot, peer_slot)
    while spent < TRADE_BUDGET:
        records = await _all_records(pair)
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])
        received = await _trade_received_counts(pair)
        if (
            primary_after
            and peer_after
            and _digests(primary_after) == expected_primary
            and _digests(peer_after) == expected_peer
            and all(count >= 1 for count in received)
        ):
            swap_records = records
            swap_states = await _all_states(pair)
            pre_evolution = await pair.evolution_counts()
            break
        states = await _all_states(pair)
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}"
            for state in states
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        actions = []
        for owner, state in enumerate(states):
            action, observation = await _owner_trade_action(
                pair,
                owner,
                state,
                party_count=party_counts[owner],
                target_slot=targets[owner],
                confirmed=owner in offered,
            )
            if action is not None and action[0] == "b":
                back_outs += 1
            if observation is not None:
                offered[owner] = observation
            actions.append(action)
        await _press_actions(pair, actions[0], actions[1])
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    if swap_records is None:
        records = await _all_records(pair)
        states = await _all_states(pair)
        raise AssertionError(
            "trade never produced the exact paired digest exchange within "
            f"{TRADE_BUDGET} frames; offered={offered} "
            f"trade_received={received} "
            f"primary={json.dumps(records[0])} "
            f"peer={json.dumps(records[1])} states={json.dumps(states)}"
        )
    _log("trade_copy", f"frames={spent} trade_received={received} milestones={milestones}")
    final_states, post_frames, evolution = await _drive_trade_completion(
        pair,
        party_counts=party_counts,
        pre_evolution=pre_evolution,
        primary_slot=primary_slot,
        peer_slot=peer_slot,
    )
    assert offered.get(0) == primary_slot, (offered, primary_slot)
    assert offered.get(1) == peer_slot, (offered, peer_slot)
    # Reread both owners after the terminal milestone: the exchange has to hold
    # in the state the ROM actually returned to, not only in the copy snapshot.
    final_records = await _all_records(pair)
    copy_digests = [_digests(_records_from_payload(payload)) for payload in swap_records]
    final_digests = [_digests(_records_from_payload(payload)) for payload in final_records]
    assert final_digests == copy_digests, (
        "a party changed between the record copy and the terminal milestone",
        copy_digests,
        final_digests,
    )
    # The append marker must still be present in the terminal state; a session
    # that lost (or never latched) the ROM's own copy event is not accepted even
    # if the digests happen to agree.
    final_received = await _trade_received_counts(pair)
    assert all(count >= 1 for count in final_received), (final_received, received)
    _log(
        "offers",
        f"primary={primary_slot} peer={peer_slot} observed={offered} "
        f"submenu_back_outs={back_outs} trade_received={final_received}",
    )
    return TradeRun(
        final_records=final_records,
        copy_records=swap_records,
        copy_states=swap_states,
        final_states=final_states,
        post_frames=post_frames,
        evolution=evolution,
        offered=offered,
        back_outs=back_outs,
        received=final_received,
    )


async def _drive_trade_completion(
    pair, *, party_counts, pre_evolution, primary_slot=0, peer_slot=0
):
    """Drive past the record copy to the source-defined trade completion.

    After the copy the ROM runs a 100-frame delay, the trade animation, a
    forced evolution check (``TryEvolvingMon`` → the ``evolution_check`` hook),
    a final serial synchronization, ``SavePartyAndDexData``, and then returns
    to the trade-center selection loop (engine/link/cable_club.asm
    ``.doTrade`` → ``CableClub_DoBattleOrTradeAgain``).  Completion requires
    the later, ROM-owned evolution milestone *and* the restored menu, both
    reachable only by a game that actually ran the full sequence; a game frozen
    at the copy can satisfy neither.
    """
    assert pre_evolution is not None, pre_evolution
    spent = 0
    milestones = []
    while spent < POST_TRADE_BUDGET:
        states = await _all_states(pair)
        evolution = await pair.evolution_counts()
        advanced = all(
            previous is None or current is None or current > previous
            for previous, current in zip(pre_evolution, evolution, strict=True)
        )
        restored = all(
            _selection_loop_ready(state, party_counts[owner]) for owner, state in enumerate(states)
        )
        if advanced and restored:
            _log("trade_completion", f"frames={spent} evolution={evolution}")
            return states, spent, evolution
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}:{evolution[owner]}"
            for owner, state in enumerate(states)
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        # An owner that has already reached the restored selection loop must
        # receive no further input: ``_trade_action`` returns ``"a"`` there and
        # would start a *second* trade, desynchronising it from a peer that is
        # still finishing the first one.  Holding the settled owner still lets
        # its peer arrive at the same milestone.
        actions = [
            None
            if _selection_loop_ready(state, party_counts[owner])
            else _trade_action(state, party_counts[owner], slot)
            for owner, (state, slot) in enumerate(
                zip(states, (primary_slot, peer_slot), strict=True)
            )
        ]
        await _press_actions(pair, actions[0], actions[1], duration=4)
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _all_states(pair)
    evolution = await pair.evolution_counts()
    raise AssertionError(
        "trade never reached the post-copy completion milestone within "
        f"{POST_TRADE_BUDGET} frames; evolution_events={evolution} "
        f"(pre={pre_evolution}) states={json.dumps(states)} "
        f"milestones={json.dumps(milestones)}"
    )


async def _enter_trade_flow(pair, *, arm_barrier=None):
    """Drive the public flow from the loaded fixture into the Trade Center.

    ``arm_barrier`` is forwarded to :meth:`TcpPair.link_up` so a caller can
    override the pair's own pacing decision (the negative control does).
    """
    await pair.link_up(arm_barrier=arm_barrier)
    await _drive_to_link_menu(pair)
    await _select_trade_center(pair)
    await _walk_to_trade_trigger(pair)


async def _await_party_menu(pair, party_counts, *, budget=TRADE_BUDGET):
    """Advance until both owners publish the live trade party menu.

    An owner that already publishes the live menu receives no further input, and
    an owner that was driven into the STATS/TRADE sub-menu by an input aimed at
    the preceding dialogue is backed out of it (the ROM's ``B`` return key)
    instead of being pressed on: ``_trade_action`` would confirm the offer from
    the sub-menu, which leaves the state the caller is waiting for.  Because the
    two owners reach the menu a few frames apart, pressing both keeps them
    alternating between the menu and the sub-menu and the predicate never holds
    for the pair; holding the settled owner still lets its peer catch up.
    """
    spent = 0
    back_outs = 0
    while spent < budget:
        states = await _all_states(pair)
        if all(
            _player_mon_menu_live(state, party_counts[owner]) for owner, state in enumerate(states)
        ):
            _log("party_menu", f"frames={spent} submenu_back_outs={back_outs}")
            return states, spent
        actions = []
        for owner, state in enumerate(states):
            action = _approach_action(state, party_counts[owner])
            if action is not None and action[0] == "b":
                back_outs += 1
            actions.append(action)
        await _press_actions(pair, actions[0], actions[1])
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"trade party menu never became live within {budget} frames; states={json.dumps(states)}"
    )


async def _enter_stats_trade_submenu(pair, party_count):
    """Advance from the live party menu into each owner's STATS/TRADE sub-menu."""
    spent = 0
    while spent < 800:
        states = await _all_states(pair)
        opened = [
            _menu(state)["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS) for state in states
        ]
        if all(opened):
            _log("submenu", f"frames={spent}")
            return states, spent
        # Press A only on an owner whose live party menu is up and which has not
        # opened the sub-menu yet: A on the TRADE entry would confirm the offer,
        # so a blind mash would commit instead of cancelling.
        for owner, state in enumerate(states):
            if not opened[owner] and _player_mon_menu_live(state, party_count):
                await pair.press(owner, "a", duration=4)
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"STATS/TRADE sub-menu never opened on both owners; states={json.dumps(states)}"
    )


async def _back_out_of_submenu(pair, party_count):
    """Press B on both owners and require the live party menu to return."""
    await _press_both(pair, "b", duration=4)
    return await _advance_until(
        pair,
        lambda state: _player_mon_menu_live(state, party_count),
        budget=400,
        prompt="B did not return both owners to the trade party menu",
    )


async def _drive_to_commitment(
    pair, *, party_counts, primary_before, peer_before, primary_slot=0, peer_slot=0
):
    """Drive the pair to the first observed committed record copy, then stop.

    The caller interrupts *here* on purpose: the ROM has confirmed both offers
    and appended each received record, but the trade animation, the forced
    evolution check, the save and the room return are all still outstanding, so
    the disconnect happens with committed work in flight.  Returns the records,
    states, ROM-owned copy counts and frames spent at that observation.
    """
    expected_primary = _compaction_expected_digests(
        primary_before, primary_slot, peer_before[peer_slot]["digest"]
    )
    expected_peer = _compaction_expected_digests(
        peer_before, peer_slot, primary_before[primary_slot]["digest"]
    )
    targets = (primary_slot, peer_slot)
    offered: dict[int, int] = {}
    spent = 0
    received: list[int] = []
    while spent < TRADE_BUDGET:
        records = await _all_records(pair)
        received = await _trade_received_counts(pair)
        if all(count >= 1 for count in received):
            primary_after = _records_from_payload(records[0])
            peer_after = _records_from_payload(records[1])
            if (
                _digests(primary_after) == expected_primary
                and _digests(peer_after) == expected_peer
            ):
                states = await _all_states(pair)
                _log(
                    "trade_copy",
                    f"frames={spent} trade_received={received} interrupted_at_commitment",
                )
                assert offered == {0: primary_slot, 1: peer_slot}, offered
                return records, states, received, spent
        states = await _all_states(pair)
        actions = []
        for owner, state in enumerate(states):
            action, observation = await _owner_trade_action(
                pair,
                owner,
                state,
                party_count=party_counts[owner],
                target_slot=targets[owner],
                confirmed=owner in offered,
            )
            if observation is not None:
                offered[owner] = observation
            actions.append(action)
        await _press_actions(pair, actions[0], actions[1])
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    raise AssertionError(
        "trade never reached an observed committed copy within "
        f"{TRADE_BUDGET} frames; trade_received={received} offered={offered}"
    )
