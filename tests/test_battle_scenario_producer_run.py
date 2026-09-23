"""ROM-free producer run/report finalization tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from tests._battle_scenario_fixtures_support import (
    _FakePins,
    _FakeSession,
    _load_catalog,
    _prepared_run,
    _run_with_fake_session,
    _scenario,
    _staged_files,
    _verified_run_inputs,
)


def test_producer_run_preserves_a_competing_fixture_when_the_report_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report rollback must not delete an entry a competing writer replaced."""
    prepared = _prepared_run(tmp_path)
    output = tmp_path / "out.state"
    replacement = tmp_path / "competitor.state"
    replacement.write_bytes(b"competitor must survive")

    def replace_output_then_fail(*_args: object, **_kwargs: object) -> None:
        os.replace(replacement, output)
        raise OSError("report could not be finalized")

    monkeypatch.setattr(producer, "write_report", replace_output_then_fail)
    prepared.update({"out": output, "report": tmp_path / "report.json", "root": tmp_path})
    with pytest.raises(OSError, match="could not be finalized"):
        _run_with_fake_session(prepared)

    assert output.read_bytes() == b"competitor must survive"
    assert not (tmp_path / "report.json").exists()
    assert _staged_files(tmp_path) == []


def test_producer_run_withdraws_its_report_when_finalization_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report published for a rolled-back fixture must not survive as a success."""
    prepared = _prepared_run(tmp_path)
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"
    real_replace = producer.os.replace
    fired: list[int] = []

    def replace_then_interrupt(source: object, destination: object) -> None:
        real_replace(source, destination)
        if Path(destination) == report and not fired:
            fired.append(1)
            raise KeyboardInterrupt

    monkeypatch.setattr(producer.os, "replace", replace_then_interrupt)
    prepared.update({"out": output, "report": report, "root": tmp_path})
    with pytest.raises(KeyboardInterrupt):
        _run_with_fake_session(prepared)

    assert fired, "the report rename must have completed before the interrupt"
    assert not report.exists()
    assert not output.exists()
    assert _staged_files(tmp_path) == []


def test_producer_run_restores_a_replaced_report_when_finalization_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior report is put back rather than destroyed by this capture's rollback."""
    prepared = _prepared_run(tmp_path)
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"
    prior = b'{\n  "prior": true\n}\n'
    report.write_bytes(prior)
    real_replace = producer.os.replace
    fired: list[int] = []

    def replace_then_interrupt(source: object, destination: object) -> None:
        real_replace(source, destination)
        if Path(destination) == report and not fired:
            fired.append(1)
            raise KeyboardInterrupt

    monkeypatch.setattr(producer.os, "replace", replace_then_interrupt)
    prepared.update({"out": output, "report": report, "root": tmp_path})
    with pytest.raises(KeyboardInterrupt):
        _run_with_fake_session(prepared)

    assert report.read_bytes() == prior
    assert not output.exists()
    assert _staged_files(tmp_path) == []


def test_producer_run_refuses_a_report_that_aliases_the_catalog(tmp_path: Path) -> None:
    """The catalog is a protected input: a report over it would erase the contract."""
    prepared = _prepared_run(tmp_path)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(prepared["catalog"]), encoding="utf-8")
    before = catalog_path.read_bytes()
    hard_link = tmp_path / "catalog-hardlink.json"
    os.link(catalog_path, hard_link)

    for report in (catalog_path, tmp_path / "." / "catalog.json", hard_link):
        with pytest.raises(producer.ScenarioRefusal, match="same file as"):
            producer.run(
                scenario_id="red_color_ordinary",
                catalog=prepared["catalog"],
                rom=prepared["rom"],
                sym=prepared["sym"],
                input_fixture=prepared["input_fixture"],
                output=tmp_path / "out.state",
                report=report,
                repo_root=tmp_path,
                pins=prepared["pins"],
                catalog_path=catalog_path,
                capture=lambda **_kwargs: pytest.fail("capture must not run"),
            )

    assert catalog_path.read_bytes() == before
    assert hard_link.read_bytes() == before
    assert json.loads(catalog_path.read_text(encoding="utf-8"))["scenarios"]
    assert not (tmp_path / "out.state").exists()


