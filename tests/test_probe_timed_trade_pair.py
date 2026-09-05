"""Authored/fake trade-entry checks; never commercial-ROM qualification."""

import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from scripts import probe_timed_rom_pair as ordinary
from scripts import probe_timed_trade_pair as trade


def cli(**overrides):
    options = {
        "frame-limit": "120",
        "overall-timeout": "30",
        "pair-timeout": "20",
        "operation-timeout": "5",
        "rearm-budget": "4096",
        "rearm-instruction-cap": "1024",
        "max-edge-lateness": "4096",
        "listener-slot": "1",
        "connector-slot": "6",
        "output": "/tmp/unused-authored-trade-report.json",
    }
    options.update(overrides)
    return [f"--{key}={value}" for key, value in options.items() if value is not None]


@pytest.mark.parametrize(
    "option",
    [
        "frame-limit",
        "overall-timeout",
        "pair-timeout",
        "operation-timeout",
        "rearm-budget",
        "rearm-instruction-cap",
        "max-edge-lateness",
        "listener-slot",
        "connector-slot",
    ],
)
def test_requires_explicit_bounds_policy_and_outgoing_slots(option):
    with pytest.raises(SystemExit) as caught:
        trade.parse_args(cli(**{option: None}))
    assert caught.value.code == 2


@pytest.mark.parametrize("option", ["overall-timeout", "pair-timeout", "operation-timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_wall_bounds_are_finite_and_positive(option, value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{option: value}))


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "inf"])
def test_frame_bound_is_a_positive_integer(value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{"frame-limit": value}))


@pytest.mark.parametrize("option", ["listener-slot", "connector-slot"])
@pytest.mark.parametrize("value", ["0", "7", "-1", "1.5", "nan"])
def test_outgoing_slot_rejects_out_of_range_or_noninteger(option, value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{option: value}))


@pytest.mark.parametrize("listener,connector", [(1, 6), (6, 1), (3, 4)])
def test_outgoing_slots_convert_once_and_preserve_cli_values(listener, connector):
    args = trade.parse_args(cli(**{"listener-slot": listener, "connector-slot": connector}))
    assert (args.listener_slot, args.connector_slot) == (listener, connector)
    assert (args.listener_slot_index, args.connector_slot_index) == (listener - 1, connector - 1)


def test_trade_defaults_fix_process_whole_frames_and_evidence():
    args = trade.parse_args(cli())
    assert args.checkpoint == "reciprocal-exchange"
    assert args.owner_mode == "process"
    assert args.listener_chunk == args.connector_chunk == 1
    assert args.call_retention == "stream"
    assert args.rom_milestones is True
    assert args.operation_timeout == 5
    assert ordinary.QUANTUM_CYCLES == 256


def test_select_mon_is_explicit_checkpoint():
    assert trade.parse_args(cli() + ["--checkpoint", "select-mon"]).checkpoint == "select-mon"


@pytest.mark.parametrize(
    "goal,checkpoint", [("trade", "reciprocal-exchange"), ("select-mon", "select-mon")]
)
def test_goal_alias_normalizes_to_checkpoint(goal, checkpoint):
    assert trade.parse_args(cli() + ["--goal", goal]).checkpoint == checkpoint


def test_goal_alias_and_checkpoint_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        trade.parse_args(cli() + ["--goal", "trade", "--checkpoint", "select-mon"])


@pytest.mark.parametrize(
    "extra",
    [
        ["--owner-mode", "thread"],
        ["--listener-chunk", "2"],
        ["--connector-chunk", "2"],
        ["--call-retention", "inline"],
        ["--quantum", "512"],
    ],
)
def test_cli_cannot_weaken_fixed_execution_contract(extra):
    with pytest.raises(SystemExit):
        trade.parse_args(cli() + extra)


def test_driver_is_internal_not_an_arbitrary_cli_import():
    with pytest.raises(SystemExit):
        trade.parse_args(cli() + ["--owner-driver", "untrusted.module:factory"])


