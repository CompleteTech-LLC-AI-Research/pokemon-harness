"""Opt-in concurrent runtime orchestration for the production gate (#107).

``--parallel-runtimes`` runs the source and Cython gates as separate owned child
processes under one immutable plan and ONE global pair budget.  The sequential
gate remains the default; nothing in this module changes it.  Every child gets
its own output, temp, cache and build trees and its own process group; the
parent signals only process groups it started itself.  A PASS is derived only
from an observed child exit status plus a terminal artifact that matches the
frozen plan (see ``production_gate_parallel_artifacts``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_artifacts import (
    TERMINAL_ARTIFACT_NAME,
    aggregate_outcomes,
    outcome_record,
    read_terminal_artifact,
    validate_terminal_artifact,
)
from scripts.production_gate_parallel_plan import ParallelPlan, PlanError, load_plan

TREE_DIRECTORIES = ("out", "tmp", "cache", "build", "evidence", "raw")
BASE_ENVIRONMENT_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "VIRTUAL_ENV", "PYTHONPATH")
THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_POLL_SECONDS = 0.05
LIMITATIONS = (
    "authored controls do not close the real-ROM dual-runtime comparison; release stays PARTIAL",
    (
        "the sequential comparison is NOT_RUN unless a sequential summary with identical "
        "identities is supplied and validated"
    ),
)


COVERAGE_DECLARATION = {
    "implemented": [
        "opt-in --parallel-runtimes with sequential execution as the unchanged default",
        (
            "immutable frozen plan: run/commit/build/input/asset identities, tiers, case ids, "
            "timing repetitions, deadlines, fail_fast and the single global pair budget"
        ),
        "separate owned child processes with private output/temp/cache/build trees",
        (
            "strict terminal-artifact aggregation (exit status, exact plan identity, tiers, "
            "counts, return codes, row ids when reported) that fails closed"
        ),
        (
            "fail_fast False lets the other runtime finish; True, interrupt, deadline and "
            "capacity loss cancel only owned process groups with bounded cleanup"
        ),
    ],
    "exclusions": [
        "full real-ROM dual-runtime run over the six tiers (#107.5) is NOT done",
        "elapsed-time comparison against sequential execution on equivalent conditions is NOT done",
        "operator allocation (#85/#86), #106 policy and the 18 orientation fixtures are unmet",
        "real runs report tier counts and return codes only: row-level identity is not enforced",
        "authored controls are not gameplay evidence; release status remains PARTIAL",
    ],
}


class CapacityError(RuntimeError):
    """Raised when the single global pair budget cannot admit the plan (fail closed)."""


class PairBudget:
    """The one global pair authority for every job; never cloned per job."""

    def __init__(self, total_pairs: object) -> None:
        if type(total_pairs) is not int or total_pairs < 1:
            raise CapacityError("pair budget is unknown, zero or not an integer")
        self.total_pairs = total_pairs
        self._reserved: dict[str, int] = {}
        self._lock = threading.Lock()
        self.lost_reason = ""

    def reserve(self, owner: str, pairs: object) -> int:
        with self._lock:
            if self.lost_reason:
                raise CapacityError(f"capacity lost: {self.lost_reason}")
            if type(pairs) is not int or pairs < 1:
                raise CapacityError(f"{owner}: reservation must be a positive integer")
            if owner in self._reserved:
                raise CapacityError(f"{owner}: duplicate reservation")
            if sum(self._reserved.values()) + pairs > self.total_pairs:
                raise CapacityError(f"{owner}: reservation exceeds the single global budget")
            self._reserved[owner] = pairs
            return pairs

    def release(self, owner: str) -> None:
        with self._lock:
            self._reserved.pop(owner, None)

    def mark_lost(self, reason: str) -> None:
        with self._lock:
            self.lost_reason = reason

    @property
    def reserved_total(self) -> int:
        with self._lock:
            return sum(self._reserved.values())


def require_parallel_capacity(plan: ParallelPlan, budget: PairBudget) -> None:
    """Refuse parallel dispatch unless the budget admits one pair per runtime job."""

    jobs = plan.as_dict()["jobs"]
    if budget.total_pairs != plan.as_dict()["capacity"]["total_pairs"]:
        raise CapacityError("pair budget differs from the frozen plan")
    if budget.total_pairs < len(jobs):
        raise CapacityError("parallel runtimes need one pair per job; only one heavy job admitted")


def prepare_job_tree(run_root: Path, run_id: str, job_id: str) -> dict[str, Path]:
    """Create the owned private trees for one job; refuse symlinks and existing trees."""

    if run_root.is_symlink() or not run_root.is_dir():
        raise OSError("run root must be an existing real directory")
    base = run_root / run_id / job_id
    if not base.resolve().is_relative_to(run_root.resolve()):
        raise OSError("job tree escapes the run root")
    base.mkdir(mode=0o700, parents=True, exist_ok=False)
    tree = {"root": base}
    for name in TREE_DIRECTORIES:
        path = base / name
        path.mkdir(mode=0o700, exist_ok=False)
        tree[name] = path
    return tree


def job_environment(
    tree: Mapping[str, Path], base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build an isolated environment: own temp/cache/build trees, one thread per library."""

    source = os.environ if base_env is None else base_env
    env = {key: source[key] for key in BASE_ENVIRONMENT_KEYS if key in source}
    env.update(
        {
            "TMPDIR": str(tree["tmp"]),
            "XDG_CACHE_HOME": str(tree["cache"]),
            "PYTHONPYCACHEPREFIX": str(tree["cache"] / "pycache"),
            "HOME": str(tree["root"]),
            "POKERED_PARALLEL_JOB_ROOT": str(tree["root"]),
            "POKERED_PARALLEL_BUILD_DIR": str(tree["build"]),
        }
    )
    env.update({name: "1" for name in THREAD_VARIABLES})
    return env


