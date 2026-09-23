"""Ownership, rollback and bounded-callback contracts (#147).

Split from ``tests/test_timed_battle_probe.py`` for #147 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. Foreign-thread isolation, hook rollback and finite quotas
only; never gameplay or commercial-ROM acceptance.
"""

import json
import threading

import pytest

from tests._timed_battle_probe_support import (
    FakeSession,
    exception_leaves,
    make_driver,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operation", ["before_step", "after_step", "snapshot", "close"])
def test_foreign_thread_cannot_observe_control_or_remove_hooks(probe, operation):
    session, _, driver = make_driver(probe)
    reads, lookups = len(session.memory.reads), len(session.symbols.lookups)
    hooks = dict(session.hooks)
    errors = []

    def foreign():
        try:
            if operation == "before_step":
                driver.before_step(frame_offset=0)
            elif operation == "after_step":
                driver.after_step(call={})
            else:
                getattr(driver, operation)()
        except BaseException as exc:  # noqa: BLE001 - inspect foreign-thread contract errors.
            errors.append(exc)

    try:
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert "owner" in str(errors[0]).lower()
        assert len(session.memory.reads) == reads
        assert len(session.symbols.lookups) == lookups
        assert session.hooks == hooks
        assert session.forbidden == session.memory.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_partial_registration_rolls_back_owned_hooks_and_preserves_both_errors(
    probe, cleanup_fails
):
    session = FakeSession(probe)
    collision = RuntimeError("test registration collision")
    cleanup = RuntimeError("test rollback failure")
    existing = (object(), object())
    failed = []
    attempted_removals = []

    def register(bank, address, callback, context):
        if len(session.installs) == 3:
            failed.append((bank, address))
            session.hooks[bank, address] = existing
            raise collision
        session.register(bank, address, callback, context)

    def deregister(bank, address):
        location = bank, address
        attempted_removals.append(location)
        if cleanup_fails and location == session.installs[-1]:
            raise cleanup
        session.deregister(bank, address)

    session._pyboy.hook_register = register
    session._pyboy.hook_deregister = deregister
    with pytest.raises((RuntimeError, BaseExceptionGroup)) as caught:
        make_driver(probe, session=session)
    assert len(failed) == 1
    assert attempted_removals == list(reversed(session.installs))
    assert session.hooks[failed[0]] is existing
    assert failed[0] not in attempted_removals
    leaves = exception_leaves(caught.value)
    assert collision in leaves
    assert (cleanup in leaves) is cleanup_fails
    expected = {failed[0], session.installs[-1]} if cleanup_fails else {failed[0]}
    assert set(session.hooks) == expected
    assert session.memory.forbidden == session.forbidden == []


def test_close_attempts_all_owned_hooks_even_when_one_removal_fails(probe):
    session, _, driver = make_driver(probe)
    installed = list(session.installs)
    sentinel = (object(), object())
    session.hooks[99, 0x4444] = sentinel
    attempted = []

    def deregister(bank, address):
        location = bank, address
        attempted.append(location)
        if location == installed[-1]:
            raise RuntimeError("one removal failed")
        session.deregister(bank, address)

    session._pyboy.hook_deregister = deregister
    try:
        with pytest.raises((RuntimeError, BaseExceptionGroup)):
            driver.close()
        assert attempted == list(reversed(installed))
        assert session.hooks[99, 0x4444] is sentinel
        # A native hook that could not be removed must be inert after close.
        callback, context = session.hooks[installed[-1]]
        reads = len(session.memory.reads)
        callback(context)
        assert len(session.memory.reads) == reads
    finally:
        session._pyboy.hook_deregister = session.deregister
        driver.close()
    assert session.hooks == {(99, 0x4444): sentinel}


def test_callbacks_and_reports_are_bounded_and_never_apply_input(probe):
    session, _, driver = make_driver(probe)
    before = dict(session.memory.values)
    try:
        for _ in range(100):
            session.fire("SaveGameData")
        snapshot = driver.snapshot()
        assert len(snapshot["recent_transitions"]) <= 32
        assert len(json.dumps(snapshot)) < 16000
        assert session._pyboy.frame_count == 0
        assert session.memory.values == before
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


def test_repeated_frame_and_nonprogressing_inputs_have_finite_quotas(probe):
    session, _, driver = make_driver(probe)
    actions = []
    try:
        for frame in range(0, 10000, 40):
            try:
                action = driver.before_step(frame_offset=frame)
            except RuntimeError:
                break
            if action is not None:
                assert isinstance(action, tuple) and len(action) == 2
                actions.append(action)
                assert driver.before_step(frame_offset=frame) is None
        assert actions, "test must exercise input proposals, not a permanently idle fake"
        assert len(actions) <= 64, "unbounded retries on a nonprogressing menu"
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize(
    "side,fault",
    [
        ("local", None),
        *[
            (side, fault)
            for side in ("local", "enemy")
            for fault in ("unsupported_effect", "corrupt_id", "table_id", "zero_power")
        ],
    ],
)
def test_observer_requires_valid_moves_completed_actions_and_a_later_boundary(probe, side, fault):
    session, _, driver = make_driver(probe)

    def menu(**fields):
        for name, value in fields.items():
            session.set_bytes(name, [value])

    try:
        session.fire("MainInBattleLoop")
        menu(
            wCurrentMenuItem=0,
            wMaxMenuItem=1,
            wMenuWatchedKeys=1,
            wTopMenuItemY=14,
            wTopMenuItemX=15,
        )
        session.fire("DisplayBattleMenu")
        session.fire("HandleMenuInput")
        assert driver.before_step(frame_offset=0)[0] == "a"  # Inspect party.
        session.fire("HandlePartyMenuInput")
        menu(wPartyMenuTypeOrMessageID=0, wMenuWatchedKeys=3, wMaxMenuItem=0)
        assert driver.before_step(frame_offset=20)[0] == "b"
        menu(wTopMenuItemX=9, wMenuWatchedKeys=1, wMaxMenuItem=1)
        session.fire("DisplayBattleMenu")
        session.fire("HandleMenuInput")
        assert driver.before_step(frame_offset=40)[0] == "a"  # FIGHT.
        menu(wCurrentMenuItem=1, wMaxMenuItem=2)
        session.fire("MoveSelectionMenu")
        session.fire("HandleMenuInput")
        assert driver.before_step(frame_offset=60)[0] == "a"
        menu(
            wPlayerSelectedMove=1,
            wEnemySelectedMove=33,
            wSerialExchangeNybbleSendData=0,
            wSerialExchangeNybbleReceiveData=0,
        )
        move_id = 1 if side == "local" else 33
        table = 0x4000 + (move_id - 1) * 6
        if fault == "unsupported_effect":
            session.memory.values[14, table + 1] = 1
        elif fault == "corrupt_id":
            if side == "local":
                menu(wPlayerSelectedMove=166)
            else:
                session.set_bytes("wEnemyMonMoves", [166, 0, 0, 0])
        elif fault == "table_id":
            session.memory.values[14, table] = move_id + 1
        elif fault == "zero_power":
            session.memory.values[14, table + 2] = 0
        bank, start = session.symbols.bank_addr("SelectEnemyMove")
        callback, context = session.hooks[bank, start + 13]
        callback(context)
        if fault:
            with pytest.raises((ValueError, RuntimeError)):
                driver.after_step(call={})
            assert driver.snapshot()["turn"] is None
            assert driver.objective_complete() is False
            assert session.memory.forbidden == session.forbidden == []
            return
        for label in ("ExecutePlayerMove", "ExecuteEnemyMove"):
            session.fire(label)
        assert driver.snapshot()["turn"] is None
        assert driver.objective_complete() is False
        session.fire("PlayerCanExecuteMove")
        session.set_bytes("wBattleMonPP", [19, 0, 0, 0])
        menu(hWhoseTurn=0)
        session.fire("DecrementPP")
        session.set_bytes("wDamage", [0, 15])
        session.fire("ApplyDamageToEnemyPokemon")
        session.set_bytes("wEnemyMonHP", [0, 65])
        session.fire("ApplyAttackToEnemyPokemonDone")
        session.fire("ExecutePlayerMoveDone")
        session.fire("EnemyCanExecuteMove")
        menu(hWhoseTurn=1)
        session.fire("DecrementPP")  # Enemy-side entry must not count as local PP use.
        session.set_bytes("wDamage", [0, 10])
        session.fire("ApplyDamageToPlayerPokemon")
        session.set_bytes("wBattleMonHP", [0, 70])
        session.fire("ApplyAttackToPlayerPokemonDone")
        session.fire("ExecuteEnemyMoveDone")
        assert driver.snapshot()["turn"] is None, "Done entries are not the settled boundary"
        session.fire("MainInBattleLoop")
        snapshot = driver.snapshot()
        assert snapshot["unsupported_reason"] is None
        assert snapshot["turn"]["local"]["hp"] == 70
        assert snapshot["turn"]["enemy"]["hp"] == 65
        assert snapshot["turn"]["pp"]["after"] == [19, 0, 0, 0]
        assert snapshot["turn"]["pp"]["decrement_entries"] == 1
        assert snapshot["baseline"]["seq"] < snapshot["turn"]["exchange_seq"]
        assert snapshot["turn"]["exchange_seq"] < snapshot["turn"]["settled_seq"]
        assert driver.objective_complete() is True
        assert driver.before_step(frame_offset=80) is None
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()
