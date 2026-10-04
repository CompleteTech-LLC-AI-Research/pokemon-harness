"""Catalog shape, one-turn scope, effect families and selector coverage (#143).

Split from ``tests/test_battle_coverage.py`` for #143 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Validates the shipped coverage/effects catalog only; never
instantiates PyBoy or loads a ROM/state.
"""

import copy

import pytest

from scripts import coverage_report as coverage
from scripts import tcp_link_matrix as matrix
from scripts import validate_battle_scenarios as validator
from tests._battle_coverage_support import (
    _EXPECTED_RUNTIMES,
    _manifest,
)


def test_catalog_shape_still_validates_and_declares_nineteen_pairing_cases(catalog: dict) -> None:
    scenarios = validator._validate_schema(catalog, _manifest())
    # Every admitted manifest row is declared, including the captured boundary
    # rows; the one-turn pairing dimension still resolves its canonical
    # settled-battle scenario per version.
    assert len(scenarios) == 28
    coverage.validate_catalog(catalog)

    cases = coverage.one_turn_pairing_cases(catalog)
    assert len(cases) == 19
    assert {case["selector"] for case in cases} == set(matrix.STRICT_BATTLE_NODEIDS)
    for case in cases:
        assert case["runtimes"] == _EXPECTED_RUNTIMES
        assert set(case["roles"]) == {"listen", "connect"}
        assert case["fixture_id"] in {
            scenario["fixture"]["fixture_id"] for scenario in catalog["scenarios"]
        }


def test_report_states_one_turn_scope_separately_from_expanded_mechanics(catalog: dict) -> None:
    report = coverage.build_report(catalog)

    assert report["overall"] == "INCOMPLETE"
    assert report["dimensions"]["one_turn_pairing"]["status"] == "INCOMPLETE"
    assert report["dimensions"]["expanded_mechanics"]["status"] == "PLANNED_UNVERIFIED"
    text = coverage.render_text(report).lower()
    assert "one-turn" in text
    assert "expanded mechanics" in text


def test_effect_families_cover_every_pinned_effect_id(catalog: dict) -> None:
    families = coverage.effect_families(catalog)

    assert len(families) == 87
    assert [family["effect_id"] for family in families] == list(range(87))
    tested = [family["effect_id"] for family in families if family["scope"] == "tested"]
    assert tested == [0]
    for family in families[1:]:
        assert family["scope"] in coverage._SCOPES
        assert isinstance(family["reason"], str) and family["reason"].strip()
        assert isinstance(family["move_ids"], list)
        assert family["scope"] != "tested"


def test_effect_families_account_for_every_move_and_reserved_slot(catalog: dict) -> None:
    families = coverage.effect_families(catalog)

    all_moves = sorted(move for family in families for move in family["move_ids"])
    assert all_moves == list(range(1, 166))
    for family in families:
        if family["move_ids"]:
            if family["effect_id"] == 0:
                assert family["scope"] == "tested"
            else:
                assert family["scope"] == "planned_unverified"
            assert family["kind"] == "effect"
        else:
            assert family["scope"] == "deliberately_excluded"
    reserved = {family["effect_id"] for family in families if family["kind"] == "const_skip"}
    assert reserved == {72, 73, 74, 75, 78}


def test_current_selector_admits_0_6_44_and_excludes_counter(catalog: dict) -> None:
    selector = catalog["move_effects"]["current_selector"]
    families = {family["effect_id"]: family for family in coverage.effect_families(catalog)}

    assert selector["admitted_effect_ids"] == [0, 6, 44]
    assert [entry["move_id"] for entry in selector["excluded_moves"]] == [68]
    assert selector["excluded_moves"][0]["name"] == "COUNTER"
    assert selector["excluded_moves"][0]["effect_id"] == 0
    assert families[0]["scope"] == "tested"
    assert families[0]["evidence"] == ["mechanics_effect_0_no_additional_effect"]
    for effect_id in (6, 44):
        assert families[effect_id]["scope"] == "planned_unverified"


def test_deliberately_excluded_is_exactly_the_pinned_unused_set(catalog: dict) -> None:
    """`deliberately_excluded` is tool-owned, not a free choice (#89).

    #89 requires battle-mechanics coverage to be declared *separately* from the
    ROM pairings, so the expanded dimension is the thing that records which
    mechanics are still outstanding. `deliberately_excluded` is legitimate only
    for the pinned slots that no Red/Blue/Yellow move references and the
    reserved `const_skip` padding; every other family owes real evidence.
    """
    families = coverage.effect_families(catalog)
    excluded = {
        family["effect_id"] for family in families if family["scope"] == "deliberately_excluded"
    }
    owned = {family["effect_id"] for family in families if family["move_ids"]}

    assert excluded == set(coverage._PINNED_UNUSED_EFFECT_IDS)
    assert not (excluded & owned), (
        "an effect that owns moves cannot be deliberately_excluded -- that would "
        "hide outstanding mechanics behind 'out of scope'"
    )


def test_validator_rejects_excluding_a_family_that_owns_moves(catalog: dict) -> None:
    """A count-consistent downgrade to `deliberately_excluded` must fail.

    The declared `planned_unverified_count` / `deliberately_excluded_count`
    cross-checks are necessary but not sufficient: an editor can move a family
    between scopes *and* move the counters with it, and then every counter
    agrees while the outstanding set has silently shrunk. This pins the
    scope-is-tool-owned invariant with the counts left consistent, which is
    the only mutation the counter checks alone cannot see.
    """
    doc = copy.deepcopy(catalog)
    move_effects = doc["move_effects"]
    donor = next(
        family
        for family in move_effects["families"]
        if family["scope"] == "planned_unverified" and family["move_ids"]
    )
    donor["scope"] = "deliberately_excluded"
    donor["reason"] = "probe: downgrade a family that owns moves"
    move_effects["planned_unverified_count"] -= 1
    move_effects["deliberately_excluded_count"] += 1

    # Sanity-check that the mutation is otherwise self-consistent, so a
    # failure below can only come from the scope invariant and not from a
    # counter that was left stale.
    tally = {}
    for family in move_effects["families"]:
        tally[family["scope"]] = tally.get(family["scope"], 0) + 1
    assert tally["planned_unverified"] == move_effects["planned_unverified_count"]
    assert tally["deliberately_excluded"] == move_effects["deliberately_excluded_count"]

    with pytest.raises(coverage.CoverageError, match="must not be deliberately_excluded"):
        coverage.validate_catalog(doc)
