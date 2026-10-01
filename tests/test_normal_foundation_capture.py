"""Asset-free guards for the normal-input foundation controller."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import produce_normal_foundation as capture


@dataclass
class Mon:
    species: int = 0x99


@dataclass
class Party:
    count: int = 1
    mons: list[Mon] = field(default_factory=lambda: [Mon()])


@dataclass
class Overworld:
    map_id: int = capture.walkthrough.MAP_OAKS_LAB
    x: int = 8
    y: int = 4


@dataclass
class Battle:
    active: bool = False


@dataclass
class State:
    party: Party = field(default_factory=Party)
    overworld: Overworld = field(default_factory=Overworld)
    battle: Battle = field(default_factory=Battle)


class FakeSession:
    def __init__(self):
        self.tick = 0
        self.calls = []
        self.state = State()
        self.close_error = None

    def current_tick(self):
        return self.tick

    def press(self, button, *, duration):
        self.calls.append(("press", button, duration))
        self.tick += duration

    def step(self, ticks, *, render):
        self.calls.append(("step", ticks, render))
        self.tick += ticks

    def read_game_state(self):
        return self.state

    def save_state(self):
        return b"unit-control-checkpoint-not-a-ROM-state"

    def close(self, *, save):
        self.calls.append(("close", save))
        if self.close_error:
            raise self.close_error


def inputs(tmp_path: Path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    rom, symbols = folder / "red.gb", folder / "red.sym"
    rom.write_bytes(b"unit-rom-control")
    symbols.write_bytes(b"unit-symbol-control")
    return rom, symbols


@pytest.mark.parametrize("extension", (".sav", ".ram", ".state"))
def test_fresh_input_guard_refuses_inherited_files(tmp_path, extension):
    rom, symbols = inputs(tmp_path)
    (rom.parent / f"red.gb{extension}").write_bytes(b"inherited")
    with pytest.raises(capture.CaptureRefused, match="inherited"):
        capture.require_fresh_inputs(rom, symbols, tmp_path / "output")


def test_existing_output_is_preserved(tmp_path):
    rom, symbols = inputs(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "earlier-receipt"
    marker.write_bytes(b"preserved")
    with pytest.raises(capture.CaptureRefused, match="already exists"):
        capture.require_fresh_inputs(rom, symbols, output)
    assert marker.read_bytes() == b"preserved"


@pytest.mark.parametrize("seconds", (0, -1, float("inf"), float("nan"), True))
def test_invalid_wall_budget_never_launches(tmp_path, seconds):
    rom, symbols = inputs(tmp_path)
    output = tmp_path / "output"
    with pytest.raises(capture.CaptureRefused, match="finite and positive"):
        capture.capture(rom, symbols, output, stop_after="pick_starter", seconds=seconds)
    assert not output.exists()


def test_journal_records_exact_ordinary_actions_and_deadline(tmp_path, monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(capture.time, "monotonic", lambda: 1)
    driver = capture.JournalDriver(session, tmp_path / "output", deadline=2)
    driver.press("a", duration=6, step_ticks=24)
    driver.idle(10)
    assert session.calls == [("press", "a", 6), ("step", 24, True), ("step", 10, False)]
    rows = [json.loads(line) for line in driver.journal.read_text().splitlines()]
    assert [row["status"] for row in rows] == ["intent", "completed", "intent", "completed"]
    assert [(row["tick_before"], row["tick_after"]) for row in rows[1::2]] == [(0, 30), (30, 40)]
    monkeypatch.setattr(capture.time, "monotonic", lambda: 2)
    with pytest.raises(capture.CaptureRefused, match="deadline"):
        driver.press("b")
    assert session.calls == [("press", "a", 6), ("step", 24, True), ("step", 10, False)]


@pytest.mark.parametrize("invalid", ("map", "starter", "battle"))
def test_phase_boundary_requires_real_observed_conditions(invalid):
    state = State()
    state.overworld.map_id = capture.walkthrough.MAP_PALLET_TOWN
    capture.validate_phase("rival_battle", state)
    if invalid == "map":
        state.overworld.map_id = capture.walkthrough.MAP_REDS_HOUSE_2F
    elif invalid == "starter":
        state.party.mons[0].species = 1
    else:
        state.battle.active = True
    with pytest.raises(capture.CaptureRefused):
        capture.validate_phase("rival_battle", state)


def test_failed_phase_keeps_original_error_and_closes_without_save(tmp_path, monkeypatch):
    rom, symbols = inputs(tmp_path)
    session = FakeSession()
    session.close_error = RuntimeError("cleanup-control")
    monkeypatch.setattr(capture.Session, "from_files", lambda *args, **kwargs: session)
    monkeypatch.setattr(capture, "register_default_hooks", lambda session: None)

    def failing(driver):
        driver.press("a")
        raise ValueError("original-phase-control")

    monkeypatch.setattr(capture.walkthrough, "PHASES", [("pick_starter", failing)])
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="original-phase-control"):
        capture.capture(rom, symbols, output, stop_after="pick_starter", seconds=30)
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["error"] == "ValueError: original-phase-control"
    assert receipt["teardown_error"] == "RuntimeError: cleanup-control"
    assert not receipt["session_closed_without_save"]
    assert session.calls[-1] == ("close", False)
    assert not list(output.glob("*.state"))


def test_success_preserves_input_chain_and_closes_without_save(tmp_path, monkeypatch):
    rom, symbols = inputs(tmp_path)
    session = FakeSession()
    monkeypatch.setattr(capture.Session, "from_files", lambda *args, **kwargs: session)
    monkeypatch.setattr(capture, "register_default_hooks", lambda session: None)

    def ordinary_phase(driver):
        driver.press("a")

    monkeypatch.setattr(capture.walkthrough, "PHASES", [("pick_starter", ordinary_phase)])
    output = tmp_path / "output"
    receipt = capture.capture(rom, symbols, output, stop_after="pick_starter", seconds=30)
    assert receipt["status"] == "captured"
    assert receipt["source_state"] is None
    assert not receipt["inherited_battery_ram"]
    assert not receipt["controller_ram_writes"]
    assert receipt["session_closed_without_save"]
    assert receipt["actions_completed"] == 1
    assert session.calls[-1] == ("close", False)
    assert (output / "pick_starter.state").read_bytes() == session.save_state()
    assert json.loads((output / "receipt.json").read_text()) == receipt


def test_successful_phase_with_failed_teardown_is_not_captured(tmp_path, monkeypatch):
    rom, symbols = inputs(tmp_path)
    session = FakeSession()
    session.close_error = RuntimeError("cleanup-control")
    monkeypatch.setattr(capture.Session, "from_files", lambda *args, **kwargs: session)
    monkeypatch.setattr(capture, "register_default_hooks", lambda session: None)
    monkeypatch.setattr(capture.walkthrough, "PHASES", [("pick_starter", lambda driver: None)])
    output = tmp_path / "output"
    with pytest.raises(RuntimeError, match="cleanup-control"):
        capture.capture(rom, symbols, output, stop_after="pick_starter", seconds=30)
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert not receipt["session_closed_without_save"]


def test_route_retains_direction_after_observed_wild_escape():
    session = FakeSession()
    session.state.overworld.map_id = capture.walkthrough.MAP_ROUTE_1
    session.state.battle.kind = SimpleNamespace(name="WILD")
    session.state.battle.menu_open = True
    route_inputs = []
    escape_inputs = []

    class Driver:
        def __init__(self):
            self.session = session

        def input_locked(self):
            return False

        def press(self, button, **kwargs):
            if session.state.battle.active:
                escape_inputs.append(button)
                if button == "a":
                    session.state.battle.active = False
            else:
                route_inputs.append(button)
                session.state.overworld.y -= 1
                if len(route_inputs) == 1:
                    session.state.battle.active = True
                else:
                    session.state.overworld.map_id = capture.walkthrough.MAP_VIRIDIAN_CITY

    capture.run_route1_to_viridian(Driver())
    assert route_inputs == ["up", "up"]
    assert escape_inputs == ["b", "up", "left", "right", "down", "a"]
    assert not session.state.battle.active


@pytest.mark.parametrize("invalid", ("blackout", "trainer", "unsettled"))
def test_route_declines_invalid_or_unsettled_interruptions(invalid):
    session = FakeSession()
    session.state.overworld.map_id = capture.walkthrough.MAP_ROUTE_1
    session.state.battle.kind = SimpleNamespace(name="WILD")
    session.state.battle.menu_open = False
    session.state.battle.active = True
    if invalid == "blackout":
        session.state.overworld.map_id = capture.walkthrough.MAP_REDS_HOUSE_2F
    elif invalid == "trainer":
        session.state.battle.kind.name = "TRAINER"
    driver = SimpleNamespace(session=session, press=lambda *args, **kwargs: None)
    with pytest.raises(capture.CaptureRefused):
        capture.run_route1_to_viridian(driver)
