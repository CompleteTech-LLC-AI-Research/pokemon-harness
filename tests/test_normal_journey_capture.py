"""Asset-free refusal and receipt controls; these do not qualify gameplay."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import produce_normal_journey as journey
from tests.test_normal_foundation_capture import FakeSession


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    rom, symbols = inputs / "red.gb", inputs / "red.sym"
    rom.write_bytes(b"authored-unit-rom")
    symbols.write_bytes(b"authored-unit-symbols")
    source = tmp_path / "game-source"
    folder = source / "gfx" / "blocksets"
    folder.mkdir(parents=True)
    blocksets = {}
    for name in journey.BLOCKSET_SHA256:
        raw = ("authored-unit-blockset:" + name).encode()
        (folder / name).write_bytes(raw)
        blocksets[name] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(journey, "BLOCKSET_SHA256", blocksets)
    pins = SimpleNamespace(
        sha1_for_path=lambda _: hashlib.sha1(rom.read_bytes()).hexdigest(),
        symbol_sha1_for_path=lambda _: hashlib.sha1(symbols.read_bytes()).hexdigest(),
        pyboy_version="unit-runtime",
        pyboy_revision="unit-revision",
    )
    # Freeze the authored expected values before any negative control mutates inputs.
    rom_pin, sym_pin = pins.sha1_for_path(None), pins.symbol_sha1_for_path(None)
    pins.sha1_for_path = lambda _: rom_pin
    pins.symbol_sha1_for_path = lambda _: sym_pin
    monkeypatch.setattr(journey.foundation, "load_versions", lambda _: pins)
    session = FakeSession()
    launches = []

    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return session

    monkeypatch.setattr(journey.foundation.Session, "from_files", launch)
    monkeypatch.setattr(journey.foundation, "register_default_hooks", lambda _: None)
    return rom, symbols, tmp_path / "output", source, session, launches


@pytest.mark.parametrize("seconds", (True, False, 0, -1, float("inf"), float("nan"), 3601))
def test_invalid_budget_never_launches(prepared, seconds):
    rom, symbols, output, source, _, launches = prepared
    with pytest.raises(journey.foundation.CaptureRefused, match="wall budget"):
        journey.capture(rom, symbols, output, source, seconds=seconds)
    assert not launches
    assert not output.exists()


@pytest.mark.parametrize("which", ("rom", "symbols", "blockset"))
def test_mutated_pin_never_launches(prepared, which):
    rom, symbols, output, source, _, launches = prepared
    target = {"rom": rom, "symbols": symbols, "blockset": source / "gfx/blocksets/gym.bst"}[which]
    target.write_bytes(b"mutated authored input")
    with pytest.raises(journey.foundation.CaptureRefused, match="pin|blockset"):
        journey.capture(rom, symbols, output, source)
    assert not launches
    assert not output.exists()


@pytest.mark.parametrize("extension", (".sav", ".ram", ".state"))
def test_inherited_input_refused(prepared, extension):
    rom, symbols, output, source, _, launches = prepared
    (rom.parent / ("old" + extension)).write_bytes(b"earlier attempt")
    with pytest.raises(journey.foundation.CaptureRefused, match="inherited"):
        journey.capture(rom, symbols, output, source)
    assert not launches
    assert not output.exists()


def test_existing_attempt_preserved(prepared):
    rom, symbols, output, source, _, launches = prepared
    output.mkdir()
    marker = output / "original-receipt"
    marker.write_bytes(b"preserve")
    with pytest.raises(journey.foundation.CaptureRefused, match="already exists"):
        journey.capture(rom, symbols, output, source)
    assert marker.read_bytes() == b"preserve"
    assert not launches


@pytest.mark.parametrize("method", ("menu", "resolution", "end"))
@pytest.mark.parametrize("failure", ("false", "raises"))
def test_initializer_failure_closes_without_input(prepared, method, failure):
    rom, symbols, output, source, session, launches = prepared

    def unavailable():
        if failure == "raises":
            raise RuntimeError("original initializer failure")
        return False

    setattr(session, "enable_battle_" + method + "_observation", unavailable)
    with pytest.raises((journey.foundation.CaptureRefused, RuntimeError)):
        journey.capture(rom, symbols, output, source)
    assert len(launches) == 1
    assert session.calls == [("close", False)]
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["session_closed_without_save"] is True
    assert receipt["actions_attempted"] == 0
    assert not (output / "inputs.jsonl").exists()


def test_duplicate_checkpoint_names_preserve_every_state(prepared):
    rom, symbols, output, source, session, _ = prepared
    owner = journey.NormalJourney(rom, symbols, output, source)
    owner.session = session
    for _ in range(3):
        owner.checkpoint("same_boundary")
    names = [row["name"] for row in owner.receipt["checkpoints"]]
    assert names == ["same_boundary", "same_boundary_2", "same_boundary_3"]
    for row in owner.receipt["checkpoints"]:
        raw = (output / (row["name"] + ".state")).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
    assert not (output / "receipt.json.tmp").exists()


def test_primary_error_survives_checkpoint_and_close_failures(prepared, monkeypatch):
    rom, symbols, output, source, session, _ = prepared
    owner = journey.NormalJourney(rom, symbols, output, source)
    monkeypatch.setattr(journey.foundation.walkthrough, "PHASES", [])

    def primary(*args, **kwargs):
        raise RuntimeError("primary navigation failure")

    def failed_checkpoint(*args, **kwargs):
        raise ValueError("checkpoint failure")

    owner.navigate = primary
    owner.checkpoint = failed_checkpoint
    session.close_error = OSError("close failure")
    with pytest.raises(RuntimeError, match="primary navigation failure"):
        owner.run()
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["error"] == "RuntimeError: primary navigation failure"
    assert receipt["failure_checkpoint_error"] == "ValueError: checkpoint failure"
    assert receipt["teardown_error"] == "OSError: close failure"
    assert receipt["session_closed_without_save"] is False
    with pytest.raises(journey.foundation.CaptureRefused, match="already ran"):
        owner.run()


@pytest.mark.parametrize(
    "seeded,grass,expected", ((False, False, 2), (True, False, 3), (False, True, 3))
)
def test_move_selection_observes_seed_and_pp(monkeypatch, seeded, grass, expected):
    owner = object.__new__(journey.NormalJourney)
    memory = object()
    owner.session = SimpleNamespace(
        _pyboy=SimpleNamespace(memory=memory),
        symbols=SimpleNamespace(addr_of=lambda _: 0xD123),
    )
    reads = []

    def read(bank_memory, address):
        reads.append((bank_memory, address))
        return 0x80 if seeded else 0

    monkeypatch.setattr(journey, "read_wram_u8", read)
    mon = SimpleNamespace(valid=True, moves=(33, 45, 73, 22), pp=(35, 40, 10, 10))
    enemy = SimpleNamespace(valid=True, type1=22 if grass else 4, type2=4)
    state = SimpleNamespace(
        party=SimpleNamespace(active_mon=mon), battle=SimpleNamespace(enemy_mon=enemy)
    )
    assert owner.choose_move_slot(state) == expected
    assert reads == [(memory, 0xD123)]
    mon.pp = (0, 40, 0, 0)
    with pytest.raises(journey.foundation.CaptureRefused, match="readable PP"):
        owner.choose_move_slot(state)


def test_restored_lead_requires_valid_full_hp_and_full_known_pp():
    mon = SimpleNamespace(
        valid=True, hp_valid=True, hp=21, max_hp=21, moves=(33, 45, 0, 0), pp=(35, 40, 0, 0)
    )
    assert journey.NormalJourney.restored_lead(mon) is True
    mon.pp = (34, 40, 0, 0)
    assert journey.NormalJourney.restored_lead(mon) is False
    mon.pp = (35, 40, 0, 0)
    mon.valid = False
    assert journey.NormalJourney.restored_lead(mon) is False
    assert journey.NormalJourney.restored_lead(None) is False
    mon.valid = True
    mon.moves, mon.pp = (0, 0, 0, 0), (0, 0, 0, 0)
    assert journey.NormalJourney.restored_lead(mon) is False


def test_unobserved_wild_command_refuses_without_input():
    owner = object.__new__(journey.NormalJourney)
    owner.training_wild = False
    state = SimpleNamespace(
        battle=SimpleNamespace(active=True, kind=SimpleNamespace(name="WILD"), menu_open=None)
    )
    owner.session = SimpleNamespace(read_game_state=lambda: state)
    calls = []
    owner.driver = SimpleNamespace(
        input_locked=lambda: False, press=lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(journey.foundation.CaptureRefused, match="unavailable"):
        owner.settle_encounter()
    assert calls == []


def test_factory_failure_retains_original_and_no_input(prepared, monkeypatch):
    rom, symbols, output, source, _, _ = prepared

    def fail(*args, **kwargs):
        raise RuntimeError("original factory failure")

    monkeypatch.setattr(journey.foundation.Session, "from_files", fail)
    with pytest.raises(RuntimeError, match="original factory failure"):
        journey.capture(rom, symbols, output, source)
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["error"] == "RuntimeError: original factory failure"
    assert receipt["actions_attempted"] == 0
    assert not (output / "inputs.jsonl").exists()
