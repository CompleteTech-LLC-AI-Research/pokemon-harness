"""Asset-free admission controls for the new captured versioned scenario."""

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts import normal_red_battle_drive as drive
from scripts import normal_red_battle_return as battle_return
from scripts import normal_red_link_admission as admission
from scripts.normal_red_battle_drive import PairBattleClient, available_damaging_slot
from scripts.normal_red_link_journal import JournalPair
from scripts.validate_fixture_manifest import _validate_schema


def test_new_capture_has_strict_metadata_and_preserves_legacy_manifest():
    document = json.loads(
        (admission.ROOT / "release-evidence/scenarios" / f"{admission.SCENARIO}.json").read_text()
    )
    rows = _validate_schema(
        {
            "manifest_id": "pokered-harness.external-link-fixtures",
            "manifest_version": 1,
            "fixture_root": "operator-managed",
            "asset_policy": document["asset_policy"],
            "fixtures": [document["fixture"]],
        }
    )
    assert len(rows) == 1
    assert rows[0]["provenance"]["controller_ram_writes"] is False
    assert rows[0]["provenance"]["fresh_boot"] is False
    assert rows[0]["sha1"] == "79a9d30c0a49220e4501ef861fdc1b3d334e1b6c"
    legacy = json.loads((admission.ROOT / "release-evidence/fixture-manifest.json").read_text())
    assert admission.SCENARIO not in {row["id"] for row in legacy["fixtures"]}


@pytest.mark.parametrize("contents", [b"", b"wrong fixture", b"x" * 200548])
def test_wrong_or_missing_state_fails_before_rom_read(tmp_path, contents):
    path = tmp_path / "red" / f"{admission.SCENARIO}.state"
    path.parent.mkdir()
    path.write_bytes(contents)
    with pytest.raises(ValueError, match="size|SHA"):
        admission.resolve_normal_red_assets(Path("missing.gb"), Path("missing.sym"), tmp_path)


def test_missing_fixture_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        admission.resolve_normal_red_assets(Path("missing.gb"), Path("missing.sym"), tmp_path)


def test_present_but_wrong_rom_fails_even_after_fixture_validation(tmp_path, monkeypatch):
    # Only the expensive pinned state validation is replaced here. The real ROM
    # hash check must still reject bytes whose filename matches the pin.
    monkeypatch.setattr(admission, "_validate_assets", lambda rows, root: None)
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    rom.write_bytes(b"wrong ROM")
    sym.write_bytes(b"wrong symbols")
    assert hashlib.sha1(rom.read_bytes()).hexdigest() != "e1deed63080bc24cad5fba18ecb3184f905d16d4"
    with pytest.raises(ValueError, match="mismatch"):
        admission.resolve_normal_red_assets(rom, sym, tmp_path)


@pytest.mark.parametrize(
    ("moves", "pp", "expected"),
    [
        ([33, 45, 73, 22], [0, 40, 10, 10], 3),
        ([33, 45], [35, 40], 0),
        ([45, 73], [40, 10], None),
        ([33], [0x40], None),
        ([33], [0x41], 0),
    ],
)
def test_battle_slot_requires_available_natural_damage_move(moves, pp, expected):
    state = {"party": {"active_mon": {"moves": moves, "pp": pp}}}
    assert available_damaging_slot(state) == expected


@pytest.mark.asyncio
async def test_battle_adapter_uses_owner_public_operations_only():
    calls = []

    class Pair:
        async def press(self, owner, button, *, duration):
            calls.append((owner, button, duration))

        async def step(self, count):
            calls.append(("step", count))

        async def state(self, owner):
            return {"owner": owner}

    client = PairBattleClient(Pair())
    await client.tool("link_peer_press", {"button": "a", "duration": 4})
    await client.tool("link_step", {"count": 10})
    assert calls == [(1, "a", 4), ("step", 10)]
    result = await client.request("resources/read", {"uri": "pokered://peer-game-state"})
    assert json.loads(result["contents"][0]["text"]) == {"owner": 1}
    with pytest.raises(ValueError, match="undocumented"):
        await client.tool("write_memory", {})


@pytest.mark.asyncio
async def test_operation_journal_retains_failed_intent_without_fake_completion(tmp_path):
    class Pair:
        async def step(self, frames):
            raise RuntimeError("real call failed")

    path = tmp_path / "operations.jsonl"
    with path.open("x") as stream:
        pair = JournalPair(Pair(), stream)
        with pytest.raises(RuntimeError, match="real call failed"):
            await pair.step(4)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["event"] == "intent"
    assert rows[0]["operation"] == "step" and rows[0]["args"] == [4]


