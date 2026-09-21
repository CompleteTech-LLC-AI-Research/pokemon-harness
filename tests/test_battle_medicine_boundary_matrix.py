"""Boundary-matrix specification for the ordinary-medicine cases (#90.2, ROM-free half).

The matrix in ``tests/data/battle_medicine/boundary-cases.json`` declares the party and
target boundaries that the real-ROM half (90.3-90.5) must execute: below-cap and clamped
healing, HP above 255, active and benched targets, full-HP and fainted targets, and
same-species party members that a target-selection mistake could confuse.

Every expected outcome in that file is written by hand from the pinned sources. These tests
re-derive each one from the source-pinned oracle, so a declared expectation cannot drift away
from the pinned revisions, and they assert the fixture-design invariants that make a boundary
case able to detect the mistake it exists to catch. Nothing here executes a ROM; the
declarations are not gameplay evidence.

The checks are split by what they police, and each one carries a negative control so a green
suite is never mistaken for the invariant holding:

* ``domain_problems`` - the item must be one this HP subroutine applies to. The pinned ID
  table holds every item in the source and the ladder's default amount is reached by any ID
  below ``SUPER_POTION``, so without this check an ineligible item silently inherits a
  healing expectation (a Master Ball would "heal" to maximum).
* ``structure_problems`` / ``expectation_problems`` - the declared outcome must match the
  pinned oracle, and only the declared target member may change.
* ``carry_problems`` - an ``hp_above_255`` case must actually carry out of the low byte, not
  merely exceed 255 in total, because the low-byte carry is the mistake the two-byte
  ``add b`` / ``inc [hl]`` sequence makes possible.
* ``category_problems`` - a category label must be backed by the case's inputs: full HP means
  the target is at maximum, fainted means zero HP, below-cap/clamped mean the matching
  arithmetic and branch, active/benched agree with the fixture's active slot, and a
  same-species case must put the selected target inside the distinguishable pair.
* ``inventory_problems`` - the required case inventory is declared in this module, not in the
  data file, so a requirement cannot vanish by deleting a case or by renaming an item.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from tests._battle_medicine_oracle import (
    APPLIED_BRANCHES,
    BRANCH_CLAMPED_TO_MAX,
    BRANCH_FIXED_HEAL,
    BRANCH_FULL_RESTORE_STATUS_ONLY,
    BRANCH_REFUSED_FAINTED,
    BRANCH_REFUSED_FULL_HP,
    BRANCH_REFUSED_REVIVE_ON_LIVING,
    BRANCH_REVIVE_HALF_MAX,
    BRANCH_REVIVE_TO_MAX,
    DATA_DIR,
    HP_APPLICATION_EXCERPTS,
    ITEM_EFFECTS_PATH,
    RED_BLUE_ITEM_EFFECTS_SHA256,
    RED_BLUE_REPOSITORY,
    RED_BLUE_REVISION,
    YELLOW_ITEM_EFFECTS_SHA256,
    YELLOW_REPOSITORY,
    YELLOW_REVISION,
    clamps_to_max_hp,
    digest_text,
    excerpt_text,
    heal_application,
    pinned_heal_ladder_ops,
    pinned_item_ids,
    raw_loaded_amount,
)

MATRIX_FILE = DATA_DIR / "boundary-cases.json"
SCHEMA = "battle-medicine-boundary-cases/1"

# Party index 0 is the fixture's battle-active slot; a benched target is any other slot.
ACTIVE_SLOT = 0
BYTE_MAX = 0xFF
HP_FIELD_MAX = 0xFFFF
MEMBER_FIELDS = ("species", "level", "hp", "max_hp", "status")

# The HP-medicine domain this matrix may declare. Status items and the drink items
# (Fresh Water, Soda Pop, Lemonade) are a different issue's scope; a non-medicine item must
# not be handed a healing expectation derived from the healing ladder.
ADMITTED_ITEMS = frozenset(
    {
        "FULL_RESTORE",
        "HYPER_POTION",
        "MAX_POTION",
        "MAX_REVIVE",
        "POTION",
        "REVIVE",
        "SUPER_POTION",
    }
)

# The boundaries 90.2 names explicitly. Declared here as the requirement, so the data file
# cannot shrink its own coverage claim by dropping an entry from its list.
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

# The required inventory: every case that must exist, with the item it must use and the branch
# the pinned source must resolve for it. Declared independently of the data file, so deleting a
# case or swapping in a different item fails here instead of quietly shrinking the matrix.
REQUIRED_CASES: Mapping[str, tuple[str, str]] = {
    "above-255-hp-hyper-potion-on-benched-carry-target": ("HYPER_POTION", BRANCH_FIXED_HEAL),
    "above-255-hp-potion-adds-with-carry": ("POTION", BRANCH_FIXED_HEAL),
    "above-255-hp-super-potion-carries-then-clamps": ("SUPER_POTION", BRANCH_CLAMPED_TO_MAX),
    "below-cap-hyper-potion-on-active": ("HYPER_POTION", BRANCH_FIXED_HEAL),
    "below-cap-potion-on-active": ("POTION", BRANCH_FIXED_HEAL),
    "below-cap-super-potion-on-active": ("SUPER_POTION", BRANCH_FIXED_HEAL),
    "below-cap-super-potion-on-benched-target": ("SUPER_POTION", BRANCH_FIXED_HEAL),
    "full-restore-at-full-hp-clears-status-only": ("FULL_RESTORE", BRANCH_FULL_RESTORE_STATUS_ONLY),
    "full-restore-at-full-hp-without-status-is-refused": ("FULL_RESTORE", BRANCH_REFUSED_FULL_HP),
    "full-restore-below-cap-clamps-and-clears-status": ("FULL_RESTORE", BRANCH_CLAMPED_TO_MAX),
    "full-restore-on-fainted-target-is-refused": ("FULL_RESTORE", BRANCH_REFUSED_FAINTED),
    "hyper-potion-clamps-to-max": ("HYPER_POTION", BRANCH_CLAMPED_TO_MAX),
    "max-potion-clamps-by-item-id": ("MAX_POTION", BRANCH_CLAMPED_TO_MAX),
    "max-revive-fainted-target-to-full": ("MAX_REVIVE", BRANCH_REVIVE_TO_MAX),
    "potion-at-full-hp-is-refused-and-keeps-status": ("POTION", BRANCH_REFUSED_FULL_HP),
    "potion-on-benched-same-species-target": ("POTION", BRANCH_CLAMPED_TO_MAX),
    "potion-on-fainted-target-is-refused": ("POTION", BRANCH_REFUSED_FAINTED),
    "potion-reaches-cap-and-clamps": ("POTION", BRANCH_CLAMPED_TO_MAX),
    "revive-fainted-target-to-half-max-floor": ("REVIVE", BRANCH_REVIVE_HALF_MAX),
    "revive-on-a-living-target-is-refused": ("REVIVE", BRANCH_REFUSED_REVIVE_ON_LIVING),
}

REFUSED_BRANCHES = frozenset(
    {
        BRANCH_REFUSED_FAINTED,
        BRANCH_REFUSED_FULL_HP,
        BRANCH_REFUSED_REVIVE_ON_LIVING,
    }
)


def load_matrix() -> dict[str, Any]:
    """Read the declared boundary matrix."""

    return json.loads(MATRIX_FILE.read_text(encoding="utf-8"))


MATRIX = load_matrix()
CASES: tuple[dict[str, Any], ...] = tuple(MATRIX["cases"])
CASE_IDS = [case["id"] for case in CASES]


def case_by_id(case_id: str) -> dict[str, Any]:
    """Return the declared case with ``case_id``."""

    return next(case for case in CASES if case["id"] == case_id)


def member_key(member: dict[str, Any]) -> tuple[Any, ...]:
    """Return the identity of one party record."""

    return tuple(member[field] for field in MEMBER_FIELDS)


def target_of(case: dict[str, Any]) -> dict[str, Any]:
    """Return the declared target member."""

    return case["party"][case["target_index"]]


def amount_for(case: dict[str, Any]) -> int:
    """Return the amount the pinned ladder loads for the case's item."""

    item_ids = pinned_item_ids()
    return raw_loaded_amount(item_ids[case["item"]], item_ids=item_ids)


def outcome_for(case: dict[str, Any]) -> Any:
    """Resolve the case through the pinned oracle."""

    item_ids = pinned_item_ids()
    target = target_of(case)
    return heal_application(
        item_ids[case["item"]],
        target["hp"],
        target["max_hp"],
        target["status"],
        item_ids=item_ids,
        ops=pinned_heal_ladder_ops(),
    )


def alternative_party(case: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """Return the party record set produced by healing ``index`` instead of the target."""

    member = case["party"][index]
    item_ids = pinned_item_ids()
    outcome = heal_application(
        item_ids[case["item"]],
        member["hp"],
        member["max_hp"],
        member["status"],
        item_ids=item_ids,
        ops=pinned_heal_ladder_ops(),
    )
    members = [dict(declared) for declared in case["expected"]["members"]]
    members[index] = {"hp": outcome.hp, "status": outcome.status}
    return members


def domain_problems(case: dict[str, Any]) -> list[str]:
    """Return why the case's item is not a legitimate subject for this oracle, if it is not."""

    if case["item"] not in ADMITTED_ITEMS:
        return [
            (
                f"item {case['item']!r} is outside the admitted HP-medicine domain "
                f"{sorted(ADMITTED_ITEMS)}"
            )
        ]
    if case["item"] not in pinned_item_ids():
        return [f"item {case['item']!r} is not in the pinned item table"]
    return []


def structure_problems(case: dict[str, Any]) -> list[str]:
    """Return every structural defect in the case's declared party and target."""

    party = case["party"]
    target_index = case["target_index"]
    if not party:
        return ["party is empty"]
    if not 0 <= target_index < len(party):
        return [f"target_index {target_index} outside party of {len(party)}"]

    problems: list[str] = []
    for position, member in enumerate(party):
        if member["hp"] > member["max_hp"]:
            problems.append(
                f"member {position}: hp {member['hp']} exceeds max_hp {member['max_hp']}"
            )
        if member["max_hp"] == 0:
            problems.append(f"member {position}: max_hp is zero")
        if member["level"] <= 0:
            problems.append(f"member {position}: level {member['level']} is not positive")
        for field in ("hp", "max_hp"):
            if not 0 <= member[field] <= HP_FIELD_MAX:
                problems.append(f"member {position}: {field} {member[field]} is not 16-bit")
        if not 0 <= member["status"] <= BYTE_MAX:
            problems.append(f"member {position}: status {member['status']} is not a byte")

    members = case["expected"]["members"]
    if len(members) != len(party):
        problems.append(f"expected {len(members)} member records for a party of {len(party)}")
    return problems


def expectation_problems(case: dict[str, Any]) -> list[str]:
    """Return every way the declared outcome disagrees with the pinned oracle."""

    problems: list[str] = []
    outcome = outcome_for(case)
    target_index = case["target_index"]

    if case["expected"]["applied"] is not outcome.applied:
        problems.append(
            f"declared applied={case['expected']['applied']} but source says {outcome.applied}"
        )
    if case["expected"]["branch"] != outcome.branch:
        problems.append(
            f"declared branch={case['expected']['branch']!r} but source says {outcome.branch!r}"
        )

    for position, member in enumerate(case["party"]):
        declared = case["expected"]["members"][position]
        if position == target_index:
            derived = (outcome.hp, outcome.status)
        else:
            # Every non-target member must be declared untouched: the routine rewrites only
            # the record it was pointed at.
            derived = (member["hp"], member["status"])
        given = (declared["hp"], declared["status"])
        if given != derived:
            problems.append(
                f"member {position}: declared (hp={given[0]}, status={given[1]}) "
                f"but the pinned source yields (hp={derived[0]}, status={derived[1]})"
            )

    return problems


def problems_for_case(case: dict[str, Any]) -> list[str]:
    """Return every way ``case`` disagrees with the admitted domain or the pinned sources.

    Shared by the real assertions and by the negative controls, so the controls prove the
    comparison actually compares something.
    """

    problems = domain_problems(case)
    if problems:
        return problems
    problems = structure_problems(case)
    if problems:
        return problems
    return expectation_problems(case)


def carry_problems(cases: Any) -> list[str]:
    """Return why an ``hp_above_255`` case does not exercise the low-byte carry."""

    problems: list[str] = []
    for case in cases:
        if "hp_above_255" not in case["categories"]:
            continue
        target = target_of(case)
        amount = amount_for(case)
        low_byte_sum = (target["hp"] & BYTE_MAX) + amount
        if target["max_hp"] <= BYTE_MAX:
            problems.append(
                f"{case['id']}: max HP {target['max_hp']} is not above 255, so the two-byte "
                "field ordering is not exercised"
            )
            continue
        if low_byte_sum <= BYTE_MAX:
            problems.append(
                f"{case['id']}: low byte {target['hp'] & BYTE_MAX} + {amount} = {low_byte_sum} "
                "does not carry out of the byte, so the 16-bit add is not exercised"
            )
            continue
        outcome = outcome_for(case)
        if not outcome.applied or outcome.branch not in (BRANCH_FIXED_HEAL, BRANCH_CLAMPED_TO_MAX):
            problems.append(
                f"{case['id']}: resolves to {outcome.branch}, so the carrying addition is not "
                "the path under test"
            )
    return problems


def _below_cap_problem(case: dict[str, Any]) -> str | None:
    target = target_of(case)
    outcome = outcome_for(case)
    amount = amount_for(case)
    if not outcome.applied or outcome.branch != BRANCH_FIXED_HEAL:
        return f"declares below_cap but resolves to {outcome.branch}"
    if target["hp"] + amount >= target["max_hp"]:
        return f"declares below_cap but {target['hp']} + {amount} reaches max HP {target['max_hp']}"
    return None


def _clamped_problem(case: dict[str, Any]) -> str | None:
    target = target_of(case)
    outcome = outcome_for(case)
    amount = amount_for(case)
    if outcome.branch not in (BRANCH_CLAMPED_TO_MAX, BRANCH_REVIVE_TO_MAX):
        return f"declares clamped but resolves to {outcome.branch}"
    item_ids = pinned_item_ids()
    by_item_id = clamps_to_max_hp(item_ids[case["item"]], item_ids)
    if not by_item_id and target["hp"] + amount < target["max_hp"]:
        return (
            "declares clamped but neither the item-ID clamp applies nor does "
            f"{target['hp']} + {amount} reach max HP {target['max_hp']}"
        )
    return None


def _full_hp_problem(case: dict[str, Any]) -> str | None:
    target = target_of(case)
    if target["hp"] != target["max_hp"]:
        return f"declares full_hp but the target is at {target['hp']}/{target['max_hp']}"
    return None


def _fainted_problem(case: dict[str, Any]) -> str | None:
    target = target_of(case)
    if target["hp"] != 0:
        return f"declares fainted but the target has {target['hp']} HP"
    return None


def _target_active_problem(case: dict[str, Any]) -> str | None:
    if case["target_index"] != ACTIVE_SLOT:
        return f"declares target_active but targets party slot {case['target_index']}"
    return None


def _target_benched_problem(case: dict[str, Any]) -> str | None:
    if case["target_index"] == ACTIVE_SLOT:
        return "declares target_benched but targets the active slot"
    return None


def _hp_above_255_problem(case: dict[str, Any]) -> str | None:
    problems = carry_problems([case])
    return problems[0] if problems else None


def _same_species_problem(case: dict[str, Any]) -> str | None:
    target = target_of(case)
    target_index = case["target_index"]
    partners = [
        position
        for position, member in enumerate(case["party"])
        if position != target_index and member["species"] == target["species"]
    ]
    if not partners:
        return (
            f"declares same_species_distinct but the selected target {target['species']} is not "
            "part of a same-species group"
        )
    group = [case["party"][position] for position in [target_index, *partners]]
    if len({member_key(member) for member in group}) != len(group):
        return "same-species records are not distinguishable from one another"
    for partner in partners:
        if alternative_party(case, partner) == case["expected"]["members"]:
            return (
                f"healing member {partner} instead of the target would produce the declared "
                "party, so the case cannot detect the wrong target"
            )
    return None


CATEGORY_CHECKS: Mapping[str, Callable[[dict[str, Any]], str | None]] = {
    "below_cap": _below_cap_problem,
    "clamped": _clamped_problem,
    "fainted": _fainted_problem,
    "full_hp": _full_hp_problem,
    "hp_above_255": _hp_above_255_problem,
    "same_species_distinct": _same_species_problem,
    "target_active": _target_active_problem,
    "target_benched": _target_benched_problem,
}


def category_problems(cases: Any) -> list[str]:
    """Return every category label whose case inputs do not actually carry that boundary."""

    problems: list[str] = []
    for case in cases:
        if not case["categories"]:
            problems.append(f"{case['id']}: no category declared")
        for category in case["categories"]:
            checker = CATEGORY_CHECKS.get(category)
            if checker is None:
                problems.append(f"{case['id']}: unknown category {category!r}")
                continue
            problem = checker(case)
            if problem is not None:
                problems.append(f"{case['id']}: {problem}")
        if case["party"][ACTIVE_SLOT]["hp"] == 0:
            problems.append(
                f"{case['id']}: the active slot is fainted, so the active/benched distinction "
                "the fixture declares does not exist"
            )
    return problems


def inventory_problems(cases: Any) -> list[str]:
    """Return every required case or branch/domain entry the matrix no longer covers."""

    problems: list[str] = []
    by_id = {case["id"]: case for case in cases}
    for case_id, (item, branch) in REQUIRED_CASES.items():
        case = by_id.get(case_id)
        if case is None:
            problems.append(f"required case {case_id!r} is missing")
            continue
        if case["item"] != item:
            problems.append(f"required case {case_id!r} no longer uses {item}")
        if case["expected"]["branch"] != branch:
            problems.append(
                f"required case {case_id!r} no longer resolves to {branch} "
                f"(it resolves to {case['expected']['branch']})"
            )

    items = {case["item"] for case in cases}
    for item in sorted(ADMITTED_ITEMS - items):
        problems.append(f"no case exercises the admitted item {item}")
    branches = {case["expected"]["branch"] for case in cases}
    for branch in sorted(APPLIED_BRANCHES | REFUSED_BRANCHES):
        if branch not in branches:
            problems.append(f"no case resolves to the branch {branch}")
    return problems


def test_matrix_schema_and_unique_case_ids() -> None:
    assert MATRIX["schema"] == SCHEMA
    assert MATRIX["issue"] == 90
    assert MATRIX["leaf"] == "90.2"
    assert CASES, "the boundary matrix must declare at least one case"
    assert len(CASE_IDS) == len(set(CASE_IDS)), f"duplicate case ids in {CASE_IDS}"
    for case in CASES:
        assert set(case) >= {
            "id",
            "categories",
            "item",
            "target_index",
            "party",
            "expected",
        }, case["id"]
        assert case["categories"], f"case {case['id']} declares no category"
        assert set(case["expected"]) == {"applied", "branch", "members"}, case["id"]
        for member in case["party"]:
            assert set(member) == set(MEMBER_FIELDS), case["id"]
        for member in case["expected"]["members"]:
            assert set(member) == {"hp", "status"}, case["id"]


def test_matrix_declares_the_admitted_item_domain() -> None:
    """The data file's declared domain must agree with this module's independent pin."""

    assert set(MATRIX["admitted_items"]) == ADMITTED_ITEMS
    active_slot_rule = MATRIX["active_slot_rule"]
    assert isinstance(active_slot_rule, str)
    assert active_slot_rule.strip()
    for case in CASES:
        assert domain_problems(case) == [], case["id"]


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


def test_required_case_inventory_is_present() -> None:
    """The declared inventory must survive, at the item and branch it was declared with."""

    assert inventory_problems(CASES) == []


def test_every_category_label_is_backed_by_its_inputs() -> None:
    """A category label must describe the case's actual inputs, not merely mark it."""

    assert category_problems(CASES) == []


def test_above_255_cases_exercise_the_sixteen_bit_carry() -> None:
    tagged = [case for case in CASES if "hp_above_255" in case["categories"]]
    assert tagged, "no case declares the HP-above-255 boundary"
    assert carry_problems(tagged) == []


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


def test_same_species_cases_put_the_selected_target_in_the_pair() -> None:
    tagged = [case for case in CASES if "same_species_distinct" in case["categories"]]
    assert tagged, "no case declares the same-species boundary"
    for case in tagged:
        assert _same_species_problem(case) is None, case["id"]


def test_matrix_file_is_the_committed_pin_consumers_read() -> None:
    path = Path(MATRIX_FILE)
    assert path.name == "boundary-cases.json"
    assert path.parent == DATA_DIR
    assert json.loads(path.read_text(encoding="utf-8")) == MATRIX


def test_a_corrupted_declaration_is_detected() -> None:
    """Negative control: a wrong hand-written expectation must be rejected."""

    corrupted = copy.deepcopy(case_by_id("below-cap-potion-on-active"))
    corrupted["expected"]["members"][0]["hp"] += 1
    assert problems_for_case(corrupted) != []

    wrong_branch = copy.deepcopy(case_by_id("potion-reaches-cap-and-clamps"))
    wrong_branch["expected"]["branch"] = "fixed_heal"
    assert problems_for_case(wrong_branch) != []

    bystander_changed = copy.deepcopy(case_by_id("below-cap-potion-on-active"))
    bystander_changed["expected"]["members"][1]["hp"] -= 1
    assert problems_for_case(bystander_changed) != []

    # And the unmodified originals stay clean, so the control is not vacuous.
    for case in CASES:
        assert problems_for_case(case) == [], case["id"]


def test_an_ineligible_item_is_rejected() -> None:
    """Negative control: a known but non-medicine item must not receive a healing verdict."""

    master_ball = copy.deepcopy(case_by_id("max-potion-clamps-by-item-id"))
    master_ball["item"] = "MASTER_BALL"
    assert domain_problems(master_ball) != []
    assert problems_for_case(master_ball) != []

    unknown = copy.deepcopy(case_by_id("below-cap-potion-on-active"))
    unknown["item"] = "SITRUS_BERRY"
    assert problems_for_case(unknown) != []


def test_category_labels_without_their_boundary_are_rejected() -> None:
    """Negative control: keeping a label while removing the boundary must fail."""

    # A damaged target that still claims to be a full-HP case, with its corrected outcome.
    damaged = copy.deepcopy(case_by_id("full-restore-at-full-hp-clears-status-only"))
    target = damaged["party"][damaged["target_index"]]
    target["hp"] = target["max_hp"] - 1
    damaged["expected"]["branch"] = BRANCH_CLAMPED_TO_MAX
    assert problems_for_case(damaged) == []
    assert "full_hp" in " ".join(category_problems([damaged]))

    # A same-species label whose pair no longer contains the selected target.
    bystander_pair = copy.deepcopy(case_by_id("potion-on-benched-same-species-target"))
    bystander_pair["party"][bystander_pair["target_index"]]["species"] = "RATTATA"
    assert problems_for_case(bystander_pair) == []
    assert "same_species_distinct" in " ".join(category_problems([bystander_pair]))

    # An active label on a benched target.
    mislabelled = copy.deepcopy(case_by_id("potion-on-benched-same-species-target"))
    mislabelled["categories"] = [*mislabelled["categories"], "target_active"]
    assert problems_for_case(mislabelled) == []
    assert "target_active" in " ".join(category_problems([mislabelled]))

    # A fainted active slot, which would make the active/benched labels meaningless.
    dead_active = copy.deepcopy(case_by_id("below-cap-potion-on-active"))
    dead_active["party"][ACTIVE_SLOT]["hp"] = 0
    dead_active["party"][ACTIVE_SLOT]["status"] = 8
    dead_active["expected"] = {
        "applied": False,
        "branch": BRANCH_REFUSED_FAINTED,
        "members": [
            {"hp": 0, "status": 8},
            {"hp": 19, "status": 0},
            {"hp": 9, "status": 0},
        ],
    }
    assert problems_for_case(dead_active) == []
    assert "active slot is fainted" in " ".join(category_problems([dead_active]))


def test_a_non_carrying_above_255_case_is_rejected() -> None:
    """Negative control: exceeding 255 in total is not the same as a low-byte carry."""

    non_carrying = copy.deepcopy(case_by_id("above-255-hp-potion-adds-with-carry"))
    non_carrying["party"][non_carrying["target_index"]]["hp"] = 320
    non_carrying["expected"]["members"][non_carrying["target_index"]]["hp"] = 340
    assert problems_for_case(non_carrying) == []
    assert carry_problems([non_carrying]) != []

    for case in CASES:
        assert carry_problems([case]) == [], case["id"]


def test_a_deleted_required_case_is_rejected() -> None:
    """Negative control: the inventory must be enforced by this module, not by the data file."""

    without_max_potion = [case for case in CASES if case["id"] != "max-potion-clamps-by-item-id"]
    assert len(without_max_potion) == len(CASES) - 1
    assert inventory_problems(without_max_potion) != []

    without_full_restore = [case for case in CASES if case["item"] != "FULL_RESTORE"]
    assert inventory_problems(without_full_restore) != []

    assert inventory_problems(CASES) == []
