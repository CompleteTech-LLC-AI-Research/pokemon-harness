"""Menu-profile scheduling checks for the timed probe."""

import hashlib
import json
import multiprocessing
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_timed_rom_pair as probe
from tests._probe_timed_rom_pair_support import (
    _MenuSession,
    _run_menu_owner,
    _spawn_diagnostic_owner,
    arguments,
)


def test_menu_profile_is_explicit_and_requires_both_single_frame_chunks():
    assert arguments().input_profile == "none"
    assert arguments("--input-profile", "none").connector_chunk == 2
    for listener, connector in ((1, 2), (2, 1), (2, 2)):
        with pytest.raises(SystemExit):
            arguments(
                "--input-profile",
                "menu",
                "--listener-chunk",
                str(listener),
                "--connector-chunk",
                str(connector),
            )
    args = arguments("--input-profile", "menu", "--connector-chunk", "1")
    assert args.input_profile == "menu"
    assert args.listener_chunk == args.connector_chunk == 1
    with pytest.raises(SystemExit):
        arguments("--input-profile", "automatic")


def test_menu_profile_schedules_once_per_actual_offset_without_press_ticks():
    session, record = _run_menu_owner()
    assert record["errors"] == []
    assert record["termination"] == "frame_bound"
    expected = [
        (0, "up", 6),
        (20, "up", 6),
        (40, "up", 6),
        (60, "a", 4),
        (68, "a", 4),
        (76, "a", 4),
    ]
    assert [event[:3] for event in session.inputs] == expected
    assert [event[3] for event in session.inputs] == [
        100 + offset * 100 for offset, _, _ in expected
    ]
    assert session.steps == [1] * 77
    assert record["final"]["frame_count"] == record["final"]["session_tick"] == 87
    assert record["final"]["cpu_cycles"] == 7800
    assert all(
        call["actual_completed_frames"] == call["requested_frames"] == 1 for call in record["calls"]
    )
    assert [call["frame_offset"] for call in record["calls"]] == list(range(77))
    queued = [call["input"] for call in record["calls"] if "input" in call]
    assert queued == [
        {
            "button": button,
            "duration": duration,
            "actual_completed_frame_offset": offset,
            "status": "queued",
        }
        for offset, button, duration in expected
    ]
    for observation in [record["loaded"], record["attached"], record["final"]] + [
        observation for call in record["calls"] for observation in (call["before"], call["after"])
    ]:
        assert len(observation["menu"]) == 10
        assert observation["menu"]["wCurMap"] == len("wCurMap")


@pytest.mark.parametrize(
    "behavior,frames,cycles,status,termination",
    [
        ("zero", 0, 0, "completed_no_progress", "no_progress"),
        ("interrupt_before_frame", 0, 4, "interrupted", "owner_failure"),
        ("interrupt_after_frame", 1, 100, "interrupted", "owner_failure"),
        ("overadvance", 2, 200, "completed_partial", "incomplete_public_call"),
    ],
)
def test_menu_profile_terminal_call_keeps_actual_progress_and_no_duplicate_input(
    behavior,
    frames,
    cycles,
    status,
    termination,
):
    session, record = _run_menu_owner(behavior=behavior)
    assert session.steps == [1]
    assert [event[:3] for event in session.inputs] == [(0, "up", 6)]
    assert record["termination"] == termination
    call = record["calls"][0]
    assert call["status"] == status
    assert call["actual_completed_frames"] == frames
    assert call["after"]["frame_count"] == record["final"]["frame_count"] == 10 + frames
    assert record["final"]["cpu_cycles"] == 100 + cycles
    assert record["cleanup"] == ["endpoint_detached", "session_closed_without_save"]


def test_none_profile_preserves_no_input_and_unequal_chunk_support():
    session, record = _run_menu_owner("--listener-chunk", "2", "--frame-limit", "4", profile="none")
    assert session.inputs == []
    assert session.steps == [2, 2]
    assert record["termination"] == "frame_bound"
    assert record["final"]["frame_count"] == 14


def test_process_spawn_propagates_explicit_menu_profile(spawned_probe_args):
    spawned_probe_args.input_profile = "menu"
    spawned_probe_args.connector_chunk = 1
    spawned_probe_args.test_scenario = "failure"
    result = probe.run_process_pair(spawned_probe_args, child_target=_spawn_diagnostic_owner)
    assert result["processes_alive"] == result["report_readers_alive"] == []
    assert [owner["test_input_profile"] for owner in result["owners"]] == ["menu", "menu"]


