"""Process-spawn orchestration checks for the timed probe."""

import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_timed_rom_pair as probe
from tests._probe_timed_rom_pair_support import (
    FakeSession,
    Harness,
    _spawn_diagnostic_owner,
    arguments,
)


def test_process_spawn_missing_assets_reports_both_child_failures(spawned_probe_args):
    # The first missing-manifest failure cancels the pair while the second
    # spawned interpreter may still be importing its runtime. This test
    # requires both final reports, so give that peer bounded shutdown time;
    # the dedicated deadline/kill tests retain the fixture's short grace.
    spawned_probe_args.cleanup_timeout = 5
    spawned_probe_args.overall_timeout = 10
    result = probe.run_process_pair(spawned_probe_args)
    assert result["tcp_nodelay"] == [0, 0]
    assert result["processes_alive"] == []
    assert result["threads_alive"] == []
    assert len(result["owners"]) == 2
    for owner in result["owners"]:
        assert owner["pid"] != os.getpid()
        assert owner["owner_complete"] and not owner["alive"]
        assert owner["termination"] == "owner_failure"
        expected = f"manifest not found: {spawned_probe_args.repo_root / 'release-evidence' / 'fixture-manifest.json'}"
        assert any(expected in error for error in owner["errors"])
        assert owner["actual_missing"] is True
        assert owner["cleanup"] == []
        assert not owner["forced_termination"]
        assert owner["runtime"]["executable"] == sys.executable
        assert owner["runtime"]["modules"] == probe.runtime_identity()["modules"]
        assert owner["stderr"]["bytes"] <= probe.STDERR_LIMIT
        stdout = owner["stdout"]
        assert stdout["bytes"] <= probe.STDERR_LIMIT
        assert stdout["total_bytes"] >= stdout["bytes"]
        assert stdout["truncated"] is False
        assert stdout["drainer_alive"] is False
        assert Path(stdout["path"]).is_file()
        assert Path(stdout["path"]).stat().st_size == stdout["bytes"]
        assert json.loads(json.dumps(owner))["stdout"] == stdout


@pytest.mark.parametrize("explicit_default_phases", [False, True])
def test_process_spawn_failure_cancels_waiting_peer(spawned_probe_args, explicit_default_phases):
    spawned_probe_args.test_scenario = "failure"
    phase_options = {"owner_driver_phases": None} if explicit_default_phases else {}
    result = probe.run_process_pair(
        spawned_probe_args, child_target=_spawn_diagnostic_owner, **phase_options
    )
    assert "owner_driver" not in result
    assert result["owner_driver_phases"] == list(probe.READINESS_PHASES)
    assert (spawned_probe_args.listener_chunk, spawned_probe_args.connector_chunk) == (1, 2)
    assert result["processes_alive"] == []
    assert result["tcp_nodelay"] == [0, 0]
    owners = result["owners"]
    assert len({owner["pid"] for owner in owners}) == 2
    for owner in owners:
        assert owner["pid"] == owner["test_pid"] != os.getpid()
        assert owner["test_executable"] == sys.executable
        assert owner["test_repo_root"] == str(spawned_probe_args.repo_root)
        assert owner["test_tcp_nodelay"] == 0
        assert owner["test_cancel_seen"] is True
        assert owner["owner_complete"] and not owner["alive"]
        assert owner["exitcode"] == 0
        assert not owner["forced_termination"]
        assert owner["actual_missing"] is True
    assert owners[0]["termination"] == "owner_failure"
    assert owners[1]["termination"] == "cancelled_or_deadline"


def test_process_spawn_deadline_terminates_unresponsive_owners(spawned_probe_args):
    spawned_probe_args.test_scenario = "ignore_cancel"
    started = time.monotonic()
    result = probe.run_process_pair(spawned_probe_args, child_target=_spawn_diagnostic_owner)
    assert time.monotonic() - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "deadline"
    assert result["processes_alive"] == []
    for owner in result["owners"]:
        assert owner["forced_termination"]
        assert owner["exitcode"] == -signal.SIGKILL
        assert owner["actual_missing"] is True
        assert not owner["alive"]
        assert owner["cleanup"] == []
        assert "final" not in owner
        assert owner["last_checkpoint"] == {
            "synthetic_checkpoint": True,
            "phase": "before_unresponsive_wait",
        }
        assert owner["checkpoint_is_final"] is False


