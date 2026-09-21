"""Boundary-matrix specification for the ordinary-medicine cases (#90.2, ROM-free half).

The matrix in ``tests/data/battle_medicine/boundary-cases.json`` declares the party and
target boundaries that the real-ROM half (90.3-90.5) must execute: below-cap and clamped
healing, HP above 255, active and benched targets, full-HP and fainted targets, and
same-species party members that a target-selection mistake could confuse.

Every expected outcome in that file is written by hand from the pinned sources. These
tests re-derive each one from the source-pinned oracle, so a declared expectation cannot
drift away from the pinned revisions, and they assert the fixture-design invariants that
make a boundary case able to detect the mistake it exists to catch. Nothing here executes
a ROM; the declarations are not gameplay evidence.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests._battle_medicine_oracle import (
    DATA_DIR,
    ITEM_EFFECTS_PATH,
    RED_BLUE_ITEM_EFFECTS_SHA256,
    RED_BLUE_REPOSITORY,
    RED_BLUE_REVISION,
    YELLOW_ITEM_EFFECTS_SHA256,
    YELLOW_REPOSITORY,
    YELLOW_REVISION,
    HP_APPLICATION_EXCERPTS,
    digest_text,
    excerpt_text,
    heal_application,
    pinned_heal_ladder_ops,
    pinned_item_ids,
    raw_loaded_amount,
)

MATRIX_FILE = DATA_DIR / "boundary-cases.json"
SCHEMA = "battle-medicine-boundary-cases/1"

# The boundaries 90.2 names explicitly. Declared here as the requirement, so the data
# file cannot shrink its own coverage claim by dropping an entry from its list.
REQUIRED_CATEGORIES = frozenset(
    {
        "below_cap",
        "clamped",
        "hp_above_255",
        "target_active",
        "target_benched",
        "full_hp",
        "fainted",
        "same_species_distinct",
    }
)

BYTE_MAX = 0xFF
MEMBER_FIELDS = ("species", "level", "hp", "max_hp", "status")


def load_matrix() -> dict[str, Any]:
    """Read the declared boundary matrix."""

    return json.loads(MATRIX_FILE.read_text(encoding="utf-8"))


MATRIX = load_matrix()
CASES: tuple[dict[str, Any], ...] = tuple(MATRIX["cases"])
CASE_IDS = [case["id"] for case in CASES]


def member_key(member: dict[str, Any]) -> tuple[Any, ...]:
    """Return the identity of one party record."""

    return tuple(member[field] for field in MEMBER_FIELDS)


def problems_for_case(case: dict[str, Any]) -> list[str]:
    """Return every way ``case`` disagrees with the pinned sources or its own schema.

    Shared by the real assertions and by the negative control, so the control proves the
    comparison actually compares something.
    """

    problems: list[str] = []
    item_ids = pinned_item_ids()
    ops = pinned_heal_ladder_ops()

    if case["item"] not in item_ids:
        return [f"unknown item {case['item']!r}"]

    party = case["party"]
    target_index = case["target_index"]
    if not 0 <= target_index < len(party):
        return [f"target_index {target_index} outside party of {len(party)}"]

    for position, member in enumerate(party):
        if member["hp"] > member["max_hp"]:
            problems.append(
                f"member {position}: hp {member['hp']} exceeds max_hp {member['max_hp']}"
            )
        if member["max_hp"] == 0:
            problems.append(f"member {position}: max_hp is zero")
        for field in ("hp", "max_hp"):
            if member[field] > 0xFFFF:
                problems.append(f"member {position}: {field} {member[field]} is not 16-bit")
        if not 0 <= member["status"] <= BYTE_MAX:
            problems.append(f"member {position}: status {member['status']} is not a byte")

    members = case["expected"]["members"]
    if len(members) != len(party):
        return [f"expected {len(members)} member records for a party of {len(party)}"]

    target = party[target_index]
    outcome = heal_application(
        item_ids[case["item"]],
        target["hp"],
        target["max_hp"],
        target["status"],
        item_ids=item_ids,
        ops=ops,
    )

    if case["expected"]["applied"] is not outcome.applied:
        problems.append(
            f"declared applied={case['expected']['applied']} but source says {outcome.applied}"
        )
    if case["expected"]["branch"] != outcome.branch:
        problems.append(
            f"declared branch={case['expected']['branch']!r} but source says {outcome.branch!r}"
        )

    for position, member in enumerate(party):
        declared = members[position]
        if position == target_index:
            derived = (outcome.hp, outcome.status)
        else:
            # Every non-target member must be declared untouched: the routine rewrites
            # only the record it was pointed at.
            derived = (member["hp"], member["status"])
        given = (declared["hp"], declared["status"])
        if given != derived:
            problems.append(
                f"member {position}: declared (hp={given[0]}, status={given[1]}) "
                f"but the pinned source yields (hp={derived[0]}, status={derived[1]})"
            )

    return problems


def test_matrix_schema_and_unique_case_ids() -> None:
    assert MATRIX["schema"] == SCHEMA
    assert MATRIX["issue"] == 90
    assert MATRIX["leaf"] == "90.2"
    assert CASES, "the boundary matrix must declare at least one case"
    assert len(CASE_IDS) == len(set(CASE_IDS)), f"duplicate case ids in {CASE_IDS}"
    for case in CASES:
        assert set(case) >= {"id", "categories", "item", "target_index", "party", "expected"}, case[
            "id"
        ]
        assert case["categories"], f"case {case['id']} declares no category"
        assert set(case["expected"]) == {"applied", "branch", "members"}, case["id"]


def test_matrix_pins_the_committed_source_revisions() -> None:
    pinned = MATRIX["pinned_sources"]
    assert pinned["red_blue"] == {
        "repository": RED_BLUE_REPOSITORY,
        "revision": RED_BLUE_REVISION,
        "path": ITEM_EFFECTS_PATH,
        "file_sha256": RED_BLUE_ITEM_EFFECTS_SHA256,
    }
    assert pinned["yellow"] == {
        "repository": YELLOW_REPOSITORY,
        "revision": YELLOW_REVISION,
        "path": ITEM_EFFECTS_PATH,
        "file_sha256": YELLOW_ITEM_EFFECTS_SHA256,
    }
    # The committed excerpts really are the slices of those pinned revisions.
    for record in HP_APPLICATION_EXCERPTS:
        assert digest_text(excerpt_text(record)) == record.excerpt_sha256, record.label
        assert record.revision == (
            RED_BLUE_REVISION if record.repository == RED_BLUE_REPOSITORY else YELLOW_REVISION
        )


def test_every_required_boundary_category_is_declared_and_covered() -> None:
    declared = set(MATRIX["required_categories"])
    assert REQUIRED_CATEGORIES <= declared, (
        f"matrix no longer claims {REQUIRED_CATEGORIES - declared}"
    )
    covered = {category for case in CASES for category in case["categories"]}
    assert REQUIRED_CATEGORIES <= covered, f"no case covers {REQUIRED_CATEGORIES - covered}"


def test_every_case_declares_its_execution_requirements() -> None:
    requirements = MATRIX["execution_requirements"]
    assert set(requirements["games"]) == {"red", "blue", "yellow"}
    assert set(requirements["runtimes"]) == {"source", "native"}
    assert requirements["mode"] == "ordinary"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_declared_expectations_match_the_source_pinned_oracle(case: dict[str, Any]) -> None:
    assert problems_for_case(case) == []


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_only_the_declared_target_member_changes(case: dict[str, Any]) -> None:
    target_index = case["target_index"]
    for position, (member, declared) in enumerate(zip(case["party"], case["expected"]["members"])):
        if position == target_index:
            continue
        assert (declared["hp"], declared["status"]) == (member["hp"], member["status"]), (
            f"case {case['id']} declares a bystander at index {position} as changed"
        )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_party_records_are_pairwise_distinguishable(case: dict[str, Any]) -> None:
    keys = [member_key(member) for member in case["party"]]
    assert len(keys) == len(set(keys)), (
        f"case {case['id']} has two identical party records, so a real run could not tell "
        "which member was healed"
    )


def test_same_species_cases_really_carry_a_distinguishable_same_species_pair() -> None:
    tagged = [case for case in CASES if "same_species_distinct" in case["categories"]]
    assert tagged, "no case declares the same-species boundary"
    for case in tagged:
        by_species: dict[str, list[dict[str, Any]]] = {}
        for member in case["party"]:
            by_species.setdefault(member["species"], []).append(member)
        pairs = [members for members in by_species.values() if len(members) > 1]
        assert pairs, f"case {case['id']} claims same-species members but has none"
        for members in pairs:
            assert len({member_key(member) for member in members}) == len(members), (
                f"case {case['id']} has same-species members that are not distinguishable"
            )


def test_above_255_cases_exercise_the_sixteen_bit_carry() -> None:
    tagged = [case for case in CASES if "hp_above_255" in case["categories"]]
    assert tagged, "no case declares the HP-above-255 boundary"
    item_ids = pinned_item_ids()
    for case in tagged:
        target = case["party"][case["target_index"]]
        assert target["max_hp"] > BYTE_MAX, (
            f"case {case['id']} is tagged hp_above_255 but max_hp is {target['max_hp']}"
        )
        amount = raw_loaded_amount(item_ids[case["item"]], item_ids=item_ids)
        assert target["hp"] + amount > BYTE_MAX, (
            f"case {case['id']} does not cross a byte boundary: {target['hp']} + {amount}"
        )


def test_a_corrupted_declaration_is_detected() -> None:
    """Negative control: the comparison must fail on a wrong hand-written expectation."""

    templates = {case["id"]: case for case in CASES}
    corrupted = copy.deepcopy(templates["below-cap-potion-on-active"])
    corrupted["expected"]["members"][0]["hp"] += 1
    assert problems_for_case(corrupted) != []

    wrong_branch = copy.deepcopy(templates["potion-reaches-cap-and-clamps"])
    wrong_branch["expected"]["branch"] = "fixed_heal"
    assert problems_for_case(wrong_branch) != []

    bystander_changed = copy.deepcopy(templates["below-cap-potion-on-active"])
    bystander_changed["expected"]["members"][1]["hp"] -= 1
    assert problems_for_case(bystander_changed) != []

    # And the unmodified originals stay clean, so the control is not vacuous.
    for case in CASES:
        assert problems_for_case(case) == [], case["id"]


def test_matrix_file_is_the_committed_pin_consumers_read() -> None:
    path = Path(MATRIX_FILE)
    assert path.name == "boundary-cases.json"
    assert path.parent == DATA_DIR
    assert json.loads(path.read_text(encoding="utf-8")) == MATRIX
