"""Strict trade/battle matrix audit and execution.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import hashlib
import os
import runpy
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_execution import _process_creation_kwargs, _retain_raw_output
from scripts.production_gate_model import (
    DEFAULT_MATRIX_WORKERS,
    DEFAULT_TIMEOUT_SECONDS,
    MATRIX_CLEANUP_TIMEOUT_SECONDS,
    MATRIX_READER_JOIN_TIMEOUT_SECONDS,
    MAX_FAILURE_DETAILS_CHARS,
    PYTEST_GATE_ARGUMENTS,
    REQUIRED_TIER_ASSETS,
    TIER_DESCRIPTIONS,
    TIER_EXPRESSIONS,
    CollectionResult,
    Counts,
    FailureDetail,
    GateReport,
    MatrixCaseResult,
    TierResult,
    _normalize_nodeid,
)
from scripts.production_gate_runtime import _load_gate_report
from scripts.production_gate_text import _retain_failure_detail


def run_matrix_collection_audit(
    *,
    project_root: Path,
    collections: Iterable[CollectionResult],
) -> dict[str, Any]:
    """Apply the repository matrix auditor to the gate's collection result."""

    collection_list = list(collections)
    matrix_path = project_root / "scripts" / "tcp_link_matrix.py"
    try:
        namespace = runpy.run_path(str(matrix_path))
        audit_collection = namespace["audit_collection"]
        audited_nodeids = _audited_matrix_nodeids(namespace)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "structural_pass": False,
            "acceptance_matrix_complete": False,
            "collected": 0,
            "groups": {},
            "acceptance_gaps": {},
            "runtime": "not-run",
            "reason": f"matrix auditor could not be loaded: {type(exc).__name__}: {exc}",
        }

    nodeids = collection_list[0].nodeids if collection_list else ()
    errors = tuple(
        collection.reason
        for collection in collection_list
        if collection.status != "PASS" and collection.reason
    )
    skips = tuple(
        collection.reason
        for collection in collection_list
        if collection.status == "PASS" and collection.reason
    )
    try:
        audit = audit_collection(
            nodeids,
            collection_errors=errors,
            collection_skips=skips,
        )
    except (TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "structural_pass": False,
            "acceptance_matrix_complete": False,
            "collected": len(nodeids),
            "groups": {},
            "acceptance_gaps": {},
            "runtime": "not-run",
            "reason": f"matrix audit failed: {type(exc).__name__}: {exc}",
        }
    audit["status"] = (
        "PASS"
        if audit.get("structural_pass") and audit.get("acceptance_matrix_complete")
        else "FAIL"
    )
    audit["audited_nodeids"] = audited_nodeids
    return audit


def synthetic_optional_skip(
    name: str,
    reason: str,
) -> TierResult:
    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=False,
        status="SKIP",
        counts=Counts(total=1, skipped=1),
        skip_reasons={reason: 1},
        reason=reason,
    )


def _test_key_from_nodeid(nodeid: str) -> tuple[str, str] | None:
    if "::" not in nodeid:
        return None
    path, test_name = nodeid.split("::", 1)
    return Path(path).name, test_name.split("[", 1)[0]