def terminate_owned_group(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Stop a process group this module started: TERM, bounded wait, then KILL."""

    group = process.pid  # start_new_session=True makes the child its own group leader
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    limit = clock() + grace_seconds
    while process.poll() is None and clock() < limit:
        sleeper(0.02)
    if process.poll() is None:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(grace_seconds, 1.0))
        except subprocess.TimeoutExpired:
            pass
    try:
        os.killpg(group, signal.SIGKILL)  # stragglers that outlived the leader
    except ProcessLookupError:
        pass


def affinity_pairs() -> int:
    """Pairs the allowed CPU set can host (two emulators per pair); Linux affinity if present."""

    try:
        return len(os.sched_getaffinity(0)) // 2
    except AttributeError:  # pragma: no cover - non-Linux fallback
        return (os.cpu_count() or 0) // 2


def capacity_loss_probe(
    reserved_pairs: int, measure: Callable[[], object] = affinity_pairs
) -> Callable[[], object]:
    """Return a probe that reports capacity loss, never a policy re-derivation.

    The first reading is the baseline; the admitted ceiling stays the capacity policy.  The
    probe reports ``reserved_pairs`` (no loss) while the reading holds the baseline-or-reserved
    floor, the lower reading once it drops, and ``None`` when the quota cannot be read.
    """

    def read() -> object:
        try:
            return measure()
        except (OSError, ValueError):
            return None

    baseline = read()
    floor = min(reserved_pairs, baseline) if type(baseline) is int else None

    def probe() -> object:
        current = read()
        if floor is None or type(current) is not int:
            return None
        return reserved_pairs if current >= floor else current

    return probe


def _group_gone(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _manifest(directory: Path) -> dict[str, tuple[int, int]]:
    entries: dict[str, tuple[int, int]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            entries[str(path.relative_to(directory))] = (info.st_size, info.st_mtime_ns)
    return entries


@dataclass
class _Running:
    job_id: str
    process: subprocess.Popen[Any]
    started: float
    deadline: float
    tree: dict[str, Path]
    handles: list[Any] = field(default_factory=list)


WorkerArgvFactory = Callable[[ParallelPlan, str, Mapping[str, Path], Path], Sequence[str]]


def default_worker_argv(
    gate_arguments: Sequence[str], interpreters: Mapping[str, str] | None = None
) -> WorkerArgvFactory:
    """Return the factory that launches the real per-runtime worker module."""

    def factory(
        plan: ParallelPlan, job_id: str, tree: Mapping[str, Path], project_root: Path
    ) -> list[str]:
        mode = plan.job(job_id)["mode"]
        interpreter = (interpreters or {}).get(mode, sys.executable)
        argv = [
            interpreter,
            "-m",
            "scripts.production_gate_parallel_worker",
            f"--plan={tree['root'].parent / 'plan.json'}",
            f"--job-id={job_id}",
            f"--job-root={tree['root']}",
            f"--project-root={project_root}",
            f"--python={interpreter}",
        ]
        argv.extend(f"--gate-arg={item}" for item in gate_arguments)
        return argv

    return factory


def _write_plan_file(run_root: Path, plan: ParallelPlan) -> Path:
    directory = run_root / plan.run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "plan.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(plan.text)
    if load_plan(path.read_text(encoding="utf-8")).digest != plan.digest:
        raise PlanError("plan file read-back differs from the frozen plan")
    return path


def _finish_job(
    plan: ParallelPlan, running: _Running, exit_status: int, clock: Callable[[], float]
) -> dict[str, Any]:
    artifact, read_problems = read_terminal_artifact(running.tree["root"])
    problems = list(read_problems)
    if artifact is not None:
        problems.extend(
            validate_terminal_artifact(
                artifact,
                plan,
                running.job_id,
                child_exit_status=exit_status,
                child_pid=running.process.pid,
            )
        )
    elapsed = clock() - running.started
    return outcome_record(
        plan,
        running.job_id,
        "failed" if problems else "passed",
        child_exit_status=exit_status,
        artifact=artifact,
        artifact_problems=problems,
        reason="; ".join(problems[:1]),
        elapsed_seconds=round(elapsed, 3),
    )


def _close_handles(running: _Running) -> None:
    for handle in running.handles:
        try:
            handle.close()
        except OSError:
            pass


def run_parallel_plan(
    plan: ParallelPlan,
    *,
    run_root: Path,
    project_root: Path,
    budget: PairBudget,
    worker_argv_factory: WorkerArgvFactory,
    base_env: Mapping[str, str] | None = None,
    capacity_probe: Callable[[], object] | None = None,
    interrupt: threading.Event | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Dispatch every planned job concurrently and return the retained aggregate report."""

    body = plan.as_dict()
    started = clock()
    outcomes: dict[str, dict[str, Any]] = {}
    running: list[_Running] = []
    cleanup: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, dict[str, tuple[int, int]]] = {}
    problems: list[str] = []
    trees: dict[str, dict[str, Path]] = {}
    cancel_reason = ""

    def settle(job_id: str, state: str, reason: str, status: int | None = None) -> None:
        outcomes[job_id] = outcome_record(
            plan,
            job_id,
            state,
            child_exit_status=status,
            artifact=None,
            artifact_problems=[reason],
            reason=reason,
        )

    def cancel(reason: str) -> None:
        nonlocal cancel_reason
        cancel_reason = cancel_reason or reason
        for item in list(running):
            terminate_owned_group(
                item.process, grace_seconds=grace_seconds, clock=clock, sleeper=sleeper
            )
            status = item.process.poll()
            _close_handles(item)
            settle(item.job_id, "interrupted", f"cancelled: {reason}", status)
            running.remove(item)

    try:
        require_parallel_capacity(plan, budget)
        for job in body["jobs"]:
            budget.reserve(job["job_id"], job["pairs"])
        _write_plan_file(run_root, plan)
    except (CapacityError, PlanError, OSError) as exc:
        for job in body["jobs"]:
            budget.release(job["job_id"])
            settle(job["job_id"], "not_started", f"refused before dispatch: {exc}")
        report = aggregate_outcomes(plan, [outcomes[j["job_id"]] for j in body["jobs"]])
        report.update(_report_extras(plan, started, clock, cancel_reason, cleanup, [str(exc)]))
        return report

    try:
        for job in body["jobs"]:
            job_id = job["job_id"]
            if cancel_reason:
                settle(job_id, "not_started", f"not started: {cancel_reason}")
                continue
            handles: list[Any] = []
            try:
                tree = prepare_job_tree(run_root, body["run_id"], job_id)
                trees[job_id] = tree
                argv = list(worker_argv_factory(plan, job_id, tree, project_root))
                handles.append(open(tree["root"] / "child.stdout", "wb"))  # noqa: SIM115
                handles.append(open(tree["root"] / "child.stderr", "wb"))  # noqa: SIM115
                process = subprocess.Popen(
                    argv,
                    cwd=project_root,
                    env=job_environment(tree, base_env),
                    stdin=subprocess.DEVNULL,
                    stdout=handles[0],
                    stderr=handles[1],
                    start_new_session=True,
                )
            except OSError as exc:
                for handle in handles:
                    handle.close()
                settle(job_id, "failed", f"could not start: {type(exc).__name__}")
                if body["fail_fast"]:
                    cancel(f"fail_fast: {job_id} could not start")
                continue
            running.append(
                _Running(
                    job_id,
                    process,
                    clock(),
                    clock() + float(job["deadline_seconds"]),
                    tree,
                    handles,
                )
            )
        while running:
            if interrupt is not None and interrupt.is_set():
                cancel("external interrupt")
                break
            if capacity_probe is not None:
                available = capacity_probe()
                if type(available) is not int or available < budget.reserved_total:
                    budget.mark_lost(f"available pairs {available!r}")
                    cancel(f"capacity loss: available pairs {available!r}")
                    break
            for item in list(running):
                if item not in running:
                    continue
                status = item.process.poll()
                if status is None:
                    if clock() > item.deadline:
                        terminate_owned_group(
                            item.process, grace_seconds=grace_seconds, clock=clock, sleeper=sleeper
                        )
                        _close_handles(item)
                        settle(item.job_id, "timeout", "deadline exceeded", item.process.poll())
                        running.remove(item)
                        if body["fail_fast"]:
                            cancel(f"fail_fast: {item.job_id} timed out")
                    continue
                _close_handles(item)
                outcomes[item.job_id] = _finish_job(plan, item, status, clock)
                snapshots[item.job_id] = _manifest(item.tree["out"])
                running.remove(item)
                if outcomes[item.job_id]["state"] != "passed" and body["fail_fast"] and running:
                    cancel(f"fail_fast: {item.job_id} {outcomes[item.job_id]['state']}")
            if running:
                sleeper(poll_seconds)
    except KeyboardInterrupt:
        cancel("external interrupt")
    finally:
        if running:
            cancel(cancel_reason or "orchestrator exit")
        for job in body["jobs"]:
            job_id = job["job_id"]
            if job_id not in outcomes:
                settle(job_id, "not_started", f"not started: {cancel_reason or 'no dispatch'}")
            budget.release(job_id)
        for job_id, tree in trees.items():
            leader = outcomes[job_id].get("child_exit_status")
            shutil.rmtree(tree["tmp"], ignore_errors=True)
            cleanup[job_id] = {
                "tmp_removed": not tree["tmp"].exists(),
                "cache_retained": tree["cache"].exists(),
                "child_exit_status": leader,
            }
    for job_id, tree in trees.items():
        if job_id in snapshots and _manifest(tree["out"]) != snapshots[job_id]:
            problems.append(
                f"{job_id}: output tree changed after the job finished (cross-job mutation)"
            )
    ordered = [outcomes[job["job_id"]] for job in body["jobs"]]
    report = aggregate_outcomes(plan, ordered)
    report.update(_report_extras(plan, started, clock, cancel_reason, cleanup, problems))
    return report