def test_process_report_overflow_is_explicit_bounded_and_not_success(monkeypatch, tmp_path):
    packets = []

    def huge_owner(index, args, records, *rest):
        records[index].update(
            calls=[{"evidence": "x" * (probe.MAX_REPORT_BYTES + 1)}],
            final={"frame_count": 123},
            termination="frame_bound",
        )

    monkeypatch.setattr(probe, "_run_owner", huge_owner)
    monkeypatch.setattr(probe, "runtime_identity", dict)
    shared = probe._SharedFlag(multiprocessing.get_context("spawn").RawValue("B", 0))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unused:
        # process_owner closes its socket; its injected owner performs no transport.
        sender = SimpleNamespace(send_bytes=packets.append, close=lambda: None)
        probe.process_owner(
            vars(arguments()),
            0,
            unused,
            shared,
            threading.Event(),
            threading.Barrier(1),
            time.monotonic() + 1,
            time.monotonic() + 2,
            sender,
            str(tmp_path / "overflow.stderr"),
        )
    assert len(packets) == 1
    assert len(packets[0]) <= probe.MAX_REPORT_BYTES
    report = json.loads(packets[0])
    assert report["termination"] == "report_overflow"
    assert report["actual_missing"] is True
    assert report["errors"] == ["owner report exceeds byte limit"]
    assert "final" not in report
    assert "stdout" in report and "stderr" in report
    artifact = Path(report["full_evidence"]["path"]).read_bytes()
    assert len(artifact) == report["full_evidence"]["bytes"] > probe.MAX_REPORT_BYTES
    assert hashlib.sha256(artifact).hexdigest() == report["full_evidence"]["sha256"]
    assert json.loads(artifact)["final"] == {"frame_count": 123}
    assert len(json.loads(artifact)["calls"][0]["evidence"]) == probe.MAX_REPORT_BYTES + 1


@pytest.mark.parametrize("runner", ["run_pair", "run_process_pair"])
def test_programmatic_menu_profile_rejects_multiframe_before_owner_creation(runner):
    args = arguments()
    args.input_profile = "menu"
    with pytest.raises(ValueError):
        getattr(probe, runner)(args)


def test_menu_profile_capacity_stops_before_another_input_or_tick():
    def expand_evidence(record):
        if record["phase"] == "public_tick_returned":
            record["test_evidence"] = "x" * probe.MAX_REPORT_BYTES

    session, record = _run_menu_owner(checkpoint=expand_evidence)
    assert session.steps == [1]
    assert len(session.inputs) == 1
    assert record["termination"] == "owner_failure"
    assert any("menu report capacity reached" in error for error in record["errors"])
    assert record["final"]["frame_count"] == 11
    assert len(record["test_evidence"]) == probe.MAX_REPORT_BYTES


def test_menu_profile_missing_symbol_fails_before_attach_or_input():
    session, record = _run_menu_owner(behavior="missing_symbol")
    assert session.inputs == session.steps == []
    assert "bind" not in session.calls
    assert record["termination"] == "owner_failure"
    assert any("wCurMap" in error for error in record["errors"])
    assert record["last_native_observation"]["frame_count"] == 10


def test_menu_profile_observation_failure_preserves_completed_native_frame():
    session, record = _run_menu_owner(behavior="observation_failure")
    assert session.steps == [1]
    assert record["termination"] == "owner_failure"
    call = record["calls"][0]
    assert call["actual_completed_frames"] == 1
    assert call["after"]["frame_count"] == record["final"]["frame_count"] == 11
    assert "wCurMap" in call["observation_error"]


def test_menu_profile_press_failure_keeps_native_observations_without_tick():
    session, record = _run_menu_owner(behavior="press_failure")
    assert session.steps == []
    assert len(session.inputs) == 1
    assert record["termination"] == "owner_failure"
    call = record["calls"][0]
    assert call["input"]["status"] == "requested"
    assert call["status"] == "interrupted"
    assert call["actual_completed_frames"] == 0
    assert call["before"]["frame_count"] == call["after"]["frame_count"] == 10


def test_menu_profile_checkpoints_queued_input_before_public_tick(monkeypatch):
    checkpoints = []
    original = _MenuSession.step

    def capture(record):
        checkpoints.append(json.loads(json.dumps(record)))

    def checked_step(self, count, *, render):
        checkpoint = checkpoints[-1]
        assert checkpoint["phase"] == "input_queued_before_public_tick"
        call = checkpoint["calls"][-1]
        assert call["input"] == {
            "button": "up",
            "duration": 6,
            "actual_completed_frame_offset": 0,
            "status": "queued",
        }
        assert call["before"]["frame_count"] == self._pyboy.frame_count == 10
        assert "after" not in call
        return original(self, count, render=render)

    monkeypatch.setattr(_MenuSession, "step", checked_step)
    session, record = _run_menu_owner("--frame-limit", "1", checkpoint=capture)
    assert session.steps == [1]
    assert record["errors"] == []
    assert record["final"]["frame_count"] == 11
