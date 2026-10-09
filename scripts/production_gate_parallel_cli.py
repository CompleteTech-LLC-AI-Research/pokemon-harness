"""Command-line entrypoint of the opt-in concurrent runtime gate (#107).

Split out of ``production_gate_parallel`` to keep every source file below the line cap.  It
freezes the plan from independently measured identities, builds the private immutable snapshot,
runs the orchestrator under the single live capacity authority (the session's background
monitoring, SIGTERM/SIGHUP converted to the bounded-cancel interrupt) and publishes one report.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel import (
    CLEANUP_SECONDS,
    INPUT_FILES,
    Measurer,
    default_worker_argv,
    run_parallel_plan,
)
from scripts.production_gate_parallel_capacity import (
    LiveCapacity,
    PairBudget,
    capacity_loss_probe,
)
from scripts.production_gate_parallel_cgroup import kernel_ownership
from scripts.production_gate_parallel_comparison import compare_bundles
from scripts.production_gate_parallel_identity import measure_commit, measure_identity
from scripts.production_gate_parallel_isolation import (
    IsolationError,
    build_snapshot,
    thaw_and_remove,
)
from scripts.production_gate_parallel_plan import PlanError, build_parallel_plan, cli_deadlines
from scripts.production_gate_parallel_processes import interrupt_on_signals
from scripts.production_gate_parallel_report import file_sha256, render_report_text, write_report
from scripts.production_gate_parallel_runtime_inputs import build_private_runtimes, runtime_digest
from scripts.production_gate_parallel_validation import loads_strict


def freeze_selection(
    *,
    project_root: Path,
    snapshot: Mapping[str, Any],
    interpreters: Mapping[str, str],
    expected_sha1: Mapping[Path, str] | None,
    rom_root: Path,
    fixture_root: Path,
    tiers: Sequence[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Collect the COMPLETE selection per tier in each runtime, from the snapshot, before dispatch.

    This reuses the gate's own collection preflight and planned-row classifier, so the frozen
    selection cannot drift from what the tiers will dispatch.  The two runtimes must agree.
    """

    from scripts.production_gate_capacity import planned_tier_nodeids
    from scripts.production_gate_execution import run_collection_preflight
    from scripts.production_gate_runtime import build_test_environment

    snap_root = Path(snapshot["checkout"])
    snap_rom, snap_fixture = Path(snapshot["rom_root"]), Path(snapshot["fixture_root"])
    mapped: dict[Path, str] = {}
    for original, digest in (expected_sha1 or {}).items():
        for source_root, copy_root in ((rom_root, snap_rom), (fixture_root, snap_fixture)):
            try:
                mapped[copy_root / Path(original).relative_to(source_root)] = digest
                break
            except ValueError:
                continue
    problems: list[str] = []
    per_mode: dict[str, dict[str, list[str]]] = {}
    for mode, interpreter in interpreters.items():
        environment = build_test_environment(
            snap_root, snap_rom, snap_fixture, mapped, runtime_mode=mode
        )
        environment["POKERED_IMPORT_ORIGIN_EQUIVALENT_ROOTS"] = str(project_root)
        collections = run_collection_preflight(
            project_root=snap_root, python_executable=Path(interpreter), environment=environment
        )
        failed = [item for item in collections if item.status != "PASS"]
        if failed:
            problems.append(f"{mode}: collection preflight failed: {failed[0].reason[:200]}")
            continue
        selection: dict[str, list[str]] = {}
        for tier in tiers:
            planned, problem = planned_tier_nodeids(collections, tier, snap_root)
            if problem:
                problems.append(f"{mode}/{tier}: {problem}")
            selection[tier] = list(planned)
        per_mode[mode] = selection
    if len({json.dumps(item, sort_keys=True) for item in per_mode.values()}) > 1:
        problems.append("the two runtimes collect different selections")
    return (next(iter(per_mode.values()), {}), problems)


