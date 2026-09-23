"""Action-economy contracts for the timed battle probe (#147).

Split from ``tests/test_timed_battle_probe.py`` for #147 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. PP consumption, paralysis, non-faint and zero-outcome
settlement only; never gameplay or commercial-ROM acceptance.
"""

import copy

import pytest

from tests._timed_battle_probe_support import (
    adjudicate,
    paired_snapshots,
    zero_outcome_pair,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("side", [0, 1])
def test_nonfaint_contract_rejects_faint_even_when_peers_agree(probe, side):
    owners = paired_snapshots()
    owners[side]["turn"]["local"]["hp"] = 0
    owners[1 - side]["turn"]["enemy"]["hp"] = 0
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("wrong_pp", [19, 18])
def test_fully_paralyzed_action_must_not_consume_local_pp(probe, wrong_pp):
    owners = paired_snapshots(paralyzed=True)
    owners[0]["turn"]["local"]["pp"][0] = wrong_pp
    owners[0]["turn"]["pp"]["after"][0] = wrong_pp
    assert adjudicate(probe, owners)["complete"] is False


def test_paralysis_claim_without_status_is_not_a_valid_skipped_action(probe):
    owners = paired_snapshots(paralyzed=True)
    for owner in owners:
        for phase in ("baseline", "turn"):
            for side in ("local", "enemy"):
                owner[phase][side]["status"] = 0
    raw = bytearray.fromhex(owners[0]["before_party"]["records"][0])
    raw[4] = 0
    owners[0]["before_party"]["records"][0] = raw.hex()
    assert adjudicate(probe, owners)["complete"] is False


def test_multihit_consumes_one_pp_for_aggregate_settlement(probe):
    owners = paired_snapshots(multi_hit=True)
    assert adjudicate(probe, owners)["complete"] is True
    owners[1]["turn"]["local"]["pp"][0] = 28
    owners[1]["turn"]["pp"]["after"][0] = 28
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("missed", [False, True], ids=["completed-zero", "reported-miss"])
def test_no_hp_delta_can_settle_with_completed_application_or_observed_miss(probe, missed):
    # wMoveMissed records the ROM path; it does not independently classify immunity.
    owners = zero_outcome_pair(missed=missed)
    result = adjudicate(probe, owners)
    assert result["complete"] is True, result
    for owner in owners:
        assert owner["turn"]["local"]["hp"] == owner["baseline"]["local"]["hp"]
        assert owner["turn"]["pp"]["after"][0] == owner["turn"]["pp"]["before"][0] - 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing_path",
        "missing_sample",
        "miss_with_application",
        "wrong_calculated_damage",
        "hp_increase",
        "residual_drop",
    ],
)
def test_fabricated_zero_and_mismatched_damage_paths_fail_even_when_peers_agree(probe, fault):
    owners = zero_outcome_pair()
    action = owners[0]["turn"]["actions"]["local"]
    if fault == "missing_path":
        action.update(damage_done=0, damage_samples=[])
    elif fault == "missing_sample":
        action["damage_samples"] = []
    elif fault == "miss_with_application":
        action["move_missed"] = 1
    elif fault == "wrong_calculated_damage":
        action["damage_samples"][0]["damage"] = 1
    else:
        changed_hp = 91 if fault == "hp_increase" else 89
        owners[0]["turn"]["enemy"]["hp"] = changed_hp
        owners[1]["turn"]["local"]["hp"] = changed_hp
    owners[1]["turn"]["actions"]["enemy"] = copy.deepcopy(action)
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
def test_counter_is_explicitly_unsupported_despite_catalog_effect_zero(probe, side):
    owners = paired_snapshots()
    raw = bytearray.fromhex(owners[side]["before_party"]["records"][0])
    raw[8] = 68
    owners[side]["before_party"]["records"][0] = raw.hex()
    owners[side]["chosen"]["move_id"] = 68
    owners[side]["turn"]["local_move_id"] = 68
    owners[1 - side]["turn"]["enemy_move_id"] = 68
    for phase in ("baseline", "turn"):
        owners[side][phase]["local"]["moves"][0] = 68
        owners[1 - side][phase]["enemy"]["moves"][0] = 68
    result = adjudicate(probe, owners)
    assert result["complete"] is False
    assert "counter" in " ".join(result["errors"]).lower()