def test_producer_run_refuses_expiry_during_report_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that expires at report finalization withdraws the whole pair."""
    prepared = _prepared_run(tmp_path)
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"
    now = {"seconds": 0.0}
    real_write_report = producer.write_report

    def write_then_expire(path: object, payload: object) -> object:
        publication = real_write_report(path, payload)
        now["seconds"] = 181.0
        return publication

    monkeypatch.setattr(producer, "write_report", write_then_expire)
    prepared.update({"out": output, "report": report, "root": tmp_path})
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        _run_with_fake_session(prepared, clock=lambda: now["seconds"])

    assert not report.exists()
    assert not output.exists()
    assert _staged_files(tmp_path) == []


def test_producer_run_refuses_a_report_that_aliases_the_output(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    output = tmp_path / "out.state"
    pins = _FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"])
    for report in (output, tmp_path / "." / "out.state"):
        with pytest.raises(producer.ScenarioRefusal, match="same file as"):
            producer.run(
                scenario_id="red_color_ordinary",
                catalog=catalog,
                rom=tmp_path / "absent.gb",
                sym=tmp_path / "absent.sym",
                output=output,
                report=report,
                repo_root=tmp_path,
                pins=pins,
            )
    assert not output.exists()


def test_producer_run_refuses_a_report_that_aliases_an_input(tmp_path: Path) -> None:
    catalog = copy.deepcopy(_load_catalog())
    scenario = _scenario(catalog, "red_color_ordinary")
    rom = tmp_path / "rom.gb"
    rom.write_bytes(b"rom bytes")
    with pytest.raises(producer.ScenarioRefusal, match="same file as"):
        producer.run(
            scenario_id="red_color_ordinary",
            catalog=catalog,
            rom=rom,
            sym=tmp_path / "absent.sym",
            input_fixture=tmp_path / "absent.state",
            output=tmp_path / "out.state",
            report=rom,
            repo_root=tmp_path,
            pins=_FakePins(scenario["game"]["rom_sha1"], scenario["game"]["sym_sha1"]),
        )
    assert rom.read_bytes() == b"rom bytes"


def test_producer_run_removes_its_fixture_when_the_report_cannot_be_written(
    tmp_path: Path,
) -> None:
    prepared = _prepared_run(tmp_path)
    output = tmp_path / "out.state"
    report_directory = tmp_path / "report-dir"
    report_directory.mkdir()

    with pytest.raises(OSError):
        producer.run(
            scenario_id="red_color_ordinary",
            catalog=prepared["catalog"],
            rom=prepared["rom"],
            sym=prepared["sym"],
            input_fixture=prepared["input_fixture"],
            output=output,
            report=report_directory,
            repo_root=tmp_path,
            pins=prepared["pins"],
            capture=lambda **kwargs: producer.capture_battle_scenario(
                **kwargs, session_factory=lambda **_kwargs: _FakeSession()
            ),
        )

    assert not output.exists()
    assert _staged_files(tmp_path) == []


def test_producer_run_interruption_at_finalization_removes_the_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupt after publication but before the report must roll the fixture back."""
    prepared = _prepared_run(tmp_path)
    output = tmp_path / "out.state"
    report = tmp_path / "report.json"

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(producer, "write_report", interrupt)

    with pytest.raises(KeyboardInterrupt):
        producer.run(
            scenario_id="red_color_ordinary",
            catalog=prepared["catalog"],
            rom=prepared["rom"],
            sym=prepared["sym"],
            input_fixture=prepared["input_fixture"],
            output=output,
            report=report,
            repo_root=tmp_path,
            pins=prepared["pins"],
            capture=lambda **kwargs: producer.capture_battle_scenario(
                **kwargs, session_factory=lambda **_kwargs: _FakeSession()
            ),
        )

    assert not output.exists()
    assert not report.exists()
    assert _staged_files(tmp_path) == []


def test_producer_run_accepts_a_verified_capture_with_a_pinned_producer(tmp_path: Path) -> None:
    prepared = _verified_run_inputs(tmp_path)
    output = tmp_path / "out.state"

    plan = producer.run(
        scenario_id="red_color_ordinary",
        catalog=prepared["catalog"],
        rom=prepared["rom"],
        sym=prepared["sym"],
        input_fixture=prepared["input_fixture"],
        output=output,
        repo_root=tmp_path,
        pins=prepared["pins"],
        capture=lambda **kwargs: producer.capture_battle_scenario(
            **kwargs, session_factory=lambda **_kwargs: _FakeSession()
        ),
    )

    assert plan["producer_identity"]["identity_verified"] is True
    assert plan["capture"]["reproduction"]["matches"] is True
    assert output.read_bytes() == b"fake-bounded-capture-state"


def test_producer_run_refuses_a_changed_producer_for_a_verified_entry(tmp_path: Path) -> None:
    prepared = _verified_run_inputs(tmp_path)
    prepared["producer_file"].write_bytes(b"changed producer bytes")

    with pytest.raises(producer.ScenarioRefusal, match="changed producer"):
        producer.run(
            scenario_id="red_color_ordinary",
            catalog=prepared["catalog"],
            rom=prepared["rom"],
            sym=prepared["sym"],
            input_fixture=prepared["input_fixture"],
            output=tmp_path / "out.state",
            repo_root=tmp_path,
            pins=prepared["pins"],
            capture=lambda **_kwargs: pytest.fail("capture must not run"),
        )

    assert not (tmp_path / "out.state").exists()


def test_producer_run_refuses_a_verified_producer_absent_from_the_tree(tmp_path: Path) -> None:
    prepared = _verified_run_inputs(tmp_path)
    prepared["producer_file"].unlink()

    with pytest.raises(producer.ScenarioRefusal, match="cannot be reproduced from this tree"):
        producer.run(
            scenario_id="red_color_ordinary",
            catalog=prepared["catalog"],
            rom=prepared["rom"],
            sym=prepared["sym"],
            input_fixture=prepared["input_fixture"],
            output=tmp_path / "out.state",
            repo_root=tmp_path,
            pins=prepared["pins"],
            capture=lambda **_kwargs: pytest.fail("capture must not run"),
        )

    assert not (tmp_path / "out.state").exists()


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

    assert plan["capture"]["inputs_used"] == 13
    assert output.read_bytes() == session.payload
    assert plan["declared_producer"] == "scripts/produce_cable_club_fixture.py"
    assert (
        plan["producer_identity"]["observed_sha1"]
        == hashlib.sha1(declared_producer.read_bytes()).hexdigest()
    )
    assert plan["producer_identity"]["identity_verified"] is False
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["capture"]["output"]["sha1"] == hashlib.sha1(session.payload).hexdigest()
    assert written["capture"]["producer"] == "scripts/produce_battle_scenario.py"


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
