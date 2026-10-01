"""Execution-backed asset-free producer controls; no ROM or gameplay qualification."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace as NS

import pytest

from scripts import produce_normal_club_journey as club
from tests.test_normal_journey_capture import (
    prepared as prepared,  # noqa: PLC0414 - pytest fixture re-export
)


def mon(**changes):
    fields = {
        "valid": True,
        "hp_valid": True,
        "hp": 40,
        "max_hp": 40,
        "status": NS(raw=0),
        "moves": (33, 45, 73, 22),
        "pp": (0, 40, 10, 4),
    }
    fields.update(changes)
    return NS(**fields)


def battle_journey(monster=None, *, current=1, observed=True, disabled=0, locked=False):
    active = monster or mon()
    state = NS(
        battle=NS(active=True, menu_open=observed, enemy_mon=NS(valid=True, type1=3, type2=3)),
        party=NS(active_mon=active),
        menu=NS(max_item=5, watched_keys=199, current_item=current),
    )
    owner = object.__new__(club.ClubJourney)
    owner.session = NS(
        read_game_state=lambda: state,
        _pyboy=NS(memory={0: 0, 1: disabled}),
        symbols=NS(addr_of=lambda n: {"wMoveMenuType": 0, "wPlayerDisabledMove": 1}[n]),
    )
    owner.overworld_returns = 0
    calls = []

    def press(key, **kwargs):
        calls.append((key, kwargs["note"]))
        if key == "down":
            state.menu.current_item += 1
        elif state.menu.current_item == 0:
            state.menu.current_item = 1
        else:
            state.battle.active = False

    owner.driver = NS(input_locked=lambda: locked, press=press)
    owner.finish_trainer_return = lambda _: calls.append(("returned", "overworld"))
    return owner, calls, state


def test_real_regular_move_selection_skips_exhausted_tackle():
    owner, calls, _ = battle_journey()
    owner.ordinary_trainer_battle()
    assert [key for key, _ in calls] == ["down", "down", "down", "a", "returned"]
    assert "PP-qualified regular move" in calls[-2][1]


def test_no_pp_zero_index_dialog_advances_once_then_available_move():
    owner, calls, _ = battle_journey(current=0)
    owner.ordinary_trainer_battle()
    assert [key for key, _ in calls] == ["a", "down", "down", "down", "a", "returned"]


def test_disabled_available_move_refuses_before_input():
    owner, calls, _ = battle_journey(disabled=4)
    with pytest.raises(club.foundation.CaptureRefused, match="PP exhausted"):
        owner.ordinary_trainer_battle()
    assert calls == []


def test_unavailable_public_observation_refuses_before_input():
    owner, calls, _ = battle_journey(observed=None)
    with pytest.raises(club.foundation.CaptureRefused, match="observation unavailable"):
        owner.ordinary_trainer_battle()
    assert calls == []


@pytest.mark.parametrize("change", [{"max_item": 4}, {"watched_keys": 3}])
def test_nonregular_menu_never_uses_move_cursor(change):
    owner, calls, state = battle_journey()
    for name, value in change.items():
        setattr(state.menu, name, value)
    owner.ordinary_trainer_battle()
    assert [key for key, _ in calls] == ["a", "returned"]
    assert "advance outcome" in calls[0][1]


def test_cave_pair_collision_detours_instead_of_blocked_left():
    # Authored tiny map replicates observed0x20→0x05 blocked transition.
    walk = [[True] * 3 for _ in range(2)]
    grid = [[0] * 6 for _ in range(4)]
    for x, tile in enumerate((5, 32, 32)):
        grid[1][x * 2] = tile
    for x in range(3):
        grid[3][x * 2] = 32
    assert club.tiles.astar(walk, (1, 0), (0, 0), tile_grid=grid) == "l"
    assert (
        club.tiles.astar(
            walk, (1, 0), (0, 0), tile_grid=grid, pair_collisions=club.tiles._PAIR_COLLISIONS[17]
        )
        is None
    )


def test_navigation_passes_real_tileset_pair_rules(monkeypatch):
    owner = object.__new__(club.ClubJourney)
    state = NS(battle=NS(active=False), overworld=NS(map_id=59, x=1, y=0))
    owner.session = NS(
        read_game_state=lambda: state,
        _pyboy=NS(memory={}),
        symbols=NS(
            read_u8=lambda _, name: {"wCurMapWidth": 2, "wCurMapHeight": 1, "wCurMapTileset": 17}[
                name
            ]
        ),
    )
    owner.game_source = None
    owner.driver = NS(input_locked=lambda: False, press=lambda *_a, **_k: None)
    monkeypatch.setattr(club.tiles, "read_overworld_map", lambda *_: None)
    monkeypatch.setattr(club.tiles, "load_blockset", lambda *_: None)
    monkeypatch.setattr(club.tiles, "expand_to_tile_grid", lambda *_: [[5] * 8 for _ in range(4)])
    monkeypatch.setattr(club.tiles, "read_passable_tiles", lambda *_: {5})
    monkeypatch.setattr(club.tiles, "read_sprite_blockers", lambda *_: set())
    seen = []

    def astar(*_args, **kwargs):
        seen.append(kwargs["pair_collisions"])

    monkeypatch.setattr(club.tiles, "astar", astar)
    with pytest.raises(club.foundation.CaptureRefused, match="no readable"):
        owner._tile_navigate((0, 0), 59)
    assert seen == [club.tiles._PAIR_COLLISIONS[17]]


def parent_files(tmp_path):
    state = tmp_path / "ordinary_six_party_captured.state"
    state.write_bytes(b"authored-parent-not-ROM")
    raw = state.read_bytes()
    one = hashlib.sha1(raw).hexdigest()
    two = hashlib.sha256(raw).hexdigest()
    receipt = tmp_path / "parent.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "scouted",
                "session_closed_without_save": True,
                "checkpoints": [{"name": state.stem, "size": len(raw), "sha1": one, "sha256": two}],
            }
        )
    )
    return (
        state,
        receipt,
        {
            "state_sha1": one,
            "state_sha256": two,
            "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        },
    )


@pytest.mark.parametrize("which", ("state", "receipt", "sha1", "sha256", "receipt_pin"))
def test_parent_pin_tampering_refuses(tmp_path, which):
    state, receipt, pins = parent_files(tmp_path)
    if which == "state":
        state.write_bytes(b"changed")
    elif which == "receipt":
        receipt.write_bytes(b"changed")
    else:
        pins[
            {"sha1": "state_sha1", "sha256": "state_sha256", "receipt_pin": "receipt_sha256"}[which]
        ] = "bad-pin"
    with pytest.raises(club.foundation.CaptureRefused, match="pins"):
        club.verify_parent(state, receipt, **pins)


def test_parent_exact_checkpoint_and_closed_receipt_required(tmp_path):
    state, receipt, pins = parent_files(tmp_path)
    parent = json.loads(receipt.read_text())
    parent["session_closed_without_save"] = False
    receipt.write_text(json.dumps(parent))
    pins["receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises(club.foundation.CaptureRefused, match="closed parent"):
        club.verify_parent(state, receipt, **pins)


def test_consulted_source_mutation_refuses(tmp_path, monkeypatch):
    raw = b"authored-source-oracle"
    (tmp_path / "oracle.asm").write_bytes(raw)
    monkeypatch.setattr(club, "ORACLE_SHA256", {"oracle.asm": hashlib.sha256(raw).hexdigest()})
    club.verify_oracles(tmp_path)
    (tmp_path / "oracle.asm").write_bytes(b"changed")
    with pytest.raises(club.foundation.CaptureRefused, match="pret source"):
        club.verify_oracles(tmp_path)


@pytest.mark.parametrize(
    "change", ("fainted", "invalid", "no_pp", "unhealed", "status", "count", "sentinel")
)
def test_six_party_guard_refuses_known_bad_records(change):
    mons = [mon(pp=(35, 40, 10, 10)) for _ in range(6)]
    party = NS(valid=True, sentinel_valid=True, mons=mons)
    if change == "fainted":
        mons[2].hp = 0
    elif change == "invalid":
        mons[2].valid = False
    elif change == "no_pp":
        mons[2].pp = (0, 0, 0, 0)
    elif change == "unhealed":
        mons[2].hp = 20
    elif change == "status":
        mons[2].status.raw = 8
    elif change == "count":
        party.mons = mons[:5]
    else:
        party.sentinel_valid = False
    with pytest.raises(club.foundation.CaptureRefused):
        club.require_six_party(NS(party=party), healed=True)


@pytest.mark.parametrize("failure", ("false", "raises", "route", "close", "none"))
def test_capture_lifecycle_closes_and_retains_failure(prepared, tmp_path, monkeypatch, failure):
    rom, sym, out, game, session, launches = prepared
    state, receipt, pins = parent_files(tmp_path)
    session.load_state = lambda raw: session.calls.append(("load", raw))
    session.symbols = NS(bank_addr=lambda _: (0, 0))
    session._pyboy = NS(hook_register=lambda *_: None)
    monkeypatch.setattr(club, "verify_oracles", lambda _: None)
    monkeypatch.setattr(club, "committed_source_identity", lambda: "authored-test-commit")
    if failure == "false":
        session.enable_battle_menu_observation = lambda: False
    elif failure == "raises":

        def raises():
            raise RuntimeError("initializer cause")

        session.enable_battle_menu_observation = raises
    elif failure == "close":
        session.close_error = RuntimeError("close cause")

    def run(owner):
        if failure == "route":
            raise club.foundation.CaptureRefused("route cause")
        owner.receipt["authored_test_route_only"] = True

    monkeypatch.setattr(club, "run_club_route", run)
    if failure == "none":
        result = club.capture(rom, sym, out, game, state, receipt, **pins)
        assert result["status"] == "captured" and result["authored_test_route_only"] is True
    else:
        expected = {
            "false": (club.foundation.CaptureRefused, "public battle observation"),
            "raises": (RuntimeError, "initializer cause"),
            "route": (club.foundation.CaptureRefused, "route cause"),
            "close": (RuntimeError, "close cause"),
        }[failure]
        with pytest.raises(expected[0], match=expected[1]):
            club.capture(rom, sym, out, game, state, receipt, **pins)
    stored = json.loads((out / "receipt.json").read_text())
    assert stored["status"] == ("captured" if failure == "none" else "failed")
    assert stored["session_closed_without_save"] is (failure != "close")
    assert ("close", False) in session.calls
    assert launches and not any(c[0] == "press" for c in session.calls)


def authored_route(*, event_bits=130, bag=True, script=0, misplaced_final=False):
    # Both guardian (bit1) and Helix (bit7) use authored event byte175.
    party = NS(valid=True, sentinel_valid=True, mons=[mon(pp=(35, 40, 10, 10)) for _ in range(6)])
    state = NS(
        party=party,
        battle=NS(active=False),
        overworld=NS(map_id=13, x=7, y=3),
        menu=NS(watched_keys=3, max_item=1, current_item=0),
        bag=NS(has_item=lambda _: bag),
    )
    calls = []
    session = NS(
        read_game_state=lambda: state,
        _pyboy=NS(memory={175: event_bits, 1000: script}),
        symbols=NS(addr_of=lambda name: 0 if name == "wEventFlags" else 1000),
    )

    def navigate(goal, expected_map, transition=None, **_kwargs):
        state.overworld.map_id = transition if transition is not None else expected_map
        state.overworld.x, state.overworld.y = goal
        if misplaced_final and goal == (11, 3):
            state.overworld.x = 10

    def heal(city, _center, _door):
        state.overworld.map_id = city

    driver = NS(
        phase=None,
        idle=lambda *_a, **_kw: None,
        press=lambda key, **kw: calls.append((key, kw["note"])),
    )
    checkpoints = []
    owner = NS(
        session=session,
        driver=driver,
        navigate=navigate,
        normal_center_heal=heal,
        checkpoint=checkpoints.append,
        receipt={},
        ordinary_trainer_battle=lambda: None,
    )
    return owner, calls, checkpoints


def test_complete_authored_route_requires_exact_final_shape():
    owner, _calls, checkpoints = authored_route()
    club.run_club_route(owner)
    assert checkpoints[-1] == "ordinary_cerulean_six_living_club_ready"
    assert owner.receipt["cerulean_six_party_club_position"] is True


def test_missing_guardian_event_never_claims_fossil_or_exit():
    owner, calls, checkpoints = authored_route(event_bits=128)
    with pytest.raises(club.foundation.CaptureRefused, match="guardian victory event"):
        club.run_club_route(owner)
    assert len(calls) == 120
    assert "ordinary_mt_moon_guardian_beaten" not in checkpoints
    assert "cerulean_six_party_club_position" not in owner.receipt


@pytest.mark.parametrize("bits,bag,script", [(2, True, 0), (130, False, 0), (130, True, 4)])
def test_helix_requires_item_event_and_finished_script(bits, bag, script):
    owner, calls, checkpoints = authored_route(event_bits=bits, bag=bag, script=script)
    with pytest.raises(club.foundation.CaptureRefused, match="Helix item/event/script"):
        club.run_club_route(owner)
    assert len(calls) == 81  # One source-facing input plus exactly80bounded dialog inputs.
    assert "ordinary_helix_fossil_received" not in checkpoints
    assert "cerulean_six_party_club_position" not in owner.receipt


def test_wrong_final_position_cannot_be_captured():
    owner, _calls, checkpoints = authored_route(misplaced_final=True)
    with pytest.raises(club.foundation.CaptureRefused, match="starting point"):
        club.run_club_route(owner)
    assert "ordinary_cerulean_six_living_club_ready" not in checkpoints


@pytest.mark.parametrize(
    "mismatch",
    (
        None,
        "produce_normal_club_journey.py",
        "produce_normal_journey.py",
        "produce_normal_foundation.py",
        "path_from_tiles.py",
    ),
)
def test_committed_source_proof_compares_every_loaded_controller(monkeypatch, mismatch):
    root = club.Path(club.__file__).resolve().parents[1]

    def git(command, timeout):
        assert timeout == 10
        if command[-2:] == ["rev-parse", "HEAD"]:
            return b"authored-commit\n"
        relative = command[-1].split(":", 1)[1]
        raw = (root / relative).read_bytes()
        return b"changed-committed-bytes" if relative.endswith(str(mismatch)) else raw

    monkeypatch.setattr(club.subprocess, "check_output", git)
    if mismatch is None:
        assert club.committed_source_identity() == "authored-commit"
    else:
        with pytest.raises(club.foundation.CaptureRefused, match="differs from committed"):
            club.committed_source_identity()


def test_committed_source_timeout_refuses(monkeypatch):
    def timeout(*args, **kwargs):
        raise club.subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(club.subprocess, "check_output", timeout)
    with pytest.raises(club.foundation.CaptureRefused, match="identity unavailable"):
        club.committed_source_identity()


@pytest.mark.parametrize("map_id,x,y", [(13, 7, 2), (13, 8, 3), (12, 7, 3)])
def test_parent_geometry_guard_matches_actual_pinned_readback(map_id, x, y):
    owner, calls, checkpoints = authored_route()
    state = owner.session.read_game_state()
    state.overworld.map_id, state.overworld.x, state.overworld.y = map_id, x, y
    with pytest.raises(club.foundation.CaptureRefused, match="checkpoint13,7,3"):
        club.run_club_route(owner)
    assert calls == [] and checkpoints == []
