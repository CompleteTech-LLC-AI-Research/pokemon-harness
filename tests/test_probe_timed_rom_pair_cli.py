"""CLI argument parsing and synthetic-owner supervisor checks for the timed probe."""

import json
import threading

import pytest

from scripts import probe_timed_rom_pair as probe
from tests._probe_timed_rom_pair_support import (
    FakeEndpoint,
    FakeSession,
    Harness,
    arguments,
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