def _report_extras(
    plan: ParallelPlan,
    started: float,
    clock: Callable[[], float],
    cancel_reason: str,
    cleanup: Mapping[str, Any],
    problems: Sequence[str],
) -> dict[str, Any]:
    body = plan.as_dict()
    return {
        "fail_fast": body["fail_fast"],
        "elapsed_seconds": round(clock() - started, 3),
        "cancellation": cancel_reason,
        "cleanup": dict(cleanup),
        "orchestration_problems": list(problems),
        "capacity": body["capacity"],
        "sequential_comparison": {"status": "NOT_RUN"},
        "limitations": list(LIMITATIONS),
        "coverage_declaration": COVERAGE_DECLARATION,
        **({"overall": "FAIL"} if problems else {}),
    }


def write_report(run_root: Path, report: Mapping[str, Any]) -> Path:
    """Write the single published aggregate next to the plan, never overwriting."""

    path = run_root / str(report["run_id"]) / "parallel-report.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_report_text(report: Mapping[str, Any]) -> str:
    lines = [
        f"parallel runtime gate run {report.get('run_id')} plan {str(report.get('plan_sha256'))[:12]}",
        f"commit {report.get('commit')} elapsed {report.get('elapsed_seconds')}s",
    ]
    for job in report.get("jobs", []):
        lines.append(f"  {job.get('mode')}: {job.get('state')} exit={job.get('child_exit_status')}")
        lines.extend(f"    {item}" for item in job.get("artifact_problems", [])[:3])
    lines.extend(f"problem: {item}" for item in report.get("problems", []))
    lines.extend(f"limitation: {item}" for item in report.get("limitations", []))
    lines.append(f"overall: {report.get('overall')}")
    return "\n".join(lines)