def battle_snapshot(*, hp=5, terminal=False, replacement=False, valid=True):
    return {
        "battle": {
            "raw_is_in_battle": 0 if terminal else 2,
            "kind": 0 if terminal else 2,
            "phase": 5 if terminal else 1,
            "phase_valid": valid,
            "raw_battle_result": 1,
            "terminal_result": 1,
            "phase_evidence": ["wIsInBattle", "wBattleResult"],
        },
        "party": {
            "active_mon": {"hp": hp, "moves": [33], "pp": [35]},
            "mons": [{"hp": hp}, {"hp": 15}],
        },
        "menu": {"current_item": 0, "max_item": 1, "watched_keys": 3 if replacement else 17},
    }


async def exercise_battle(monkeypatch, states, *, budget=32):
    async def no_op(client):
        pass

    for name in ("_drive_to_link_menu", "_select_colosseum", "_enter_battle"):
        monkeypatch.setattr(drive, name, no_op)

    class Pair:
        def __init__(self):
            self.index = 0

        async def link_up(self, *, arm_barrier=None):
            assert arm_barrier is True

        async def state(self, owner):
            return states[min(self.index, len(states) - 1)]

        async def press(self, owner, button, *, duration):
            assert button in ("a", "up", "down")

        async def step(self, count):
            self.index += 1

    return await drive.complete_battle(Pair(), io.StringIO(), budget_frames=budget)


@pytest.mark.asyncio
async def test_full_battle_requires_nonterminal_replacement_and_terminal_edges(monkeypatch):
    states = [
        battle_snapshot(),
        battle_snapshot(hp=0, replacement=True),
        battle_snapshot(hp=15),
        battle_snapshot(terminal=True),
    ]
    result = await exercise_battle(monkeypatch, states)
    assert result["replacement_completed"] == [True, True]
    assert all(result["terminal"])


@pytest.mark.asyncio
async def test_terminal_battle_without_nonterminal_replacement_is_rejected(monkeypatch):
    with pytest.raises(AssertionError, match="no witnessed"):
        await exercise_battle(monkeypatch, [battle_snapshot(), battle_snapshot(terminal=True)])


@pytest.mark.asyncio
async def test_battle_menu_smoke_never_satisfies_completed_battle(monkeypatch):
    with pytest.raises(AssertionError, match="battle frame bound"):
        await exercise_battle(monkeypatch, [battle_snapshot()], budget=16)


@pytest.mark.asyncio
async def test_invalid_terminal_observation_is_rejected(monkeypatch):
    with pytest.raises(AssertionError):
        await exercise_battle(
            monkeypatch,
            [
                battle_snapshot(),
                battle_snapshot(hp=0, replacement=True),
                battle_snapshot(hp=15),
                battle_snapshot(terminal=True, valid=False),
            ],
        )


