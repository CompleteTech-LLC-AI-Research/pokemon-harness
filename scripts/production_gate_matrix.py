"""Strict trade/battle matrix audit and execution.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_capacity import (
    TierInterruptedDuringCleanup,
    _capacity_execution,
    _defer_sigint_while_finalizing,
    register_blocked_rows,
)
from scripts.production_gate_execution import _process_creation_kwargs, _retain_raw_output
from scripts.production_gate_matrix_audit import _test_key_from_nodeid
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
    Counts,
    FailureDetail,
    GateReport,
    MatrixCaseResult,
    TierResult,
    _normalize_nodeid,
)
from scripts.production_gate_runtime import _load_gate_report
from scripts.production_gate_text import _retain_failure_detail


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


@_capacity_execution
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
    capacity_session: Any | None = None,
    scope: str = "",
    publish: Callable[[TierResult], None] | None = None,
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
        # This pre-dispatch exit is a terminal BLOCKED for every row the tier
        # would have run: without it those rows had no admission decision and
        # no lifecycle state, so the blocked tier looked like a tier that ran
        # nothing.
        register_blocked_rows(
            capacity_session,
            name=name,
            collections=(),
            project_root=project_root,
            reason=reason,
            required_nodeids=nodeids,
            scope=scope,
        )
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
    if capacity_session is not None:
        # The declared policy may be tighter than the CLI worker count; never
        # admit more concurrent pairs than the policy permits.  A telemetry
        # failure here leaves the existing worker count in effect.
        with contextlib.suppress(Exception):
            capacity_session.ensure_started()
            max_workers = max(1, min(max_workers, capacity_session.policy.max_concurrent_pairs))
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
    # A row that never started because declared capacity could not admit it is
    # not a product failure.  Keep those reasons separate so a tier whose only
    # problem is unavailable capacity reports BLOCKED, while a real execution
    # failure anywhere still reports FAIL.
    capacity_blocked: list[str] = []

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

    def record_not_started(
        nodeid: str,
        reason: str,
        *,
        status: str = "NOT_STARTED",
        capacity_block: bool = False,
    ) -> None:
        if capacity_block:
            capacity_blocked.append(f"{nodeid}: {reason}")
        else:
            failures.append(f"{nodeid}: {reason}")
        case_results_by_nodeid[nodeid] = MatrixCaseResult(
            nodeid=nodeid,
            status=status,
            returncode=None,
            duration_seconds=0.0,
            reason=reason,
            deadline_seconds=timeout,
            report_kind="not-started",
            partial=True,
        )
        if capacity_session is not None:
            with contextlib.suppress(Exception):
                capacity_session.telemetry.mark(capacity_key(nodeid), "not_started")

    capacity_run_id = capacity_session.next_run_id(name, scope=scope) if capacity_session else ""

    def capacity_key(nodeid: str) -> str:
        return f"{capacity_run_id}:{nodeid}"

    def capacity_admit(nodeid: str) -> tuple[str, str]:
        """Return the admission status and reason for one matrix row."""

        if capacity_session is None:
            return "ok", ""
        try:
            capacity_session.maybe_observe()
            decision = capacity_session.admission.admit(capacity_key(nodeid))
        except Exception as exc:  # noqa: BLE001 - fail closed, never crash.
            return "blocked", f"capacity admission failed: {type(exc).__name__}: {exc}"
        reason = decision.reason
        if decision.status != "ok" and capacity_session.availability_reasons:
            reason = "; ".join(capacity_session.availability_reasons)
        return decision.status, reason

    def release_capacity(nodeid: str, state: str) -> None:
        if capacity_session is None:
            return
        # Releasing the slot may promote a queued pair.  A telemetry failure
        # here must never erase an original failure or block cleanup.
        with contextlib.suppress(Exception):
            capacity_session.admission.release(capacity_key(nodeid))
            capacity_session.telemetry.mark(capacity_key(nodeid), state)

    def capacity_end_unstarted(nodeid: str, state: str, reason: str) -> bool:
        """Terminalize a never-started row; return whether capacity was why.

        A queued waiter that is recorded as never started while still holding
        queue state is a live hazard: the next ``release`` promotes it into a
        slot and starves a real row.  Ending the admission entry here keeps
        every terminal unstarted row terminal, and the returned flag lets the
        caller report a capacity-only stop as BLOCKED rather than FAIL.
        """

        if capacity_session is None:
            return False
        try:
            capacity = capacity_session.end_unstarted(
                capacity_key(nodeid), state=state, reason=reason
            )
        except Exception:  # noqa: BLE001 - accounting must never mask a failure.
            return False
        if capacity == "capacity":
            return True
        # Declared capacity that is not currently healthy cannot admit any
        # remaining row, so a stop taken under it is a capacity stop.
        with contextlib.suppress(Exception):
            return capacity_session.availability_status != "ok"
        return False

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
                release_capacity(nodeid, "not_started")
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
            release_capacity(nodeid, "not_started")
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
        if capacity_session is not None:
            with contextlib.suppress(Exception):
                capacity_session.telemetry.mark(capacity_key(nodeid), "running")

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
        release_capacity(nodeid, "interrupted" if interrupted else "completed")

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
                    nodeid = pending.popleft()
                    reason = "matrix aggregate deadline expired before the case started"
                    if capacity_end_unstarted(nodeid, "not_started", reason):
                        # Capacity, not the product, is why this row never
                        # started: record it as a block so the tier reports
                        # BLOCKED while a real failure anywhere still reports
                        # FAIL.
                        record_not_started(
                            nodeid,
                            f"{reason}; {capacity_session.capacity_reason()}",
                            status="BLOCKED",
                            capacity_block=True,
                        )
                    else:
                        record_not_started(nodeid, reason)
                break

            while pending and len(active) < max_workers:
                if time.monotonic() >= aggregate_deadline:
                    break
                candidate = pending[0]
                admission_status, admission_reason = capacity_admit(candidate)
                if admission_status == "queued":
                    # No slot for this row yet; wait rather than spin so an
                    # active owner's deadline is never enlarged or reprioritized.
                    break
                pending.popleft()
                if admission_status in {"blocked", "expired"}:
                    # A row the controller refused (including an admission
                    # failure) is terminal: end any queue or ownership state it
                    # still holds before it is recorded, so a later release
                    # cannot promote it into a live slot.
                    capacity_end_unstarted(
                        candidate,
                        "not_started",
                        f"capacity {admission_status}: {admission_reason}",
                    )
                    record_not_started(
                        candidate,
                        f"capacity {admission_status}: {admission_reason}",
                        status="BLOCKED",
                        capacity_block=True,
                    )
                    continue
                start_case(candidate)

            if not active:
                if pending:
                    time.sleep(min(0.05, max(0.0, aggregate_deadline - time.monotonic())))
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
                capacity_end_unstarted(
                    nodeid, "interrupted", "matrix interrupted before this case started"
                )
                record_not_started(nodeid, "matrix interrupted before this case started")

    # Every child report has been loaded at this point.  A cancellation during
    # the in-memory finalization below is deferred so the assembled tier still
    # carries its counts, diagnostics, output, and rows.
    finalization_guard = _defer_sigint_while_finalizing()
    case_results = [case_results_by_nodeid[nodeid] for nodeid in sorted(nodeids)]
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    capacity_only = (
        bool(capacity_blocked)
        and not failures
        and not unexpected
        and aggregate.total + len(capacity_blocked) == len(nodeids)
        and aggregate.skipped == 0
        and aggregate.failed + aggregate.errors == 0
    )
    status = (
        "INTERRUPTED"
        if interrupted
        else "BLOCKED"
        if capacity_only
        else "PASS"
        if not failures
        and not unexpected
        and len(case_results) == len(nodeids)
        and aggregate.total == len(nodeids)
        and aggregate.skipped == 0
        and aggregate.passed == len(nodeids)
        else "FAIL"
    )
    tier_result = TierResult(
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
        reason=(
            "capacity could not admit every required row: " + "; ".join(capacity_blocked[:3])
            if capacity_only
            else ""
        ),
        selected_nodeids=list(nodeids),
        case_results=case_results,
        failure_details=failure_details,
        failure_details_omitted=failure_details_omitted,
    )
    # Publish the assembled tier while signal deferral is still installed.  A
    # ``SIGINT`` delivered between ``restore()`` and the caller's receipt of the
    # return value is raised by the restored handler, so the value never reaches
    # the caller; without a durable copy the orchestrator would then replace an
    # executed tier with an empty ``INTERRUPTED`` row.  The sink is the runtime's
    # ``completed_tiers`` mapping, which every cancellation handoff already
    # consults, so the loaded evidence survives that window.
    if publish is not None:
        publish(tier_result)
    if finalization_guard.restore():
        # The evidence is complete; carry it out and record the cancellation
        # separately instead of letting the orchestrator synthesize an empty row.
        raise TierInterruptedDuringCleanup(
            tier_result,
            "matrix finalization interrupted after loading case evidence",
        ) from None
    return tier_result


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.production_gate as _entry