@pytest.mark.parametrize("scenario", ["invalid_json", "oversize"])
def test_process_spawn_rejects_invalid_child_report(spawned_probe_args, scenario):
    spawned_probe_args.test_scenario = scenario
    result = probe.run_process_pair(spawned_probe_args, child_target=_spawn_diagnostic_owner)
    assert result["processes_alive"] == []
    assert result["owners"][0]["errors"]
    assert result["owners"][0]["actual_missing"] is True
    assert result["owners"][0]["cleanup"] == []
    assert result["owners"][1]["test_cancel_seen"] is True


def test_process_cancellation_bridge_sets_local_event_before_publication():
    shared = probe._SharedFlag(multiprocessing.get_context("spawn").RawValue("B", 0))
    local = threading.Event()
    stop = threading.Event()
    errors = []
    shared.set()
    watcher = threading.Thread(
        target=probe.cancellation_bridge,
        args=(shared, local, [None], threading.Lock(), stop, errors),
    )
    watcher.start()
    try:
        assert local.wait(1), "pre-publication cancellation never reached local threading.Event"
        assert type(local) is threading.Event
    finally:
        stop.set()
        watcher.join(1)
    assert not watcher.is_alive()
    assert errors == []


def test_process_cancellation_bridge_continues_after_endpoint_cancel_error():
    shared = probe._SharedFlag(multiprocessing.get_context("spawn").RawValue("B", 0))
    local, stop, reached = threading.Event(), threading.Event(), threading.Event()
    errors, callers = [], []

    def fail():
        assert local.is_set()
        raise RuntimeError("first endpoint cancellation failed")

    def second():
        callers.append(threading.get_ident())
        reached.set()

    endpoints = [SimpleNamespace(cancel=fail), SimpleNamespace(cancel=second)]
    watcher = threading.Thread(
        target=probe.cancellation_bridge,
        args=(shared, local, endpoints, threading.Lock(), stop, errors),
    )
    watcher.start()
    try:
        shared.set()
        assert reached.wait(1)
    finally:
        stop.set()
        watcher.join(1)
    assert not watcher.is_alive()
    assert callers and all(caller == watcher.ident for caller in callers)
    assert any("first endpoint cancellation failed" in str(error) for error in errors)


@pytest.mark.parametrize("owner_mode", ["thread", "process"])
def test_run_probe_dispatches_owner_mode_and_preserves_reversed_chunks(monkeypatch, owner_mode):
    calls = []
    args = arguments("--owner-mode", owner_mode, "--both-orientations")

    def selected(options):
        calls.append(vars(options).copy())
        return {"threads_alive": [], "processes_alive": []}

    def wrong_mode(options):
        pytest.fail("probe dispatched the wrong execution mode")

    monkeypatch.setattr(probe, "runtime_identity", dict)
    monkeypatch.setattr(probe, "run_pair", selected if owner_mode == "thread" else wrong_mode)
    monkeypatch.setattr(
        probe,
        "run_process_pair",
        selected if owner_mode == "process" else wrong_mode,
    )
    report = probe.run_probe(args)
    assert len(report["pairs"]) == len(calls) == 2
    assert (calls[0]["listener"], calls[0]["connector"]) == ("blue_color", "yellow")
    assert (calls[1]["listener"], calls[1]["connector"]) == ("yellow", "blue_color")
    assert calls[0]["absolute_deadline"] == calls[1]["absolute_deadline"]
    assert [(call["listener_chunk"], call["connector_chunk"]) for call in calls] == [(1, 2)] * 2


@pytest.mark.parametrize(
    "failure",
    [
        "live_process",
        "killed",
        "missing_actual",
        "nonzero_child",
        "partial_call",
        "watcher",
        "stderr_drainer",
        "stdout_drainer",
        "stdout_error",
    ],
)
def test_process_main_never_reports_success_for_incomplete_owner(monkeypatch, tmp_path, failure):
    owner = {
        "errors": [],
        "termination": "frame_bound",
        "owner_complete": True,
        "alive": False,
        "forced_termination": False,
        "actual_missing": False,
        "exitcode": 0,
        "calls": [],
        "final": {"frame_count": 6},
    }
    pair = {
        "owner_mode": "process",
        "stop_reason": "owner_completion_or_failure",
        "threads_alive": [],
        "processes_alive": [],
        "owners": [owner],
    }
    if failure == "live_process":
        pair["processes_alive"] = [123]
        owner.update(alive=True, owner_complete=False)
    elif failure == "killed":
        owner["forced_termination"] = True
    elif failure == "missing_actual":
        owner["actual_missing"] = True
        del owner["final"]
    elif failure == "nonzero_child":
        owner["exitcode"] = 1
    elif failure == "watcher":
        owner["watcher_alive"] = True
    elif failure == "stderr_drainer":
        owner["stderr"] = {"drainer_alive": True}
    elif failure == "stdout_drainer":
        owner["stdout"] = {"drainer_alive": True}
    elif failure == "stdout_error":
        owner["stdout"] = {"error": "injected stdout capture failure"}
    else:
        owner["calls"] = [{"status": "interrupted", "actual_completed_frames": 1, "requested": 2}]
    args = arguments("--owner-mode", "process", "--output", str(tmp_path / "report.json"))
    monkeypatch.setattr(probe, "parse_args", lambda argv: args)
    monkeypatch.setattr(probe, "run_probe", lambda args: {"pairs": [pair]})
    assert probe.main([]) != 0


