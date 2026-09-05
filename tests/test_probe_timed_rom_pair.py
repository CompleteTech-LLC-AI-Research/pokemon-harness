"""Asset-free diagnostic orchestration checks, not ROM or liveness evidence."""

import hashlib
import json
import threading
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