@pytest.mark.parametrize("option", ["rearm-budget", "rearm-instruction-cap", "max-edge-lateness"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5"])
def test_native_policy_stays_explicit_positive_integer(option, value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{option: value}))


def test_ordinary_probe_defaults_are_preserved():
    args = ordinary.parse_args(
        [
            "--operation-timeout=5",
            "--rearm-budget=4096",
            "--rearm-instruction-cap=1024",
            "--max-edge-lateness=4096",
            "--output=/tmp/unused-authored-ordinary-report.json",
        ]
    )
    assert args.owner_mode == "thread"
    assert args.input_profile == "none"
    assert (args.listener_chunk, args.connector_chunk) == (1, 2)
    assert args.frame_limit == 6
    assert args.call_retention == "inline"
    assert args.rom_milestones is False


@pytest.mark.parametrize("value", ["1", "4.9", "6"])
def test_trade_operation_timeout_requires_the_fixed_five_seconds(value):
    with pytest.raises(SystemExit):
        trade.parse_args(cli(**{"operation-timeout": value}))


def fake_pair():
    return {
        "stop_reason": "both_owner_goals",
        "owner_goals": [True, True],
        "goal_stop": True,
        "threads_alive": [],
        "processes_alive": [],
        "report_readers_alive": [],
        "supervisor_cancel_errors": [],
        "owners": [
            {
                "side": side,
                "calls": [],
                "errors": [],
                "cleanup": [
                    "endpoint_detached",
                    "owner_driver_closed",
                    "session_closed_without_save",
                ],
                "termination": "goal_cancelled",
                "exitcode": 0,
                "alive": False,
                "forced_termination": False,
                "actual_missing": False,
                "watcher_alive": False,
                "owner_complete": True,
                "local_goal": True,
                "milestone_observer": "owner_driver",
                "driver_snapshot": {"side": side, "authored": True},
                "call_log": {"authored": True},
            }
            for side in ("listener", "connector")
        ],
    }


def fake_supervisor(monkeypatch, pair):
    calls = []

    def run(args, *, owner_driver):
        calls.append((args, owner_driver))
        return pair

    monkeypatch.setattr(trade.probe, "run_process_pair", run)
    monkeypatch.setattr(trade.probe, "runtime_identity", lambda: {"authored": True})
    monkeypatch.setattr(trade, "_validate_calls", lambda owner: None)
    return calls


@pytest.mark.parametrize("checkpoint", ["reciprocal-exchange", "select-mon"])
def test_entry_reuses_supervisor_and_parent_adjudicator(monkeypatch, checkpoint):
    pair = fake_pair()
    calls = fake_supervisor(monkeypatch, pair)
    judgments = []

    def adjudicate(snapshots, checkpoint):
        judgments.append((snapshots, checkpoint))
        return {
            "complete": False,
            "checkpoint_only": checkpoint == "select-mon",
            "errors": [],
            "status": "authored_partial",
        }

    monkeypatch.setattr(trade, "adjudicate_pair", adjudicate)
    args = trade.parse_args(cli() + ["--checkpoint", checkpoint])
    report = trade.run_probe(args)
    assert len(calls) == 1
    assert calls[0][0] is args
    assert calls[0][1] == trade.DRIVER == "scripts._timed_trade_probe:create_owner_driver"
    assert args.owner_driver_options == [
        {"version": args.listener, "outgoing_slot": 0, "checkpoint": checkpoint},
        {"version": args.connector, "outgoing_slot": 5, "checkpoint": checkpoint},
    ]
    assert judgments == [([owner["driver_snapshot"] for owner in pair["owners"]], checkpoint)]
    assert report["complete"] is False


def test_joint_local_goals_do_not_replace_reciprocal_parent_proof(monkeypatch):
    pair = fake_pair()
    for owner in pair["owners"]:
        owner["driver_snapshot"]["objective_complete"] = True
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": False,
            "checkpoint_only": False,
            "errors": ["reciprocal party records do not match"],
            "status": "incomplete",
        },
    )
    report = trade.run_probe(trade.parse_args(cli()))
    assert report["complete"] is False