def terminal_artifact_path(tree: Mapping[str, Path]) -> Path:
    return tree["out"] / TERMINAL_ARTIFACT_NAME


INPUT_FILES = (
    "VERSIONS.md",
    "tests/_tier_config.py",
    "scripts/production_gate.py",
    "scripts/production_gate_model.py",
    "scripts/production_gate_runtime_gates.py",
    "scripts/production_gate_parallel.py",
    "scripts/production_gate_parallel_plan.py",
    "scripts/production_gate_parallel_artifacts.py",
    "scripts/production_gate_parallel_worker.py",
)


def _clean_commit(project_root: Path, problems: list[str]) -> str:
    """Return the exact clean HEAD sha, recording a problem instead of guessing."""

    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(project_root), *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    head, status = git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=all")
    if head.returncode != 0 or status.returncode != 0:
        problems.append("the checkout commit could not be read")
        return ""
    if status.stdout.strip():
        problems.append("the checkout is not clean: parallel runs require an exact clean commit")
    return head.stdout.strip()


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
) -> int:
    """Run the opt-in concurrent mode; every refusal is explicit and non-zero."""

    from dataclasses import asdict, is_dataclass

    from scripts.production_gate_parallel_plan import (
        build_parallel_plan,
        canonical_json,
    )

    if args.runtime_mode != "both":
        parser.error("--parallel-runtimes requires --runtime-mode both")
    if capacity_policy is None or capacity_policy_path is None:
        parser.error("--parallel-runtimes requires a valid --capacity-policy")
    if args.parallel_run_root is None:
        parser.error("--parallel-runtimes requires --parallel-run-root")
    problems = list(configuration_problems)
    commit = _clean_commit(project_root, problems)
    run_root = args.parallel_run_root.expanduser().resolve()
    if run_root.is_relative_to(project_root):
        problems.append("--parallel-run-root must be outside the checkout")
    total = getattr(capacity_policy, "max_concurrent_pairs", None)
    inputs = {
        name: file_sha256(project_root / name)
        for name in INPUT_FILES
        if (project_root / name).is_file()
    }
    asset_rows = [
        asdict(item) if is_dataclass(item) and not isinstance(item, type) else vars(item)
        for item in assets
    ]
    asset_digest = hashlib.sha256(
        json.dumps(asset_rows, sort_keys=True, default=str).encode()
    ).hexdigest()
    cython = cython_python_executable or python_executable
    build_identity = hashlib.sha256(
        canonical_json(
            {"source": str(python_executable), "cython": str(cython), "commit": commit}
        ).encode()
    ).hexdigest()
    plan = None
    if not problems:
        try:
            plan = build_parallel_plan(
                commit=commit,
                build_identity=build_identity,
                input_digests=inputs,
                asset_digest=asset_digest,
                games=("red", "blue", "yellow"),
                roles=("listener", "connector"),
                modes=("source", "cython"),
                tiers=tuple(selected),
                required_nodeids_by_tier=required_nodeids_by_tier,
                repetitions=args.repeat_timing,
                deadline_seconds=float(args.timeout_seconds or 4 * 3600),
                total_pairs=total if type(total) is int else 0,
                policy_digest=file_sha256(capacity_policy_path),
                early_smoke=early_smoke,
                fail_fast=fail_fast,
                require_row_ids=False,
            )
        except PlanError as exc:
            problems.append(f"plan refused: {exc}")
    if problems or plan is None:
        print("parallel runtime gate refused:\n" + "\n".join(f"  {item}" for item in problems))
        return 2
    run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    selection = (
        ["--smoke-only"]
        if args.smoke_only
        else ["--unit-only"]
        if args.unit_only
        else [f"--tier={tier}" for tier in (args.tier or [])]
    )
    gate_arguments = [
        *selection,
        "--fail-fast" if fail_fast else "--keep-going",
        f"--repeat-timing={args.repeat_timing}",
        *([f"--timeout-seconds={args.timeout_seconds}"] if args.timeout_seconds else []),
        *(
            [f"--matrix-timeout-seconds={args.matrix_timeout_seconds}"]
            if args.matrix_timeout_seconds
            else []
        ),
        *([f"--rom-root={args.rom_root}"] if args.rom_root else []),
        *([f"--fixture-root={args.fixture_root}"] if args.fixture_root else []),
    ]
    factory = default_worker_argv(
        gate_arguments, {"source": str(python_executable), "cython": str(cython)}
    )
    report = run_parallel_plan(
        plan,
        run_root=run_root,
        project_root=project_root,
        budget=PairBudget(total),
        worker_argv_factory=factory,
        capacity_probe=capacity_loss_probe(sum(job["pairs"] for job in plan.as_dict()["jobs"])),
    )
    report["row_identity_enforced"] = False
    report["limitations"] = [
        *report["limitations"],
        (
            "real runs aggregate tier counts and return codes; per-row ids are not reported by "
            "the sequential gate, so row-level identity is NOT enforced for real runs"
        ),
    ]
    path = write_report(run_root, report)
    print(
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_report_text(report)
    )
    print(f"report: {path}", file=sys.stderr)
    return 0 if report["overall"] == "PASS" else 1