@pytest.mark.asyncio
async def test_journal_positive_completion_redacts_raw_state_bytes():
    class Pair:
        async def load_fixture(self, asset, *, owner):
            return {"ok": True}

    stream = io.StringIO()
    pair = JournalPair(Pair(), stream)
    await pair.load_fixture({"state": b"operator state"}, owner=0)
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == ["intent", "completion"]
    assert rows[0]["sequence"] == rows[1]["sequence"] == 1
    assert rows[0]["args"][0]["state"] == {
        "size": 14,
        "sha256": hashlib.sha256(b"operator state").hexdigest(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("map_id", "maximum"), [(239, 2), (64, 1)])
async def test_colosseum_vote_declines_warped_or_stale_menu(monkeypatch, map_id, maximum):
    calls = []

    class Pair:
        async def release_buttons(self):
            calls.append("release")

        async def state(self, owner):
            return {
                "overworld": {"map_id": map_id},
                "menu": {"current_item": 0, "max_item": maximum, "watched_keys": 3},
            }

        async def press(self, *args, **kwargs):
            calls.append("unsafe input")

    with pytest.raises(AssertionError, match="warp|geometry"):
        await drive._select_colosseum(PairBattleClient(Pair()))
    assert calls == ["release"]


@pytest.mark.asyncio
async def test_colosseum_vote_requires_both_observed_cursors_before_a():
    calls = []

    class Pair:
        def __init__(self):
            self.pressed = 0

        async def release_buttons(self):
            calls.append("release")

        async def state(self, owner):
            return {
                "overworld": {"map_id": 240 if self.pressed == 2 else 64},
                "menu": {"current_item": 1, "max_item": 2, "watched_keys": 3},
            }

        async def press(self, owner, button, *, duration):
            assert button == "a"
            self.pressed += 1
            calls.append((owner, button))

        async def step(self, count):
            calls.append(("step", count))

    result = await drive._select_colosseum(PairBattleClient(Pair()))
    assert calls == ["release", (0, "a"), ("step", 4), (1, "a"), ("step", 16)]
    assert all(state["overworld"]["map_id"] == 240 for state in result)


@pytest.mark.asyncio
async def test_lost_colosseum_confirmation_retries_only_selected_menu():
    class Pair:
        def __init__(self):
            self.confirmed = [False, False]
            self.calls = []

        async def release_buttons(self):
            pass

        async def state(self, owner):
            return {
                "overworld": {"map_id": 240 if self.confirmed[owner] else 64},
                "menu": {"current_item": 1, "max_item": 2, "watched_keys": 3},
            }

        async def press(self, owner, button, *, duration):
            assert not self.confirmed[owner], "input after room transition"
            assert button == "a"
            self.calls.append((owner, duration))
            if duration == 1:
                self.confirmed[owner] = True

        async def step(self, count):
            pass

    pair = Pair()
    result = await drive._select_colosseum(PairBattleClient(pair))
    assert pair.calls == [(0, 4), (1, 4), (0, 1), (1, 1)]
    assert all(state["overworld"]["map_id"] == 240 for state in result)


@pytest.mark.asyncio
async def test_dialogue_stops_a_at_fresh_save_choice_then_waits_passively():
    calls = []

    class Pair:
        def __init__(self):
            self.fresh = [False, False]
            self.confirmed = [False, False]

        async def rom_events(self, owner):
            return [{"name": "yes_no_prompt"}] if self.fresh[owner] else []

        async def state(self, owner):
            return {
                "overworld": {"map_id": 64},
                "menu": {
                    "current_item": 0,
                    "max_item": 2 if all(self.confirmed) else 1,
                    "watched_keys": 3,
                },
            }

        async def press(self, owner, button, *, duration):
            calls.append((owner, button, duration))
            if button == "a" and duration == 1:
                self.fresh[owner] = True
            elif button == "a" and duration == 4:
                assert all(self.fresh)
                self.confirmed[owner] = True

        async def step(self, count):
            pass

        async def release_buttons(self):
            pass

    result = await drive._drive_to_link_menu(PairBattleClient(Pair()))
    assert [(owner, duration) for owner, button, duration in calls if button == "a"] == [
        (0, 1),
        (1, 1),
        (0, 4),
        (1, 4),
    ]
    assert all(state["menu"]["max_item"] == 2 for state in result)


@pytest.mark.asyncio
async def test_lost_early_confirmation_retries_until_fresh_post_save_text_only():
    calls = []

    class Pair:
        def __init__(self):
            self.fresh = [False, False]
            self.early = [False, False]
            self.saved = [False, False]

        async def rom_events(self, owner):
            rows = [{"name": "yes_no_prompt"}] if self.fresh[owner] else []
            return rows + ([{"name": "text_shown"}] if self.saved[owner] else [])

        async def state(self, owner):
            return {
                "overworld": {"map_id": 64},
                "menu": {
                    "current_item": 0,
                    "max_item": 2 if all(self.saved) else 1,
                    "watched_keys": 3,
                },
            }

        async def press(self, owner, button, *, duration):
            if button != "a":
                return
            calls.append((owner, duration))
            assert not self.saved[owner], "input injected after post-save text"
            if duration == 4:
                self.early[owner] = True  # Deliberately lost during menu draw.
            elif self.early[owner]:
                self.saved[owner] = True
            else:
                self.fresh[owner] = True

        async def step(self, count):
            pass

        async def release_buttons(self):
            pass

    await drive._drive_to_link_menu(PairBattleClient(Pair()))
    assert calls == [(0, 1), (1, 1), (0, 4), (1, 4), (0, 1), (1, 1)]


def returned_snapshot(owner, *, result=0):
    return {
        "overworld": {"map_id": 240, "x": 3 if owner == 0 else 6, "y": 4, "walk_counter": 0},
        "battle": {"raw_is_in_battle": 0, "kind": 0, "raw_battle_result": result},
        "bag": {"valid": True, "stacks": [{"item_id": 4, "quantity": 5}]},
        "progress": {"money": 100},
        "party": {"mons": [
            {"valid": True, "hp": 20, "max_hp": 20, "status": {"raw": 0}, "pp": [35, 0, 0, 0]}
            for _ in range(6)
        ]},
    }


def return_case():
    states = [returned_snapshot(0), returned_snapshot(1, result=1)]
    records = [{"digest": f"{index:064x}"} for index in range(6)]
    last_live = copy.deepcopy(states)
    for state in last_live:
        state["battle"]["raw_is_in_battle"] = 2
    for mon in last_live[1]["party"]["mons"]:
        mon["hp"] = 0
    return states, records, last_live


@pytest.mark.parametrize("winner", [0, 1])
def test_pinned_red_complementary_results_classify_zero_as_win(winner):
    states, _, live = return_case()
    if winner == 1:
        states.reverse()
        live.reverse()
    assert battle_return.complementary_results(states, live) == {
        "winner": winner, "loser": 1 - winner, "raw_results": [0, 1] if winner == 0 else [1, 0],
    }


@pytest.mark.parametrize("results", [(0, 0), (1, 1), (2, 2), (False, 1), (None, 1)])
def test_unknown_noncomplementary_or_draw_results_do_not_qualify(results):
    states, _, live = return_case()
    for state, result in zip(states, results, strict=True):
        state["battle"]["raw_battle_result"] = result
    with pytest.raises(AssertionError):
        battle_return.complementary_results(states, live)


def test_partial_party_or_forfeit_is_not_exhaustion_match():
    states, _, live = return_case()
    live[1]["party"]["mons"][5]["hp"] = 1
    with pytest.raises(AssertionError, match="naturally exhausted"):
        battle_return.complementary_results(states, live)


def test_both_exhausted_parties_require_a_separate_tie_oracle():
    states, _, live = return_case()
    for mon in live[0]["party"]["mons"]:
        mon["hp"] = 0
    with pytest.raises(AssertionError, match="simultaneous exhaustion"):
        battle_return.complementary_results(states, live)


def test_cached_terminal_snapshot_cannot_replace_live_match_history():
    states, _, live = return_case()
    live[0]["battle"]["raw_is_in_battle"] = 0
    with pytest.raises(AssertionError, match="live battle history"):
        battle_return.complementary_results(states, live)


@pytest.mark.parametrize("field", ["hp", "status", "pp", "bag", "money", "records", "map", "battle"])
def test_restoration_requires_every_gameplay_component(field):
    states, records, _ = return_case()
    before, after = states[0], copy.deepcopy(states[0])
    after_records = {"records": copy.deepcopy(records)}
    if field == "hp":
        after["party"]["mons"][0]["hp"] = 1
    elif field == "status":
        after["party"]["mons"][0]["status"]["raw"] = 8
    elif field == "pp":
        after["party"]["mons"][0]["pp"][0] = 34
    elif field == "bag":
        after["bag"]["stacks"][0]["quantity"] = 4
    elif field == "money":
        after["progress"]["money"] = 99
    elif field == "records":
        after_records["records"][0]["digest"] = "f" * 64
    elif field == "map":
        after["overworld"]["map_id"] = 64
    else:
        after["battle"]["raw_is_in_battle"] = 2
    with pytest.raises(AssertionError):
        battle_return.assert_restored(before, after, [row["digest"] for row in records], after_records)


@pytest.mark.asyncio
@pytest.mark.parametrize("moves", [True, False])
async def test_return_requires_actual_new_room_control_after_restoration(moves):
    states, records, live = return_case()

    class Pair:
        def __init__(self):
            self.moving = False
            self.calls = []

        async def state(self, owner):
            state = copy.deepcopy(states[owner])
            if self.moving and moves:
                state["overworld"]["y"] = 5
            return state

        async def records(self, owner):
            return {"records": records}

        async def release_buttons(self):
            self.calls.append("release")

        async def press(self, owner, button, *, duration):
            assert button == "down" and duration in (8, 2)
            self.calls.append(owner)
            self.moving = True

        async def step(self, count):
            pass

    pair = Pair()
    if moves:
        result = await battle_return.finish_battle_return(pair, states, [records, records], states, live)
        assert result["source_defined_result"]["winner"] == 0
        assert pair.calls == ["release", 0, 1, "release"]
    else:
        with pytest.raises(AssertionError, match="ordinary movement"):
            await battle_return.finish_battle_return(pair, states, [records, records], states, live)