def _audited_matrix_nodeids(namespace: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return the exact strict rows used by the collection auditor."""

    raw = namespace.get("STRICT_ACCEPTANCE_NODEIDS")
    if not isinstance(raw, dict):
        raise TypeError("matrix auditor has no strict acceptance node IDs")

    result: dict[str, tuple[str, ...]] = {}
    for name in ("trade", "battle"):
        values = raw.get(name)
        if not isinstance(values, (set, frozenset, tuple, list)):
            raise TypeError(f"matrix auditor has no strict {name} node IDs")
        if any(not isinstance(nodeid, str) or "::" not in nodeid for nodeid in values):
            raise ValueError(f"matrix auditor has invalid strict {name} node ID")
        normalized = tuple(sorted({_normalize_nodeid(nodeid) for nodeid in values}))
        if not normalized:
            raise ValueError(f"matrix auditor has no strict {name} node IDs")
        result[name] = normalized
    return result


def _required_test_problems(
    nodeids: Iterable[str],
    required_test_keys: Iterable[tuple[str, str]],
) -> list[str]:
    actual = {key for nodeid in nodeids if (key := _test_key_from_nodeid(nodeid)) is not None}
    missing = sorted(set(required_test_keys) - actual)
    return [
        f"required acceptance test is absent from selected items: {module}::{name}"
        for module, name in missing
    ]


def _required_nodeid_problems(
    nodeids: Iterable[str],
    required_nodeids: Iterable[str],
) -> list[str]:
    actual = {_normalize_nodeid(nodeid) for nodeid in nodeids}
    missing = sorted({_normalize_nodeid(nodeid) for nodeid in required_nodeids} - actual)
    return [f"required matrix case is absent from selected items: {nodeid}" for nodeid in missing]


def _matrix_execution_problems(
    *,
    matrix_audit: dict[str, Any],
    required_nodeids_by_tier: dict[str, frozenset[str]],
    selected: Iterable[str],
) -> list[str]:
    """Ensure strict execution uses every row counted by the collection audit."""

    selected_names = set(selected)
    strict_tiers = tuple(name for name in ("trade", "battle") if name in selected_names)
    if not strict_tiers:
        return []

    raw_audited = matrix_audit.get("audited_nodeids")
    if not isinstance(raw_audited, dict):
        return [
            (
                "strict matrix execution cannot be coupled to the collection audit: "
                "canonical node IDs were not retained"
            )
        ]

    groups = matrix_audit.get("groups")
    problems: list[str] = []
    for name in strict_tiers:
        expected_raw = raw_audited.get(name)
        if not isinstance(expected_raw, (tuple, list)) or any(
            not isinstance(nodeid, str) for nodeid in expected_raw
        ):
            problems.append(f"{name} collection audit has no canonical strict node IDs")
            continue
        expected = {_normalize_nodeid(nodeid) for nodeid in expected_raw}

        group_name = f"strict-{name}-entrypoints"
        group = groups.get(group_name) if isinstance(groups, dict) else None
        if not isinstance(group, dict) or group.get("expected") != len(expected):
            problems.append(
                f"{name} collection audit expected count does not match its canonical "
                f"node IDs ({group_name})"
            )

        configured_raw = required_nodeids_by_tier.get(name, ())
        if not isinstance(configured_raw, (set, frozenset, tuple, list)):
            configured = set()
        else:
            configured = {
                _normalize_nodeid(nodeid) for nodeid in configured_raw if isinstance(nodeid, str)
            }
        missing = expected - configured
        extra = configured - expected
        if missing or extra:
            problems.append(
                f"{name} execution manifest does not match the audited strict matrix: "
                f"expected={len(expected)} configured={len(configured)} "
                f"missing={len(missing)} extra={len(extra)}"
            )
    return problems


def _add_counts(target: Counts, source: Counts) -> None:
    """Add one validated pytest result to an aggregate counter."""

    for field_name in Counts.__dataclass_fields__:
        setattr(target, field_name, getattr(target, field_name) + getattr(source, field_name))


def _matrix_report_path(report_directory: Path, nodeid: str) -> Path:
    """Return a collision-resistant report path for one matrix selector."""

    digest = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()[:20]
    return report_directory / f"matrix-{digest}.json"


def _drain_matrix_stream(stream: Any, chunks: list[str]) -> None:
    """Drain one matrix child stream without coupling it to the supervisor."""

    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            chunks.append(chunk)
    except (OSError, ValueError):
        return
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _kill_matrix_process(
    process: subprocess.Popen[str],
    *,
    deadline: float | None = None,
) -> bool:
    """Kill and reap a matrix process tree without crossing ``deadline``."""

    pid = getattr(process, "pid", None)
    if pid is None:
        return False
    cleanup_deadline = deadline
    if cleanup_deadline is None:
        cleanup_deadline = time.monotonic() + MATRIX_CLEANUP_TIMEOUT_SECONDS

    def wait_for_exit() -> bool:
        if process.poll() is not None:
            return True
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            return process.poll() is not None
        try:
            process.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, OSError):
            return process.poll() is not None
        return process.poll() is not None

    # Signal the complete private process group even if the direct pytest
    # process already exited: a descendant can otherwise retain the pipe.
    if os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    else:
        remaining = cleanup_deadline - time.monotonic()
        if remaining > 0:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=remaining,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.kill()
        except OSError:
            pass

    # Reaping is still required, but it shares one absolute cleanup budget
    # with the process-tree kill. A failed reap is retained as gate evidence.
    reaped = wait_for_exit()
    if not reaped:
        try:
            process.kill()
        except OSError:
            pass
        reaped = wait_for_exit()

    # The reader (or the bounded drain for an interrupted reader start) owns
    # this pipe. Closing it here can discard unread output or block behind a
    # concurrent read, crossing the cleanup deadline.
    return reaped


def run_matrix_tier(
    *,
    name: str,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    required_problems: list[str],
    timeout_override: float | None,
    report_directory: Path,
    required_test_keys: Iterable[tuple[str, str]] = (),
    required_nodeids: Iterable[str] = (),
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
    raw_output_directory: Path | None = None,
) -> TierResult:
    """Run strict acceptance rows under hard per-case and aggregate bounds.

    Each selector gets its own pytest process and report files.  A small
    supervisor keeps at most ``matrix_workers`` processes active, kills active
    process groups at the aggregate cutoff, and records queued rows as
    ``NOT_STARTED``.  It never relies on an executor context whose shutdown
    could wait indefinitely for an emulator child.
    """

    required = name not in _entry.OPTIONAL_TIERS
    nodeids = tuple(sorted(set(required_nodeids)))
    if not nodeids:
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=required,
            status="FAIL",
            reason="strict matrix tier has no required node IDs",
        )
    if matrix_workers <= 0:
        raise ValueError("matrix_workers must be positive")
    if required and name in REQUIRED_TIER_ASSETS and required_problems:
        reason = "required assets unavailable: " + "; ".join(required_problems)
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=True,
            status="BLOCKED",
            reason=reason,
            selected_nodeids=list(nodeids),
        )

    selected_keys = {
        key for nodeid in nodeids if (key := _test_key_from_nodeid(nodeid)) is not None
    }
    failures = [
        f"required acceptance test is absent from matrix selectors: {module}::{test_name}"
        for module, test_name in sorted(set(required_test_keys) - selected_keys)
    ]
    timeout = timeout_override or _entry.MATRIX_CASE_TIMEOUT_SECONDS.get(
        name, DEFAULT_TIMEOUT_SECONDS[name]
    )
    max_workers = min(matrix_workers, len(nodeids))
    waves = (len(nodeids) + max_workers - 1) // max_workers
    aggregate_timeout = matrix_timeout_override or (
        timeout * waves + _entry.MATRIX_AGGREGATE_GRACE_SECONDS
    )
    if aggregate_timeout <= 0:
        raise ValueError("matrix aggregate timeout must be positive")
    started = time.monotonic()
    aggregate_deadline = started + aggregate_timeout
    aggregate_cleanup_deadline = aggregate_deadline + MATRIX_CLEANUP_TIMEOUT_SECONDS
    aggregate = Counts()
    aggregate_reasons: Counter[str] = Counter()
    case_results_by_nodeid: dict[str, MatrixCaseResult] = {}
    failure_details: list[FailureDetail] = []
    failure_details_omitted = 0
    failure_detail_budget = MAX_FAILURE_DETAILS_CHARS
    output_tails: list[str] = []
    pending = deque(nodeids)
    active: dict[str, dict[str, Any]] = {}

    def record_case(
        *,
        nodeid: str,
        returncode: int | None,
        report: GateReport,
        output: str,
        duration: float,
        timed_out: bool = False,
        interrupted: bool = False,
        timeout_reason: str = "",
        report_kind: str = "final",
        cleanup_error: str = "",
    ) -> None:
        nonlocal failure_detail_budget, failure_details_omitted
        _add_counts(aggregate, report.counts)
        aggregate_reasons.update(report.skip_reasons)
        problems: list[str] = []
        capture_error = _retain_raw_output(
            raw_output_directory,
            _matrix_report_path(report_directory, nodeid).with_suffix(".log").name,
            output,
        )
        if capture_error:
            problems.append(capture_error)
        if timed_out:
            problems.append(timeout_reason or f"matrix case timed out after {timeout:.1f}s")
        if interrupted:
            problems.append(timeout_reason or "matrix execution interrupted")
        if cleanup_error:
            problems.append(cleanup_error)
        if report.error:
            problems.append(report.error)
        if returncode not in (None, 0) and not timed_out:
            problems.append(f"pytest returned exit code {returncode}")
        if report.collection_errors:
            problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
        if report.collection_skips:
            problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
        if report.counts.total == 0:
            problems.append("pytest selected no tests for matrix selector")
        if report.counts.total != 1:
            problems.append(
                f"matrix selector produced {report.counts.total} test outcomes; expected exactly 1"
            )
        normalized_report_nodeids = {_normalize_nodeid(item) for item in report.nodeids}
        if _normalize_nodeid(nodeid) not in normalized_report_nodeids:
            problems.append(f"matrix selector was not collected: {nodeid}")
        if (
            report.counts.failed
            or report.counts.errors
            or report.counts.xfailed
            or report.counts.xpassed
        ):
            problems.append(
                "unexpected outcomes: "
                f"failed={report.counts.failed} errors={report.counts.errors} "
                f"xfailed={report.counts.xfailed} xpassed={report.counts.xpassed}"
            )
        if required and report.counts.skipped:
            problems.append("required matrix case produced a test skip")
        if report.counts.passed != 1:
            problems.append(
                f"matrix case did not produce exactly one pass: passed={report.counts.passed}"
            )
        if problems:
            failures.extend(f"{nodeid}: {problem}" for problem in problems)
            for records, outcome in (
                (report.failed_records, None),
                (report.collection_errors, "collection_error"),
                (report.collection_skips, "collection_skip"),
            ):
                for record in records:
                    failure_detail_budget, omitted = _retain_failure_detail(
                        failure_details,
                        record if outcome is None else {**record, "outcome": outcome},
                        iteration=1,
                        remaining_chars=failure_detail_budget,
                    )
                    failure_details_omitted += omitted
            if output.strip():
                output_tails.append(f"{nodeid}\n{output[-4000:]}")
        case_results_by_nodeid[nodeid] = MatrixCaseResult(
            nodeid=nodeid,
            status="INTERRUPTED"
            if interrupted
            else ("TIMEOUT" if timed_out else ("PASS" if not problems else "FAIL")),
            returncode=130 if interrupted else (124 if timed_out else returncode),
            duration_seconds=duration,
            counts=report.counts,
            reason="; ".join(problems),
            output_tail=output[-4000:],
            deadline_seconds=timeout,
            report_kind=report_kind,
            partial=timed_out or interrupted or report_kind == "partial",
        )

    def record_not_started(nodeid: str, reason: str) -> None:
        failures.append(f"{nodeid}: {reason}")
        case_results_by_nodeid[nodeid] = MatrixCaseResult(
            nodeid=nodeid,
            status="NOT_STARTED",
            returncode=None,
            duration_seconds=0.0,
            reason=reason,
            deadline_seconds=timeout,
            report_kind="not-started",
            partial=True,
        )

    def load_case_report(
        state: dict[str, Any],
        *,
        returncode: int,
        timed_out: bool,
    ) -> tuple[GateReport, str]:
        final_report = _load_gate_report(
            state["report_path"],
            expected_returncode=None if timed_out else returncode,
        )
        if not timed_out:
            return final_report, "final"
        if not final_report.error:
            return final_report, "final"
        progress_report = _load_gate_report(state["progress_path"], allow_partial=True)
        if not progress_report.error:
            return progress_report, "partial"
        return GateReport(
            Counts(errors=1),
            error=(f"{final_report.error}; progress report: {progress_report.error}"),
        ), "missing"

    def start_case(nodeid: str) -> None:
        report_path = _matrix_report_path(report_directory, nodeid)
        progress_path = report_path.with_suffix(".progress.json")
        for path in (report_path, progress_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                record_case(
                    nodeid=nodeid,
                    returncode=None,
                    report=GateReport(
                        Counts(errors=1),
                        error=f"could not prepare matrix report path: {type(exc).__name__}: {exc}",
                    ),
                    output="",
                    duration=0.0,
                    report_kind="missing",
                )
                return
        child_environment = dict(environment)
        child_environment["POKERED_GATE_REPORT"] = str(report_path)
        child_environment["POKERED_GATE_PROGRESS_REPORT"] = str(progress_path)
        command = [str(python_executable), "-m", "pytest", nodeid]
        command.extend(
            (
                "-m",
                TIER_EXPRESSIONS[name],
                *PYTEST_GATE_ARGUMENTS,
                "-rA",
                "--maxfail=0",
            )
        )
        now = time.monotonic()
        chunks: list[str] = []
        try:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **_process_creation_kwargs(),
            )
        except OSError as exc:
            record_case(
                nodeid=nodeid,
                returncode=None,
                report=GateReport(
                    Counts(errors=1),
                    error=f"could not start matrix pytest: {type(exc).__name__}: {exc}",
                ),
                output="",
                duration=0.0,
                report_kind="missing",
            )
            return
        active[nodeid] = {
            "process": process,
            "chunks": chunks,
            "reader": None,
            "started_at": now,
            "deadline_at": now + timeout,
            "report_path": report_path,
            "progress_path": progress_path,
        }
        reader = threading.Thread(
            target=_drain_matrix_stream,
            args=(process.stdout, chunks),
            name=f"pokered-gate-output-{name}",
            daemon=True,
        )
        active[nodeid]["reader"] = reader
        reader.start()

    def finish_case(
        nodeid: str,
        *,
        timed_out: bool,
        interrupted: bool = False,
        reason: str = "",
        cleanup_deadline: float | None = None,
    ) -> None:
        state = active[nodeid]
        process = state["process"]
        cleanup_error = ""
        if cleanup_deadline is None:
            cleanup_deadline = min(
                aggregate_cleanup_deadline,
                time.monotonic() + MATRIX_CLEANUP_TIMEOUT_SECONDS,
            )
        cleanup_attempted = timed_out or interrupted
        if timed_out or interrupted:
            if not _entry._kill_matrix_process(process, deadline=cleanup_deadline):
                cleanup_error = "matrix child did not terminate after the cleanup deadline"
            returncode = 130 if interrupted else 124
        else:
            returncode = process.poll()
            if returncode is None:
                returncode = 125
        # ``poll()`` only means the process has exited; the reader can still
        # be draining its pipe.  Join it before retaining diagnostics so a
        # fast case does not lose the very failure text needed to audit it.
        reader = state["reader"]
        if reader is None or reader.ident is None:
            # Cancellation can arrive before Thread.start() launches its
            # reader. The owned process is already registered and terminated;
            # collect its remaining output through the bounded drain path.
            state["chunks"].append(_entry._communicate_after_termination(process))
        else:
            reader.join(
                timeout=max(
                    0.0,
                    min(
                        MATRIX_READER_JOIN_TIMEOUT_SECONDS,
                        cleanup_deadline - time.monotonic(),
                    ),
                )
            )
        if reader is not None and reader.is_alive():
            if not cleanup_attempted:
                cleanup_attempted = True
                if not _entry._kill_matrix_process(process, deadline=cleanup_deadline):
                    cleanup_error = (
                        f"{cleanup_error}; " if cleanup_error else ""
                    ) + "matrix child did not terminate after the cleanup deadline"
            remaining = cleanup_deadline - time.monotonic()
            if remaining > 0:
                reader.join(timeout=remaining)
        if reader is not None and reader.is_alive():
            cleanup_error = (
                f"{cleanup_error}; " if cleanup_error else ""
            ) + "matrix output reader did not terminate after the cleanup deadline"
        report, report_kind = load_case_report(
            state,
            returncode=int(returncode),
            timed_out=timed_out or interrupted,
        )
        record_case(
            nodeid=nodeid,
            returncode=int(returncode),
            report=report,
            output="".join(state["chunks"]),
            duration=time.monotonic() - state["started_at"],
            timed_out=timed_out,
            interrupted=interrupted,
            timeout_reason=reason,
            report_kind=report_kind,
            cleanup_error=cleanup_error,
        )
        active.pop(nodeid, None)

    interrupted = False
    try:
        while pending or active:
            for nodeid, state in list(active.items()):
                process = state["process"]
                current = time.monotonic()
                if current >= aggregate_deadline:
                    finish_case(
                        nodeid,
                        timed_out=True,
                        reason=(
                            f"matrix aggregate deadline exceeded after {aggregate_timeout:.1f}s"
                        ),
                        cleanup_deadline=aggregate_cleanup_deadline,
                    )
                elif current >= state["deadline_at"]:
                    finish_case(
                        nodeid,
                        timed_out=True,
                        reason=f"matrix case timed out after {timeout:.1f}s",
                        cleanup_deadline=min(
                            aggregate_cleanup_deadline,
                            current + MATRIX_CLEANUP_TIMEOUT_SECONDS,
                        ),
                    )
                elif process.poll() is not None:
                    finish_case(
                        nodeid,
                        timed_out=False,
                        cleanup_deadline=min(
                            aggregate_cleanup_deadline,
                            current + MATRIX_CLEANUP_TIMEOUT_SECONDS,
                        ),
                    )

            if time.monotonic() >= aggregate_deadline:
                for nodeid in list(active):
                    finish_case(
                        nodeid,
                        timed_out=True,
                        reason=(
                            f"matrix aggregate deadline exceeded after {aggregate_timeout:.1f}s"
                        ),
                        cleanup_deadline=aggregate_cleanup_deadline,
                    )
                while pending:
                    record_not_started(
                        pending.popleft(),
                        "matrix aggregate deadline expired before the case started",
                    )
                break

            while pending and len(active) < max_workers:
                if time.monotonic() >= aggregate_deadline:
                    break
                start_case(pending.popleft())

            if not active:
                continue
            remaining = aggregate_deadline - time.monotonic()
            if remaining > 0:
                next_deadline = min(
                    [state["deadline_at"] for state in active.values()] + [aggregate_deadline]
                )
                time.sleep(min(0.05, max(0.0, next_deadline - time.monotonic())))
    except KeyboardInterrupt:
        interrupted = True
        cleanup_deadline = time.monotonic() + MATRIX_CLEANUP_TIMEOUT_SECONDS
        for nodeid in list(active):
            finish_case(
                nodeid,
                timed_out=False,
                interrupted=True,
                reason="matrix execution interrupted",
                cleanup_deadline=cleanup_deadline,
            )
        for nodeid in nodeids:
            if nodeid not in case_results_by_nodeid:
                record_not_started(nodeid, "matrix interrupted before this case started")

    case_results = [case_results_by_nodeid[nodeid] for nodeid in sorted(nodeids)]
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    status = (
        "INTERRUPTED"
        if interrupted
        else "PASS"
        if not failures
        and not unexpected
        and len(case_results) == len(nodeids)
        and aggregate.total == len(nodeids)
        and aggregate.skipped == 0
        and aggregate.passed == len(nodeids)
        else "FAIL"
    )
    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=required,
        status=status,
        counts=aggregate,
        returncodes=[case.returncode for case in case_results if case.returncode is not None],
        duration_seconds=time.monotonic() - started,
        skip_reasons=dict(sorted(aggregate_reasons.items())),
        command=[
            str(python_executable),
            "-m",
            "pytest",
            "<one-required-nodeid-per-worker>",
            "-m",
            TIER_EXPRESSIONS[name],
            f"--matrix-workers={max_workers}",
            f"--case-timeout={timeout:.1f}",
            f"--aggregate-timeout={aggregate_timeout:.1f}",
        ],
        output_tail="\n\n".join(output_tails)[-8000:],
        iteration_failures=failures,
        selected_nodeids=list(nodeids),
        case_results=case_results,
        failure_details=failure_details,
        failure_details_omitted=failure_details_omitted,
    )


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.production_gate as _entry