def run_parallel_cli(
    *,
    args: Any,
    parser: Any,
    project_root: Path,
    python_executable: Path,
    cython_python_executable: Path | None,
    assets: Sequence[Any],
    required_nodeids_by_tier: Mapping[str, Sequence[str]],
    configuration_problems: Sequence[str],
    capacity_policy: Any,
    capacity_policy_path: Path | None,
    selected: Sequence[str],
    early_smoke: bool,
    fail_fast: bool,
    rom_root: Path | None = None,
    fixture_root: Path | None = None,
    capacity_session: Any | None = None,
    explicit_matrix_workers: bool = False,
    expected_sha1: Mapping[Path, str] | None = None,
) -> int:
    """Run the opt-in concurrent mode; every refusal is explicit and non-zero."""

    if args.runtime_mode != "both":
        parser.error("--parallel-runtimes requires --runtime-mode both")
    if capacity_policy is None or capacity_policy_path is None:
        parser.error("--parallel-runtimes requires a valid --capacity-policy")
    if args.parallel_run_root is None:
        parser.error("--parallel-runtimes requires --parallel-run-root")
    ownership = kernel_ownership(not getattr(args, "parallel_allow_unqualified", False))
    if not ownership["admitted"]:
        # Before any snapshot, probe, measurement or child: the qualified mode needs kernel ownership.
        print(
            "parallel runtime gate refused:\n  the qualified mode requires kernel ownership of each "
            f"job's whole process tree and it is unavailable here ({ownership['reason']}); "
            "--parallel-allow-unqualified runs on retention evidence only and is never an acceptance"
        )
        return 2
    problems = list(configuration_problems)
    _head, _clean, commit_problems = measure_commit(project_root)
    problems.extend(commit_problems)
    run_root = args.parallel_run_root.expanduser().resolve()
    if run_root.is_relative_to(project_root):
        problems.append("--parallel-run-root must be outside the checkout")
    if rom_root is None or fixture_root is None:
        problems.append("the ROM and fixture roots must be resolved explicitly")
    total = getattr(capacity_policy, "max_concurrent_pairs", None)
    run_id = uuid.uuid4().hex
    cython = cython_python_executable or python_executable
    interpreters = {"source": str(python_executable), "cython": str(cython)}
    snapshot: dict[str, Any] = {}
    private: dict[str, Any] = {}
    plan = None
    measure: Measurer | None = None
    if not problems:
        run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            snapshot = build_snapshot(
                project_root,
                Path(str(rom_root)),
                Path(str(fixture_root)),
                run_root / run_id / "snapshot",
            )
        except (IsolationError, OSError) as exc:
            problems.append(f"private immutable snapshot refused: {exc}")
    runtime_root = run_root / run_id / "runtime"
    if not problems:
        try:
            private = build_private_runtimes(
                interpreters,
                project_root=project_root,
                checkout=Path(snapshot["checkout"]),
                destination=runtime_root,
            )
        except (IsolationError, OSError) as exc:
            problems.append(f"private immutable runtime refused: {exc}")
        else:
            interpreters = {mode: info["interpreter"] for mode, info in private.items()}
            for info in private.values():
                problems.extend(info["problems"])
    if not problems:
        snap_root, snap_rom, snap_fixture = (
            Path(snapshot["checkout"]),
            Path(snapshot["rom_root"]),
            Path(snapshot["fixture_root"]),
        )

        def measure(job_id: str) -> tuple[dict[str, Any], list[str]]:
            mode = "cython" if job_id.startswith("cython") else "source"
            return measure_identity(
                mode=mode,
                interpreter=interpreters[mode],
                project_root=snap_root,
                input_names=INPUT_FILES,
                rom_root=snap_rom,
                fixture_root=snap_fixture,
                git_root=project_root,
            )

        measured = {mode: measure(f"{mode}-job") for mode in ("source", "cython")}
        for _values, mode_problems in measured.values():
            problems.extend(mode_problems)
        first, second = measured["source"][0], measured["cython"][0]
        if (first["commit"], first["input_digests"], first["asset_digest"]) != (
            second["commit"],
            second["input_digests"],
            second["asset_digest"],
        ):
            problems.append("the two runtimes measured different commits, inputs or assets")
        frozen: dict[str, list[str]] = {}
        if not problems:
            frozen, selection_problems = freeze_selection(
                project_root=project_root,
                snapshot=snapshot,
                interpreters=interpreters,
                expected_sha1=expected_sha1,
                rom_root=Path(str(rom_root)),
                fixture_root=Path(str(fixture_root)),
                tiers=tuple(selected),
            )
            problems.extend(selection_problems)
        if not problems:
            share = (total if type(total) is int else 0) // 2
            effective = min(share, args.matrix_workers) if explicit_matrix_workers else share
            try:
                plan = build_parallel_plan(
                    commit=first["commit"],
                    build_identity={mode: measured[mode][0]["build_identity"] for mode in measured},
                    input_digests=first["input_digests"],
                    asset_digest=first["asset_digest"],
                    games=("red", "blue", "yellow"),
                    roles=("listener", "connector"),
                    modes=("source", "cython"),
                    tiers=tuple(selected),
                    required_nodeids_by_tier=frozen,
                    repetitions=args.repeat_timing,
                    deadlines=cli_deadlines(
                        args, frozen, effective, CLEANUP_SECONDS, tuple(selected)
                    ),
                    total_pairs=total if type(total) is int else 0,
                    policy_digest=file_sha256(capacity_policy_path),
                    capacity_policy_text=capacity_policy_path.read_text(encoding="utf-8"),
                    runtime_inputs={
                        mode: runtime_digest(
                            Path(info["manifest"]["private_prefix"]), Path(snapshot["checkout"])
                        )
                        for mode, info in private.items()
                        if info["manifest"]["private_prefix"]
                    },
                    early_smoke=early_smoke,
                    fail_fast=fail_fast,
                    matrix_workers=args.matrix_workers if explicit_matrix_workers else None,
                    run_id=run_id,
                )
            except PlanError as exc:
                problems.append(f"plan refused: {exc}")
    if problems or plan is None:
        if snapshot:
            thaw_and_remove(Path(snapshot["root"]))
        thaw_and_remove(runtime_root)
        print("parallel runtime gate refused:\n" + "\n".join(f"  {item}" for item in problems))
        return 2
    gate_arguments = [
        "--fail-fast" if fail_fast else "--keep-going",
        f"--repeat-timing={args.repeat_timing}",
        *([f"--timeout-seconds={args.timeout_seconds}"] if args.timeout_seconds else []),
        *(
            [f"--matrix-timeout-seconds={args.matrix_timeout_seconds}"]
            if args.matrix_timeout_seconds
            else []
        ),
    ]
    factory = default_worker_argv(
        gate_arguments,
        interpreters,
        git_root=project_root,
        rom_root=Path(snapshot["rom_root"]),
        fixture_root=Path(snapshot["fixture_root"]),
        capacity_policy=True,
    )
    budget = PairBudget(total)
    live = LiveCapacity(
        budget,
        session=capacity_session,
        probe=capacity_loss_probe(sum(job["pairs"] for job in plan.as_dict()["jobs"][:2])),
    )
    interrupt = threading.Event()
    with contextlib.ExitStack() as stack:
        stack.enter_context(interrupt_on_signals(interrupt))
        if capacity_session is not None:
            # Background sampling keeps session observation from delaying deadline enforcement.
            stack.enter_context(capacity_session.monitoring())
        report = run_parallel_plan(
            plan,
            run_root=run_root,
            project_root=Path(snapshot["checkout"]),
            budget=budget,
            worker_argv_factory=factory,
            capacity=live,
            interrupt=interrupt,
            serial=bool(getattr(args, "parallel_serial", False)),
            measure=measure,
            capacity_policy_text=capacity_policy_path.read_text(encoding="utf-8"),
            require_kernel_ownership=not getattr(args, "parallel_allow_unqualified", False),
        )
    report["runtime_inputs"] = {mode: info["manifest"] for mode, info in private.items()}
    report["runtime_immutability"] = "NOT_COMPLETE: see acceptance_limits of each runtime manifest"
    if any("cleanup unproven" in item for item in report.get("orchestration_problems", [])):
        report["snapshot_retained"] = snapshot["root"]  # never remove what cleanup has not cleared
    else:
        thaw_and_remove(Path(snapshot["root"]))
        thaw_and_remove(runtime_root)
    compare_to = getattr(args, "parallel_compare_to", None)
    if compare_to is not None:
        try:
            other = loads_strict(Path(compare_to).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report["sequential_comparison"] = {
                "status": "REFUSED",
                "problems": ["the serial bundle is unreadable or not strict bounded JSON"],
            }
        else:
            report["sequential_comparison"] = compare_bundles(report, other)
    path = write_report(run_root, report)
    print(
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_report_text(report)
    )
    print(f"report: {path}", file=sys.stderr)
    return 0 if report["overall"] == "PASS" else 1
