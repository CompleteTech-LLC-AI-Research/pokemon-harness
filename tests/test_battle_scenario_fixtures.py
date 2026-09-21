"""ROM-free tests for the battle-scenario catalog and producer contract.

These tests never instantiate PyBoy or load a ROM/state.  They validate the
declared catalog, recompute fixture hashes from temporary bytes, and exercise
the producer's refusal/bounds logic by injection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import types
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from scripts import validate_battle_scenarios as validator

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "release-evidence" / "battle-scenarios.json"
MANIFEST_PATH = ROOT / "release-evidence" / "fixture-manifest.json"


class _FakePins:
    """Minimal stand-in for ``pokered_harness.config.VersionsConfig``."""

    def __init__(self, rom_sha1: str | None, sym_sha1: str | None) -> None:
        self._rom_sha1 = rom_sha1
        self._sym_sha1 = sym_sha1

    def sha1_for_path(self, path: str | Path) -> str | None:
        return self._rom_sha1

    def symbol_sha1_for_path(self, path: str | Path) -> str | None:
        return self._sym_sha1


def _load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _scenario(catalog: dict, scenario_id: str) -> dict:
    return next(item for item in catalog["scenarios"] if item["scenario_id"] == scenario_id)


def test_catalog_validates_against_the_fixture_manifest() -> None:
    catalog = _load_catalog()
    scenarios = validator._validate_schema(catalog, _load_manifest())
    assert len(scenarios) == 10
    assert {scenario["fixture"]["fixture_id"] for scenario in scenarios} == {
        fixture["id"] for fixture in _load_manifest()["fixtures"]
    }


def test_catalog_copies_provenance_statuses_truthfully_and_has_no_execution_claim() -> None:
    catalog = _load_catalog()
    manifest = _load_manifest()
    observed = {
        scenario["scenario_id"]: scenario["provenance"]["status"]
        for scenario in catalog["scenarios"]
    }
    expected = {
        fixture["id"].replace("-", "_"): fixture["provenance"]["status"]
        for fixture in manifest["fixtures"]
    }
    assert observed == expected
    assert sum(status == "verified" for status in observed.values()) == 6
    assert sum(status == "partial" for status in observed.values()) == 4
    assert all(
        scenario["evidence_class"]["real_rom_execution"] is None
        for scenario in catalog["scenarios"]
    )


def test_scenario_ids_match_the_pattern_and_are_unique() -> None:
    catalog = _load_catalog()
    ids = [scenario["scenario_id"] for scenario in catalog["scenarios"]]
    assert len(ids) == len(set(ids))
    for scenario_id in ids:
        validator._validate_scenario_id(scenario_id, "scenario_id")
        assert validator.base_scenario_id(scenario_id) == scenario_id


@pytest.mark.parametrize(
    "scenario_id",
    [
        "red_color",
        "red_color_2",
        "red_color__listen",
        "red_color__connect",
    ],
)
def test_scenario_id_regex_accepts_declared_and_role_suffixed_ids(scenario_id: str) -> None:
    validator._validate_scenario_id(scenario_id, "scenario_id")


@pytest.mark.parametrize(
    "scenario_id",
    [
        "",
        "Red_Color",
        "red-color",
        "red__",
        "red color",
        "__listen",
        "red__listen__connect",
    ],
)
def test_scenario_id_regex_rejects_malformed_ids(scenario_id: str) -> None:
    with pytest.raises(ValueError, match="scenario_id"):
        validator._validate_scenario_id(scenario_id, "scenario_id")


def test_duplicate_scenario_ids_are_rejected() -> None:
    catalog = _load_catalog()
    duplicated = copy.deepcopy(catalog)
    duplicated["scenarios"].append(copy.deepcopy(duplicated["scenarios"][0]))
    with pytest.raises(ValueError, match="duplicate scenario id"):
        validator._validate_schema(duplicated, _load_manifest())
    with pytest.raises(producer.ScenarioRefusal, match="duplicate scenario id"):
        producer.scenario_index(duplicated)


def test_verified_provenance_requires_identity_timestamp_and_method() -> None:
    provenance = {
        "status": "verified",
        "producer": "scripts/produce_cable_club_fixture.py",
        "source_fixture_id": None,
        "input_sequence": None,
        "capture_command_template": "python scripts/produce_cable_club_fixture.py",
        "runtime_identity": None,
        "captured_at_utc": None,
        "verification_method": None,
    }
    with pytest.raises(ValueError, match="runtime_identity is required"):
        validator._validate_provenance(provenance, "provenance")


def test_derived_provenance_requires_source_and_transformation() -> None:
    provenance = {
        "status": "derived",
        "producer": "scripts/prepare_battle_cable_club_fixtures.py",
        "source_fixture_id": None,
        "input_sequence": None,
        "capture_command_template": "python scripts/prepare_battle_cable_club_fixtures.py",
        "runtime_identity": None,
        "captured_at_utc": None,
        "verification_method": None,
    }
    with pytest.raises(ValueError, match="source_fixture_id is required"):
        validator._validate_provenance(provenance, "provenance")

    provenance["source_fixture_id"] = "red-color-ordinary"
    with pytest.raises(ValueError, match="transformation is required"):
        validator._validate_provenance(provenance, "provenance")

    provenance["input_sequence"] = "copy lead record into six party slots"
    validator._validate_provenance(provenance, "provenance")


def test_partial_and_unknown_provenance_stay_visible() -> None:
    base = {
        "producer": "scripts/produce_cable_club_fixture.py",
        "source_fixture_id": None,
        "input_sequence": None,
        "capture_command_template": "python scripts/produce_cable_club_fixture.py",
        "runtime_identity": None,
        "captured_at_utc": None,
        "verification_method": None,
    }
    for status in ("partial", "unknown"):
        validator._validate_provenance({**base, "status": status}, "provenance")


@pytest.mark.parametrize("value", [0, -1, math.inf, -math.inf, math.nan, True, "4"])
def test_capture_bounds_reject_non_finite_and_non_positive_values(value: object) -> None:
    with pytest.raises(ValueError):
        validator._validate_bounds(
            {"max_frames": value, "max_wall_seconds": 1.0, "max_inputs": 1},
            "capture_bounds",
        )
    with pytest.raises(producer.ScenarioRefusal):
        producer.validate_bounds(max_frames=value, max_wall_seconds=1.0, max_inputs=1)


def test_producer_bounds_reject_non_finite_wall_clock() -> None:
    for value in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(producer.ScenarioRefusal, match="max_wall_seconds"):
            producer.validate_bounds(max_frames=1, max_wall_seconds=value, max_inputs=1)


def test_catalog_cross_reference_rejects_fixture_hash_drift() -> None:
    catalog = copy.deepcopy(_load_catalog())
    catalog["scenarios"][0]["fixture"]["sha1"] = "0" * 40
    with pytest.raises(ValueError, match="fixture.sha1 disagrees with the manifest"):
        validator._validate_schema(catalog, _load_manifest())


def test_catalog_cross_reference_rejects_unknown_and_missing_fixtures() -> None:
    catalog = copy.deepcopy(_load_catalog())
    catalog["scenarios"][0]["fixture"]["fixture_id"] = "not-a-real-fixture"
    with pytest.raises(ValueError, match="not in the manifest"):
        validator._validate_schema(catalog, _load_manifest())

    catalog = copy.deepcopy(_load_catalog())
    catalog["scenarios"] = catalog["scenarios"][:-1]
    with pytest.raises(ValueError, match="missing manifest fixtures"):
        validator._validate_schema(catalog, _load_manifest())


def test_catalog_cross_reference_rejects_unknown_source_fixture() -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    scenario["provenance"]["source_fixture_id"] = "not-a-real-fixture"
    with pytest.raises(ValueError, match="source_fixture_id is not in the manifest"):
        validator._validate_schema(catalog, _load_manifest())


def test_catalog_cross_reference_rejects_source_input_hash_drift() -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    scenario["provenance"]["input_fixture_sha1"] = "0" * 40
    with pytest.raises(ValueError, match="input_fixture_sha1 disagrees with manifest fixture"):
        validator._validate_schema(catalog, _load_manifest())


def test_missing_required_scenario_is_blocked_not_pass() -> None:
    catalog = _load_catalog()
    assert producer.scenario_status(catalog, "not_declared") == "BLOCKED"
    with pytest.raises(producer.ScenarioBlocked):
        producer.find_scenario(catalog, "not_declared")
    assert producer.scenario_status(catalog, "not_declared") != "PASS"
    assert producer.scenario_status(catalog, "red_color_battle") == "READY"


def test_producer_refuses_unpinned_rom_and_symbol() -> None:
    with pytest.raises(producer.ScenarioRefusal, match="ROM is not pinned"):
        producer.resolve_pins(
            _FakePins(None, "b" * 40), "rom/red/unknown.gb", "rom/red/pokemon-red.sym"
        )
    with pytest.raises(producer.ScenarioRefusal, match="symbol file is not pinned"):
        producer.resolve_pins(
            _FakePins("a" * 40, None), "rom/red/pokemon-red.gb", "rom/red/unknown.sym"
        )


def test_producer_refuses_wrong_resolved_rom_pin(tmp_path: Path) -> None:
    catalog = _load_catalog()
    scenario = _scenario(catalog, "red_color_battle")
    with pytest.raises(producer.ScenarioRefusal, match="disagrees with scenario"):
        producer.run(
            scenario_id="red_color_battle",
            catalog=catalog,
            rom="rom/red/pokemon-red.gb",
            sym="rom/red/pokemon-red.sym",
            output=tmp_path / "out.state",
            pins=_FakePins("f" * 40, scenario["game"]["sym_sha1"]),
        )


def test_producer_refuses_input_fixture_sha_mismatch(tmp_path: Path) -> None:
    payload = b"declared input fixture bytes"
    target = tmp_path / "input.state"
    target.write_bytes(payload)
    expected = hashlib.sha1(payload).hexdigest()
    assert producer.verify_input_fixture(target, expected) == expected
    with pytest.raises(producer.ScenarioRefusal, match="input fixture SHA-1 mismatch"):
        producer.verify_input_fixture(target, "0" * 40)


def test_producer_refuses_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "out.state"
    output.write_bytes(b"existing")
    with pytest.raises(producer.ScenarioRefusal, match="refusing to overwrite"):
        producer.ensure_output_available(output)


def test_producer_capture_stub_refuses_without_writing(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    input_fixture = tmp_path / "input.state"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    input_fixture.write_bytes(b"temporary input state bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    scenario["provenance"]["input_fixture_sha1"] = hashlib.sha1(
        input_fixture.read_bytes()
    ).hexdigest()
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"
    with pytest.raises(producer.CaptureNotAvailable, match="capture requires a controlled ROM run"):
        producer.run(
            scenario_id="red_color_battle",
            catalog=catalog,
            rom=rom,
            sym=sym,
            input_fixture=input_fixture,
            output=output,
            report=report,
            pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        )
    assert not output.exists()
    assert not report.exists()


# --- bounded capture path (ROM-free, injected session) ------------------


class _FakeSymbols:
    """Symbol table stand-in exposing only the verified ``wLinkState`` byte."""

    def __init__(self, *, link_state: int, present: bool) -> None:
        self._present = present
        self._values = {"wLinkState": link_state}

    def __contains__(self, name: object) -> bool:
        return self._present and isinstance(name, str) and name in self._values

    def addr_of(self, name: str) -> int:
        if name not in self._values:
            raise KeyError(f"unknown symbol: {name!r}")
        return self._values[name]

    def read_u8(self, memory: dict[int, int], name: str) -> int:
        return memory[self.addr_of(name)] & 0xFF


class _FakeSession:
    """Scripted stand-in for a bounded capture session: no ROM, no PyBoy."""

    def __init__(
        self,
        *,
        link_state: int = 0,
        map_id: int = 64,
        start_x: int = 9,
        start_y: int = 4,
        party_count: int = 0,
        active_slot: int | None = None,
        payload: bytes = b"fake-bounded-capture-state",
        symbols_present: bool = True,
    ) -> None:
        self.symbols = _FakeSymbols(link_state=link_state, present=symbols_present)
        self._pyboy = types.SimpleNamespace(memory={link_state: link_state})
        self.map_id = map_id
        self.x = start_x
        self.y = start_y
        self.party = types.SimpleNamespace(count=party_count, active_slot=active_slot)
        self.payload = payload
        self.ticks = 0
        self.presses: list[tuple[str, int]] = []
        self.loaded: list[bytes] = []
        self.closed = False

    def load_state(self, data: bytes) -> None:
        self.loaded.append(bytes(data))

    def step(self, count: int = 1, **_kwargs: object) -> None:
        self.ticks += count

    def press(self, button: str, *, duration: int = 1) -> None:
        self.presses.append((button, duration))
        if button == "right":
            self.x += 1

    def read_game_state(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            overworld=types.SimpleNamespace(map_id=self.map_id, x=self.x, y=self.y),
            party=self.party,
        )

    def save_state(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


def _writable_scenario(scenario_id: str = "red_color_ordinary") -> dict:
    """A declared scenario whose fixture bytes are not yet pinned.

    Clearing the pinned hashes also drops the provenance to ``partial``: an
    entry that no longer declares the bytes its provenance claims to have
    verified must not keep claiming ``verified``.
    """
    scenario = copy.deepcopy(_scenario(_load_catalog(), scenario_id))
    scenario["fixture"].update({"sha1": None, "sha256": None, "size_bytes": None})
    scenario["provenance"]["status"] = "partial"
    return scenario


def _capture_kwargs(tmp_path: Path, scenario: dict, **overrides: object) -> dict:
    input_fixture = tmp_path / "source.state"
    input_fixture.write_bytes(b"source-state-bytes")
    kwargs: dict = {
        "scenario": scenario,
        "rom": tmp_path / "pokemon-red-color.gb",
        "sym": tmp_path / "pokemon-red.sym",
        "input_fixture": input_fixture,
        "output": tmp_path / "out.state",
        "role": "listen",
        "bounds": {"max_frames": 100000, "max_wall_seconds": 180.0, "max_inputs": 64},
        "report": None,
        "repo_root": tmp_path,
        "runtime": "source",
        "python": None,
        "plan": {"rom_sha1": "a" * 40, "sym_sha1": "b" * 40, "input_fixture_sha1": None},
    }
    kwargs.update(overrides)
    return kwargs


def _staged_files(tmp_path: Path) -> list[Path]:
    return [path for path in tmp_path.iterdir() if ".partial-" in path.name]


def test_producer_capture_drives_link_reception_and_records_provenance(tmp_path: Path) -> None:
    session = _FakeSession()
    record = producer.capture_battle_scenario(
        **_capture_kwargs(tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: session)
    )

    assert (tmp_path / "out.state").read_bytes() == session.payload
    assert session.loaded == [b"source-state-bytes"]
    assert session.closed is True
    assert record["producer"] == "scripts/produce_battle_scenario.py"
    assert record["role"] == "listen"
    assert record["inputs_used"] == 10
    assert record["frames_used"] == 220
    assert record["input_sequence"][0] == "step:10"
    assert record["input_sequence"][-1] == "press:up:8:30"
    assert record["observed_boundary"] == {
        "map_id": 64,
        "x": 11,
        "y": 4,
        "link_state_raw": 0,
        "party_count": 0,
        "active_slot": None,
    }
    assert record["output"]["size_bytes"] == len(session.payload)
    assert record["output"]["sha1"] == hashlib.sha1(session.payload).hexdigest()
    assert record["output"]["sha256"] == hashlib.sha256(session.payload).hexdigest()
    assert record["reproduction"]["matches"] is False
    assert _staged_files(tmp_path) == []


def test_producer_capture_records_a_matching_reproduction(tmp_path: Path) -> None:
    payload = b"declared-fixture-bytes"
    scenario = _writable_scenario()
    scenario["fixture"].update(
        {
            "sha1": hashlib.sha1(payload).hexdigest(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    )
    record = producer.capture_battle_scenario(
        **_capture_kwargs(
            tmp_path,
            scenario,
            session_factory=lambda **_kwargs: _FakeSession(payload=payload),
        )
    )

    assert record["reproduction"]["matches"] is True
    assert (tmp_path / "out.state").read_bytes() == payload


def test_producer_capture_aborts_when_the_input_budget_is_exhausted(tmp_path: Path) -> None:
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_inputs"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                bounds={"max_frames": 100000, "max_wall_seconds": 180.0, "max_inputs": 5},
                session_factory=lambda **_kwargs: _FakeSession(),
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_aborts_before_exceeding_the_frame_budget(tmp_path: Path) -> None:
    session = _FakeSession()
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_frames"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                bounds={"max_frames": 25, "max_wall_seconds": 180.0, "max_inputs": 64},
                session_factory=lambda **_kwargs: session,
            )
        )

    # The settle step was charged; the next advance was refused before ticking.
    assert session.ticks == 10
    assert not (tmp_path / "out.state").exists()


def test_producer_capture_aborts_on_the_wall_clock_bound(tmp_path: Path) -> None:
    ticks = {"now": 0.0}

    def clock() -> float:
        ticks["now"] += 1000.0
        return ticks["now"]

    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(),
                clock=clock,
            )
        )

    assert not (tmp_path / "out.state").exists()


def test_producer_capture_rejects_a_map_precondition_mismatch(tmp_path: Path) -> None:
    with pytest.raises(producer.CapturePreconditionFailed, match="map_id"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(map_id=12),
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_rejects_a_link_state_precondition_mismatch(tmp_path: Path) -> None:
    with pytest.raises(producer.CapturePreconditionFailed, match="link_state"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(link_state=1),
            )
        )

    assert not (tmp_path / "out.state").exists()


def test_producer_capture_asserts_a_declared_party_shape(tmp_path: Path) -> None:
    scenario = _writable_scenario()
    scenario["party"]["count"] = 6
    scenario["party"]["active_slot"] = 0
    with pytest.raises(producer.CapturePreconditionFailed, match="party.count"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession())
        )

    scenario["party"]["count"] = 0
    scenario["party"]["active_slot"] = None
    record = producer.capture_battle_scenario(
        **_capture_kwargs(tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession())
    )
    assert record["observed_boundary"]["party_count"] == 0


def test_producer_capture_refuses_when_the_link_symbol_is_absent(tmp_path: Path) -> None:
    with pytest.raises(producer.CapturePreconditionFailed, match="absent from the loaded"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(symbols_present=False),
            )
        )

    assert not (tmp_path / "out.state").exists()


def test_producer_capture_refuses_a_boundary_the_bounded_drive_cannot_reach(
    tmp_path: Path,
) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_battle"))
    with pytest.raises(producer.CaptureNotAvailable, match="capture requires a controlled ROM run"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession())
        )

    assert not (tmp_path / "out.state").exists()


def test_producer_capture_requires_a_source_state(tmp_path: Path) -> None:
    with pytest.raises(producer.ScenarioRefusal, match="requires a source state"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                input_fixture=None,
                session_factory=lambda **_kwargs: _FakeSession(),
            )
        )


def test_producer_capture_refuses_a_verified_fixture_hash_mismatch(tmp_path: Path) -> None:
    with pytest.raises(producer.ScenarioRefusal, match="did not reproduce"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                # red_color_ordinary is verified with pinned fixture hashes.
                copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary")),
                session_factory=lambda **_kwargs: _FakeSession(),
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_never_overwrites_an_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "out.state"
    output.write_bytes(b"existing-bytes")

    with pytest.raises(producer.ScenarioRefusal, match="refusing to overwrite"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert output.read_bytes() == b"existing-bytes"
    assert _staged_files(tmp_path) == []


def test_producer_run_merges_the_capture_record_into_the_report(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    scenario["fixture"].update({"sha1": None, "sha256": None, "size_bytes": None})
    scenario["provenance"]["status"] = "partial"
    declared_producer = tmp_path / "scripts" / "produce_cable_club_fixture.py"
    declared_producer.parent.mkdir(parents=True, exist_ok=True)
    declared_producer.write_text("# declared producer stand-in\n", encoding="utf-8")
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    source = tmp_path / "source.state"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    source.write_bytes(b"source-state-bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"
    session = _FakeSession()

    plan = producer.run(
        scenario_id="red_color_ordinary",
        catalog=catalog,
        rom=rom,
        sym=sym,
        input_fixture=source,
        output=output,
        report=report,
        repo_root=tmp_path,
        pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        capture=lambda **kwargs: producer.capture_battle_scenario(
            **kwargs, session_factory=lambda **_kwargs: session
        ),
    )

    assert plan["capture"]["inputs_used"] == 10
    assert output.read_bytes() == session.payload
    assert plan["declared_producer"] == "scripts/produce_cable_club_fixture.py"
    assert (
        plan["declared_producer_sha1"] == hashlib.sha1(declared_producer.read_bytes()).hexdigest()
    )
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["capture"]["output"]["sha1"] == hashlib.sha1(session.payload).hexdigest()
    assert written["capture"]["producer"] == "scripts/produce_battle_scenario.py"


def test_producer_requires_declared_input_fixture(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_battle")
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    scenario["game"]["rom_sha1"] = hashlib.sha1(rom.read_bytes()).hexdigest()
    scenario["game"]["sym_sha1"] = hashlib.sha1(sym.read_bytes()).hexdigest()
    with pytest.raises(producer.ScenarioRefusal, match="input fixture is required"):
        producer.run(
            scenario_id="red_color_battle",
            catalog=catalog,
            rom=rom,
            sym=sym,
            output=tmp_path / "out.state",
            pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        )


def test_producer_refuses_missing_or_substituted_assets(tmp_path: Path) -> None:
    with pytest.raises(producer.ScenarioRefusal, match="ROM not found"):
        producer.verify_pinned_assets(
            tmp_path / "absent.gb", tmp_path / "absent.sym", "a" * 40, "b" * 40
        )
    rom = tmp_path / "pokemon-red-color.gb"
    sym = tmp_path / "pokemon-red.sym"
    rom.write_bytes(b"temporary rom bytes")
    sym.write_bytes(b"temporary sym bytes")
    with pytest.raises(producer.ScenarioRefusal, match="ROM SHA-1 mismatch"):
        producer.verify_pinned_assets(rom, sym, "a" * 40, "b" * 40)
    producer.verify_pinned_assets(
        rom,
        sym,
        hashlib.sha1(rom.read_bytes()).hexdigest(),
        hashlib.sha1(sym.read_bytes()).hexdigest(),
    )


def test_producer_source_never_enables_hash_bypass() -> None:
    source = (ROOT / "scripts" / "produce_battle_scenario.py").read_text(encoding="utf-8")
    assert "POKERED_SKIP_SHA1" not in source


def test_producer_role_resolution_and_suffixes() -> None:
    catalog = _load_catalog()
    scenario = _scenario(catalog, "red_color_battle")
    assert producer.resolve_role(scenario, "red_color_battle", None) == "listen"
    assert producer.resolve_role(scenario, "red_color_battle", "connect") == "connect"
    with pytest.raises(producer.ScenarioRefusal, match="requires role"):
        producer.resolve_role(scenario, "red_color_battle__connect", "listen")
    with pytest.raises(producer.ScenarioRefusal, match="not allowed"):
        producer.resolve_role(scenario, "red_color_battle", "spectator")


def test_validator_asset_check_recomputes_hashes_from_bytes(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    fixture_root = tmp_path / "fixtures"
    fixture_path = fixture_root / scenario["fixture"]["path"]
    fixture_path.parent.mkdir(parents=True)
    payload = b"temporary fixture bytes for hash recomputation"
    fixture_path.write_bytes(payload)
    scenario["fixture"]["size_bytes"] = len(payload)
    scenario["fixture"]["sha1"] = hashlib.sha1(payload).hexdigest()
    scenario["fixture"]["sha256"] = hashlib.sha256(payload).hexdigest()

    validator._validate_fixture_assets([scenario], fixture_root)

    fixture_path.write_bytes(b"X" * len(payload))
    with pytest.raises(ValueError, match="SHA-1 mismatch"):
        validator._validate_fixture_assets([scenario], fixture_root)


def test_validator_reports_missing_fixture_bytes(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    missing_root = tmp_path / "fixtures"
    missing_root.mkdir()
    with pytest.raises(ValueError, match="fixture missing"):
        validator._validate_fixture_assets([scenario], missing_root)


def test_validator_cli_schema_only_accepts_catalog_and_rejects_drift(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    good = tmp_path / "good.json"
    good.write_text(json.dumps(catalog), encoding="utf-8")
    assert validator.main(["--catalog", str(good), "--schema-only"]) == 0

    catalog["scenarios"][0]["fixture"]["sha256"] = "0" * 64
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(catalog), encoding="utf-8")
    assert validator.main(["--catalog", str(bad), "--schema-only"]) == 2


def test_producer_cli_refusals_and_bounds(tmp_path: Path) -> None:
    rom = ROOT / "rom" / "red" / "pokemon-red-color.gb"
    sym = ROOT / "rom" / "red" / "pokemon-red.sym"
    output = tmp_path / "out.state"

    assert (
        producer.main(
            [
                "--scenario",
                "not_declared",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
            ]
        )
        == 3
    )

    output.write_bytes(b"existing")
    assert (
        producer.main(
            [
                "--scenario",
                "red_color_battle",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert output.read_bytes() == b"existing"

    output.unlink()
    assert (
        producer.main(
            [
                "--scenario",
                "red_color_battle",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
                "--max-wall-seconds",
                "inf",
            ]
        )
        == 2
    )

    # A recognized suffix is not proof the asset exists. With the declared ROM
    # absent, the producer now refuses before capture instead of reaching the
    # stub, so the missing asset can never be recorded as pinned.
    assert (
        producer.main(
            [
                "--scenario",
                "red_color_battle",
                "--rom",
                str(rom),
                "--sym",
                str(sym),
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()


def test_producer_report_writer_round_trips(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "report.json"
    producer.write_report(report, {"scenario_id": "red_color_battle"})
    assert json.loads(report.read_text(encoding="utf-8")) == {"scenario_id": "red_color_battle"}


# --- #87.6 fail invalid provenance safely -----------------------------------


class _InterruptingSession(_FakeSession):
    """A capture session that raises ``KeyboardInterrupt`` at a chosen advance."""

    def __init__(self, *, interrupt_after: int, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._interrupt_after = interrupt_after

    def step(self, count: int = 1, **kwargs: object) -> None:
        if self.ticks >= self._interrupt_after:
            raise KeyboardInterrupt
        super().step(count, **kwargs)


def test_producer_metadata_refusal_precedes_the_emulator_session(tmp_path: Path) -> None:
    """An unverifiable declared boundary must be refused before a session is built."""
    scenario = _writable_scenario()
    scenario["capture_boundary"]["link_state"] = "linked"
    opened: list[dict] = []

    def factory(**kwargs: object) -> _FakeSession:
        opened.append(dict(kwargs))
        return _FakeSession()

    with pytest.raises(producer.ScenarioRefusal, match="no verified wLinkState encoding"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=factory)
        )

    assert opened == []
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


@pytest.mark.parametrize("field", ["stage", "when", "map_id"])
def test_producer_refuses_an_incomplete_capture_boundary(tmp_path: Path, field: str) -> None:
    scenario = _writable_scenario()
    del scenario["capture_boundary"][field]
    with pytest.raises(producer.ScenarioRefusal, match=f"capture_boundary.{field}"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession())
        )
    assert not (tmp_path / "out.state").exists()


def test_producer_refuses_a_missing_capture_boundary(tmp_path: Path) -> None:
    scenario = _writable_scenario()
    del scenario["capture_boundary"]
    with pytest.raises(producer.ScenarioRefusal, match="must declare capture_boundary"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession())
        )
    assert not (tmp_path / "out.state").exists()


def test_producer_refuses_a_non_integer_map_id() -> None:
    scenario = _writable_scenario()
    scenario["capture_boundary"]["map_id"] = "64"
    with pytest.raises(producer.ScenarioRefusal, match="non-negative integer"):
        producer.validate_scenario_metadata(scenario, "red_color_ordinary")


def test_producer_refuses_missing_or_unknown_capture_bounds() -> None:
    scenario = _writable_scenario()
    del scenario["capture_bounds"]["max_frames"]
    with pytest.raises(producer.ScenarioRefusal, match="max_frames"):
        producer.validate_scenario_metadata(scenario, "red_color_ordinary")

    scenario = _writable_scenario()
    del scenario["capture_bounds"]
    with pytest.raises(producer.ScenarioRefusal, match="must declare capture_bounds"):
        producer.validate_scenario_metadata(scenario, "red_color_ordinary")


def test_producer_refuses_an_unrecognised_provenance_status() -> None:
    scenario = _writable_scenario()
    scenario["provenance"]["status"] = "probably-fine"
    with pytest.raises(producer.ScenarioRefusal, match="provenance status"):
        producer.validate_scenario_metadata(scenario, "red_color_ordinary")


def test_producer_refuses_verified_provenance_without_pinned_fixture_bytes(
    tmp_path: Path,
) -> None:
    """A verified entry with no pinned hash must not be recorded without a comparison."""
    for field in ("sha1", "sha256", "size_bytes"):
        scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
        scenario["fixture"][field] = None
        with pytest.raises(producer.ScenarioRefusal, match=f"does not declare {field}"):
            producer.validate_scenario_metadata(scenario, "red_color_ordinary")
        with pytest.raises(producer.ScenarioRefusal, match=f"does not declare {field}"):
            producer.capture_battle_scenario(
                **_capture_kwargs(
                    tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession()
                )
            )
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_refuses_a_named_producer_absent_from_the_tree(tmp_path: Path) -> None:
    """A changed or removed producer leaves a verified lineage that cannot be replayed."""
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    with pytest.raises(producer.ScenarioRefusal, match="cannot be reproduced from this tree"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)

    target = tmp_path / scenario["provenance"]["producer"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"changed producer bytes")
    digest = producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)
    assert digest == hashlib.sha1(b"changed producer bytes").hexdigest()


def test_producer_refuses_verified_provenance_without_a_named_producer(tmp_path: Path) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["provenance"]["producer"] = None
    with pytest.raises(producer.ScenarioRefusal, match="names no producer"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)


def test_producer_run_screens_metadata_before_the_assets(tmp_path: Path) -> None:
    """A malformed declaration is refused even when the ROM/SYM are also missing."""
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    del scenario["capture_boundary"]["link_state"]

    with pytest.raises(producer.ScenarioRefusal, match="link_state"):
        producer.run(
            scenario_id="red_color_ordinary",
            catalog=catalog,
            rom=tmp_path / "absent.gb",
            sym=tmp_path / "absent.sym",
            output=tmp_path / "out.state",
            repo_root=tmp_path,
            pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        )
    assert not (tmp_path / "out.state").exists()


def test_producer_refuses_a_capture_record_missing_its_identity() -> None:
    record = {
        "producer": "scripts/produce_battle_scenario.py",
        "producer_sha1": "a" * 40,
        "runtime": "source",
        "role": "listen",
        "captured_at_utc": "2026-09-21T00:00:00+00:00",
        "wall_seconds": 1.5,
        "declared_bounds": {"max_frames": 1, "max_wall_seconds": 1.0, "max_inputs": 1},
        "inputs_used": 1,
        "frames_used": 1,
        "input_sequence": ["step:1"],
        "observed_boundary": {"map_id": 64, "link_state_raw": 0},
        "rom_sha1": "b" * 40,
        "sym_sha1": "c" * 40,
        "output": {"path": "out.state", "size_bytes": 12, "sha1": "d" * 40, "sha256": "e" * 64},
        "reproduction": {"matches": False},
    }
    producer.validate_capture_record(record, "red_color_ordinary")

    for field in ("producer_sha1", "input_sequence", "observed_boundary", "output"):
        broken = copy.deepcopy(record)
        broken[field] = None
        with pytest.raises(producer.ScenarioRefusal, match=field):
            producer.validate_capture_record(broken, "red_color_ordinary")

    broken = copy.deepcopy(record)
    broken["output"] = {**record["output"], "size_bytes": 0}
    with pytest.raises(producer.ScenarioRefusal, match="positive output size"):
        producer.validate_capture_record(broken, "red_color_ordinary")


def test_producer_capture_admits_the_record_before_writing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that cannot be admitted must not leave the fixture on disk."""

    def refuse(_record: object, scenario_id: str) -> None:
        raise producer.ScenarioRefusal(f"record for {scenario_id!r} was not admitted")

    monkeypatch.setattr(producer, "validate_capture_record", refuse)
    with pytest.raises(producer.ScenarioRefusal, match="was not admitted"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_interruption_publishes_nothing_and_closes_the_session(
    tmp_path: Path,
) -> None:
    session = _InterruptingSession(interrupt_after=10)
    with pytest.raises(KeyboardInterrupt):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: session
            )
        )

    assert session.ticks == 10
    assert session.closed is True
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_interruption_leaves_an_existing_output_untouched(tmp_path: Path) -> None:
    output = tmp_path / "out.state"
    output.write_bytes(b"existing-bytes")

    with pytest.raises(KeyboardInterrupt):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _InterruptingSession(interrupt_after=0),
            )
        )

    assert output.read_bytes() == b"existing-bytes"
    assert _staged_files(tmp_path) == []


def test_producer_capture_interruption_at_publish_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(_source: object, _destination: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(producer.os, "link", interrupt)
    with pytest.raises(KeyboardInterrupt):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []
