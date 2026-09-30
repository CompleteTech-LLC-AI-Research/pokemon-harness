"""ROM-free producer capture-path tests (injected session, no ROM)."""

from __future__ import annotations

import copy
import hashlib
import math
import os
import sys
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from tests._battle_scenario_fixtures_support import (
    ROOT,
    _capture_kwargs,
    _FakePins,
    _FakeSession,
    _InterruptingSession,
    _load_catalog,
    _scenario,
    _staged_files,
    _writable_scenario,
)


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
    assert record["inputs_used"] == 13
    assert record["frames_used"] == 280
    assert record["input_sequence"][0] == "step:10"
    assert record["input_sequence"][-1] == "press:up:8:30"
    assert record["observed_boundary"] == {
        "map_id": 64,
        "x": 11,
        "y": 3,
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


# --- round-2 closure: owned publications, one deadline, measured identity ------


def test_producer_capture_interruption_after_the_stage_is_removed_still_withdraws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the staging entry is gone, only retained inode identity finds the output."""
    real_unlink = producer.os.unlink

    def unlink_then_interrupt(path: object, *args: object, **kwargs: object) -> None:
        real_unlink(path, *args, **kwargs)
        if ".partial-" in Path(path).name:
            raise KeyboardInterrupt

    monkeypatch.setattr(producer.os, "unlink", unlink_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_finalization_failure_removes_the_owned_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed staging removal is inside the rollback region, not beside it."""
    real_unlink = producer.os.unlink
    failures: list[str] = []

    def fail_stage_removal_once(path: object, *args: object, **kwargs: object) -> None:
        name = Path(path).name
        if ".partial-" in name and not failures:
            failures.append(name)
            raise PermissionError(f"staging entry {name} is not removable")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(producer.os, "unlink", fail_stage_removal_once)
    with pytest.raises(PermissionError, match="is not removable"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert failures, "the staging removal must have been attempted"
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_keeps_the_primary_error_when_stage_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup that cannot complete is reported, never substituted for the failure."""
    real_link = producer.os.link
    real_unlink = producer.os.unlink

    def link_then_interrupt(source: object, destination: object) -> None:
        real_link(source, destination)
        raise KeyboardInterrupt

    def refuse_to_remove_stage(path: object, *args: object, **kwargs: object) -> None:
        if ".partial-" in Path(path).name:
            raise PermissionError("staging entry is not removable")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(producer.os, "link", link_then_interrupt)
    monkeypatch.setattr(producer.os, "unlink", refuse_to_remove_stage)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert not (tmp_path / "out.state").exists()
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("could not be removed" in note for note in notes), notes


def test_producer_capture_preserves_a_competing_symlink_at_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competitor's symlink aimed at our staged bytes is not our publication."""
    real_symlink = producer.os.symlink

    def competing_symlink(source: object, destination: object) -> None:
        # Emulate the race: a competing writer installs a destination symlink
        # pointing at this capture's staged file, then an interrupt arrives.
        real_symlink(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(producer.os, "link", competing_symlink)
    with pytest.raises(KeyboardInterrupt):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    competitor = tmp_path / "out.state"
    assert competitor.is_symlink(), "the competing writer's entry must survive the rollback"
    assert ".partial-" in Path(os.readlink(competitor)).name
    assert _staged_files(tmp_path) == []


def test_producer_capture_refuses_expiry_during_the_producer_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that expires while looking up the revision must publish nothing."""
    now = {"seconds": 0.0}
    real_revision = producer._producer_revision

    def slow_revision(repo_root: object) -> str | None:
        now["seconds"] = 181.0
        return real_revision(repo_root)

    monkeypatch.setattr(producer, "_producer_revision", slow_revision)
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(),
                clock=lambda: now["seconds"],
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_refuses_expiry_during_the_fixture_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that expires inside ``os.link`` withdraws the published entry."""
    now = {"seconds": 0.0}
    real_link = producer.os.link

    def link_then_expire(source: object, destination: object) -> None:
        real_link(source, destination)
        now["seconds"] = 181.0

    monkeypatch.setattr(producer.os, "link", link_then_expire)
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(),
                clock=lambda: now["seconds"],
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_refuses_expiry_during_the_fixture_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that expires while staging the bytes must publish nothing."""
    now = {"seconds": 0.0}
    real_fsync = producer.os.fsync

    def fsync_then_expire(descriptor: int) -> None:
        real_fsync(descriptor)
        now["seconds"] = 181.0

    monkeypatch.setattr(producer.os, "fsync", fsync_then_expire)
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _FakeSession(),
                clock=lambda: now["seconds"],
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


@pytest.mark.parametrize(("compiled", "expected"), [(False, "source"), (True, "cython")])
def test_producer_capture_records_only_the_measured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: bool, expected: str
) -> None:
    """The recorded mode and versions are the measured build, never the pins or a request."""
    monkeypatch.setattr(producer, "_pyboy_cython_flag", lambda: compiled)
    monkeypatch.setattr(producer, "_pyboy_module_version", lambda: "9.9.9-observed")
    monkeypatch.setattr(producer, "_pyboy_module_revision", lambda: "f" * 40)
    monkeypatch.setattr(producer, "_default_session_factory", lambda **_kwargs: _FakeSession())

    record = producer.capture_battle_scenario(
        **_capture_kwargs(tmp_path, _writable_scenario(), runtime=expected, repo_root=ROOT)
    )

    assert record["runtime"] == expected
    assert record["runtime_measurement"] == "measured"
    assert record["runtime_identity"]["mode"] == expected
    assert record["runtime_identity"]["measured"] is True
    assert record["runtime_identity"]["pyboy_version_observed"] == "9.9.9-observed"
    assert record["runtime_identity"]["pyboy_revision_observed"] == "f" * 40
    assert record["runtime_identity"]["python_version"] == sys.version.split()[0]
    assert record["runtime_identity"]["environment"] == sys.prefix
    assert record["python"] is None
    assert (tmp_path / "out.state").exists()


def test_producer_capture_marks_an_injected_session_as_unmeasured(tmp_path: Path) -> None:
    """An injected session may not record the caller's request as capture identity."""
    record = producer.capture_battle_scenario(
        **_capture_kwargs(
            tmp_path,
            _writable_scenario(),
            runtime="cython",
            session_factory=lambda **_kwargs: _FakeSession(),
        )
    )

    assert record["runtime_measurement"] == "not_measured"
    assert record["runtime"] == "unmeasured"
    assert record["runtime_identity"] is None
    assert record["python"] is None
    assert record["runtime_request"] == {"runtime": "cython", "python": None}
    assert (tmp_path / "out.state").exists()


def test_producer_capture_refuses_a_nonexistent_interpreter_for_an_injected_session(
    tmp_path: Path,
) -> None:
    """An unresolvable interpreter request is refused however the session is built."""
    with pytest.raises(producer.ScenarioRefusal, match="does not exist"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                runtime="cython",
                python="/does/not/exist/python",
                session_factory=lambda **_kwargs: _FakeSession(),
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_never_records_a_cython_claim_for_a_wrapped_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper around the production factory cannot mint a measured-mode claim."""
    monkeypatch.setattr(producer, "_default_session_factory", lambda **_kwargs: _FakeSession())
    wrapped = producer._default_session_factory

    def wrapper(**kwargs: object) -> object:
        return wrapped(**kwargs)

    record = producer.capture_battle_scenario(
        **_capture_kwargs(tmp_path, _writable_scenario(), runtime="cython", session_factory=wrapper)
    )

    assert record["runtime"] == "unmeasured"
    assert record["runtime_measurement"] == "not_measured"
    assert record["runtime_identity"] is None


@pytest.mark.parametrize("row", [4, 99])
def test_producer_capture_refuses_a_wrong_receptionist_row(tmp_path: Path, row: int) -> None:
    """Column 11 on the wrong row is not the declared link-receptionist tile."""

    class _WrongRow(_FakeSession):
        def press(self, button: str, *, duration: int = 1) -> None:
            super().press(button, duration=duration)
            self.y = row  # never reaches the receptionist row (11, 3)

    sessions: list[_WrongRow] = []

    def factory(**_kwargs: object) -> _WrongRow:
        session = _WrongRow()
        sessions.append(session)
        return session

    with pytest.raises(producer.CapturePreconditionFailed, match="link receptionist tile"):
        producer.capture_battle_scenario(
            **_capture_kwargs(tmp_path, _writable_scenario(), session_factory=factory)
        )

    assert sessions and sessions[0].closed is True
    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_interruption_after_the_link_removes_its_own_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupt delivered right after ``os.link`` must not leave the fixture."""
    real_link = producer.os.link

    def link_then_interrupt(source: object, destination: object) -> None:
        real_link(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(producer.os, "link", link_then_interrupt)
    keeper = tmp_path / "keep.state"
    keeper.write_bytes(b"unrelated bytes")

    with pytest.raises(KeyboardInterrupt):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _FakeSession()
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert keeper.read_bytes() == b"unrelated bytes"
    assert _staged_files(tmp_path) == []


def test_producer_capture_rechecks_the_deadline_between_press_and_advance(
    tmp_path: Path,
) -> None:
    now = {"seconds": 0.0}

    class _ExpiringPress(_FakeSession):
        def press(self, button: str, *, duration: int = 1) -> None:
            super().press(button, duration=duration)
            now["seconds"] = 1000.0

    session = _ExpiringPress()
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: session,
                clock=lambda: now["seconds"],
            )
        )

    # Only the initial settle step advanced; the expired press was never ticked.
    assert session.ticks == 10
    assert not (tmp_path / "out.state").exists()


def test_producer_capture_refuses_a_save_that_crosses_the_deadline(tmp_path: Path) -> None:
    now = {"seconds": 0.0}
    session = _FakeSession()
    real_save = session.save_state

    def slow_save() -> bytes:
        now["seconds"] = 500.0
        return real_save()

    session.save_state = slow_save  # type: ignore[method-assign]
    with pytest.raises(producer.CaptureBoundsExceeded, match="max_wall_seconds"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: session,
                clock=lambda: now["seconds"],
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


@pytest.mark.parametrize(
    "bounds",
    [
        {"max_frames": 100000, "max_wall_seconds": math.nan, "max_inputs": 64},
        {"max_frames": 100000, "max_wall_seconds": math.inf, "max_inputs": 64},
        {"max_frames": 100000, "max_inputs": 64},
    ],
)
def test_producer_capture_refuses_invalid_effective_bounds(tmp_path: Path, bounds: dict) -> None:
    opened: list[dict] = []

    def factory(**kwargs: object) -> _FakeSession:
        opened.append(dict(kwargs))
        return _FakeSession()

    with pytest.raises(producer.ScenarioRefusal):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), bounds=bounds, session_factory=factory
            )
        )

    assert opened == []
    assert not (tmp_path / "out.state").exists()


def test_producer_capture_measures_identity_before_opening_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def measure(**kwargs: object) -> dict:
        calls.append(dict(kwargs))
        raise producer.ScenarioRefusal("runtime identity could not be established")

    monkeypatch.setattr(producer, "measure_runtime_identity", measure)
    with pytest.raises(producer.ScenarioRefusal, match="identity could not be established"):
        producer.capture_battle_scenario(**_capture_kwargs(tmp_path, _writable_scenario()))

    assert calls and calls[0]["runtime"] == "source"
    assert not (tmp_path / "out.state").exists()


def test_producer_capture_fails_when_teardown_fails(tmp_path: Path) -> None:
    class _BadClose(_FakeSession):
        def close(self) -> None:
            raise RuntimeError("close exploded")

    with pytest.raises(producer.ScenarioRefusal, match="teardown failed"):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: _BadClose()
            )
        )

    assert not (tmp_path / "out.state").exists()
    assert _staged_files(tmp_path) == []


def test_producer_capture_keeps_the_primary_error_when_teardown_also_fails(
    tmp_path: Path,
) -> None:
    class _InterruptedBadClose(_InterruptingSession):
        def close(self) -> None:
            raise RuntimeError("close exploded")

    with pytest.raises(KeyboardInterrupt) as excinfo:
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                session_factory=lambda **_kwargs: _InterruptedBadClose(interrupt_after=10),
            )
        )

    notes = getattr(excinfo.value, "__notes__", ())
    assert any("close() also failed" in note for note in notes)
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