def test_reverse_orientation_swaps_slots_with_versions_and_shares_deadline(monkeypatch):
    calls = fake_supervisor(monkeypatch, fake_pair())
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    args = trade.parse_args(cli() + ["--both-orientations"])
    report = trade.run_probe(args)
    assert len(calls) == 2
    forward, reverse = [call[0] for call in calls]
    assert (reverse.listener, reverse.connector) == (forward.connector, forward.listener)
    assert (reverse.listener_slot, reverse.connector_slot) == (6, 1)
    assert (reverse.listener_slot_index, reverse.connector_slot_index) == (5, 0)
    assert reverse.owner_driver_options == list(reversed(forward.owner_driver_options))
    assert forward.absolute_deadline == reverse.absolute_deadline
    assert len(report["pairs"]) == 2


@pytest.mark.parametrize(
    "failure", ["forced_termination", "actual_missing", "alive", "watcher_alive"]
)
def test_reciprocal_proof_cannot_override_unhealthy_owner(monkeypatch, failure):
    pair = fake_pair()
    pair["owners"][0][failure] = True
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


@pytest.mark.parametrize("reason", ["deadline", "startup_failure", "owner_completion_or_failure"])
def test_full_proof_requires_supervisor_joint_goal_stop(monkeypatch, reason):
    pair = fake_pair()
    pair["stop_reason"] = reason
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    report = trade.run_probe(trade.parse_args(cli()))
    assert report["complete"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    "termination", ["frame_bound", "owner_failure", "no_progress", "cancelled_or_deadline"]
)
def test_full_proof_does_not_hide_non_goal_termination(monkeypatch, termination):
    pair = fake_pair()
    pair["owners"][0]["termination"] = termination
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


@pytest.mark.parametrize("field", ["owner_goals", "goal_stop", "local_goal"])
@pytest.mark.parametrize("missing", [False, True])
def test_full_proof_requires_explicit_joint_and_local_goal_evidence(monkeypatch, field, missing):
    pair = fake_pair()
    target = pair["owners"][0] if field == "local_goal" else pair
    if missing:
        del target[field]
    else:
        target[field] = [True, False] if field == "owner_goals" else False
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


@pytest.mark.parametrize("missing", ["milestone_observer", "owner_driver_closed"])
def test_full_proof_requires_delegated_observation_and_driver_cleanup(monkeypatch, missing):
    pair = fake_pair()
    owner = pair["owners"][0]
    if missing == "milestone_observer":
        del owner[missing]
    else:
        owner["cleanup"].remove(missing)
        owner["cleanup"].append("milestone_hooks_removed")
    fake_supervisor(monkeypatch, pair)
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": False,
            "errors": [],
            "status": "complete",
        },
    )
    assert trade.run_probe(trade.parse_args(cli()))["complete"] is False


def test_select_mon_never_claims_full_completion_even_if_adjudicator_does(monkeypatch):
    fake_supervisor(monkeypatch, fake_pair())
    monkeypatch.setattr(
        trade,
        "adjudicate_pair",
        lambda *args, **kwargs: {
            "complete": True,
            "checkpoint_only": True,
            "errors": [],
            "status": "complete",
        },
    )
    args = trade.parse_args(cli() + ["--checkpoint", "select-mon"])
    assert trade.run_probe(args)["complete"] is False