def test_process_stderr_capture_drains_native_fd_flood_with_bounded_retention(tmp_path):
    path = tmp_path / "native-stderr.bin"
    saved, worker, stats = probe._capture_stderr(path)
    block = b"native-symbol-warning\n" * 1024
    written = 0
    try:
        for _ in range(16):
            remaining = memoryview(block)
            while remaining:
                count = os.write(2, remaining)
                written += count
                remaining = remaining[count:]
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        worker.join(2)
    assert not worker.is_alive()
    assert written > probe.STDERR_LIMIT
    assert path.stat().st_size == stats["bytes"] == probe.STDERR_LIMIT
    assert stats["total_bytes"] == written
    assert stats["truncated"] is True
    assert path.read_bytes() == (block * 16)[: probe.STDERR_LIMIT]


def test_process_stdout_capture_drains_native_fd_flood_with_bounded_retention(tmp_path):
    path = tmp_path / "native-stdout.bin"
    sys.stdout.flush()
    saved, worker, stats = probe._capture_stderr(path, fd=1)
    block = b"native-symbol-stdout-warning\n" * 1024
    written = 0
    try:
        for _ in range(16):
            remaining = memoryview(block)
            while remaining:
                count = os.write(1, remaining)
                written += count
                remaining = remaining[count:]
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        worker.join(2)
    assert not worker.is_alive()
    assert written > probe.STDERR_LIMIT
    assert path.stat().st_size == stats["bytes"] == probe.STDERR_LIMIT
    assert stats["total_bytes"] == written
    assert stats["truncated"] is True
    assert not stats.get("error")
    assert path.read_bytes() == (block * 16)[: probe.STDERR_LIMIT]
    assert json.loads(json.dumps(stats)) == stats


def test_jsonable_paths_preserve_real_filesystem_spelling(tmp_path):
    path = tmp_path / "root with spaces" / "fixture.json"
    assert json.loads(json.dumps(probe._jsonable({"repo_root": path}))) == {"repo_root": str(path)}


def test_partial_normal_public_return_is_not_frame_bound_success(monkeypatch):
    harness = Harness()

    def partial_return(self, count, *, render):
        self.record("step")
        assert count == 2
        self.harness.in_step.wait(timeout=2)
        self._pyboy.frame_count += 1
        self._pyboy.mb.cpu.cycles += 100

    monkeypatch.setattr(FakeSession, "step", partial_return)
    result = harness.run("--frame-limit", "4", "--listener-chunk", "2", "--connector-chunk", "2")
    assert result["threads_alive"] == []
    for owner in result["owners"]:
        assert owner["termination"] == "incomplete_public_call"
        assert len(owner["calls"]) == 1
        call = owner["calls"][0]
        assert call["status"] == "completed_partial"
        assert call["requested_frames"] == 2
        assert call["actual_completed_frames"] == 1
        assert owner["final"]["frame_count"] == 11
        assert owner["final"]["session_tick"] == 11
        assert owner["final"]["cpu_cycles"] == 200
        assert owner["cleanup"] == ["endpoint_detached", "session_closed_without_save"]


def test_second_process_startup_error_preserves_first_orientation_report(monkeypatch):
    first = {"threads_alive": [], "processes_alive": [], "evidence": "first orientation retained"}
    calls = []

    def runner(args):
        calls.append(args.listener)
        if len(calls) == 2:
            raise OSError("injected second process startup failure")
        return first

    monkeypatch.setattr(probe, "runtime_identity", dict)
    monkeypatch.setattr(probe, "run_process_pair", runner)
    result = probe.run_probe(arguments("--owner-mode", "process", "--both-orientations"))
    assert result["pairs"][0] == first
    assert result["pairs"][1]["stop_reason"] == "startup_failure"
    assert result["pairs"][1]["supervisor_cancel_errors"] == [
        "OSError: injected second process startup failure"
    ]
