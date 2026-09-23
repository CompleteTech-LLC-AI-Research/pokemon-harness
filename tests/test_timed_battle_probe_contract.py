"""Schema, provenance, reciprocal adjudication and room-return contracts (#147).

Split from ``tests/test_timed_battle_probe.py`` for #147 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. ROM-free mock contracts only; synthetic snapshots are NOT
gameplay proof.
"""

import pytest

from tests._timed_battle_probe_support import (
    adjudicate,
    change,
    healed_party,
    paired_snapshots,
    record,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("owners", [None, [], {}, [None, None], [{}, {}], [{}, {}, {}]])
def test_malformed_pair_schema_fails_closed(probe, owners):
    result = adjudicate(probe, owners)
    assert result["complete"] is False
    assert result["errors"]


@pytest.mark.parametrize("checkpoint", ["settled-turn", "room-return"])
def test_flags_and_entry_hook_counts_are_never_settlement(probe, checkpoint):
    owners = [
        {
            "role": side,
            "version": version,
            "checkpoint": checkpoint,
            "checkpoint_reached": True,
            "complete": True,
            "turn_settled": True,
            "room_returned": True,
            "readiness": [1] * len(probe.READINESS_PHASES),
            "hook_counts": {
                "ExecutePlayerMove": 100,
                "ExecuteEnemyMove": 100,
                "PlayerCalcMoveDamage": 100,
                "LinkBattleExchangeData": 100,
            },
        }
        for side, version in (("listen", "red_color"), ("connect", "yellow"))
    ]
    result = adjudicate(probe, owners, checkpoint)
    assert result["complete"] is False
    assert result["errors"]


@pytest.mark.parametrize("room", [False, True])
@pytest.mark.parametrize("paralyzed,multi_hit", [(False, False), (True, False), (False, True)])
def test_coherent_synthetic_pair_settles_with_unknown_provenance(probe, room, paralyzed, multi_hit):
    owners = paired_snapshots(room=room, paralyzed=paralyzed, multi_hit=multi_hit)
    result = adjudicate(probe, owners, "room-return" if room else "settled-turn")
    assert result["complete"] is True, result
    assert result["gameplay_completed"] is True
    assert result["errors"] == []


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("move_slot",), False),
        (("move_slot",), 0.0),
        (("role",), "connect"),
        (("baseline",), None),
        (("baseline", "seq"), True),
        (("baseline", "seq"), 100.5),
        (("baseline", "local", "hp"), True),
        (("baseline", "local", "species"), 153),
        (("before_party", "count"), 7),
        (("turn",), None),
        (("turn", "exchange_seq"), 99),
        (("turn", "exchange_seq"), 150.5),
        (("turn", "settled_seq"), 150),
        (("turn", "settled_seq"), 200.5),
        (("turn", "send"), 1),
        (("turn", "receive"), 1),
        (("turn", "local_move_id"), 33),
        (("turn", "local_move_id"), True),
        (("turn", "enemy_move_id"), 1),
        (("turn", "local", "hp"), 69),
        (("turn", "enemy", "hp"), 64),
        (("turn", "local", "status"), 64),
        (("turn", "enemy", "status"), 64),
        (("turn", "actions", "local", "done"), False),
        (("turn", "actions", "enemy", "done"), False),
        (("turn", "pp", "after"), [20, 0, 0, 0]),
        (("turn", "pp", "after"), [18, 0, 0, 0]),
        (("turn", "pp", "after"), [19, 1, 0, 0]),
        (("unsupported_reason",), "lost observation"),
    ],
)
def test_single_field_corruption_cannot_pass_reciprocal_adjudication(probe, path, value):
    owners = paired_snapshots()
    change(owners[0], path, value)
    result = adjudicate(probe, owners)
    assert result["complete"] is False, (path, result)
    assert result["errors"]


def test_enemy_pp_is_not_a_reciprocal_mirror(probe):
    owners = paired_snapshots()
    for owner in owners:
        owner["baseline"]["enemy"]["pp"] = [7, 0, 0, 0]
        owner["turn"]["enemy"]["pp"] = [7, 0, 0, 0]
    assert adjudicate(probe, owners)["complete"] is True


def test_baseline_cannot_substitute_a_different_species_for_ordinary_first_living_member(probe):
    owners = paired_snapshots()
    party = owners[0]["before_party"]
    raw = bytearray.fromhex(party["records"][0])
    party["species"][0] = raw[0] = 100
    party["records"][0] = raw.hex()
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize(
    "path,value",
    [
        (("room_return",), None),
        (("room_return", "return_seq"), 250),
        (("room_return", "settled_seq"), 260),
        (("room_return", "send"), 0),
        (("room_return", "receive"), 0),
        (("room_return", "map"), 239),
        (("room_return", "is_in_battle"), 1),
    ],
)
def test_room_return_requires_post_battle_reciprocal_evidence(probe, path, value):
    owners = paired_snapshots(room=True)
    change(owners[0], path, value)
    assert adjudicate(probe, owners, "room-return")["complete"] is False


@pytest.mark.parametrize("claimed", ["verified", "pristine", "", None])
def test_provenance_claims_cannot_upgrade_mock_gameplay_to_authentic_acceptance(probe, claimed):
    owners = paired_snapshots()
    for owner in owners:
        owner["provenance"] = claimed
        owner["full_authentic_acceptance"] = True
    result = adjudicate(probe, owners)
    assert result["full_authentic_acceptance"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", 1),
        ("move_id", 166),
        ("move_id", True),
        ("effect", 1),
        ("effect", 6),
    ],
)
def test_chosen_metadata_must_match_canonical_move_and_baseline(probe, side, field, value):
    owners = paired_snapshots()
    owners[side]["chosen"][field] = value
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("effect", [1, 6, 44])
def test_forging_both_peers_effect_metadata_cannot_override_canonical_catalog(probe, side, effect):
    owners = paired_snapshots()
    owners[side]["chosen"]["effect"] = effect
    owners[side]["turn"]["local_move_effect"] = effect
    owners[1 - side]["turn"]["enemy_move_effect"] = effect
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("fault", ["missing", "hp", "status", "identity", "max_hp"])
def test_room_return_must_include_actual_healed_party_records(probe, side, fault):
    owners = paired_snapshots(room=True)
    room = owners[side]["room_return"]
    if fault == "missing":
        del room["healed_party"]
    else:
        party = room["healed_party"]
        raw = bytearray.fromhex(party["records"][0])
        if fault == "hp":
            raw[1:3] = (99).to_bytes(2, "big")
        elif fault == "status":
            raw[4] = 64
        elif fault == "identity":
            party["species"][0] = raw[0] = 100
        else:
            raw[1:3] = raw[34:36] = (99).to_bytes(2, "big")
        party["records"][0] = raw.hex()
    assert adjudicate(probe, owners, "room-return")["complete"] is False


def test_healed_room_proof_checks_nonactive_party_members_too(probe):
    owners = paired_snapshots(room=True)
    party = owners[0]["before_party"]
    party["count"] = 2
    party["species"].insert(1, 100)
    party["records"].append(record(species=100, hp=50, status=64).hex())
    owners[0]["room_return"]["healed_party"] = healed_party(party)
    assert adjudicate(probe, owners, "room-return")["complete"] is True
    owners[0]["room_return"]["healed_party"]["records"][1] = party["records"][1]
    assert adjudicate(probe, owners, "room-return")["complete"] is False