@pytest.mark.parametrize("case", ["reciprocal", "select_mon", "copy_only", "one_way_copy"])
def test_entry_uses_real_helper_to_require_reciprocal_copy_and_posttrade_endpoint(
    monkeypatch, case
):
    def party(species):
        return {
            "count": 1,
            "species": [species, 255],
            "records": [(bytes([species]) + bytes(43)).hex()],
            "ot_names": [(bytes([species]) + bytes(10)).hex()],
            "nicknames": [(bytes([species + 1]) + bytes(10)).hex()],
        }

    first, second = party(1), party(2)
    checkpoint = "select-mon" if case == "select_mon" else "reciprocal-exchange"
    pair = fake_pair()
    for index, (before, copied) in enumerate(((first, second), (second, first))):
        pair["owners"][index]["driver_snapshot"] = {
            "role": ("listen", "connect")[index],
            "checkpoint": checkpoint,
            "outgoing_slot": 0,
            "checkpoint_reached": True,
            "readiness": [1] * 5 + ([0] * 3 if case == "select_mon" else [1] * 3),
            "copy_complete": case != "select_mon",
            "trade_endpoint_observed": case == "reciprocal",
            "before_party": before,
            "copied_party": copied,
            "final_party": copied,
        }
    if case == "one_way_copy":
        pair["owners"][1]["driver_snapshot"].update(
            copied_party=second,
            final_party=second,
            trade_endpoint_observed=True,
        )
        pair["owners"][0]["driver_snapshot"]["trade_endpoint_observed"] = True
    fake_supervisor(monkeypatch, pair)
    report = trade.run_probe(trade.parse_args(cli() + ["--checkpoint", checkpoint]))
    assert report["complete"] is (case == "reciprocal")
    assert report["checkpoint_only"] is (case == "select_mon")


def test_missing_driver_snapshot_fails_closed(monkeypatch):
    pair = fake_pair()
    del pair["owners"][1]["driver_snapshot"]
    fake_supervisor(monkeypatch, pair)
    report = trade.run_probe(trade.parse_args(cli()))
    assert report["complete"] is False
    assert report["errors"]


@pytest.mark.parametrize(
    "complete,checkpoint_only,expected",
    [
        (True, False, 0),
        (False, True, 0),
        (False, False, 2),
    ],
)
def test_main_persists_distinct_complete_and_partial_results(
    monkeypatch,
    tmp_path,
    complete,
    checkpoint_only,
    expected,
):
    report = {
        "pairs": [],
        "complete": complete,
        "checkpoint_only": checkpoint_only,
        "status": "complete" if complete else "checkpoint" if checkpoint_only else "incomplete",
        "errors": [],
    }
    monkeypatch.setattr(trade, "run_probe", lambda args: report)
    output = tmp_path / "report.json"
    checkpoint = "select-mon" if checkpoint_only else "reciprocal-exchange"
    result = trade.main(cli(output=output) + ["--checkpoint", checkpoint])
    assert result == expected
    saved = json.loads(output.read_text())
    assert saved["complete"] is complete
    assert saved["checkpoint_only"] is checkpoint_only


def test_main_preserves_existing_evidence(monkeypatch, tmp_path):
    output = tmp_path / "report.json"
    output.write_text("previous evidence\n")
    monkeypatch.setattr(
        trade,
        "run_probe",
        lambda args: {
            "pairs": [],
            "complete": False,
            "checkpoint_only": False,
            "status": "incomplete",
            "errors": [],
        },
    )
    with pytest.raises(FileExistsError):
        trade.main(cli(output=output))
    assert output.read_text() == "previous evidence\n"


@pytest.mark.parametrize("symlink", [False, True])
def test_output_cannot_enter_asset_checkout_via_direct_or_symlink_path(tmp_path, symlink):
    checkout = tmp_path / "assets"
    checkout.mkdir()
    destination = checkout
    if symlink:
        destination = tmp_path / "alias"
        destination.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(SystemExit):
        trade.parse_args(cli(output=destination / "report.json") + ["--repo-root", str(checkout)])
    assert not (checkout / "report.json").exists()


def test_output_cannot_enter_source_checkout():
    with pytest.raises(SystemExit):
        trade.parse_args(cli(output=trade.ROOT / "unused-authored-report.json"))


def authored_call_artifact(tmp_path, calls, *, noncompleted=0, expected=False):
    payload = b"".join((json.dumps(call) + "\n").encode() for call in calls)
    path = tmp_path / "calls.jsonl"
    path.write_bytes(payload)
    return {
        "call_log": {
            "path": str(path),
            "bytes": len(payload),
            "record_count": len(calls),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "complete": True,
            "error": None,
        },
        "call_counts": {"total": len(calls), "noncompleted": noncompleted},
        "expected_goal_cancellation": expected,
    }


def completed_call():
    return {"status": "completed", "requested_frames": 1, "actual_completed_frames": 1}


