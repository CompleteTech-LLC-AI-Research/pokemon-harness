"""ROM-free bag snapshot integrity and inventory-consumption tests (#93).

Split from ``tests/test_battle_item_evidence.py`` for #157 with no behavior
change: every assertion and test ID below is preserved verbatim, and the
expectation values stay literal so a wrong formula, amount, status mask,
ordering, or continuation rule fails the test rather than being derived from
the helper under test.
"""

from __future__ import annotations

import pytest

from pokered_harness.state.bag import parse_bag
from tests._battle_item_evidence import (
    POTION_HEAL,
    BagSnapshot,
    BagStack,
    MedicineRule,
    assert_continuation_idempotent,
    assert_last_unit_compaction,
    assert_medicine_application,
    assert_single_consumption,
    validate_snapshot,
)
from tests._battle_item_evidence_factories import (
    make_active,
    make_bag,
    make_mon,
    make_snapshot,
)

# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_bag_snapshot_records_ordered_stacks_and_position_lookup() -> None:
    bag = make_bag(((2, 1), (1, 3), (4, 9)))
    assert bag.quantity_of(1) == 3
    assert bag.quantity_of(4) == 9
    assert bag.quantity_of(3) == 0
    assert bag.has(2) is True
    assert bag.has(3) is False
    assert bag.stacks[1] == BagStack(1, 3)


def test_snapshot_validation_rejects_duplicate_or_malformed_records() -> None:
    active = make_active(0, make_mon(0, hp=10, max_hp=10))
    with pytest.raises(ValueError, match="duplicate bag item stack"):
        validate_snapshot(
            make_snapshot(1, 0, make_bag(((1, 1), (1, 2))), [make_mon(0, hp=10, max_hp=10)], active)
        )
    with pytest.raises(ValueError, match="invalid bag terminator"):
        validate_snapshot(
            make_snapshot(
                1,
                0,
                BagSnapshot(
                    (BagStack(1, 1),),
                    terminator=0x00,
                    raw_count=1,
                    terminator_position=1,
                    valid=True,
                ),
                [make_mon(0, hp=10, max_hp=10)],
                active,
            )
        )
    with pytest.raises(ValueError, match="party HP exceeds max HP"):
        validate_snapshot(
            make_snapshot(1, 0, make_bag(((1, 1),)), [make_mon(0, hp=11, max_hp=10)], active)
        )
    with pytest.raises(ValueError, match="party slots must be contiguous"):
        validate_snapshot(
            make_snapshot(
                1,
                1,
                make_bag(((1, 1),)),
                [make_mon(0, hp=10, max_hp=10), make_mon(2, hp=10, max_hp=10)],
                active,
            )
        )


# ---------------------------------------------------------------------------
# Inventory consumption (#93)
# ---------------------------------------------------------------------------


def test_successful_consumption_decrements_one_from_a_larger_stack() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(
        1, 0, make_bag(((4, 2), (1, 3), (5, 1))), party, make_active(0, party[0])
    )
    after_party = [make_mon(0, hp=50, max_hp=100)]
    after = make_snapshot(
        1, 0, make_bag(((4, 2), (1, 2), (5, 1))), after_party, make_active(0, after_party[0])
    )

    assert_single_consumption(before, after, 1, expected_consumed=1)
    assert before.bag.quantity_of(1) - after.bag.quantity_of(1) == 1


def test_last_unit_is_removed_compacted_and_terminator_preserved() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(
        1, 0, make_bag(((4, 2), (1, 1), (5, 1))), party, make_active(0, party[0])
    )
    after_party = [make_mon(0, hp=50, max_hp=100)]
    after = make_snapshot(
        1, 0, make_bag(((4, 2), (5, 1))), after_party, make_active(0, after_party[0])
    )

    assert_last_unit_compaction(before, after, 1)
    assert after.bag.stacks == (BagStack(4, 2), BagStack(5, 1))
    assert after.bag.terminator == 0xFF


def test_stale_bag_count_after_removal_is_rejected() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(
        1, 0, make_bag(((4, 2), (1, 1), (5, 1))), party, make_active(0, party[0])
    )
    # The last unit was removed and the stack list compacted to two entries,
    # but the ROM count byte was not updated and still reads three.
    stale = make_snapshot(
        1,
        0,
        BagSnapshot(
            (BagStack(4, 2), BagStack(5, 1)),
            raw_count=3,
            terminator_position=3,
            valid=True,
        ),
        [make_mon(0, hp=50, max_hp=100)],
        make_active(0, party[0]),
    )

    with pytest.raises(ValueError, match="raw count does not match"):
        assert_last_unit_compaction(before, stale, 1)


def test_omitted_bag_validity_is_rejected() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(
        1, 0, make_bag(((4, 2), (1, 1), (5, 1))), party, make_active(0, party[0])
    )
    # Count and terminator position are present but the validity tri-state was
    # not carried over from the parser, so acceptance must fail closed.
    omitted = make_snapshot(
        1,
        0,
        BagSnapshot(
            (BagStack(4, 2), BagStack(5, 1)),
            raw_count=2,
            terminator_position=2,
        ),
        [make_mon(0, hp=50, max_hp=100)],
        make_active(0, party[0]),
    )

    with pytest.raises(ValueError, match="bag observation is not valid"):
        assert_last_unit_compaction(before, omitted, 1)


def test_after_snapshot_item_identity_is_enforced() -> None:
    rule = MedicineRule(item_id=1, fixed_heal=POTION_HEAL)
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((1, 3),)), party, make_active(0, party[0]))
    after_party = [make_mon(0, hp=50, max_hp=100)]
    # The observation is relabeled as a different item while applying item 1.
    after = make_snapshot(2, 0, make_bag(((1, 2),)), after_party, make_active(0, after_party[0]))

    with pytest.raises(AssertionError, match="after snapshot is for a different item"):
        assert_medicine_application(before, after, rule, expected_consumed=1)


def test_continuation_item_identity_is_enforced() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((1, 3),)), party, make_active(0, party[0]))
    after = make_snapshot(
        1, 0, make_bag(((1, 2),)), [make_mon(0, hp=50, max_hp=100)], make_active(0, party[0])
    )
    continued = make_snapshot(
        2, 0, make_bag(((1, 2),)), [make_mon(0, hp=50, max_hp=100)], make_active(0, party[0])
    )

    with pytest.raises(AssertionError, match="after snapshot is for a different item"):
        assert_continuation_idempotent(before, after, continued, 1)


def test_terminator_not_after_last_stack_is_rejected() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(
        1, 0, make_bag(((4, 2), (1, 1), (5, 1))), party, make_active(0, party[0])
    )
    stale = make_snapshot(
        1,
        0,
        BagSnapshot(
            (BagStack(4, 2), BagStack(5, 1)),
            raw_count=2,
            terminator_position=3,
            valid=True,
        ),
        [make_mon(0, hp=50, max_hp=100)],
        make_active(0, party[0]),
    )

    with pytest.raises(ValueError, match="terminator does not immediately follow"):
        assert_last_unit_compaction(before, stale, 1)


def test_parser_backed_missing_terminator_observation_is_rejected(mem, symbols) -> None:
    mem[0xD31D] = 2  # wNumBagItems: two stacks declared
    mem[0xD31E] = 0x01
    mem[0xD31F] = 1
    mem[0xD320] = 0x04
    mem[0xD321] = 5
    parsed = parse_bag(mem, symbols)
    assert parsed.valid is False
    assert parsed.terminator_index is None
    assert parsed.raw_count == 2

    party = [make_mon(0, hp=30, max_hp=100)]
    snapshot = make_snapshot(
        1,
        0,
        BagSnapshot(
            tuple(BagStack(stack.item_id, stack.quantity) for stack in parsed.stacks),
            raw_count=parsed.raw_count,
            terminator_position=parsed.terminator_index,
            valid=parsed.valid,
        ),
        party,
        make_active(0, party[0]),
    )

    with pytest.raises(ValueError, match="bag observation is not valid"):
        assert_last_unit_compaction(snapshot, snapshot, 1)


