"""Pure contracts for one-turn fallback after a planned move is unusable."""

from __future__ import annotations

from scripts import produce_battle_state_fixtures_drive as drive
from tests import _battle_turn_evidence as evidence


class _Session:
    pass


def _install_moves(monkeypatch, moves):
    monkeypatch.setattr(drive, "_active_moves", lambda _session: list(moves))


def _install_move_records(monkeypatch, records):
    def read_move(_session, move):
        effect, power = records[move]
        return bytes((move, effect, power, 100, 1, 0))

    monkeypatch.setattr(evidence, "_move_data", read_move)


def test_unusable_planned_charge_falls_back_to_stat_move_and_records_it(monkeypatch):
    session = _Session()
    _install_moves(monkeypatch, ((76, 5), (45, 8), (0, 0), (22, 0)))
    _install_move_records(monkeypatch, {76: (0x27, 120), 45: (0x12, 0)})

    cursor = drive._planned_move_cursor(session, target_slot=3, maximum=5)
    plan = [{"slot": 3, "move": 22, "policy": "recorded"}]
    slots = [3]
    drive._record_confirmed_slot(plan, slots, 0, cursor, session)

    assert cursor == 2
    assert slots == [1]
    assert plan == [{"slot": 1, "move": 45, "policy": "pump-fallback"}]


def test_valid_planned_slot_is_preserved_without_fallback_metadata(monkeypatch):
    session = _Session()
    _install_moves(monkeypatch, ((76, 5), (45, 8), (0, 0), (0, 0)))

    def unexpected_read(_session, _move):
        raise AssertionError("a valid planned slot must not invoke fallback metadata")

    monkeypatch.setattr(evidence, "_move_data", unexpected_read)
    assert drive._planned_move_cursor(session, target_slot=0, maximum=5) == 1


def test_zero_pp_and_empty_slots_are_skipped_before_stat_fallback(monkeypatch):
    session = _Session()
    _install_moves(monkeypatch, ((76, 0), (0, 0), (45, 7), (22, 0)))
    _install_move_records(monkeypatch, {45: (0x12, 0)})

    assert drive._planned_move_cursor(session, target_slot=3, maximum=5) == 3


def test_unreadable_metadata_retains_historical_first_usable_fallback(monkeypatch):
    session = _Session()
    _install_moves(monkeypatch, ((76, 5), (45, 8), (0, 0), (22, 0)))

    def unreadable(_session, _move):
        raise KeyError("move table unavailable")

    monkeypatch.setattr(evidence, "_move_data", unreadable)
    assert drive._planned_move_cursor(session, target_slot=3, maximum=5) == 1


def test_no_preferred_move_retains_historical_first_usable_fallback(monkeypatch):
    session = _Session()
    _install_moves(monkeypatch, ((76, 5), (45, 8), (0, 0), (22, 0)))
    _install_move_records(monkeypatch, {76: (0x27, 120), 45: (0, 0)})

    assert drive._planned_move_cursor(session, target_slot=3, maximum=5) == 1


def test_existing_supported_damage_selector_still_selects_its_candidate(monkeypatch):
    session = _Session()
    _install_moves(monkeypatch, ((76, 5), (33, 8), (0, 0), (22, 0)))
    _install_move_records(monkeypatch, {76: (0x27, 120), 33: (0, 40)})

    assert drive._planned_move_cursor(session, target_slot=3, maximum=5) == 2