def cancelled_call():
    return {
        "status": "interrupted",
        "requested_frames": 1,
        "actual_completed_frames": 0,
        "error": "Cancelled: operation cancelled",
        "expected_goal_cancellation": True,
    }


def test_marked_terminal_goal_cancellation_retains_raw_interrupted_evidence(tmp_path):
    calls = [completed_call(), cancelled_call()]
    owner = authored_call_artifact(tmp_path, calls, noncompleted=1, expected=True)
    trade._validate_calls(owner)
    assert owner["call_counts"]["noncompleted"] == 1
    saved = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert saved == calls


def test_whole_completed_call_artifact_is_accepted(tmp_path):
    trade._validate_calls(authored_call_artifact(tmp_path, [completed_call()]))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_owner_marker",
        "missing_call_marker",
        "wrong_exception",
        "deadline_exception",
        "missing_actual_frames",
        "earlier_interrupt",
        "lying_count",
        "multiple_interrupts",
        "partial_completed",
        "no_progress_completed",
    ],
)
def test_only_exact_terminal_postgoal_cancellation_is_tolerated(tmp_path, mutation):
    calls = [completed_call(), cancelled_call()]
    expected, noncompleted = True, 1
    if mutation == "missing_owner_marker":
        expected = False
    elif mutation == "missing_call_marker":
        del calls[-1]["expected_goal_cancellation"]
    elif mutation == "wrong_exception":
        calls[-1]["error"] = "ChannelClosed: peer closed"
    elif mutation == "deadline_exception":
        calls[-1]["error"] = "Deadline: operation expired"
    elif mutation == "missing_actual_frames":
        del calls[-1]["actual_completed_frames"]
    elif mutation == "earlier_interrupt":
        calls.reverse()
    elif mutation == "lying_count":
        noncompleted = 0
    elif mutation == "multiple_interrupts":
        calls.append(cancelled_call())
        noncompleted = 2
    elif mutation == "partial_completed":
        calls[-1]["status"] = "completed_partial"
    elif mutation == "no_progress_completed":
        calls[-1]["status"] = "completed_no_progress"
    owner = authored_call_artifact(tmp_path, calls, noncompleted=noncompleted, expected=expected)
    with pytest.raises(ValueError):
        trade._validate_calls(owner)


