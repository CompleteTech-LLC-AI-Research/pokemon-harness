"""ROM-free producer refusal/bounds screening tests."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from tests._battle_scenario_fixtures_support import (
    ROOT,
    _capture_kwargs,
    _FakePins,
    _FakeSession,
    _load_catalog,
    _measured_record,
    _scenario,
    _staged_files,
    _writable_scenario,
)


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


def test_producer_report_writer_never_leaves_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed report rename leaves the prior report byte-identical, with no stage."""
    report = tmp_path / "report.json"
    prior = b'{\n  "prior": true\n}\n'
    report.write_bytes(prior)
    real_replace = producer.os.replace

    def refuse_replace(_source: object, _destination: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(producer.os, "replace", refuse_replace)
    with pytest.raises(OSError, match="rename refused"):
        producer.write_report(report, {"scenario_id": "red_color_battle"})

    assert report.read_bytes() == prior
    assert _staged_files(tmp_path) == []

    monkeypatch.setattr(producer.os, "replace", real_replace)
    publication = producer.write_report(report, {"scenario_id": "red_color_battle"})
    assert json.loads(report.read_text(encoding="utf-8")) == {"scenario_id": "red_color_battle"}
    assert publication.is_owned() is True
    assert publication.withdraw() is None
    assert report.read_bytes() == prior


def test_producer_refuses_a_capture_record_with_an_unqualified_runtime_claim() -> None:
    """A record may not present an unmeasured runtime as an observed one."""
    record = _measured_record()
    producer.validate_capture_record(record, "red_color_ordinary")

    cases = [
        ({**record, "runtime_measurement": None}, "missing runtime_measurement"),
        ({**record, "runtime_measurement": "sometimes"}, "does not record whether"),
        ({**record, "runtime_identity": None}, "unmeasurable runtime identity"),
        ({**record, "runtime_identity": None, "runtime_measurement": "measured"}, "unmeasurable"),
        ({**record, "runtime": "cython"}, "unmeasurable runtime identity"),
        (
            {**record, "runtime_identity": {**record["runtime_identity"], "measured": False}},
            "unmeasurable runtime identity",
        ),
        ({**record, "runtime": "unmeasured", "runtime_identity": None}, "outside"),
        ({**record, "runtime_measurement": "not_measured"}, "without measuring it"),
        (
            {**record, "runtime_measurement": "not_measured", "runtime_identity": None},
            "without measuring it",
        ),
        (
            {
                **record,
                "runtime": "unmeasured",
                "runtime_measurement": "not_measured",
                "runtime_identity": None,
                "python": "/x/python",
            },
            "interpreter it did not run",
        ),
    ]
    for broken, message in cases:
        with pytest.raises(producer.ScenarioRefusal, match=message):
            producer.validate_capture_record(broken, "red_color_ordinary")


def test_producer_refuses_a_declared_inventory_or_opponent_even_when_empty() -> None:
    """An empty declaration is still a precondition this drive cannot observe."""
    for field, value in (("inventory", []), ("inventory", {}), ("opponent", {})):
        scenario = _writable_scenario()
        scenario[field] = value
        with pytest.raises(producer.CaptureNotAvailable, match="does not observe"):
            producer._assert_supported_conditions(scenario, "red_color_ordinary")

    accepted = _writable_scenario()
    accepted["party"] = {"count": None, "active_slot": None, "mons": []}
    producer._assert_supported_conditions(accepted, "red_color_ordinary")


# --- #87.6 round 2: publication, bounds, identity, and condition safety ------


@pytest.mark.parametrize("value", [True, "1", 0, -5, math.inf, -math.inf, math.nan, 24.0])
def test_producer_refuses_a_malformed_verified_fixture_size(tmp_path: Path, value: object) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["fixture"]["size_bytes"] = value
    with pytest.raises(producer.ScenarioRefusal, match="size_bytes"):
        producer.validate_scenario_metadata(scenario, "red_color_ordinary")

    opened: list[dict] = []

    def factory(**kwargs: object) -> _FakeSession:
        opened.append(dict(kwargs))
        return _FakeSession()

    with pytest.raises(producer.ScenarioRefusal, match="size_bytes"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=factory)
        )
    assert opened == []


def test_producer_accepts_a_correct_integer_verified_fixture_size() -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["fixture"]["size_bytes"] = 200546
    producer.validate_scenario_metadata(scenario, "red_color_ordinary")


