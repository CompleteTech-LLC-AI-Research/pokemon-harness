"""Party/move admission contracts for the timed battle probe (#147).

Split from ``tests/test_timed_battle_probe.py`` for #147 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Constructor admission and damaged-code fail-closed behaviour
only; never gameplay or commercial-ROM acceptance.
"""

import pytest

from tests._timed_battle_probe_support import (
    FakeSession,
    make_driver,
    record,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("version", ["red_color", "blue_color", "yellow"])
def test_constructor_accepts_one_living_ordinary_member_without_advancing(probe, version):
    session = FakeSession(probe, version=version)
    before = dict(session.memory.values)
    _, readiness, driver = make_driver(probe, version=version, session=session)
    try:
        snapshot = driver.snapshot()
        assert snapshot["before_party"]["count"] == 1
        assert snapshot["baseline"] is None and snapshot["turn"] is None
        assert snapshot["room_return"] is None
        assert snapshot["full_authentic_acceptance"] is False
        assert "unknown" in snapshot["provenance"]
        assert "party_qualified" in readiness.local
        assert driver.objective_complete() is False
        assert session._pyboy.frame_count == 0
        assert session.memory.values == before
        assert session.memory.forbidden == session.forbidden == []
        snapshot["before_party"]["records"].clear()
        assert len(driver.snapshot()["before_party"]["records"]) == 1
    finally:
        driver.close()
    assert session.hooks == {}


@pytest.mark.parametrize(
    "records,move_slot",
    [
        ([record(pp=0)], None),
        ([record(pp=0)], 0),
        ([record(move=0, pp=0)], None),
        ([record(hp=0)], 0),
        ([record()], 1),
        ([record()], 4),
        ([record()], -1),
        ([record()], True),
    ],
)
def test_unusable_initial_party_or_move_fails_closed_without_repair(probe, records, move_slot):
    session = FakeSession(probe, records=records)
    before = dict(session.memory.values)
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, session=session, move_slot=move_slot)
    assert session.memory.values == before
    assert session.memory.forbidden == session.forbidden == []
    assert session.hooks == {}


@pytest.mark.parametrize("fainted_prefix", [1, 2, 5])
def test_first_living_member_qualifies_even_when_fainted_leads_have_no_pp(probe, fainted_prefix):
    records = [record(hp=0, pp=0) for _ in range(fainted_prefix)] + [record(species=153)]
    session, _, driver = make_driver(probe, records=records)
    try:
        captured = driver.snapshot()["before_party"]
        assert captured["count"] == fainted_prefix + 1
        assert captured["records"] == [r.hex() for r in records]
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


def test_later_member_cannot_replace_first_living_member_with_no_legal_pp(probe):
    session = FakeSession(probe, records=[record(hp=0), record(pp=0), record(species=153)])
    before = dict(session.memory.values)
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, session=session)
    assert session.hooks == {}
    assert session.memory.values == before
    assert session.memory.forbidden == session.forbidden == []


@pytest.mark.parametrize("offset", range(9))
@pytest.mark.parametrize("version", ["red_color", "yellow"])
def test_damaged_exchange_continuation_bytes_fail_before_hook_install(probe, version, offset):
    session = FakeSession(probe, version=version)
    bank, start = session.symbols.bank_addr("SelectEnemyMove")
    session.memory.values[bank, start + 10 + offset] ^= 1
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, version=version, session=session)
    assert session.installs == []
    assert session.hooks == {}
    assert session.memory.forbidden == session.forbidden == []


@pytest.mark.parametrize(
    "label",
    [
        "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed",
        "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed",
    ],
)
@pytest.mark.parametrize("offset", range(6))
def test_damaged_paralysis_branch_is_not_accepted_as_skip_proof(probe, label, offset):
    session = FakeSession(probe)
    bank, end = session.symbols.bank_addr(label)
    session.memory.values[bank, end - 6 + offset] ^= 1
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, session=session)
    assert session.installs == []
    assert session.hooks == {}