def test_no_effect_and_cancelled_branches_consume_zero_and_do_not_reorder() -> None:
    party = [make_mon(0, hp=100, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((4, 2), (1, 3))), party, make_active(0, party[0]))
    after = make_snapshot(1, 0, make_bag(((4, 2), (1, 3))), party, make_active(0, party[0]))

    assert_single_consumption(before, after, 1, expected_consumed=0)
    assert_single_consumption(before, after, 1, expected_consumed=0)


def test_double_consumption_across_a_continuation_is_rejected() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((1, 3),)), party, make_active(0, party[0]))
    # A buggy driver advances text twice and decrements twice.
    after = make_snapshot(1, 0, make_bag(((1, 1),)), party, make_active(0, party[0]))

    with pytest.raises(AssertionError, match="observed 2"):
        assert_single_consumption(before, after, 1, expected_consumed=1)


def test_settled_snapshot_does_not_reconsume_on_text_advance() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((1, 2),)), party, make_active(0, party[0]))
    after_party = [make_mon(0, hp=50, max_hp=100)]
    after = make_snapshot(1, 0, make_bag(((1, 1),)), after_party, make_active(0, after_party[0]))
    continued = make_snapshot(
        1, 0, make_bag(((1, 1),)), after_party, make_active(0, after_party[0])
    )

    assert_continuation_idempotent(before, after, continued, 1)
    assert continued.bag.quantity_of(1) == 1


def test_continuation_detects_a_second_decrement_after_text_advance() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((1, 3),)), party, make_active(0, party[0]))
    after_party = [make_mon(0, hp=50, max_hp=100)]
    after = make_snapshot(1, 0, make_bag(((1, 2),)), after_party, make_active(0, after_party[0]))
    # The bug: advancing the continuation text decrements the settled stack again.
    continued = make_snapshot(
        1, 0, make_bag(((1, 1),)), after_party, make_active(0, after_party[0])
    )

    with pytest.raises(AssertionError, match="observed 1"):
        assert_continuation_idempotent(before, after, continued, 1)


def test_inventory_underflow_or_unexpected_stack_change_is_rejected() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((1, 1),)), party, make_active(0, party[0]))
    grew = make_snapshot(1, 0, make_bag(((1, 2),)), party, make_active(0, party[0]))
    with pytest.raises(AssertionError, match="observed -1"):
        assert_single_consumption(before, grew, 1, expected_consumed=1)

    duplicated = make_snapshot(1, 0, make_bag(((1, 1), (9, 1))), party, make_active(0, party[0]))
    after = make_snapshot(1, 0, make_bag(((9, 2),)), party, make_active(0, party[0]))
    with pytest.raises(AssertionError, match="beyond the single consumption"):
        assert_single_consumption(duplicated, after, 1, expected_consumed=1)


def test_consuming_an_absent_or_zero_quantity_stack_is_rejected() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    before = make_snapshot(1, 0, make_bag(((4, 2),)), party, make_active(0, party[0]))
    after = make_snapshot(1, 0, make_bag(((4, 2),)), party, make_active(0, party[0]))
    with pytest.raises(AssertionError, match="inventory underflow"):
        assert_single_consumption(before, after, 1, expected_consumed=1)
    with pytest.raises(ValueError, match="invalid bag quantity"):
        validate_snapshot(
            make_snapshot(
                1,
                0,
                BagSnapshot(
                    (BagStack(1, 0),),
                    raw_count=1,
                    terminator_position=1,
                    valid=True,
                ),
                party,
                make_active(0, party[0]),
            )
        )


def test_expected_consumption_must_be_declared_and_bounded() -> None:
    party = [make_mon(0, hp=30, max_hp=100)]
    snap = make_snapshot(1, 0, make_bag(((1, 1),)), party, make_active(0, party[0]))
    with pytest.raises(ValueError, match="exactly 0 or 1"):
        assert_single_consumption(snap, snap, 1, expected_consumed=2)
    with pytest.raises(AssertionError, match="different item"):
        assert_single_consumption(snap, snap, 2, expected_consumed=0)
