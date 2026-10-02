"""Pinned Red result, healing, room return and public control acceptance.

EndOfBattle prints WIN for raw result 0, LOSE for 1 and DRAW for 2. The
public observation's raw-zero ambiguity is retained; this scenario oracle
requires party exhaustion and complementary 0/1 before classifying a winner.
CableClub_DoBattleOrTrade then calls HealParty and ReturnToCableClubRoom.
Colosseum has no map warps: actual room return remains map 240.
"""

from __future__ import annotations

from scripts.normal_red_link_journal import mark_phase
from tests._mcp_trade_records_rom_drivers_support import _digests, _records_from_payload


def complementary_results(terminals, last_live):
    results = [state["battle"]["raw_battle_result"] for state in terminals]
    assert len(results) == 2 and all(type(value) is int for value in results), results
    assert sorted(results) == [0, 1], ("expected complementary Red win/loss", results)
    assert len(last_live) == 2 and all(
        type(state["battle"]["raw_is_in_battle"]) is int
        and state["battle"]["raw_is_in_battle"] in (1, 2)
        for state in last_live
    ), "missing actual live battle history"
    exhausted = last_live[results.index(1)]["party"]["mons"]
    assert len(exhausted) == 6 and all(
        mon["valid"] is True and type(mon["hp"]) is int and mon["hp"] == 0 for mon in exhausted
    ), (
        "losing party was not naturally exhausted",
        exhausted,
    )
    survivor = last_live[results.index(0)]["party"]["mons"]
    assert len(survivor) == 6 and any(
        mon["valid"] is True and type(mon["hp"]) is int and mon["hp"] > 0 for mon in survivor
    ), "unsupported simultaneous exhaustion"
    return {"winner": results.index(0), "loser": results.index(1), "raw_results": results}


def assert_restored(before, after, record_digests, after_records):
    assert after["overworld"]["map_id"] == 240, ("expected returned Colosseum", after)
    assert after["battle"]["raw_is_in_battle"] == 0, after["battle"]
    assert after["battle"]["kind"] == 0, after["battle"]
    assert after["bag"] == before["bag"] and after["bag"]["valid"] is True, (
        "inventory changed or unknown",
        before["bag"],
        after["bag"],
    )
    assert type(before["progress"]["money"]) is int
    assert type(after["progress"]["money"]) is int
    assert after["progress"]["money"] == before["progress"]["money"], "money changed"
    mons, prior = after["party"]["mons"], before["party"]["mons"]
    assert len(mons) == len(prior) == 6
    for restored, original in zip(mons, prior, strict=True):
        assert original["valid"] is True and original["hp"] == original["max_hp"]
        assert original["status"]["raw"] == 0
        assert restored["valid"] is True and restored["hp"] == restored["max_hp"]
        assert restored["status"]["raw"] == 0
        assert restored["pp"] == original["pp"], ("PP not restored", restored, original)
    assert _digests(_records_from_payload(after_records)) == record_digests, (
        "full 44-byte party not restored",
        after_records,
    )


async def finish_battle_return(
    pair, before_states, before_records, terminals, last_live, *, budget_frames=1200
):
    """Wait for healing, then prove new ordinary movement in the returned room.

    Passive progress consumes result text and ROM healing; inputs are issued
    only after both full party records and inventories satisfy restoration.
    A cached terminal observation or cleanup cannot substitute for tile motion.
    """
    outcome = complementary_results(terminals, last_live)
    expected = [_digests(records) for records in before_records]
    restored = None
    for _ in range(budget_frames // 4):
        states = [await pair.state(owner) for owner in range(2)]
        records = [await pair.records(owner) for owner in range(2)]
        try:
            for owner in range(2):
                assert_restored(
                    before_states[owner], states[owner], expected[owner], records[owner]
                )
        except AssertionError:
            await pair.step(4)
            continue
        restored = states
        break
    mark_phase(pair, "restoration_wait_end")
    assert restored is not None, "post-battle restoration/room return frame bound"
    await pair.release_buttons()
    positions = [(state["overworld"]["x"], state["overworld"]["y"]) for state in restored]
    assert positions == [(3, 4), (6, 4)] or positions == [(6, 4), (3, 4)], (
        "unexpected returned room positions",
        positions,
    )
    # The tile directly below each source-defined spawn is clear Club floor.
    for owner in range(2):
        await pair.press(owner, "down", duration=8)
    for attempt in range(100 // 4):
        await pair.step(4)
        states = [await pair.state(owner) for owner in range(2)]
        assert all(state["overworld"]["map_id"] == 240 for state in states), states
        assert all(state["battle"]["raw_is_in_battle"] == 0 for state in states), states
        if all(
            (state["overworld"]["x"], state["overworld"]["y"]) == (positions[owner][0], 5)
            and state["overworld"]["walk_counter"] == 0
            for owner, state in enumerate(states)
        ):
            await pair.release_buttons()
            final_records = [await pair.records(owner) for owner in range(2)]
            for owner in range(2):
                assert_restored(
                    before_states[owner], states[owner], expected[owner], final_records[owner]
                )
            return {
                "source_defined_result": outcome,
                "restored_states": restored,
                "usable_room_states": states,
                "party_restored_full_44_byte_digests": expected,
                "post_match_ordinary_action": "down one tile from returned Colosseum spawns",
            }
        # Healing can complete before the room fade accepts input. Retry a
        # short public pulse only at its original spawn, never after movement.
        if attempt % 2 == 1:
            for owner, state in enumerate(states):
                location = state["overworld"]
                if (
                    (location["x"], location["y"]) == positions[owner]
                    and type(location["walk_counter"]) is int
                    and location["walk_counter"] == 0
                ):
                    await pair.press(owner, "down", duration=2)
    raise AssertionError("returned Colosseum did not accept ordinary movement")
