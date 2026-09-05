"""Bounded timed trade diagnostic using the existing process supervisor.

Callers must choose finite frame, pair, and overall budgets. These are limits,
not throughput or completion guarantees: trade animations can take minutes.
SelectMon is an optional partial checkpoint, never reciprocal trade completion.
Use the reviewed runtime and keep reports and copied party evidence outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import probe_timed_rom_pair as probe

# Retain failures as unsuccessful evidence while preserving supervisor cleanup.
# ruff: noqa: BLE001

DRIVER = "scripts._timed_trade_probe:create_owner_driver"


def party_slot(value):
    slot = int(value)
    if not 1 <= slot <= 6:
        raise argparse.ArgumentTypeError("must be a 1-based party slot in 1..6")
    return slot


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listener", choices=probe.VERSIONS, default="blue_color")
    parser.add_argument("--connector", choices=probe.VERSIONS, default="yellow")
    parser.add_argument(
        "--both-orientations",
        action="store_true",
        help="reverse roles with each slot following its selected ROM version",
    )
    parser.add_argument(
        "--listener-slot",
        type=party_slot,
        required=True,
        help="1-based outgoing slot for the first orientation's listener ROM",
    )
    parser.add_argument(
        "--connector-slot",
        type=party_slot,
        required=True,
        help="1-based outgoing slot for the first orientation's connector ROM",
    )
    goal = parser.add_mutually_exclusive_group()
    goal.add_argument("--checkpoint", choices=("reciprocal-exchange", "select-mon"))
    goal.add_argument("--goal", choices=("trade", "select-mon"))
    parser.add_argument("--frame-limit", type=probe.positive_int, required=True)
    parser.add_argument("--overall-timeout", type=probe.positive_float, required=True)
    parser.add_argument("--pair-timeout", type=probe.positive_float, required=True)
    parser.add_argument("--cleanup-timeout", type=probe.positive_float, default=3)
    parser.add_argument("--operation-timeout", type=probe.positive_float, required=True)
    parser.add_argument("--rearm-budget", type=probe.positive_int, required=True)
    parser.add_argument("--rearm-instruction-cap", type=probe.positive_int, required=True)
    parser.add_argument("--max-edge-lateness", type=probe.positive_int, required=True)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.checkpoint = args.checkpoint or (
        "select-mon" if args.goal == "select-mon" else "reciprocal-exchange"
    )
    args.listener_slot_index = args.listener_slot - 1
    args.connector_slot_index = args.connector_slot - 1
    args.owner_mode = "process"
    args.listener_chunk = args.connector_chunk = 1
    args.input_profile = "menu"
    args.call_retention = "stream"
    args.rom_milestones = True
    if args.operation_timeout != 5:
        parser.error("this diagnostic requires explicit --operation-timeout 5")
    if args.cleanup_timeout >= args.overall_timeout:
        parser.error("--overall-timeout must exceed --cleanup-timeout")
    args.output = args.output.resolve()
    if any(args.output.is_relative_to(root.resolve()) for root in (ROOT, args.repo_root)):
        parser.error("--output must be outside the source and asset checkouts (for example /tmp)")
    probe.validate_input_profile(args)
    return args


def adjudicate_pair(owners, checkpoint):
    """Lazy import keeps CLI validation independent of ROM/runtime imports."""
    from scripts._timed_trade_probe import adjudicate_pair as adjudicate

    return adjudicate(owners, checkpoint)


def _evidence_errors(pair):
    """Validate retained evidence; lifecycle execution belongs to the supervisor."""
    errors = []
    if pair.get("stop_reason") != "both_owner_goals":
        errors.append("supervisor did not observe both owner goals")
    if pair.get("owner_goals") != [True, True] or pair.get("goal_stop") is not True:
        errors.append("supervisor goal flag evidence is incomplete")
    for key in (
        "threads_alive",
        "processes_alive",
        "report_readers_alive",
        "supervisor_cancel_errors",
    ):
        if pair.get(key):
            errors.append(f"supervisor {key}: {pair[key]}")
    owners = pair.get("owners", [])
    if len(owners) != 2:
        errors.append("exactly two owner reports are required")
    for owner in owners:
        side = owner.get("side", "unknown")
        if owner.get("errors"):
            errors.append(f"{side} owner errors: {owner['errors']}")
        for key in ("alive", "forced_termination", "actual_missing", "watcher_alive"):
            if owner.get(key):
                errors.append(f"{side} {key}")
        if owner.get("exitcode", 0) != 0:
            errors.append(f"{side} unsuccessful process exit")
        if owner.get("termination") != "goal_cancelled":
            errors.append(f"{side} did not finish with goal cancellation")
        if owner.get("local_goal") is not True:
            errors.append(f"{side} local goal evidence is missing")
        required_cleanup = {
            "endpoint_detached",
            "owner_driver_closed",
            "session_closed_without_save",
        }
        if not required_cleanup.issubset(owner.get("cleanup", [])):
            errors.append(f"{side} cleanup evidence is incomplete")
        if owner.get("milestone_observer") != "owner_driver":
            errors.append(f"{side} milestone observation was not delegated to the owner driver")
        for channel in ("stdout", "stderr"):
            if owner.get(channel, {}).get("error") or owner.get(channel, {}).get("drainer_alive"):
                errors.append(f"{side} {channel} evidence failure")
        if owner.get("milestones", {}).get("error"):
            errors.append(f"{side} milestone evidence failure")
        try:
            _validate_calls(owner)
        except Exception as exc:
            errors.append(f"{side} call artifact: {type(exc).__name__}: {exc}")
    return errors


def _validate_calls(owner):
    """Only a marked final cancellation may interrupt a whole-frame call."""
    if "call_log" not in owner:
        raise ValueError("streamed call artifact manifest is missing")
    probe.validate_call_artifact(owner)
    noncompleted = owner.get("call_counts", {}).get("noncompleted")
    if type(noncompleted) is not int or noncompleted not in (0, 1):
        raise ValueError("unexpected noncompleted calls")
    if noncompleted and not owner.get("expected_goal_cancellation"):
        raise ValueError("unmarked owner goal cancellation")
    # Reopening a pathname can select a replacement artifact. Validate this
    # descriptor and hash precisely the bounded bytes used for parsing as well.
    manifest = owner["call_log"]
    seen = interrupted = size = 0
    digest = hashlib.sha256()
    fd = os.open(manifest["path"], os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > probe.CALL_LOG_BYTE_LIMIT
            or metadata.st_size != manifest["bytes"]
        ):
            raise ValueError(
                "parsed call artifact must be a bounded regular file matching manifest"
            )
        while line := stream.readline(
            min(probe.MAX_REPORT_BYTES + 1, probe.CALL_LOG_BYTE_LIMIT + 1 - size)
        ):
            size += len(line)
            if len(line) > probe.MAX_REPORT_BYTES or size > probe.CALL_LOG_BYTE_LIMIT:
                raise ValueError("call evidence exceeds bounds")
            digest.update(line)
            if not line.endswith(b"\n"):
                raise ValueError("call artifact record is missing its terminal newline")
            call = json.loads(line)
            if type(call.get("requested_frames")) is not int or call["requested_frames"] != 1:
                raise ValueError("call did not request whole step(1)")
            actual = call.get("actual_completed_frames")
            if type(actual) is not int or actual not in (0, 1):
                raise ValueError("call has missing or invalid actual frame evidence")
            if interrupted:
                raise ValueError("interrupted call is not the terminal suffix")
            if call.get("status") != "completed":
                if (
                    call.get("status") != "interrupted"
                    or call.get("expected_goal_cancellation") is not True
                    or not str(call.get("error", "")).startswith("Cancelled: ")
                    or call.get("observation_error")
                ):
                    raise ValueError("unmarked interrupted call")
                interrupted += 1
            elif actual != 1 or call.get("error") or call.get("observation_error"):
                raise ValueError("completed call lacks a clean whole frame")
            seen += 1
    if (
        size != manifest["bytes"]
        or digest.hexdigest() != manifest["sha256"]
        or seen != manifest["record_count"]
    ):
        raise ValueError("parsed call artifact bytes/hash/record count mismatch")
    if interrupted != noncompleted or seen != owner["call_counts"]["total"]:
        raise ValueError("expected cancellation count mismatch")


def run_probe(args):
    """Dispatch fixed owner drivers and adjudicate reciprocal evidence in parent."""
    probe.validate_input_profile(args)
    if (
        args.owner_mode != "process"
        or args.listener_chunk != 1
        or args.connector_chunk != 1
        or args.call_retention != "stream"
        or not args.rom_milestones
        or args.operation_timeout != 5
    ):
        raise ValueError("trade requires process owners, whole step(1), streamed milestones, op5")
    started = time.monotonic()
    args.absolute_deadline = started + args.overall_timeout
    report = {
        "label": "timed_trade_diagnostic",
        "checkpoint": args.checkpoint,
        "complete": False,
        "checkpoint_only": False,
        "status": "incomplete",
        "pairs": [],
        "errors": [],
    }
    orientations = [args]
    if args.both_orientations:
        reverse = argparse.Namespace(**vars(args))
        for field in ("", "_slot", "_slot_index"):
            setattr(reverse, "listener" + field, getattr(args, "connector" + field))
            setattr(reverse, "connector" + field, getattr(args, "listener" + field))
        orientations.append(reverse)
    for options in orientations:
        if time.monotonic() >= args.absolute_deadline:
            report["errors"].append("overall budget exhausted before orientation")
            break
        options.owner_driver_options = [
            {
                "version": options.listener,
                "outgoing_slot": options.listener_slot_index,
                "checkpoint": options.checkpoint,
            },
            {
                "version": options.connector,
                "outgoing_slot": options.connector_slot_index,
                "checkpoint": options.checkpoint,
            },
        ]
        try:
            pair = probe.run_process_pair(options, owner_driver=DRIVER)
        except Exception as exc:
            report["errors"].append(f"supervisor: {type(exc).__name__}: {exc}")
            break
        report["pairs"].append(pair)
        pair["trade_options"] = {
            "listener": options.listener,
            "connector": options.connector,
            "listener_slot": options.listener_slot,
            "connector_slot": options.connector_slot,
            "owner_driver_options": options.owner_driver_options,
        }
        errors = _evidence_errors(pair)
        try:
            snapshots = [owner["driver_snapshot"] for owner in pair.get("owners", [])]
            if len(snapshots) != 2 or not all(isinstance(value, dict) for value in snapshots):
                raise ValueError("exactly two driver snapshots are required")
            objective = adjudicate_pair(snapshots, options.checkpoint)
            if not isinstance(objective, dict):
                raise TypeError("invalid pair adjudication")
        except Exception as exc:
            objective = {
                "complete": False,
                "checkpoint_only": False,
                "status": "unsupported",
                "errors": [f"adjudication: {type(exc).__name__}: {exc}"],
            }
        # A checkpoint can never be promoted by even a malformed helper result.
        if options.checkpoint == "select-mon":
            objective["complete"] = False
        pair["objective"] = objective
        errors.extend(objective.get("errors", []))
        report["errors"].extend(errors)
        if any(
            pair.get(key) for key in ("threads_alive", "processes_alive", "report_readers_alive")
        ):
            break
    report["options"] = probe._jsonable(vars(args))
    objectives = [pair["objective"] for pair in report["pairs"]]
    healthy = not report["errors"] and len(objectives) == len(orientations)
    report["complete"] = bool(
        healthy
        and args.checkpoint == "reciprocal-exchange"
        and all(item.get("complete") is True for item in objectives)
    )
    report["checkpoint_only"] = bool(
        healthy
        and args.checkpoint == "select-mon"
        and all(item.get("checkpoint_only") is True for item in objectives)
    )
    report["status"] = (
        "complete"
        if report["complete"]
        else "partial"
        if report["checkpoint_only"]
        else "incomplete"
    )
    report["elapsed_s"] = time.monotonic() - started
    return report


def main(argv=None):
    args = parse_args(argv)
    # Reserve exclusively before starting processes; never overwrite evidence.
    with args.output.open("x", encoding="utf-8") as stream:
        try:
            report = run_probe(args)
        except Exception as exc:
            report = {
                "label": "timed_trade_diagnostic",
                "complete": False,
                "checkpoint_only": False,
                "status": "incomplete",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
        json.dump(report, stream, indent=2, default=str)
        stream.write("\n")
    print(f"Timed trade diagnostic ({report['status']}): {args.output}")
    return 0 if report.get("complete") or report.get("checkpoint_only") else 2


if __name__ == "__main__":
    raise SystemExit(main())