@pytest.mark.parametrize("mutation", ["inventory", "opponent", "prior_actions", "mons"])
def test_producer_refuses_conditions_the_bounded_drive_cannot_observe(
    tmp_path: Path, mutation: str
) -> None:
    scenario = _writable_scenario()
    if mutation == "inventory":
        scenario["inventory"] = [{"item": "POTION", "count": 1}]
    elif mutation == "opponent":
        scenario["opponent"] = {"name": "rival"}
    elif mutation == "prior_actions":
        scenario["required_prior_actions"] = ["defeat_rival"]
    else:
        scenario["party"]["mons"] = [{"species": "PIKACHU"}]
    opened: list[dict] = []

    def factory(**kwargs: object) -> _FakeSession:
        opened.append(dict(kwargs))
        return _FakeSession()

    with pytest.raises(producer.CaptureNotAvailable, match="controlled ROM run"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, scenario, session_factory=factory)
        )

    assert opened == []
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_accepts_unspecified_conditions_and_empty_prior_actions(tmp_path: Path) -> None:
    scenario = _writable_scenario()
    assert scenario["inventory"] is None
    assert scenario["opponent"] is None
    assert scenario["required_prior_actions"] == []
    record = producer.capture_battle_scenario(
        **_capture_kwargs(tmp_path, scenario, session_factory=lambda **_kwargs: _FakeSession())
    )
    assert record["observed_boundary"]["map_id"] == 64


def test_producer_accepts_an_unchanged_pinned_producer(tmp_path: Path) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    target = tmp_path / scenario["provenance"]["producer"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"producer bytes")
    scenario["provenance"]["producer_sha1"] = hashlib.sha1(b"producer bytes").hexdigest()

    identity = producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)

    assert identity["kind"] == "repository"
    assert identity["identity_verified"] is True
    assert identity["observed_sha1"] == identity["declared_sha1"]


def test_producer_refuses_a_changed_producer_that_breaks_its_pin(tmp_path: Path) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    target = tmp_path / scenario["provenance"]["producer"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original producer bytes")
    scenario["provenance"]["producer_sha1"] = hashlib.sha1(b"original producer bytes").hexdigest()
    target.write_bytes(b"changed producer bytes")

    with pytest.raises(producer.ScenarioRefusal, match="changed producer"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)


def test_producer_supports_an_external_verified_producer_pinned_by_digest(tmp_path: Path) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["provenance"].update(
        {
            "producer": "Operator capture rig (out of tree)",
            "producer_kind": "external",
            "producer_sha1": "a" * 40,
        }
    )
    identity = producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)
    assert identity["kind"] == "external"
    assert identity["declared_sha1"] == "a" * 40
    # An out-of-tree producer's bytes cannot be read here, so its identity stays
    # pinned-but-unobserved rather than claimed as verified.
    assert identity["identity_verified"] is False


def test_producer_refuses_an_external_verified_producer_without_a_digest(tmp_path: Path) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["provenance"].update(
        {"producer": "Operator capture rig (out of tree)", "producer_kind": "external"}
    )
    with pytest.raises(producer.ScenarioRefusal, match="without pinning producer_sha1"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)


def test_producer_refuses_a_derived_producer_claiming_verified_provenance(
    tmp_path: Path,
) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["provenance"].update(
        {"producer": "derive: transform an existing state", "producer_kind": "derived"}
    )
    with pytest.raises(producer.ScenarioRefusal, match="derived bytes must be declared as derived"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)


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

        opened: list[dict] = []

        def factory(_opened: list = opened, **kwargs: object) -> _FakeSession:
            _opened.append(dict(kwargs))
            return _FakeSession()

        with pytest.raises(producer.ScenarioRefusal, match=f"does not declare {field}"):
            producer.capture_battle_scenario(
                **_capture_kwargs(tmp_path, scenario, session_factory=factory)
            )
        assert opened == []
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_refuses_a_named_producer_absent_from_the_tree(tmp_path: Path) -> None:
    """A removed producer leaves a verified lineage that cannot be replayed."""
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    with pytest.raises(producer.ScenarioRefusal, match="cannot be reproduced from this tree"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)

    target = tmp_path / scenario["provenance"]["producer"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"changed producer bytes")
    identity = producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)
    # An unpinned producer is fingerprinted, but its identity stays unqualified:
    # the digest is recorded as observed, never as a verified historical pin.
    assert identity["observed_sha1"] == hashlib.sha1(b"changed producer bytes").hexdigest()
    assert identity["declared_sha1"] is None
    assert identity["identity_verified"] is False


def test_producer_refuses_verified_provenance_without_a_named_producer(tmp_path: Path) -> None:
    scenario = copy.deepcopy(_scenario(_load_catalog(), "red_color_ordinary"))
    scenario["provenance"]["producer"] = None
    with pytest.raises(producer.ScenarioRefusal, match="names no producer"):
        producer.verify_declared_producer(scenario, "red_color_ordinary", tmp_path)


def test_producer_refuses_a_capture_record_missing_its_identity() -> None:
    record = _measured_record()
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
