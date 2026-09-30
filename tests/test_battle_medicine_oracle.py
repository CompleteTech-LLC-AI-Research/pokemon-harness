"""Independent, ROM-free verification of the pinned medicine healing oracle.

Issue #90.1 asks for Potion/Super/Hyper fixed amounts, Max Potion and Full
Restore HP branches to be pinned against each source, with uncapped tests that
detect wrong constants.  The oracle itself is ``tests/_battle_medicine_oracle``;
this module re-parses the committed byte-exact excerpts, interprets the pinned
branch structure, and cross-checks the result against the merged #89/#93/#94
item-evidence contract.

Every assertion here is pure arithmetic and text: no emulator, ROM, symbol
file, save state or runtime fixture is touched, so the module belongs to the
always-selected ROM-free unit tier and must never skip.  The audit that
re-derives the excerpts from a local pinned checkout needs
``POKERED_PRET_ROOT`` / ``POKEYELLOW_PRET_ROOT`` and therefore lives in the
separate opt-in module ``tests/test_battle_medicine_source_conformance.py``,
which no tier marker expression selects.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._battle_item_evidence import (
    REASON_APPLIED,
    REASON_FAINTED_NON_REVIVE,
    REASON_FULL_HP,
    REASON_FULL_HP_NO_STATUS,
    REASON_REVIVE_ON_LIVING,
    MedicineRule,
    PartyMonSnapshot,
    medicine_outcome,
)
from tests._battle_medicine_oracle import (
    ALL_EXCERPTS,
    BRANCH_CLAMPED_TO_MAX,
    BRANCH_FIXED_HEAL,
    BRANCH_FULL_RESTORE_STATUS_ONLY,
    BRANCH_REFUSED_FAINTED,
    BRANCH_REFUSED_FULL_HP,
    BRANCH_REFUSED_REVIVE_ON_LIVING,
    BRANCH_REVIVE_HALF_MAX,
    BRANCH_REVIVE_TO_MAX,
    EXPECTED_ITEM_IDS,
    HP_APPLICATION_EXCERPTS,
    ITEM_ID_EXCERPTS,
    LADDER_CONDITIONS,
    OP_BRANCH,
    LadderOp,
    add_heal_amount,
    clamps_to_max_hp,
    digest_text,
    excerpt_text,
    heal_application,
    ladder_compare_symbols,
    parse_heal_ladder_ops,
    parse_item_aliases,
    parse_item_id_annotations,
    parse_item_ids,
    pinned_heal_ladder_ops,
    pinned_item_aliases,
    pinned_item_ids,
    raw_loaded_amount,
    run_heal_ladder,
    yellow_heal_ladder_ops,
)
from tests._tier_config import KNOWN_TEST_MODULES, classify_test

EXCERPT_IDS = [record.label.replace(" ", "-") for record in ALL_EXCERPTS]

# Fixed amounts the pinned ladder must load into ``b``.
PINNED_FIXED_AMOUNTS = {
    "POTION": 20,
    "SUPER_POTION": 50,
    "HYPER_POTION": 200,
    "FRESH_WATER": 50,
    "SODA_POP": 60,
    "LEMONADE": 80,
}

# Branch matchers used for every target, so each item is exercised fainted, one
# HP below the cap, at the cap, and with a live status ailment.
CORE_CASES = (
    (0, 101, 0x00),
    (1, 101, 0x08),
    (100, 101, 0x00),
    (101, 101, 0x08),
    (101, 101, 0x00),
    (100, 300, 0x40),
    (300, 300, 0x00),
    (50, 999, 0x00),
)


def _rules_for_all_medicine() -> dict[str, MedicineRule]:
    ids = pinned_item_ids()
    return {
        "POTION": MedicineRule(ids["POTION"], fixed_heal=20),
        "SUPER_POTION": MedicineRule(ids["SUPER_POTION"], fixed_heal=50),
        "HYPER_POTION": MedicineRule(ids["HYPER_POTION"], fixed_heal=200),
        "MAX_POTION": MedicineRule(ids["MAX_POTION"], heal_to_max=True),
        "FULL_RESTORE": MedicineRule(ids["FULL_RESTORE"], heal_to_max=True, cure_mask=0xFF),
        "REVIVE": MedicineRule(ids["REVIVE"], revive=True),
        "MAX_REVIVE": MedicineRule(ids["MAX_REVIVE"], revive=True, heal_to_max=True),
    }


def _mon(hp: int, max_hp: int, status: int) -> PartyMonSnapshot:
    return PartyMonSnapshot(
        slot=0,
        species=1,
        level=5,
        hp=hp,
        max_hp=max_hp,
        status=status,
        moves=(0, 0, 0, 0),
        pp=(0, 0, 0, 0),
    )


def _merged_contract_reason(outcome, rule: MedicineRule) -> str:
    if outcome.applied:
        return REASON_APPLIED
    if outcome.branch == BRANCH_REFUSED_FAINTED:
        return REASON_FAINTED_NON_REVIVE
    if outcome.branch == BRANCH_REFUSED_REVIVE_ON_LIVING:
        return REASON_REVIVE_ON_LIVING
    if outcome.branch == BRANCH_REFUSED_FULL_HP:
        return REASON_FULL_HP_NO_STATUS if rule.cure_mask else REASON_FULL_HP
    raise AssertionError(f"unmapped oracle branch: {outcome.branch!r}")


# ---------------------------------------------------------------------------
# Committed excerpts are byte-exact slices of the pinned sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record", ALL_EXCERPTS, ids=EXCERPT_IDS)
def test_committed_excerpt_digest_is_the_pinned_digest(record) -> None:
    text = excerpt_text(record)
    assert text.endswith("\n"), "excerpt must end at a line boundary"
    assert digest_text(text) == record.excerpt_sha256
    assert record.first_line <= record.last_line
    assert text.count("\n") == record.last_line - record.first_line + 1


def test_item_id_excerpt_is_shared_by_both_sources() -> None:
    red_blue, yellow = ITEM_ID_EXCERPTS
    assert red_blue.data_file == yellow.data_file
    assert red_blue.excerpt_sha256 == yellow.excerpt_sha256
    # The whole files differ, so the shared slice is a real agreement rather
    # than two checkouts of the same commit.
    assert red_blue.file_sha256 != yellow.file_sha256
    assert red_blue.revision != yellow.revision


def test_hp_application_excerpts_agree_line_for_line() -> None:
    red_blue = excerpt_text(HP_APPLICATION_EXCERPTS[0]).splitlines()
    yellow = excerpt_text(HP_APPLICATION_EXCERPTS[1]).splitlines()
    assert [line for line in yellow if line.strip()] == [line for line in red_blue if line.strip()]
    assert red_blue != yellow, "the two excerpts are kept separate for byte-exactness"


# ---------------------------------------------------------------------------
# The ladder, its order, and its interpreter
# ---------------------------------------------------------------------------


def test_red_blue_ladder_parses_to_the_pinned_operations() -> None:
    assert pinned_heal_ladder_ops() == (
        LadderOp("compare", "SODA_POP", "cp SODA_POP"),
        LadderOp("load", 60, "ld b, 60"),
        LadderOp("branch", "z", "jr z, .addHealAmount"),
        LadderOp("load", 80, "ld b, 80"),
        LadderOp("branch", "nc", "jr nc, .addHealAmount"),
        LadderOp("compare", "FRESH_WATER", "cp FRESH_WATER"),
        LadderOp("load", 50, "ld b, 50"),
        LadderOp("branch", "z", "jr z, .addHealAmount"),
        LadderOp("compare", "SUPER_POTION", "cp SUPER_POTION"),
        LadderOp("load", 200, "ld b, 200"),
        LadderOp("branch", "c", "jr c, .addHealAmount"),
        LadderOp("load", 50, "ld b, 50"),
        LadderOp("branch", "z", "jr z, .addHealAmount"),
        LadderOp("load", 20, "ld b, 20"),
    )


def test_yellow_ladder_parses_to_the_same_program_as_red_blue() -> None:
    assert yellow_heal_ladder_ops() == pinned_heal_ladder_ops()


def test_ladder_order_is_load_bearing() -> None:
    ops = pinned_heal_ladder_ops()
    assert ladder_compare_symbols(ops) == ("SODA_POP", "FRESH_WATER", "SUPER_POTION")
    # The comparisons are ordered by descending constant value, so the
    # branches below only resolve correctly while the ID ordering holds.
    ids = pinned_item_ids()
    compared = [ids[symbol] for symbol in ladder_compare_symbols(ops)]
    assert compared == sorted(compared, reverse=True)
    with pytest.raises(ValueError, match="ladder"):
        parse_heal_ladder_ops(".notUsingSoftboiled2\n\tld b, 20\n")


@pytest.mark.parametrize("symbol,amount", sorted(PINNED_FIXED_AMOUNTS.items()))
def test_ladder_resolves_documented_amounts_by_item_id(symbol: str, amount: int) -> None:
    assert raw_loaded_amount(pinned_item_ids()[symbol]) == amount


def test_ladder_interpreter_reads_the_amount_from_the_program() -> None:
    mutated = tuple(
        LadderOp(op.kind, 250 if op.operand == 200 else op.operand, op.text)
        for op in pinned_heal_ladder_ops()
    )
    ids = pinned_item_ids()
    assert raw_loaded_amount(ids["HYPER_POTION"], ops=mutated) == 250
    outcome = heal_application(ids["HYPER_POTION"], 100, 999, 0x00, ops=mutated)
    assert (outcome.hp, outcome.branch) == (350, BRANCH_FIXED_HEAL)
    assert heal_application(ids["POTION"], 100, 999, 0x00, ops=mutated).hp == 120


def test_ladder_interpreter_rejects_an_unsupported_condition() -> None:
    ops = pinned_heal_ladder_ops() + (LadderOp("branch", "nz", "jr nz, .addHealAmount"),)
    with pytest.raises(ValueError, match="condition"):
        run_heal_ladder(pinned_item_ids()["POTION"], ops=ops, item_ids=pinned_item_ids())


def test_ladder_parser_rejects_a_reworded_instruction() -> None:
    text = excerpt_text(HP_APPLICATION_EXCERPTS[0]).replace(
        "jr c, .addHealAmount", "jp c, .addHealAmount"
    )
    with pytest.raises(ValueError, match="unexpected ladder instruction"):
        parse_heal_ladder_ops(text)


def test_ladder_condition_table_matches_the_cp_semantics() -> None:
    # ``cp n`` compares the item id in ``a`` against the operand: ``jr z`` takes
    # equality, ``jr c`` takes ``a < n`` and ``jr nc`` takes ``a >= n``, so
    # equality must *jump* for ``nc``.  On the pinned program the rungs that use
    # ``c``/``nc`` are preceded by a ``jr z`` that consumes the equality case, so
    # the boundary is asserted here against the table itself.
    assert LADDER_CONDITIONS["z"](5, 5) is True
    assert LADDER_CONDITIONS["z"](5, 6) is False
    assert LADDER_CONDITIONS["c"](5, 4) is True
    assert LADDER_CONDITIONS["c"](5, 5) is False
    assert LADDER_CONDITIONS["nc"](5, 5) is True
    assert LADDER_CONDITIONS["nc"](5, 4) is False


def test_pinned_ladder_only_branches_on_conditions_the_table_defines() -> None:
    conditions = [op.operand for op in pinned_heal_ladder_ops() if op.kind == OP_BRANCH]
    assert set(conditions) == {"z", "c", "nc"}
    assert set(conditions) <= set(LADDER_CONDITIONS)


# ---------------------------------------------------------------------------
# Item-ID table derived from declaration order
# ---------------------------------------------------------------------------


def test_item_ids_derived_from_declaration_order_match_the_pinned_values() -> None:
    derived = pinned_item_ids()
    for name, value in EXPECTED_ITEM_IDS.items():
        assert derived[name] == value, name


def test_item_id_annotations_agree_with_declaration_order() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0])
    annotated = parse_item_id_annotations(text)
    derived = parse_item_ids(text)
    assert annotated == derived


def test_item_id_parser_rejects_an_annotation_that_disagrees() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0]).replace(
        "const POTION        ; $14", "const POTION        ; $15"
    )
    with pytest.raises(ValueError, match="annotated"):
        parse_item_ids(text)


def test_item_id_parser_rejects_an_unknown_directive() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0]).replace("const_def", "const_def\n\tconst_bogus 1")
    with pytest.raises(ValueError, match="unexpected item-constant directive"):
        parse_item_ids(text)


def test_item_id_parser_requires_a_const_def_block() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0]).replace("const_def\n", "")
    with pytest.raises(ValueError, match="const_def"):
        parse_item_ids(text)


def test_overload_lines_are_aliases_and_do_not_consume_an_item_id() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0])
    # The pinned table overloads two badge IDs.  Neither declaration names a new
    # ID, so the item that follows each of them must keep the next value in the
    # sequence: an off-by-one here would move every later item ID.
    assert "DEF SAFARI_BAIT EQU BOULDERBADGE ; overload\n" in text
    assert "DEF SAFARI_ROCK EQU CASCADEBADGE ; overload\n" in text
    ids = parse_item_ids(text)
    assert ids["BOULDERBADGE"] == 0x15
    assert ids["CASCADEBADGE"] == 0x16
    assert "SAFARI_BAIT" not in ids
    assert parse_item_aliases(text) == {"SAFARI_BAIT": 0x15, "SAFARI_ROCK": 0x16}
    assert pinned_item_aliases() == parse_item_aliases(text)


def test_item_id_parser_rejects_an_alias_to_a_constant_declared_below_it() -> None:
    # ``S_S_TICKET`` is the first item after the committed slice, so aliasing it
    # is a forward reference rather than an overload of a declared item.
    text = excerpt_text(ITEM_ID_EXCERPTS[0]).replace("EQU BOULDERBADGE", "EQU S_S_TICKET")
    with pytest.raises(ValueError, match="not declared above it"):
        parse_item_ids(text)


def test_item_id_parser_rejects_an_alias_that_redeclares_an_item() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0]).replace("DEF SAFARI_BAIT EQU", "DEF POTION EQU")
    with pytest.raises(ValueError, match="duplicate item constant"):
        parse_item_ids(text)


def test_trailing_comment_after_the_annotation_is_not_read_as_the_value() -> None:
    text = excerpt_text(ITEM_ID_EXCERPTS[0])
    assert "\tconst ITEM_2C       ; $2C ; unused\n" in text
    ids = parse_item_ids(text)
    assert ids["ITEM_2C"] == 0x2C
    assert ids["ITEM_32"] == 0x32
    assert ids["POKE_DOLL"] == 0x33


# ---------------------------------------------------------------------------
# The two-byte add
# ---------------------------------------------------------------------------


def test_two_byte_add_carries_into_the_high_byte() -> None:
    assert add_heal_amount(0x00FA, 200) == 450
    assert add_heal_amount(0x0100, 0) == 256
    assert add_heal_amount(0x00FF, 1) == 256


def test_two_byte_add_wraps_instead_of_saturating() -> None:
    # ``inc [hl]`` on a high byte of $FF wraps to $00, so an amount that
    # overflows the field is truncated rather than clamped.
    assert add_heal_amount(0xFFF5, 200) == (0xFFF5 + 200) & 0xFFFF == 189
    saturated = min(0xFFFF, 0xFFF5 + 200)
    assert saturated == 0xFFFF
    assert saturated != add_heal_amount(0xFFF5, 200)
    outcome = heal_application(pinned_item_ids()["HYPER_POTION"], 0xFFF5, 0xFFFF, 0x00)
    assert (outcome.hp, outcome.branch) == (189, BRANCH_FIXED_HEAL)


def test_heal_amount_must_fit_in_the_b_register() -> None:
    with pytest.raises(ValueError, match="heal amount"):
        add_heal_amount(10, 256)
    with pytest.raises(ValueError, match="heal amount"):
        add_heal_amount(10, -1)


# ---------------------------------------------------------------------------
# Uncapped fixed amounts: the wrong-constant detectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol,hp,max_hp,expected",
    (
        ("POTION", 1, 999, 21),
        ("SUPER_POTION", 1, 999, 51),
        ("HYPER_POTION", 1, 999, 201),
        ("POTION", 100, 999, 120),
        ("SUPER_POTION", 100, 999, 150),
        ("HYPER_POTION", 100, 999, 300),
        ("HYPER_POTION", 250, 999, 450),
    ),
)
def test_uncapped_fixed_amounts_below_the_cap(
    symbol: str, hp: int, max_hp: int, expected: int
) -> None:
    outcome = heal_application(pinned_item_ids()[symbol], hp, max_hp, 0x00)
    assert (outcome.hp, outcome.branch) == (expected, BRANCH_FIXED_HEAL)


@pytest.mark.parametrize(
    "symbol,amount", (("POTION", 20), ("SUPER_POTION", 50), ("HYPER_POTION", 200))
)
def test_clamp_boundary_sits_exactly_at_max_hp(symbol: str, amount: int) -> None:
    item_id = pinned_item_ids()[symbol]
    below = heal_application(item_id, 400 - amount - 1, 400, 0x00)
    assert (below.hp, below.branch) == (399, BRANCH_FIXED_HEAL)
    at = heal_application(item_id, 400 - amount, 400, 0x00)
    assert (at.hp, at.branch) == (400, BRANCH_CLAMPED_TO_MAX)
    past = heal_application(item_id, 400 - amount + 1, 400, 0x00)
    assert (past.hp, past.branch) == (400, BRANCH_CLAMPED_TO_MAX)


def test_max_potion_and_hyper_potion_are_distinguished_by_constant_value() -> None:
    ids = pinned_item_ids()
    assert ids["MAX_POTION"] == 0x11
    assert ids["HYPER_POTION"] == 0x12
    assert ids["MAX_POTION"] < ids["HYPER_POTION"] < ids["SUPER_POTION"]
    max_potion = heal_application(ids["MAX_POTION"], 100, 999, 0x00)
    hyper_potion = heal_application(ids["HYPER_POTION"], 100, 999, 0x00)
    assert (max_potion.hp, max_potion.branch) == (999, BRANCH_CLAMPED_TO_MAX)
    assert (hyper_potion.hp, hyper_potion.branch) == (300, BRANCH_FIXED_HEAL)
    assert clamps_to_max_hp(ids["MAX_POTION"], ids)
    assert not clamps_to_max_hp(ids["HYPER_POTION"], ids)


def test_swapping_max_potion_and_hyper_potion_changes_the_outcome() -> None:
    ids = pinned_item_ids()
    swapped = {**ids, "MAX_POTION": 0x12, "HYPER_POTION": 0x11}
    assert clamps_to_max_hp(0x11, swapped) is False
    assert heal_application(0x11, 100, 999, 0x00, item_ids=swapped).hp == 300
    assert heal_application(0x11, 100, 999, 0x00).hp == 999


def test_moving_full_restore_above_hyper_potion_removes_the_clamp() -> None:
    ids = pinned_item_ids()
    moved = {**ids, "FULL_RESTORE": 0x20}
    assert clamps_to_max_hp(0x20, moved) is False
    outcome = heal_application(0x20, 10, 999, 0x08, item_ids=moved)
    # Neither clamp is taken, so the routine jumps from the ladder straight to
    # ``.updateInBattleData`` and never reaches ``.doneHealingPartyHP``: the
    # party status byte survives even though the ID is Full Restore.
    assert (outcome.hp, outcome.status, outcome.branch) == (30, 0x08, BRANCH_FIXED_HEAL)


def test_remapped_full_restore_clears_status_when_the_heal_is_clamped() -> None:
    ids = pinned_item_ids()
    moved = {**ids, "FULL_RESTORE": 0x20}
    # 10 + 20 exceeds the 20 max HP, so the oversized-heal clamp carries the
    # routine into ``.doneHealingPartyHP``, where Full Restore clears status.
    outcome = heal_application(0x20, 10, 20, 0x08, item_ids=moved)
    assert (outcome.hp, outcome.status, outcome.branch) == (20, 0x00, BRANCH_CLAMPED_TO_MAX)


def test_remapped_full_restore_below_hyper_potion_clears_status_by_item_id() -> None:
    ids = pinned_item_ids()
    moved = {**ids, "FULL_RESTORE": 0x0B}
    assert clamps_to_max_hp(0x0B, moved) is True
    # ``cp HYPER_POTION`` / ``jr c`` routes this ID through the same clamp and
    # the same status write, this time without exceeding max HP.
    outcome = heal_application(0x0B, 10, 999, 0x08, item_ids=moved)
    assert (outcome.hp, outcome.status, outcome.branch) == (999, 0x00, BRANCH_CLAMPED_TO_MAX)


def test_full_restore_and_max_potion_clamp_by_id_not_by_loaded_amount() -> None:
    ids = pinned_item_ids()
    for symbol in ("FULL_RESTORE", "MAX_POTION"):
        assert raw_loaded_amount(ids[symbol]) == 200
        assert heal_application(ids[symbol], 100, 999, 0x00).hp == 999


# ---------------------------------------------------------------------------
# Revive and Full Restore branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("max_hp,expected", ((101, 50), (1, 0), (300, 150), (999, 499)))
def test_revive_restores_floor_half_max_hp(max_hp: int, expected: int) -> None:
    outcome = heal_application(pinned_item_ids()["REVIVE"], 0, max_hp, 0x00)
    assert (outcome.hp, outcome.branch) == (expected, BRANCH_REVIVE_HALF_MAX)


def test_max_revive_restores_full_max_hp() -> None:
    outcome = heal_application(pinned_item_ids()["MAX_REVIVE"], 0, 999, 0x00)
    assert (outcome.hp, outcome.branch) == (999, BRANCH_REVIVE_TO_MAX)


@pytest.mark.parametrize("symbol", ("REVIVE", "MAX_REVIVE"))
def test_revive_items_are_refused_on_a_living_target(symbol: str) -> None:
    outcome = heal_application(pinned_item_ids()[symbol], 1, 999, 0x00)
    assert (outcome.applied, outcome.hp, outcome.branch) == (
        False,
        1,
        BRANCH_REFUSED_REVIVE_ON_LIVING,
    )


@pytest.mark.parametrize(
    "symbol", ("POTION", "SUPER_POTION", "HYPER_POTION", "MAX_POTION", "FULL_RESTORE")
)
def test_non_revive_medicine_is_refused_on_a_fainted_target(symbol: str) -> None:
    outcome = heal_application(pinned_item_ids()[symbol], 0, 999, 0x08)
    assert (outcome.applied, outcome.hp, outcome.branch) == (False, 0, BRANCH_REFUSED_FAINTED)


def test_full_restore_zeroes_the_status_byte_on_the_hp_branch() -> None:
    outcome = heal_application(pinned_item_ids()["FULL_RESTORE"], 100, 999, 0x08)
    assert (outcome.hp, outcome.status, outcome.branch) == (999, 0, BRANCH_CLAMPED_TO_MAX)


def test_full_restore_cures_status_at_full_hp_without_changing_hp() -> None:
    outcome = heal_application(pinned_item_ids()["FULL_RESTORE"], 999, 999, 0x08)
    assert (outcome.hp, outcome.status, outcome.branch) == (999, 0, BRANCH_FULL_RESTORE_STATUS_ONLY)


def test_full_restore_at_full_hp_without_status_is_refused() -> None:
    outcome = heal_application(pinned_item_ids()["FULL_RESTORE"], 999, 999, 0x00)
    assert (outcome.applied, outcome.hp, outcome.status, outcome.branch) == (
        False,
        999,
        0x00,
        BRANCH_REFUSED_FULL_HP,
    )


@pytest.mark.parametrize("symbol", ("POTION", "SUPER_POTION", "HYPER_POTION", "MAX_POTION"))
def test_full_hp_target_is_refused_for_every_non_full_restore(symbol: str) -> None:
    outcome = heal_application(pinned_item_ids()[symbol], 999, 999, 0x08)
    assert (outcome.applied, outcome.hp, outcome.status, outcome.branch) == (
        False,
        999,
        0x08,
        BRANCH_REFUSED_FULL_HP,
    )


@pytest.mark.parametrize(
    "symbol", ("POTION", "SUPER_POTION", "HYPER_POTION", "MAX_POTION", "REVIVE")
)
def test_medicine_without_a_status_effect_never_writes_the_status_byte(symbol: str) -> None:
    outcome = heal_application(pinned_item_ids()[symbol], 100, 999, 0x08)
    assert outcome.status == 0x08


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hp,max_hp,status",
    ((700, 699, 0), (0, 0, 0), (1, 999, 256), (1, 65536, 0), (-1, 999, 0)),
)
def test_oracle_rejects_out_of_domain_inputs(hp: int, max_hp: int, status: int) -> None:
    with pytest.raises(ValueError):
        heal_application(pinned_item_ids()["POTION"], hp, max_hp, status)


def test_ladder_rejects_a_constant_the_id_table_does_not_define() -> None:
    # The pinned routine is only reached for medicine, so an ID it does not
    # name resolves through the default rung; an unknown *symbol* is an error.
    ops = (LadderOp("compare", "BOGUS_ITEM", "cp BOGUS_ITEM"),) + pinned_heal_ladder_ops()
    with pytest.raises(ValueError, match="unknown item constant"):
        run_heal_ladder(0x14, ops=ops, item_ids=pinned_item_ids())


# ---------------------------------------------------------------------------
# The merged #89/#93/#94 contract must agree with the pinned oracle
# ---------------------------------------------------------------------------


def test_oracle_agrees_with_the_merged_item_evidence_contract() -> None:
    checked = 0
    for symbol, rule in _rules_for_all_medicine().items():
        for hp, max_hp, status in CORE_CASES:
            expected = medicine_outcome(rule, _mon(hp, max_hp, status))
            actual = heal_application(rule.item_id, hp, max_hp, status)
            assert (actual.hp, actual.status) == (expected.hp, expected.status), (
                symbol,
                hp,
                max_hp,
                status,
            )
            assert _merged_contract_reason(actual, rule) == expected.reason, (
                symbol,
                hp,
                max_hp,
                status,
            )
            checked += 1
    assert checked == len(_rules_for_all_medicine()) * len(CORE_CASES)


# ---------------------------------------------------------------------------
# The opt-in source audit must stay outside the required unit tier
# ---------------------------------------------------------------------------

SOURCE_AUDIT_MODULE = "test_battle_medicine_source_conformance.py"


def test_source_audit_stays_out_of_the_required_unit_tier() -> None:
    """The opt-in checkout audit must never be selectable by a tier marker.

    It skips whenever a pinned upstream checkout is absent, and the required
    unit tier fails closed on any skip, so registering it as ``unit`` would
    fail the asset-free unit gate.  This pins the split that keeps the audit
    runnable while leaving the always-selected suite skip-free.
    """

    assert SOURCE_AUDIT_MODULE in KNOWN_TEST_MODULES
    source = Path(__file__).resolve().parent / SOURCE_AUDIT_MODULE
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert names, "the audit module declares no tests"
    for name in names:
        assert classify_test(SOURCE_AUDIT_MODULE, name) == frozenset(), name