def test_tampered_call_artifact_is_rejected(tmp_path):
    owner = authored_call_artifact(tmp_path, [completed_call()])
    (tmp_path / "calls.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(ValueError):
        trade._validate_calls(owner)


@pytest.mark.parametrize("replacement", ["regular", "fifo"])
def test_call_artifact_replacement_after_manifest_check_is_rejected_without_blocking(
    monkeypatch,
    tmp_path,
    replacement,
):
    owner = authored_call_artifact(tmp_path, [dict(completed_call(), evidence="A")])
    path = tmp_path / "calls.jsonl"
    original_validate = ordinary.validate_call_artifact
    original_open = Path.open

    def replace_after_validation(record):
        original_validate(record)
        if replacement == "regular":
            path.write_bytes(path.read_bytes().replace(b'"A"', b'"B"'))
        else:
            path.unlink()
            os.mkfifo(path)

    def reject_blocking_fifo_open(self, *args, **kwargs):
        if replacement == "fifo" and self == path:
            raise AssertionError("FIFO must be opened nonblocking and rejected before reading")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(ordinary, "validate_call_artifact", replace_after_validation)
    monkeypatch.setattr(Path, "open", reject_blocking_fifo_open)
    with pytest.raises(ValueError):
        trade._validate_calls(owner)


class _AuthoredGoalDriver:
    """A local checkpoint after one frame must leave the owner CPU running."""

    def __init__(self, session):
        self.session = session
        self.observations = []
        self.offsets = []

    def before_step(self, *, frame_offset):
        self.offsets.append(frame_offset)
        return None if self.objective_complete() else ("a", 1)

    def after_step(self, *, call):
        self.observations.append((call["requested_frames"], call["actual_completed_frames"]))

    def objective_complete(self):
        return bool(self.observations)

    def snapshot(self):
        return {
            "authored": True,
            "observations": list(self.observations),
            "objective_complete": self.objective_complete(),
        }


def create_authored_goal_driver(*, session, side, options, peer_state):
    assert side == "listen"
    assert options == {"version": "blue_color", "outgoing_slot": 0, "checkpoint": "select-mon"}
    driver_type = (
        _AuthoredClosingDriver
        if getattr(session, "authored_close_driver", False)
        else _AuthoredGoalDriver
    )
    driver = driver_type(session)
    fault = getattr(session, "authored_driver_fault", None)
    if fault:
        original = getattr(driver, fault)

        def fail_after_executed_frame(*args, **kwargs):
            if session.steps:
                raise RuntimeError(f"authored {fault} failure")
            return original(*args, **kwargs)

        setattr(driver, fault, fail_after_executed_frame)
    session.authored_driver = driver
    return driver


class _AuthoredClosingDriver(_AuthoredGoalDriver):
    def close(self):
        self.session.record("driver_close")
        assert self.session.endpoint is None
        if self.session.authored_close_failure:
            raise RuntimeError("authored driver close failure")


def run_authored_goal_owner(
    *,
    exception=None,
    goal_stop_requested=True,
    evidence_path=None,
    close_driver=False,
    close_failure=False,
    milestones=False,
    driver_fault=None,
):
    # Reuse existing authored session/endpoint models without changing old tests.
    from tests.test_probe_timed_rom_pair import Harness, _MenuSession, arguments

    args = arguments(
        "--input-profile=menu", "--listener-chunk=1", "--connector-chunk=1", "--frame-limit=4"
    )
    harness = Harness()
    session = _MenuSession(harness, "blue", "complete")
    session.authored_close_driver = close_driver
    session.authored_close_failure = close_failure
    session.authored_driver_fault = driver_fault
    args.rom_milestones = milestones
    cancelled, goal = threading.Event(), threading.Event()
    goal_stop = threading.Event()
    done_at = []
    if evidence_path is not None:
        args.call_retention = "stream"
    if exception is not None:
        whole_step = session.step

        def interrupted_step(count, *, render):
            if session.steps:
                session.steps.append(count)
                if goal_stop_requested:
                    goal_stop.set()
                cancelled.set()
                raise exception("authored cancellation boundary")
            whole_step(count, render=render)

        session.step = interrupted_step

    class Done:
        def set(self):
            done_at.append(len(session.steps))
            cancelled.set()

    records = [{"side": "listener", "calls": [], "cleanup": [], "errors": []}, {}]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as connector:
            accepted, _ = listener.accept()
            try:
                ordinary._run_owner(
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
                    evidence_path=evidence_path,
                    owner_driver="tests.test_probe_timed_trade_pair:create_authored_goal_driver",
                    peer_state=object(),
                    goal_flag=goal,
                    goal_stop=goal_stop,
                    driver_options={
                        "version": "blue_color",
                        "outgoing_slot": 0,
                        "checkpoint": "select-mon",
                    },
                )
            finally:
                accepted.close()
    return session, records[0], goal, done_at


def test_first_local_goal_keeps_whole_cpu_steps_until_bound_without_early_done():
    session, record, goal, done_at = run_authored_goal_owner()
    assert record["errors"] == []
    assert session.steps == [1, 1, 1, 1]
    assert session.authored_driver.offsets == [0, 1, 2, 3]
    assert session.authored_driver.observations == [(1, 1)] * 4
    assert len(session.inputs) == 1
    assert goal.is_set()
    assert done_at and all(count == 4 for count in done_at)
    assert record["termination"] == "frame_bound"
    assert record["driver_snapshot"]["objective_complete"] is True


@pytest.mark.parametrize("method", ["after_step", "snapshot", "objective_complete"])
def test_driver_poststep_failure_preserves_executed_call_artifact(tmp_path, method):
    session, record, _, _ = run_authored_goal_owner(
        driver_fault=method,
        evidence_path=tmp_path / "executed.jsonl",
    )
    assert session.steps == [1]
    assert any(f"authored {method} failure" in error for error in record["errors"])
    assert record["termination"] == "owner_failure"
    assert record["call_counts"]["total"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 1
    calls = [json.loads(line) for line in (tmp_path / "executed.jsonl").read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0]["requested_frames"] == calls[0]["actual_completed_frames"] == 1
    assert record["final"]["frame_count"] == 11
    assert "session_closed_without_save" in record["cleanup"]
    ordinary.validate_call_artifact(record)


def test_driver_failure_and_call_finalization_failure_both_remain_visible(monkeypatch, tmp_path):
    def fail_append(self, call):
        raise OSError("authored finalization failure")

    monkeypatch.setattr(ordinary.CallEvidenceLog, "append", fail_append)
    session, record, _, _ = run_authored_goal_owner(
        driver_fault="after_step",
        evidence_path=tmp_path / "executed.jsonl",
    )
    assert session.steps == [1]
    errors = " ".join(record["errors"])
    assert "authored after_step failure" in errors
    assert "authored finalization failure" in errors
    assert record["call_counts"]["total"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 1
    assert record["unspooled_call"]["actual_completed_frames"] == 1
    assert record["call_log"]["complete"] is False


@pytest.mark.parametrize("close_failure", [False, True])
def test_driver_hooks_close_after_endpoint_detach_before_session_close(close_failure):
    session, record, _, _ = run_authored_goal_owner(
        close_driver=True,
        close_failure=close_failure,
        milestones=True,
    )
    assert (
        session.calls.index("unbind")
        < session.calls.index("driver_close")
        < session.calls.index("close")
    )
    assert session.calls.count("driver_close") == 1
    assert record["milestone_observer"] == "owner_driver"
    assert "milestone_hooks_removed" not in record["cleanup"]
    assert "session_closed_without_save" in record["cleanup"]
    if close_failure:
        assert any("authored driver close failure" in error for error in record["errors"])
        assert "owner_driver_closed" not in record["cleanup"]
    else:
        assert record["errors"] == []
        assert (
            record["cleanup"].index("endpoint_detached")
            < record["cleanup"].index("owner_driver_closed")
            < record["cleanup"].index("session_closed_without_save")
        )


@pytest.mark.parametrize("exception_name", ["Cancelled", "DeadlineExceeded", "ChannelClosed"])
@pytest.mark.parametrize("goal_stop_requested", [False, True])
def test_owner_tolerates_only_exact_cancelled_after_joint_goal(
    tmp_path,
    exception_name,
    goal_stop_requested,
):
    from pokered_harness.link import timed_wire

    session, record, goal, done_at = run_authored_goal_owner(
        exception=getattr(timed_wire, exception_name),
        goal_stop_requested=goal_stop_requested,
        evidence_path=tmp_path / "owner-calls.jsonl",
    )
    expected = exception_name == "Cancelled" and goal_stop_requested
    assert session.steps == [1, 1]
    assert goal.is_set()
    assert record["call_counts"]["noncompleted"] == 1
    assert record["call_counts"]["actual_completed_frames"] == 1
    terminal = [
        json.loads(line) for line in (tmp_path / "owner-calls.jsonl").read_text().splitlines()
    ][-1]
    assert terminal["status"] == "interrupted"
    assert terminal["actual_completed_frames"] == 0
    assert terminal["error"].startswith(exception_name + ":")
    assert bool(terminal.get("expected_goal_cancellation")) is expected
    assert bool(record.get("expected_goal_cancellation")) is expected
    if expected:
        assert record["errors"] == []
        assert record["termination"] == "goal_cancelled"
        assert done_at == []
        trade._validate_calls(record)
    else:
        assert record["errors"]
        assert record["termination"] == "owner_failure"
        assert done_at == [2]


def test_readiness_is_monotonic_per_owner_and_snapshots_are_detached():
    import multiprocessing

    raw = multiprocessing.get_context("spawn").RawArray("B", 16)
    first, second = ordinary._PeerReadiness(raw, 0), ordinary._PeerReadiness(raw, 1)
    before = second.snapshot()
    for phase in ordinary.READINESS_PHASES:
        assert not second.peer_ready(phase)
        first.publish_ready(phase)
        first.publish_ready(phase)
        assert second.peer_ready(phase)
        assert not first.peer_ready(phase)
    assert list(raw) == [1] * 8 + [0] * 8
    assert not any(before["peer"].values())
    detached = second.snapshot()
    detached["peer"][ordinary.READINESS_PHASES[0]] = False
    assert second.peer_ready(ordinary.READINESS_PHASES[0])


def authored_spawn_owner(
    options,
    index,
    sock,
    cancel,
    done,
    barrier,
    deadline,
    overall,
    sender,
    stderr_path,
    *driver_args,
):
    """Spawned control-only peer: no Session, PyBoy construction, or ROM inputs."""
    record = {
        "side": ("listener", "connector")[index],
        "calls": [],
        "cleanup": [],
        "errors": [],
        "final": {"authored": True},
        "continuations": 0,
    }
    try:
        barrier.wait(timeout=max(0.01, deadline - time.monotonic()))
        if driver_args:
            path, readiness, goal, goal_stop, driver_options = driver_args
            assert ordinary._driver_factory(path) is create_authored_goal_driver
            assert driver_options == options["owner_driver_options"][index]
            if index == 0:
                goal.set()
                readiness.publish_ready("party_qualified")
                while not cancel.wait(0.001) and time.monotonic() < deadline:
                    record["continuations"] += 1
            else:
                while not readiness.peer_ready("party_qualified"):
                    assert not cancel.wait(0.001)
                    assert time.monotonic() < deadline
                # First local goal cannot stop the pair during this delay.
                assert not cancel.wait(0.05)
                goal.set()
                assert cancel.wait(max(0, deadline - time.monotonic()))
            assert goal_stop.is_set()
            record["termination"] = "goal_cancelled"
            record["local_goal"] = goal.is_set()
        else:
            if index == 0:
                done.set()
            assert cancel.wait(max(0, deadline - time.monotonic()))
            record["termination"] = "frame_bound" if index == 0 else "cancelled_or_deadline"
    except (AssertionError, RuntimeError, ValueError, TimeoutError) as exc:
        record["errors"].append(f"{type(exc).__name__}: {exc}")
        done.set()
    finally:
        sender.send_bytes(json.dumps(record).encode())
        sender.close()
        sock.close()


@pytest.mark.parametrize("with_driver", [False, True])
def test_spawn_supervisor_joint_goal_and_baseline_first_done_contract(tmp_path, with_driver):
    from tests.test_probe_timed_rom_pair import _NoSharedEventContext, arguments

    args = arguments(
        "--owner-mode=process",
        "--listener-chunk=1",
        "--connector-chunk=1",
        "--overall-timeout=12",
        "--pair-timeout=8",
        "--cleanup-timeout=3",
    )
    args.owner_driver_options = [
        {"version": "blue_color", "outgoing_slot": 0, "checkpoint": "select-mon"},
        {"version": "yellow", "outgoing_slot": 5, "checkpoint": "select-mon"},
    ]
    context = _NoSharedEventContext()
    try:
        result = ordinary.run_process_pair(
            args,
            context=context,
            child_target=authored_spawn_owner,
            owner_driver="tests.test_probe_timed_trade_pair:create_authored_goal_driver"
            if with_driver
            else None,
        )
    finally:
        context.close()
    assert result["supervisor_cancel_errors"] == []
    assert result["processes_alive"] == result["report_readers_alive"] == []
    assert all(owner["errors"] == [] for owner in result["owners"])
    assert all(
        owner["exitcode"] == 0 and not owner["forced_termination"] for owner in result["owners"]
    )
    if with_driver:
        assert result["stop_reason"] == "both_owner_goals"
        assert result["owner_goals"] == [True, True]
        assert result["goal_stop"] is True
        assert result["owners"][0]["continuations"] > 0
    else:
        assert result["stop_reason"] == "owner_completion_or_failure"
        assert "owner_driver" not in result
