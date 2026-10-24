"""Bounded battle gameplay diagnostic from canonical ordinary Cable Club states.

Gameplay completion does not certify untouched source-state provenance. The
battle helper adjudicates settled-turn effects, HP/status reciprocity and, for
room-return, natural gameplay cleanup. Process cleanup is checked separately.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import probe_timed_rom_pair as probe
from scripts import probe_timed_trade_pair as trade

# Reuse only the transport/stream-ledger validator, never trade gameplay phases.
# ruff: noqa: BLE001
DRIVER = "scripts._timed_battle_probe:create_owner_driver"
CHECKPOINTS = ("settled-turn", "room-return")


def _move(value):
    slot = int(value)
    if not 1 <= slot <= 4:
        raise argparse.ArgumentTypeError("must be a 1-based move slot in 1..4")
    return slot


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listener", choices=probe.VERSIONS, default="blue_color")
    parser.add_argument("--connector", choices=probe.VERSIONS, default="yellow")
    parser.add_argument("--both-orientations", action="store_true")
    for side in ("listener", "connector"):
        parser.add_argument(f"--{side}-move", type=_move, help="move 1..4; omit for auto")
    parser.add_argument("--checkpoint", choices=CHECKPOINTS, default="room-return")
    for name in ("frame-limit", "rearm-budget", "rearm-instruction-cap", "max-edge-lateness"):
        parser.add_argument(f"--{name}", type=probe.positive_int, required=True)
    for name in ("overall-timeout", "pair-timeout", "operation-timeout"):
        parser.add_argument(f"--{name}", type=probe.positive_float, required=True)
    parser.add_argument("--cleanup-timeout", type=probe.positive_float, default=3)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    for side in ("listener", "connector"):
        move = getattr(args, f"{side}_move")
        setattr(args, f"{side}_move_index", None if move is None else move - 1)
    args.owner_mode = "process"
    args.listener_chunk = args.connector_chunk = 1
    args.input_profile = "menu"
    args.call_retention = "stream"
    args.rom_milestones = True
    args.output = args.output.resolve()
    try:
        _validate_options(args)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _validate_options(args):
    probe.validate_input_profile(args)
    for name in ("overall_timeout", "pair_timeout", "operation_timeout", "cleanup_timeout"):
        value = getattr(args, name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    for name in ("frame_limit", "rearm_budget", "rearm_instruction_cap", "max_edge_lateness"):
        value = getattr(args, name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if args.operation_timeout != 5:
        raise ValueError("this diagnostic requires explicit --operation-timeout 5")
    if args.cleanup_timeout >= args.overall_timeout:
        raise ValueError("--overall-timeout must exceed --cleanup-timeout")
    if args.checkpoint not in CHECKPOINTS:
        raise ValueError("unsupported battle checkpoint")
    for side in ("listener", "connector"):
        if getattr(args, side) not in probe.VERSIONS:
            raise ValueError("only canonical ordinary fixture versions are supported")
        slot = getattr(args, f"{side}_move_index")
        if slot is not None and (type(slot) is not int or not 0 <= slot <= 3):
            raise ValueError("move_slot must be None or 0..3")
    if (
        args.owner_mode != "process"
        or args.listener_chunk != 1
        or args.connector_chunk != 1
        or args.call_retention != "stream"
        or args.rom_milestones is not True
        or args.input_profile != "menu"
    ):
        raise ValueError("battle requires process owners, whole step(1), streamed milestones")
    output = Path(args.output).resolve()
    if any(output.is_relative_to(root.resolve()) for root in (ROOT, Path(args.repo_root))):
        raise ValueError("--output must be outside the source and asset checkouts")
    if any((parent / ".git").exists() for parent in output.parents):
        raise ValueError("--output must be outside Git worktrees")


def adjudicate_pair(owners, checkpoint):
    """Delegate gameplay proof to the battle helper, with no runtime imports here."""
    from scripts._timed_battle_probe import adjudicate_pair as adjudicate

    return adjudicate(owners, checkpoint)


def _driver_phases():
    parameter = inspect.signature(probe.run_process_pair).parameters.get("owner_driver_phases")
    if parameter is None or parameter.kind not in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise ValueError("unsupported supervisor: explicit owner_driver_phases parameter required")
    from scripts._timed_battle_probe import READINESS_PHASES

    if (
        not isinstance(READINESS_PHASES, tuple)
        or not 1 <= len(READINESS_PHASES) <= 32
        or any(
            not isinstance(phase, str) or not phase.strip() or len(phase) > 64
            for phase in READINESS_PHASES
        )
        or len(set(READINESS_PHASES)) != len(READINESS_PHASES)
    ):
        raise ValueError("battle READINESS_PHASES must be 1..32 unique strings of length 1..64")
    return READINESS_PHASES


def run_probe(args):
    """Run ordinary-fixture owners; require gameplay and supervisor evidence."""
    _validate_options(args)
    phases = _driver_phases()  # Fail before launching either owner.
    started = time.monotonic()
    args.absolute_deadline = started + args.overall_timeout
    args.owner_driver_phases = phases
    report = {
        "label": "timed_battle_diagnostic",
        "checkpoint": args.checkpoint,
        "gameplay_completed": False,
        "full_authentic_acceptance": False,
        "source_provenance": "unknown_original_playthrough",
        "complete": False,
        "status": "incomplete",
        "pairs": [],
        "errors": [],
    }
    orientations = [argparse.Namespace(**vars(args))]
    if args.both_orientations:
        reverse = argparse.Namespace(**vars(args))
        for suffix in ("", "_move", "_move_index"):
            setattr(reverse, "listener" + suffix, getattr(args, "connector" + suffix))
            setattr(reverse, "connector" + suffix, getattr(args, "listener" + suffix))
        orientations.append(reverse)
    for options in orientations:
        if time.monotonic() >= args.absolute_deadline - args.cleanup_timeout:
            report["errors"].append("overall budget exhausted before orientation")
            break
        options.owner_driver_options = [
            {
                "version": getattr(options, side),
                "move_slot": getattr(options, side + "_move_index"),
                "checkpoint": options.checkpoint,
            }
            for side in ("listener", "connector")
        ]
        try:
            pair = probe.run_process_pair(
                options,
                owner_driver=DRIVER,
                owner_driver_phases=phases,
            )
            if not isinstance(pair, dict):
                raise TypeError("supervisor report must be an object")
        except Exception as exc:
            report["errors"].append(f"supervisor: {type(exc).__name__}: {exc}")
            break
        report["pairs"].append(pair)
        pair["battle_options"] = options.owner_driver_options
        errors = []
        try:
            if pair.get("owner_driver") != DRIVER:
                raise ValueError("supervisor owner_driver identity mismatch")
            recorded_phases = pair.get("owner_driver_phases")
            if not isinstance(recorded_phases, (list, tuple)) or tuple(recorded_phases) != phases:
                raise ValueError("supervisor owner_driver_phases missing or mismatch")
            errors.extend(trade._evidence_errors(pair))
            owners = pair.get("owners", [])
            if [owner.get("side") for owner in owners] != ["listener", "connector"]:
                raise ValueError("ordered listener and connector owner evidence required")
            snapshots = [owner["driver_snapshot"] for owner in owners]
            if not all(isinstance(snapshot, dict) for snapshot in snapshots):
                raise ValueError("two battle driver snapshots required")
            for snapshot, expected, side in zip(
                snapshots,
                options.owner_driver_options,
                ("listen", "connect"),
                strict=True,
            ):
                if snapshot.get("role") != side:
                    raise ValueError(f"{side} driver snapshot role mismatch")
                if "side" in snapshot and snapshot["side"] != snapshot["role"]:
                    raise ValueError(f"{side} driver snapshot contradictory role/side")
                if snapshot.get("version") != expected["version"]:
                    raise ValueError(f"{side} driver snapshot version mismatch")
                if snapshot.get("checkpoint") != expected["checkpoint"]:
                    raise ValueError(f"{side} driver snapshot checkpoint mismatch")
                requested_slot = expected["move_slot"]
                if requested_slot is not None:
                    chosen = snapshot.get("chosen")
                    slots = []
                    if "move_slot" in snapshot:
                        slots.append(snapshot["move_slot"])
                    if isinstance(chosen, dict) and "slot" in chosen:
                        slots.append(chosen["slot"])
                    if not slots or any(
                        type(slot) is not int or slot != requested_slot for slot in slots
                    ):
                        raise ValueError(f"{side} driver snapshot move_slot mismatch")
            objective = adjudicate_pair(snapshots, options.checkpoint)
            if not isinstance(objective, dict) or not isinstance(objective.get("errors"), list):
                raise TypeError("battle adjudication requires an object with an errors list")
            if type(objective.get("gameplay_completed")) is not bool:
                raise ValueError("battle adjudication requires explicit gameplay_completed bool")
            errors.extend(objective["errors"])
            if objective["gameplay_completed"] is not True:
                errors.append("battle helper did not prove requested gameplay checkpoint")
        except Exception as exc:
            errors.append(f"evidence: {type(exc).__name__}: {exc}")
            objective = {"gameplay_completed": False, "errors": errors.copy()}
        objective = dict(objective, full_authentic_acceptance=False)
        pair["objective"] = objective
        pair["gameplay_completed"] = not errors
        report["errors"].extend(errors)
        if any(
            pair.get(key) for key in ("threads_alive", "processes_alive", "report_readers_alive")
        ):
            break
    report["gameplay_completed"] = bool(
        not report["errors"]
        and len(report["pairs"]) == len(orientations)
        and all(pair["gameplay_completed"] for pair in report["pairs"])
    )
    # 'complete' is reserved for full acceptance, never source-unknown gameplay.
    report["status"] = "gameplay_completed" if report["gameplay_completed"] else "incomplete"
    report["options"] = probe._jsonable(vars(args))
    report["elapsed_s"] = time.monotonic() - started
    return report


def main(argv=None):
    args = parse_args(argv)
    with args.output.open("x", encoding="utf-8") as stream:
        try:
            report = run_probe(args)
        except Exception as exc:
            report = {
                "label": "timed_battle_diagnostic",
                "checkpoint": args.checkpoint,
                "gameplay_completed": False,
                "full_authentic_acceptance": False,
                "source_provenance": "unknown_original_playthrough",
                "complete": False,
                "status": "incomplete",
                "pairs": [],
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
        json.dump(report, stream, indent=2, default=str)
        stream.write("\n")
    print(f"Timed battle diagnostic ({report['status']}; provenance unverified): {args.output}")
    return 0 if report.get("gameplay_completed") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
