"""Asset-free diagnostic orchestration checks, not ROM or liveness evidence."""

import hashlib
import json
import multiprocessing
import os
import signal
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_timed_rom_pair as probe


def arguments(*extra):
    return probe.parse_args(
        [
            "--operation-timeout",
            "1",
            "--rearm-budget",
            "32",
            "--rearm-instruction-cap",
            "16",
            "--max-edge-lateness",
            "32",
            "--output",
            "/tmp/unused-probe-unit-output.json",
            *extra,
        ]
    )


@pytest.mark.parametrize(
    "option",
    [
        "--operation-timeout",
        "--overall-timeout",
        "--pair-timeout",
        "--cleanup-timeout",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_rejects_unbounded_or_nonpositive_timeouts(option, value):
    with pytest.raises(SystemExit):
        arguments(f"{option}={value}")


@pytest.mark.parametrize(
    "option",
    ["--listener-chunk", "--connector-chunk", "--frame-limit"],
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_rejects_invalid_public_frame_limits(option, value):
    with pytest.raises(SystemExit):
        arguments(f"{option}={value}")


def test_operation_timeout_and_native_policy_are_explicit():
    with pytest.raises(SystemExit):
        probe.parse_args([])


def test_quantum_is_not_a_cli_tuning_knob():
    with pytest.raises(SystemExit):
        arguments("--quantum", "512")


def test_owner_mode_keeps_thread_default_and_requires_explicit_process():
    baseline = arguments()
    process = arguments("--owner-mode", "process")
    assert baseline.owner_mode == "thread"
    assert process.owner_mode == "process"
    for name in (
        "listener_chunk",
        "connector_chunk",
        "frame_limit",
        "operation_timeout",
        "rearm_budget",
        "rearm_instruction_cap",
        "max_edge_lateness",
    ):
        assert getattr(baseline, name) == getattr(process, name)
    assert probe.QUANTUM_CYCLES == 256
    with pytest.raises(SystemExit):
        arguments("--owner-mode", "fork")


@pytest.mark.parametrize("version", ["blue_gb", "red_gb", "../yellow"])
def test_cli_rejects_noncanonical_versions(version):
    with pytest.raises(SystemExit):
        arguments("--listener", version)


class FakeSession:
    """Model ownership and partial advancement without claiming native evidence."""

    def __init__(self, harness, family):
        self.harness = harness
        self.family = family
        self.owner = threading.get_ident()
        self.calls = []
        self.endpoint = None
        self.closed = False
        self._pyboy = SimpleNamespace(
            frame_count=10,
            mb=SimpleNamespace(
                cpu=SimpleNamespace(cycles=100, retired_instructions=20),
                serial=SimpleNamespace(SB=0, SC=0, bits_remaining=0),
                lcd=SimpleNamespace(LY=7, clock=32),
                double_speed=False,
                execution_governor=None,
            ),
        )

    def record(self, name):
        assert threading.get_ident() == self.owner
        self.calls.append(name)

    def load_state(self, data):
        self.record("load")
        assert data == b"authored fixture placeholder"
        assert self.endpoint is None

    def bind_timed_execution(self, endpoint, **kwargs):
        self.record("bind")
        assert endpoint.pyboy is self._pyboy
        self.endpoint = endpoint

    def unbind_timed_execution(self, endpoint, **kwargs):
        self.record("unbind")
        assert endpoint is self.endpoint
        endpoint.close()
        self.endpoint = None

    def current_tick(self):
        return self._pyboy.frame_count

    @contextmanager
    def locked(self, **kwargs):
        self.record("locked")
        yield

    def step(self, count, *, render):
        self.record("step")
        assert render is False
        assert self.endpoint is not None
        self.harness.in_step.wait(timeout=2)
        behavior = self.harness.behavior[self.family]
        if behavior == "no_progress":
            return
        if behavior == "complete":
            self._pyboy.frame_count += count
            self._pyboy.mb.cpu.cycles += 100 * count
            return
        if behavior == "fail":
            self._pyboy.frame_count += 1
            self._pyboy.mb.cpu.cycles += 60
            raise RuntimeError("injected partial public call")
        assert self.endpoint.cancel_event.wait(2), "supervisor did not cancel"
        self._pyboy.frame_count += 1
        raise RuntimeError("interrupted public call")

    def close(self, *, save=False, **kwargs):
        self.record("close")
        assert save is False
        assert self.endpoint is None, "must not worker-stop a bound governor"
        self.closed = True


class FakeEndpoint:
    def __init__(self, harness, sock, **kwargs):
        self.harness = harness
        self.sock = sock
        self.family = kwargs["rom_version"]
        self.owner = threading.get_ident()
        self.cancel_event = kwargs["cancel_event"]
        self.cancel_threads = []
        self.pyboy = None
        self.closed = False
        self.metadata = {"family": self.family}
        assert kwargs["quantum_cycles"] == 256
        assert kwargs["operation_timeout"] == 1

    def attach(self, pyboy, *, deadline):
        assert threading.get_ident() == self.owner
        self.pyboy = pyboy

    def snapshot(self):
        assert threading.get_ident() == self.owner
        return {"local_half_cycles": 2 * self.pyboy.mb.cpu.cycles}

    def cancel(self):
        self.cancel_threads.append(threading.get_ident())
        self.cancel_event.set()

    def close(self):
        assert threading.get_ident() == self.owner
        assert self.cancel_event.is_set()
        if self.family == self.harness.close_failure:
            raise RuntimeError("injected detach failure")
        self.closed = True
        self.sock.close()


class Harness:
    def __init__(self, blue="complete", yellow="wait", *, close_failure=None):
        self.behavior = {"blue": blue, "yellow": yellow}
        self.close_failure = close_failure
        self.in_step = threading.Barrier(2)
        self.sessions = {}
        self.endpoints = {}

    def assets(self, version, repo_root):
        family = version.split("_")[0]
        return {
            "rom": family,
            "sym": "unused",
            "state": b"authored fixture placeholder",
            "family": family,
            "pins": {},
            "provenance": {"synthetic": True},
        }

    def session(self, rom, sym, **kwargs):
        session = FakeSession(self, rom)
        self.sessions[rom] = session
        return session

    def endpoint(self, sock, **kwargs):
        endpoint = FakeEndpoint(self, sock, **kwargs)
        self.endpoints[endpoint.family] = endpoint
        return endpoint

    def run(self, *extra, endpoint_factory=None):
        return probe.run_pair(
            arguments("--frame-limit", "1", "--pair-timeout", "0.5", *extra),
            session_factory=self.session,
            endpoint_factory=endpoint_factory or self.endpoint,
            asset_resolver=self.assets,
        )


@pytest.mark.parametrize(
    "blue,yellow,reason",
    [
        ("complete", "wait", "owner_completion_or_failure"),
        ("fail", "wait", "owner_completion_or_failure"),
        ("wait", "wait", "deadline"),
    ],
)
def test_supervisor_cancels_both_and_owner_detaches_before_stop(blue, yellow, reason):
    harness = Harness(blue, yellow)
    supervisor = threading.get_ident()
    result = harness.run()
    assert result["stop_reason"] == reason
    assert result["threads_alive"] == []
    assert result["quantum_cycles"] == 256
    assert result["address"][0] == "127.0.0.1"
    assert result["address"][1] > 0
    assert "not_graceful" in result["label"]
    assert len(harness.endpoints) == 2
    for family, session in harness.sessions.items():
        endpoint = harness.endpoints[family]
        assert supervisor in endpoint.cancel_threads
        assert endpoint.owner == session.owner != supervisor
        assert endpoint.closed and session.closed
        assert session.calls.index("load") < session.calls.index("bind")
        assert session.calls.index("bind") < session.calls.index("step")
        assert session.calls.index("unbind") < session.calls.index("close")
    for owner in result["owners"]:
        assert owner["final"]["frame_count"] >= owner["loaded"]["frame_count"]
        assert "cpu_cycles" in owner["final"]
        assert "timed" in owner["final"]


def test_interrupted_multiframe_call_records_actual_partial_progress():
    harness = Harness("wait", "fail")
    result = harness.run("--frame-limit", "2")
    yellow = result["owners"][1]
    call = yellow["calls"][0]
    assert call["requested_frames"] == 2
    assert call["status"] == "interrupted"
    assert call["actual_completed_frames"] == 1
    assert call["after"]["session_tick"] == call["before"]["session_tick"] + 1
    assert "injected partial" in call["error"]
    assert yellow["final"]["cpu_cycles"] == 160
    assert result["threads_alive"] == []


def test_detach_failure_never_stops_session_with_live_binding():
    harness = Harness(close_failure="blue")
    result = harness.run()
    blue = harness.sessions["blue"]
    assert blue.endpoint is not None
    assert not blue.closed
    assert "close" not in blue.calls
    assert any("injected detach failure" in e for e in result["owners"][0]["errors"])
    assert "session_closed_without_save" not in result["owners"][0]["cleanup"]
    assert result["threads_alive"] == []


def test_unpublished_factory_failure_cancels_peer_original_event():
    harness = Harness()
    entered = threading.Event()
    woke = threading.Event()
    events = []

    def factory(sock, **kwargs):
        events.append(kwargs["cancel_event"])
        if kwargs["side"] == "listener":
            entered.set()
            assert kwargs["cancel_event"].wait(2), "unpublished peer never cancelled"
            woke.set()
            raise RuntimeError("factory interrupted before publication")
        assert entered.wait(2)
        raise RuntimeError("injected pre-publication factory failure")

    result = harness.run(endpoint_factory=factory)
    assert woke.is_set()
    assert len(events) == 2 and events[0] is events[1]
    assert events[0].is_set()
    assert result["threads_alive"] == []
    assert result["stop_reason"] == "owner_completion_or_failure"
    for owner in result["owners"]:
        assert owner["termination"] == "owner_failure"
        assert owner["final"]["frame_count"] == 10
        assert owner["final"]["cpu_cycles"] == 100
        assert owner["cleanup"] == ["session_closed_without_save"]
    assert all(s.closed and "bind" not in s.calls for s in harness.sessions.values())


def test_no_progress_return_is_explicit_and_does_not_count_requested_frames():
    harness = Harness("no_progress", "wait")
    result = harness.run()
    blue = result["owners"][0]
    assert blue["termination"] == "no_progress"
    assert len(blue["calls"]) == 1
    assert blue["calls"][0]["status"] == "completed_no_progress"
    assert blue["calls"][0]["actual_completed_frames"] == 0
    assert blue["final"]["frame_count"] == blue["loaded"]["frame_count"]
    assert result["threads_alive"] == []


def test_observation_contains_numeric_tick_and_json_serializable_native_state():
    session = FakeSession(Harness(), "blue")
    observation = probe.observe(session)
    assert observation["session_tick"] == 10
    assert json.loads(json.dumps(observation))["session_tick"] == 10


def test_attach_failure_retains_loaded_native_evidence_without_binding(monkeypatch):
    harness = Harness()
    original = FakeEndpoint.attach

    def attach(self, pyboy, *, deadline):
        original(self, pyboy, deadline=deadline)
        if self.family == "blue":
            raise TypeError("source loaded double_speed is int")

    monkeypatch.setattr(FakeEndpoint, "attach", attach)
    result = harness.run()
    blue = result["owners"][0]
    assert blue["loaded"]["frame_count"] == blue["final"]["frame_count"] == 10
    assert blue["final"]["cpu_cycles"] == 100
    assert blue["calls"] == []
    assert any("double_speed is int" in error for error in blue["errors"])
    assert "bind" not in harness.sessions["blue"].calls
    assert harness.sessions["blue"].closed
    assert result["threads_alive"] == []


@pytest.mark.parametrize(
    "pair",
    [
        {"stop_reason": "deadline", "owners": [], "threads_alive": []},
        {
            "stop_reason": "owner_completion_or_failure",
            "owners": [{"termination": "owner_failure", "errors": ["injected"]}],
            "threads_alive": [],
        },
        {
            "stop_reason": "owner_completion_or_failure",
            "owners": [],
            "threads_alive": ["stuck-owner"],
        },
    ],
)
def test_main_returns_nonzero_for_deadline_failure_or_live_worker(monkeypatch, tmp_path, pair):
    output = tmp_path / "result.json"
    args = arguments("--output", str(output))
    monkeypatch.setattr(probe, "parse_args", lambda argv: args)
    monkeypatch.setattr(probe, "run_probe", lambda args: {"pairs": [pair]})
    assert probe.main([]) != 0
    assert json.loads(output.read_text())["pairs"][0] == pair


def test_first_supervisor_cancel_exception_does_not_skip_second_endpoint():
    harness = Harness()
    supervisor = threading.get_ident()

    def factory(sock, **kwargs):
        endpoint = harness.endpoint(sock, **kwargs)
        if kwargs["side"] == "listener":

            def fail_cancel():
                endpoint.cancel_threads.append(threading.get_ident())
                raise RuntimeError("injected first cancel failure")

            endpoint.cancel = fail_cancel
        return endpoint

    result = harness.run(endpoint_factory=factory)
    assert supervisor in harness.endpoints["blue"].cancel_threads
    assert supervisor in harness.endpoints["yellow"].cancel_threads
    assert any(
        "injected first cancel failure" in str(e) for e in result["supervisor_cancel_errors"]
    )
    assert result["threads_alive"] == []
    assert all(s.closed for s in harness.sessions.values())


def test_live_owners_report_incomplete_cleanup_without_mutating_returned_report(monkeypatch):
    harness = Harness("wait", "wait")
    release = threading.Event()
    original = FakeSession.step

    def stuck_step(self, count, *, render):
        assert release.wait(2), "test failed to release fake native work"
        return original(self, count, render=render)

    monkeypatch.setattr(FakeSession, "step", stuck_step)
    try:
        result = harness.run("--pair-timeout", "0.05", "--cleanup-timeout", "0.05")
        assert len(result["threads_alive"]) == 2
        for owner in result["owners"]:
            assert owner["owner_complete"] is False
            assert owner["cleanup"] == []
            assert owner["cleanup_status"] == "incomplete_owner_still_alive"
        frozen = json.dumps(result, sort_keys=True)
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name in ("timed-rom-0", "timed-rom-1"):
                thread.join(2)
                assert not thread.is_alive()
    assert json.dumps(result, sort_keys=True) == frozen


@pytest.mark.parametrize("version", ["blue_color", "yellow"])
def test_asset_resolution_uses_external_roots_and_canonical_pins(monkeypatch, tmp_path, version):
    from pokered_harness import config
    from scripts import validate_fixture_manifest as manifest

    family = version.split("_")[0]
    name = "pokemon-blue-color.gb" if family == "blue" else "pokemon-yellow.gbc"
    rom_root = tmp_path / "external-roms"
    fixture_root = tmp_path / "external-fixtures"
    monkeypatch.setenv("POKERED_ROM_ROOT", str(rom_root))
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(fixture_root))
    state, rom, symbols = b"synthetic-state", b"synthetic-rom", b"synthetic-symbols"
    sha1 = lambda data: hashlib.sha1(data).hexdigest()
    row = {
        "version": family,
        "kind": "ordinary",
        "variant": "color",
        "provenance": {"status": "verified"},
        "path": f"{family}/cable_club.state",
        "size_bytes": len(state),
        "sha1": sha1(state),
        "sha256": hashlib.sha256(state).hexdigest(),
        "expected_rom": {"path": f"rom/{family}/{name}", "sha1": sha1(rom)},
        "expected_symbols": {"path": f"rom/{family}/pokemon-{family}.sym", "sha1": sha1(symbols)},
    }
    contents = {
        rom_root / family / name: rom,
        rom_root / family / f"pokemon-{family}.sym": symbols,
        fixture_root / family / "cable_club.state": state,
    }
    monkeypatch.setattr(Path, "read_bytes", lambda path: contents[path])
    monkeypatch.setattr(manifest, "_load_manifest", lambda path: [row])
    monkeypatch.setattr(manifest, "_validate_schema", lambda rows: rows)
    monkeypatch.setattr(
        config,
        "load_versions",
        lambda path: SimpleNamespace(
            sha1_for_path=lambda path: sha1(rom),
            symbol_sha1_for_path=lambda path: sha1(symbols),
            pyboy_version="test-version",
            pyboy_revision="test-revision",
        ),
    )
    assets = probe.resolve_assets(version, tmp_path)
    assert assets["rom"] == rom_root / family / name
    assert assets["sym"] == rom_root / family / f"pokemon-{family}.sym"
    assert assets["state"] == state
    assert assets["family"] == family
    assert assets["pins"]["expected_rom_sha1"] == sha1(rom)
    contents[fixture_root / family / "cable_club.state"] = b"corrupted"
    with pytest.raises(ValueError, match="fixture bytes"):
        probe.resolve_assets(version, tmp_path)


def test_fixture_kind_selects_the_manifest_kind_from_the_admitted_basename():
    """The basename is the whole selector, so every admitted name maps once."""

    assert probe._fixture_kind("cable_club.state") == "ordinary"
    assert probe._fixture_kind("cable_club-vanilla.state") == "ordinary"
    assert probe._fixture_kind("cable_club-battle.state") == "battle"
    assert probe._fixture_kind("cable_club-battle-vanilla.state") == "battle"
    assert probe._fixture_kind("cable_club-slots.state") == "slots"


def test_asset_resolution_admits_the_six_slot_fixture_kind(monkeypatch, tmp_path):
    """A ``slots`` request resolves the ``slots`` row and fails closed otherwise."""

    from pokered_harness import config
    from scripts import validate_fixture_manifest as manifest

    family, version = "red", "red_color"
    rom_root = tmp_path / "external-roms"
    fixture_root = tmp_path / "external-fixtures"
    monkeypatch.setenv("POKERED_ROM_ROOT", str(rom_root))
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(fixture_root))
    state, rom, symbols = b"slots-state", b"synthetic-rom", b"synthetic-symbols"
    sha1 = lambda data: hashlib.sha1(data).hexdigest()  # test helper

    def row_for(basename):
        return {
            "id": "red-color-slots",
            "version": family,
            "kind": "slots",
            "variant": "color",
            "provenance": {"status": "verified"},
            "path": f"{family}/{basename}",
            "size_bytes": len(state),
            "sha1": sha1(state),
            "sha256": hashlib.sha256(state).hexdigest(),
            "expected_rom": {
                "path": f"rom/{family}/pokemon-red-color.gb",
                "sha1": sha1(rom),
            },
            "expected_symbols": {
                "path": f"rom/{family}/pokemon-red.sym",
                "sha1": sha1(symbols),
            },
        }

    contents = {
        rom_root / family / "pokemon-red-color.gb": rom,
        rom_root / family / "pokemon-red.sym": symbols,
        fixture_root / family / "cable_club-slots.state": state,
    }
    monkeypatch.setattr(Path, "read_bytes", lambda path: contents[path])
    monkeypatch.setattr(
        config,
        "load_versions",
        lambda path: SimpleNamespace(
            sha1_for_path=lambda path: sha1(rom),
            symbol_sha1_for_path=lambda path: sha1(symbols),
            pyboy_version="test-version",
            pyboy_revision="test-revision",
        ),
    )
    rows = [row_for("cable_club-slots.state")]
    monkeypatch.setattr(manifest, "_load_manifest", lambda path: rows)
    monkeypatch.setattr(manifest, "_validate_schema", lambda value: value)

    assets = probe.resolve_assets(version, tmp_path, fixture="cable_club-slots.state")
    assert assets["state"] == state
    assert assets["provenance"]["registry"]["kind"] == "slots"

    # A slots key that resolves to a differently named row must still fail
    # closed rather than silently admitting the wrong bytes.
    rows[0] = row_for("cable_club-slots-alt.state")
    with pytest.raises(ValueError, match="not the registered 'slots' row"):
        probe.resolve_assets(version, tmp_path, fixture="cable_club-slots.state")


def _spawn_diagnostic_owner(
    args_dict,
    index,
    sock,
    cancel_event,
    done_event,
    barrier,
    deadline,
    overall,
    report_sender,
    stderr_path,
):
    """Serializable synthetic owner; never loads assets or claims native progress."""
    scenario = args_dict["test_scenario"]
    record = {
        "side": ("listener", "connector")[index],
        "calls": [],
        "cleanup": [],
        "errors": [],
        "termination": "cancelled_or_deadline",
        "test_pid": os.getpid(),
        "test_executable": sys.executable,
        "test_repo_root": args_dict["repo_root"],
        "test_input_profile": args_dict["input_profile"],
        "test_tcp_nodelay": sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY),
    }
    try:
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(bytes([index]))
        assert sock.recv(1) == bytes([1 - index])
        if scenario in ("ignore_cancel", "partial_report"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(stderr_path).with_suffix(".checkpoint.json").write_text(
                json.dumps({"synthetic_checkpoint": True, "phase": "before_unresponsive_wait"})
            )
        barrier.wait(timeout=max(0.001, deadline - time.monotonic()))
        if scenario in ("ignore_cancel", "partial_report"):
            if scenario == "partial_report":
                os.write(report_sender.fileno(), struct.pack("!i", 1024) + b"{")
                # A dead/stuck child may own a shared condition indefinitely.
                abandoned_event = multiprocessing.get_context("spawn").Event()
                abandoned_event._cond.acquire()
            # Parent must kill this process; test fixture also reaps on assertion failure.
            time.sleep(30)
            return
        if index == 0:
            record["errors"].append("injected failure before endpoint publication")
            record["termination"] = "owner_failure"
            done_event.set()
        record["test_cancel_seen"] = cancel_event.wait(max(0, overall - time.monotonic()))
        assert record["test_cancel_seen"]
        record["cleanup"].append("synthetic_socket_closed")
        if index == 0 and scenario == "invalid_json":
            report_sender.send_bytes(b"not JSON")
        elif index == 0 and scenario == "oversize":
            report_sender.send_bytes(b"x" * (probe.MAX_REPORT_BYTES + 1))
        else:
            report_sender.send_bytes(json.dumps(record).encode())
    finally:
        sock.close()
        report_sender.close()


@pytest.fixture
def spawned_probe_args(tmp_path):
    """Retain a final emergency reap so a failed assertion cannot leak test children."""
    before = {child.pid for child in multiprocessing.active_children()}
    args = arguments(
        "--owner-mode",
        "process",
        "--repo-root",
        str(tmp_path),
        "--pair-timeout",
        "3",
        "--cleanup-timeout",
        "1",
        "--overall-timeout",
        "8",
    )
    try:
        yield args
    finally:
        for child in multiprocessing.active_children():
            if child.pid not in before:
                child.kill()
                child.join(2)


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


class _NoSharedEventContext:
    """Fail immediately if the supervisor tries to use child-lockable Events."""

    def __init__(self, *, fail_second_start=False, block_before_owner=False):
        self.context = multiprocessing.get_context("spawn")
        self.fail_second_start = fail_second_start
        self.block_before_owner = block_before_owner
        self.process_count = 0
        self.startup = {}
        self.connections = []
        if fail_second_start:
            self.ready_receiver, self.ready_sender = self.context.Pipe(duplex=False)
            self.connections.extend((self.ready_receiver, self.ready_sender))
            if block_before_owner:
                self.gate_receiver, self.gate_sender = self.context.Pipe(duplex=False)
                self.connections.extend((self.gate_receiver, self.gate_sender))

    def close(self):
        for connection in self.connections:
            connection.close()

    def __getattr__(self, name):
        return getattr(self.context, name)

    def Event(self):
        pytest.fail("supervisor must not depend on multiprocessing.Event locks")

    def Process(self, **kwargs):
        self.process_count += 1
        if self.fail_second_start and self.process_count == 2:
            deadline, overall = kwargs["args"][6:8]
            return _FailedStartProcess(self, min(deadline, overall))
        if self.fail_second_start:
            if self.block_before_owner:
                kwargs["args"] = (kwargs["args"], self.ready_sender, self.gate_receiver)
                kwargs["target"] = _spawn_block_before_owner
            else:
                kwargs["kwargs"] = {"ready_sender": self.ready_sender}
        return self.context.Process(**kwargs)


class _FailedStartProcess:
    def __init__(self, context, deadline):
        self.context = context
        self.deadline = deadline

    def start(self):
        self.context.startup["pair_deadline"] = self.deadline
        if not self.context.ready_receiver.poll(max(0, self.deadline - time.monotonic())):
            raise TimeoutError("first owner readiness missed existing pair deadline")
        self.context.startup.update(json.loads(self.context.ready_receiver.recv_bytes(4096)))
        self.context.startup["injected_monotonic"] = time.monotonic()
        if self.context.startup["injected_monotonic"] >= self.deadline:
            raise TimeoutError("first owner readiness arrived after existing pair deadline")
        raise OSError("injected second owner start failure")


def _spawn_block_before_owner(owner_args, ready_sender, gate_receiver):
    """Hold a real spawned child before owner entry until termination or overall bound."""
    ready_sender.send_bytes(
        json.dumps(
            {
                "phase": "before_owner_entry",
                "ready_monotonic": time.monotonic(),
                "pid": os.getpid(),
            }
        ).encode()
    )
    ready_sender.close()
    # The parent retains the write end but never releases this gate. No sleep,
    # shared Event, owner report, or cooperative cancellation is involved.
    if gate_receiver.poll(max(0, owner_args[7] - time.monotonic())):
        raise AssertionError("pre-owner startup gate unexpectedly released")
    gate_receiver.close()


def _spawn_wait_for_startup_cancel(
    args_dict,
    index,
    sock,
    cancel_event,
    done_event,
    barrier,
    deadline,
    overall,
    report_sender,
    stderr_path,
    *,
    ready_sender,
):
    """Owner starts without a peer, waits for supervisor cancellation, then reports."""
    ready = time.monotonic()
    ready_sender.send_bytes(
        json.dumps(
            {
                "phase": "cancellation_wait",
                "ready_monotonic": ready,
                "pid": os.getpid(),
            }
        ).encode()
    )
    ready_sender.close()
    observed = cancel_event.wait(max(0, overall - time.monotonic()))
    record = {
        "side": "listener",
        "calls": [],
        "cleanup": [],
        "errors": [],
        "termination": "cancelled_or_deadline",
        "test_cancel_seen": observed,
        "test_ready_monotonic": ready,
        "test_cancel_seen_monotonic": time.monotonic(),
    }
    report_sender.send_bytes(json.dumps(record).encode())
    report_sender.close()
    sock.close()


def _print_startup_evidence(context, args, result, started, returned):
    owner = result["owners"][0]
    print(
        json.dumps(
            {
                **context.startup,
                "started": started,
                "cancelled_monotonic": result["cancelled_monotonic"],
                "cancel_seen_monotonic": owner.get("test_cancel_seen_monotonic"),
                "returned": returned,
                "elapsed_s": returned - started,
                "cleanup_elapsed_s": returned - result["cancelled_monotonic"],
                "pair_timeout": args.pair_timeout,
                "cleanup_timeout": args.cleanup_timeout,
                "overall_timeout": args.overall_timeout,
                "exitcode": owner["exitcode"],
                "forced_termination": owner["forced_termination"],
                "cancel_seen": owner.get("test_cancel_seen"),
                "live_process_count": len(result["processes_alive"]),
                "live_reader_count": len(result["report_readers_alive"]),
            },
            sort_keys=True,
        )
    )


def test_process_startup_failure_signals_without_shared_event_locks(spawned_probe_args):
    context = _NoSharedEventContext(fail_second_start=True)
    started = time.monotonic()
    try:
        result = probe.run_process_pair(
            spawned_probe_args,
            context=context,
            child_target=_spawn_wait_for_startup_cancel,
        )
    finally:
        context.close()
    returned = time.monotonic()
    _print_startup_evidence(context, spawned_probe_args, result, started, returned)
    assert time.monotonic() - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "startup_failure"
    assert any(
        "injected second owner start failure" in error
        for error in result["supervisor_cancel_errors"]
    )
    assert result["processes_alive"] == result["report_readers_alive"] == []
    assert result["owners"][0]["test_cancel_seen"] is True
    assert result["owners"][0]["exitcode"] == 0
    assert result["owners"][0]["forced_termination"] is False
    assert context.startup["phase"] == "cancellation_wait"
    assert context.startup["pid"] == result["owners"][0]["pid"]
    assert context.startup["ready_monotonic"] == result["owners"][0]["test_ready_monotonic"]
    assert started <= context.startup["ready_monotonic"] <= context.startup["injected_monotonic"]
    assert context.startup["injected_monotonic"] < context.startup["pair_deadline"]
    assert context.startup["injected_monotonic"] <= result["cancelled_monotonic"]
    assert (
        result["cancelled_monotonic"]
        <= result["owners"][0]["test_cancel_seen_monotonic"]
        <= returned
    )
    assert result["owners"][1]["pid"] is None
    assert result["owners"][1]["actual_missing"] is True


def test_process_early_startup_failure_forces_termination_without_shared_event_locks(
    spawned_probe_args,
):
    context = _NoSharedEventContext(fail_second_start=True, block_before_owner=True)
    started = time.monotonic()
    try:
        result = probe.run_process_pair(
            spawned_probe_args, context=context, child_target=_spawn_wait_for_startup_cancel
        )
    finally:
        context.close()
    returned = time.monotonic()
    _print_startup_evidence(context, spawned_probe_args, result, started, returned)
    assert returned - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "startup_failure"
    assert any(
        "injected second owner start failure" in error
        for error in result["supervisor_cancel_errors"]
    )
    assert context.startup["phase"] == "before_owner_entry"
    assert started <= context.startup["ready_monotonic"] <= context.startup["injected_monotonic"]
    assert context.startup["injected_monotonic"] < context.startup["pair_deadline"]
    assert context.startup["injected_monotonic"] <= result["cancelled_monotonic"] <= returned
    assert result["processes_alive"] == result["report_readers_alive"] == []
    owner = result["owners"][0]
    assert owner["pid"] == context.startup["pid"]
    assert owner["owner_complete"] and not owner["alive"]
    assert owner["forced_termination"] is True
    assert owner["exitcode"] in (-signal.SIGTERM, -signal.SIGKILL)
    assert owner["termination"] == "missing_report"
    assert "test_cancel_seen" not in owner
    assert owner["actual_missing"] is True
    assert owner["cleanup"] == []
    assert owner["cleanup_status"] == "nongraceful_forced_or_live"
    assert result["owners"][1]["pid"] is None
    assert result["owners"][1]["actual_missing"] is True


def test_process_partial_report_kill_reaps_reader_without_shared_event_locks(spawned_probe_args):
    spawned_probe_args.test_scenario = "partial_report"
    started = time.monotonic()
    result = probe.run_process_pair(
        spawned_probe_args,
        context=_NoSharedEventContext(),
        child_target=_spawn_diagnostic_owner,
    )
    assert time.monotonic() - started < spawned_probe_args.overall_timeout
    assert result["stop_reason"] == "deadline"
    assert result["processes_alive"] == result["report_readers_alive"] == []
    for owner in result["owners"]:
        assert owner["forced_termination"]
        assert owner["exitcode"] == -signal.SIGKILL
        assert owner["actual_missing"] is True
        assert owner["errors"]
        assert owner["cleanup"] == []


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


class _ReadOnlyProbeMemory:
    def __getitem__(self, address):
        mapped_io = {
            0xFFFF: 0x01,  # IE
            0xFF0F: 0x00,  # IF
            0xFF00: 0xCF,  # JOYP
            0xFF44: 0x07,  # LY
            0xFF41: 0x80,  # STAT
            0xFF40: 0x91,  # LCDC
        }
        if address in mapped_io:
            return mapped_io[address]
        return address & 255

    def __setitem__(self, address, value):
        pytest.fail("menu diagnostic attempted a memory write")


class _MenuSession(FakeSession):
    def __init__(self, harness, family, behavior):
        super().__init__(harness, family)
        self.behavior = behavior
        self.inputs = []
        self.steps = []
        self._pyboy.memory = _ReadOnlyProbeMemory()
        self._pyboy.register_file = SimpleNamespace(
            A=1, F=0xB0, B=0, C=0x13, D=0, E=0xD8, HL=0xC123, SP=0xDFFE, PC=0x1234
        )
        input_addresses = {
            name: 0xFF80 + index
            for index, name in enumerate(
                (
                    "hLoadedROMBank",
                    "hJoyInput",
                    "hJoyPressed",
                    "hJoyHeld",
                    "hJoyLast",
                    "wJoyIgnore",
                    "wStatusFlags5",
                    "wWalkCounter",
                    "hVBlankOccurred",
                    "hSerialReceivedNewData",
                    "hSerialSendData",
                    "hSerialReceiveData",
                )
            )
        }

        def address(name):
            if behavior == "missing_symbol" or (
                behavior == "observation_failure" and self._pyboy.frame_count > 10
            ):
                raise KeyError(name)
            return input_addresses.get(name, 0xC000 + len(name))

        self.symbols = SimpleNamespace(addr_of=address)

    def press(self, button, *, duration=1):
        self.record("press")
        assert self.endpoint is not None
        self.inputs.append(
            (self._pyboy.frame_count - 10, button, duration, self._pyboy.mb.cpu.cycles)
        )
        if self.behavior == "press_failure":
            raise RuntimeError("injected input admission failure")

    def step(self, count, *, render):
        self.record("step")
        self.steps.append(count)
        assert render is False
        if self.behavior == "zero":
            return
        if self.behavior == "interrupt_before_frame":
            self._pyboy.mb.cpu.cycles += 4
            raise RuntimeError("interrupted before complete frame")
        advance = 2 if self.behavior == "overadvance" else count
        self._pyboy.frame_count += advance
        self._pyboy.mb.cpu.cycles += 100 * advance
        if self.behavior == "interrupt_after_frame":
            raise RuntimeError("interrupted after actual frame")


def _run_menu_owner(*extra, behavior="complete", profile="menu", checkpoint=None):
    """One synthetic owner runs production scheduling without a peer CPU or ROM."""
    args = arguments(
        "--input-profile",
        profile,
        "--listener-chunk",
        "1",
        "--connector-chunk",
        "1",
        "--frame-limit",
        "77",
        *extra,
    )
    harness = Harness()
    session = _MenuSession(harness, "blue", behavior)
    cancelled = threading.Event()

    class Done:
        def set(self):
            cancelled.set()

    records = [{"side": "listener", "calls": [], "cleanup": [], "errors": []}, {}]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as connector:
            accepted, _ = listener.accept()
            try:
                probe._run_owner(
                    0,
                    args,
                    records,
                    [accepted, connector],
                    cancelled,
                    Done(),
                    threading.Barrier(1),
                    [None, None],
                    threading.Lock(),
                    time.monotonic() + 3,
                    time.monotonic() + 5,
                    lambda *args, **kwargs: session,
                    harness.endpoint,
                    harness.assets,
                    checkpoint,
                )
            finally:
                accepted.close()
    return session, records[0]


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
