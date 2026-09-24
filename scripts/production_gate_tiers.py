"""Tier execution, preparation and runtime gate orchestration.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_assets import required_asset_problems
from scripts.production_gate_capacity import (
    TierInterruptedDuringCleanup,
    _capacity_execution,
    _completed_tier_sink,
    _defer_sigint_while_finalizing,
    register_blocked_rows,
)
from scripts.production_gate_matrix import _matrix_execution_problems, run_matrix_tier
from scripts.production_gate_matrix_audit import _required_nodeid_problems, _required_test_problems
from scripts.production_gate_model import (
    _EVIDENCE_OMITTED,
    COLLECTION_TIMEOUT_SECONDS,
    DEFAULT_MATRIX_WORKERS,
    DEFAULT_TIMEOUT_SECONDS,
    FIXTURE_MANIFEST_RELATIVE_PATH,
    MAX_FAILED_OUTPUT_CHARS,
    MAX_FAILURE_DETAILS_CHARS,
    MAX_ITERATION_FAILURE_CHARS,
    MAX_ITERATION_FAILURES,
    REQUIRED_TIER_ASSETS,
    RUNTIME_MODES,
    SMOKE_REQUIRED_NODEIDS,
    SMOKE_REQUIRED_TESTS,
    SMOKE_SELECTORS,
    TIER_DESCRIPTIONS,
    TIER_EXPRESSIONS,
    AssetRecord,
    CollectionResult,
    Counts,
    FailureDetail,
    MatrixCaseResult,
    PreparedRuntimeGate,
    RuntimeGateResult,
    TierResult,
    _normalize_nodeid,
)
from scripts.production_gate_text import _bounded_failure_text, _retain_failure_detail


@_capacity_execution
def run_tier(
    *,
    name: str,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    required_problems: list[str],
    repeat: int,
    timeout_override: float | None,
    report_directory: Path,
    required_test_keys: Iterable[tuple[str, str]] = (),
    required_nodeids: Iterable[str] = (),
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
    raw_output_directory: Path | None = None,
    capacity_session: Any | None = None,
    collections: Sequence[CollectionResult] = (),
    scope: str = "",
    publish: Callable[[TierResult], None] | None = None,
) -> TierResult:
    if name == "smoke":
        required_test_keys = frozenset(required_test_keys) | SMOKE_REQUIRED_TESTS
        required_nodeids = frozenset(required_nodeids) | SMOKE_REQUIRED_NODEIDS
    if name in {"trade", "battle"} and required_nodeids:
        return run_matrix_tier(
            name=name,
            project_root=project_root,
            python_executable=python_executable,
            environment=environment,
            required_problems=required_problems,
            timeout_override=timeout_override,
            report_directory=report_directory,
            required_test_keys=required_test_keys,
            required_nodeids=required_nodeids,
            matrix_workers=matrix_workers,
            matrix_timeout_override=matrix_timeout_override,
            raw_output_directory=raw_output_directory,
            capacity_session=capacity_session,
            scope=scope,
            publish=publish,
        )

    required = name not in _entry.OPTIONAL_TIERS
    if required and name in REQUIRED_TIER_ASSETS and required_problems:
        reason = "required assets unavailable: " + "; ".join(required_problems)
        register_blocked_rows(
            capacity_session,
            name=name,
            collections=collections,
            project_root=project_root,
            reason=reason,
            required_nodeids=required_nodeids,
            scope=scope,
        )
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=True,
            status="BLOCKED",
            reason=reason,
            selected_nodeids=sorted(
                {_normalize_nodeid(nodeid) for nodeid in required_nodeids}
                | (set(SMOKE_REQUIRED_NODEIDS) if name == "smoke" else set())
            ),
        )

    if capacity_session is not None and name in REQUIRED_TIER_ASSETS:
        # Emulator pairs inside one ordinary tier are admitted individually.
        # A declared capacity policy must be able to stop the *next* pair when
        # observed capacity becomes unavailable, which a single batch process
        # could never notice.  The tier's own preflight enumeration supplies the
        # exact planned rows, so dispatch and admission cannot drift.  The
        # per-case timeout stays the tier's declared budget while the aggregate
        # timeout keeps the whole tier inside that same wall-clock bound.
        # Rows stay sequential: the batch ran one pytest process at a time, so
        # a capacity policy must not silently add emulator concurrency.
        planned, problem = _entry.planned_tier_nodeids(collections, name, project_root)
        if not planned:
            if problem:
                # Fail closed: without the exact planned set a per-pair policy
                # cannot prove it admitted every emulator pair, so the tier must
                # not run as an unaccounted batch.
                return _unrun_tier(
                    name,
                    f"capacity could not enumerate planned rows for tier {name}: {problem}",
                    required_nodeids,
                    status="BLOCKED",
                    capacity_session=capacity_session,
                    collections=collections,
                    project_root=project_root,
                    scope=scope,
                )
            # A genuinely empty planned set is the ordinary "selected no tests"
            # failure, not a capacity prerequisite; keep it a FAIL so an empty
            # tier cannot be excused as unavailable capacity.
            return _unrun_tier(
                name,
                f"tier {name} selected no tests for its marker expression",
                required_nodeids,
                status="FAIL",
            )
        tier_timeout = timeout_override or DEFAULT_TIMEOUT_SECONDS[name]
        # Union the strict required manifest so a row the collection could not
        # enumerate is still demanded (and reported missing) rather than silently
        # dropped from the dispatched set.
        dispatched = sorted(
            set(planned) | {_normalize_nodeid(nodeid) for nodeid in required_nodeids}
        )
        return run_matrix_tier(
            name=name,
            project_root=project_root,
            python_executable=python_executable,
            environment=environment,
            required_problems=required_problems,
            timeout_override=tier_timeout,
            report_directory=report_directory,
            required_test_keys=required_test_keys,
            required_nodeids=dispatched,
            matrix_workers=1,
            matrix_timeout_override=tier_timeout,
            raw_output_directory=raw_output_directory,
            capacity_session=capacity_session,
            scope=scope,
            publish=publish,
        )

    timeout = timeout_override or DEFAULT_TIMEOUT_SECONDS[name]
    if name == "timing":
        repeat = max(repeat, 5)
    else:
        repeat = 1

    aggregate = Counts()
    aggregate_reasons: Counter[str] = Counter()
    returncodes: list[int] = []
    output_tail = ""
    command: list[str] | None = None
    iteration_failures: list[str] = []
    failure_details: list[FailureDetail] = []
    failure_details_omitted = 0
    failure_detail_budget = MAX_FAILURE_DETAILS_CHARS
    omitted_failures = 0
    failed_output = ""
    output_omitted = False
    baseline_nodeids: set[str] | None = None
    selected_nodeids: list[str] = []
    started = time.monotonic()
    interrupted_between_iterations = False
    try:
        for iteration in range(1, repeat + 1):
            report_path = report_directory / f"{name}-{iteration}.json"
            returncode, report, output, command = _entry.run_pytest_once(
                project_root=project_root,
                python_executable=python_executable,
                environment=environment,
                expression=TIER_EXPRESSIONS[name],
                timeout_seconds=timeout,
                report_path=report_path,
                raw_output_directory=raw_output_directory,
                **({"selectors": SMOKE_SELECTORS} if name == "smoke" else {}),
            )
            counts = report.counts
            aggregate.total += counts.total
            aggregate.passed += counts.passed
            aggregate.failed += counts.failed
            aggregate.skipped += counts.skipped
            aggregate.xfailed += counts.xfailed
            aggregate.xpassed += counts.xpassed
            aggregate.errors += counts.errors
            aggregate_reasons.update(report.skip_reasons)
            returncodes.append(returncode)
            if output.strip():
                output_tail = _bounded_failure_text(output, MAX_FAILED_OUTPUT_CHARS, tail=True)

            problems: list[str] = []
            if report.error:
                problems.append(report.error)
            if returncode != 0:
                problems.append(f"pytest returned exit code {returncode}")
            if report.collection_errors:
                problems.append(
                    f"pytest reported {len(report.collection_errors)} collection error(s)"
                )
            if report.collection_skips:
                problems.append(
                    f"pytest reported {len(report.collection_skips)} collection skip(s)"
                )
            if counts.total == 0:
                problems.append("pytest selected no tests for the tier expression")
            if counts.failed or counts.errors or counts.xfailed or counts.xpassed:
                problems.append(
                    "unexpected outcomes: "
                    f"failed={counts.failed} errors={counts.errors} "
                    f"xfailed={counts.xfailed} xpassed={counts.xpassed}"
                )
            if required and counts.skipped:
                problems.append(f"required tier produced {counts.skipped} test skip(s)")
            if not counts.passed and not counts.xpassed:
                problems.append("iteration produced no passing test outcome")

            required_problems = _required_test_problems(report.nodeids, required_test_keys)
            problems.extend(required_problems)
            problems.extend(_required_nodeid_problems(report.nodeids, required_nodeids))
            current_nodeids = set(report.nodeids)
            if baseline_nodeids is None:
                baseline_nodeids = current_nodeids
                selected_nodeids = list(report.nodeids)
            elif name == "timing" and current_nodeids != baseline_nodeids:
                problems.append(
                    "timing-tier selected test set changed between repetitions: "
                    f"baseline={len(baseline_nodeids)} current={len(current_nodeids)}"
                )
            if problems:
                # Put actionable case evidence before generic accounting summaries.
                for records, outcome in (
                    (report.failed_records, None),
                    (report.collection_errors, "collection_error"),
                    (report.collection_skips, "collection_skip"),
                ):
                    for record in records:
                        failure_detail_budget, omitted = _retain_failure_detail(
                            failure_details,
                            record if outcome is None else {**record, "outcome": outcome},
                            iteration=iteration,
                            remaining_chars=failure_detail_budget,
                        )
                        failure_details_omitted += omitted
                details = (
                    f"{_bounded_failure_text(record['nodeid'], 500)}: {record['outcome']}: "
                    f"{_bounded_failure_text(record['reason'], 1300, tail=True)}"
                    for record in report.failed_records
                )
                collection_details = (
                    f"{_bounded_failure_text(record['nodeid'], 500)}: collection: "
                    f"{_bounded_failure_text(record['reason'], 1300, tail=True)}"
                    for record in (*report.collection_errors, *report.collection_skips)
                )
                for detail_group in (details, collection_details, problems):
                    for detail in detail_group:
                        if len(iteration_failures) < MAX_ITERATION_FAILURES:
                            iteration_failures.append(
                                _bounded_failure_text(
                                    f"iteration {iteration}: {detail}", MAX_ITERATION_FAILURE_CHARS
                                )
                            )
                        else:
                            omitted_failures += 1
                if not output_omitted:
                    excerpt = f"iteration {iteration}:\n" + _bounded_failure_text(
                        output if output.strip() else "[no subprocess output]", 3900, tail=True
                    )
                    candidate = failed_output + ("\n\n" if failed_output else "") + excerpt
                    budget = MAX_FAILED_OUTPUT_CHARS - len(_EVIDENCE_OMITTED)
                    if len(candidate) > budget:
                        # Never evict the first failure to make room for later runs.
                        failed_output += _EVIDENCE_OMITTED
                        output_omitted = True
                    else:
                        failed_output = candidate

            if returncode == 130:
                break
    except KeyboardInterrupt:
        # A cancellation delivered between an already-loaded iteration and
        # the next subprocess used to escape past this loop, discarding every
        # aggregate, diagnostic, output tail and return code collected so far.
        # Record it here so the accumulated iteration evidence is finalized
        # as an interrupted partial tier instead.
        interrupted_between_iterations = True

    # Every child report has been loaded by now and the remaining work is
    # in-memory assembly.  A cancellation delivered inside that window used to
    # escape with the executed evidence unreachable, so a batch tier lost its
    # counts and diagnostics the same way an unguarded matrix tier did.  Defer
    # the signal, assemble the complete tier, and carry the cancellation out
    # afterwards instead.
    finalization_guard = _defer_sigint_while_finalizing()
    if omitted_failures:
        iteration_failures.append(f"[...{omitted_failures} additional failure entries omitted...]")
    if failed_output:
        output_tail = failed_output

    duration = time.monotonic() - started
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    if interrupted_between_iterations or 130 in returncodes:
        status = "INTERRUPTED"
    elif iteration_failures or any(code != 0 for code in returncodes) or unexpected:
        status = "FAIL"
    elif aggregate.total == 0:
        status = "FAIL"
        output_tail = f"pytest selected no tests for marker expression {TIER_EXPRESSIONS[name]!r}"
    elif required and aggregate.skipped:
        status = "FAIL"
        output_tail = (
            f"required tier produced {aggregate.skipped} skip(s); "
            "missing/unsupported coverage is not accepted in the production gate\n" + output_tail
        )
    elif aggregate.skipped:
        # Optional coverage may be unavailable on a BYO-ROM machine, but a
        # partial run must remain visibly optional instead of being reported as
        # a full PASS merely because another optional case passed.
        status = "SKIP"
    else:
        status = "PASS" if aggregate.passed else "SKIP"

    tier_result = TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=required,
        status=status,
        counts=aggregate,
        returncodes=returncodes,
        duration_seconds=duration,
        skip_reasons=dict(sorted(aggregate_reasons.items())),
        command=command,
        output_tail=output_tail,
        iteration_failures=iteration_failures,
        selected_nodeids=selected_nodeids,
        failure_details=failure_details,
        failure_details_omitted=failure_details_omitted,
    )
    # Durable publication before the signal handler is restored, for the same
    # reason as the matrix supervisor: otherwise a ``SIGINT`` landing between
    # ``restore()`` and the caller's receipt of the return value loses every
    # accumulated timing iteration.
    if publish is not None:
        publish(tier_result)
    if interrupted_between_iterations:
        # The repetition loop was cancelled.  Carry the accumulated iterations
        # out as an interrupted tier instead of letting the cancellation escape
        # with the aggregates unreachable.
        raise TierInterruptedDuringCleanup(
            tier_result,
            f"{name} cancelled between timing repetitions after loading iteration evidence",
        ) from None
    if finalization_guard.restore():
        # The evidence is complete; carry it out and record the cancellation
        # separately instead of letting the orchestrator synthesize an empty row.
        raise TierInterruptedDuringCleanup(
            tier_result,
            f"{name} finalization interrupted after loading case evidence",
        ) from None
    return tier_result


def _optional_preflight_reason(name: str, records: list[AssetRecord]) -> str | None:
    roms = [record for record in records if record.kind == "rom"]
    fixtures = [record for record in records if record.kind == "fixture"]
    if not any(record.status == "ok" for record in roms):
        return f"optional {name} acceptance unavailable: no pinned ROM assets found"
    if not any(record.status == "ok" for record in fixtures):
        return f"optional {name} acceptance unavailable: no derived link fixtures found"
    return None


def prepare_runtime_gate(
    *,
    mode: str,
    project_root: Path,
    python_executable: Path,
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
    assets: list[AssetRecord],
    selected: Sequence[str],
    required_tests_by_tier: dict[str, frozenset[tuple[str, str]]],
    required_nodeids_by_tier: dict[str, frozenset[str]],
    configuration_problems: Iterable[str] = (),
    timeout_override: float | None = None,
    raw_output_directory: Path | None = None,
) -> PreparedRuntimeGate:
    """Probe and collect one runtime once before any planned tier executes.

    ``both`` is an orchestration choice, not a PyBoy runtime.  Keeping this
    function restricted to ``source`` and ``cython`` makes it impossible for
    a dual invocation to accidentally probe or run tests under an ambiguous
    inherited environment.
    """

    if mode not in RUNTIME_MODES:
        raise ValueError(f"runtime gate requires an explicit runtime mode: {mode!r}")

    runtime_output_directory = raw_output_directory / mode if raw_output_directory else None
    selected_tiers = tuple(selected)
    real_rom_scope = bool(set(selected_tiers) & REQUIRED_TIER_ASSETS)
    environment = _entry.build_test_environment(
        project_root,
        rom_root,
        fixture_root,
        expected_sha1,
        runtime_mode=mode,
    )
    # Subprocess acceptance tests must use the exact interpreter whose runtime
    # contract was probed above, not a stale auxiliary virtualenv discovered
    # from the worktree.
    environment["POKERED_PYTHON"] = str(python_executable)
    runtime = _entry.probe_runtime(python_executable, project_root, environment)
    gate_problems = _entry.runtime_problems(
        project_root,
        runtime,
        expected_mode=mode,
    )
    gate_problems.extend(
        _entry.environment_policy_problems(
            project_root=project_root,
            environment=environment,
            assets=assets,
        )
    )
    gate_problems.extend(configuration_problems)

    collections = _entry.run_collection_preflight(
        project_root=project_root,
        python_executable=python_executable,
        environment=environment,
        timeout_seconds=timeout_override or COLLECTION_TIMEOUT_SECONDS,
        raw_output_directory=runtime_output_directory,
    )
    if any(collection.status == "INTERRUPTED" for collection in collections):
        return PreparedRuntimeGate(
            result=RuntimeGateResult(
                mode=mode,
                runtime=runtime,
                collections=collections,
                fixture_manifest={
                    "status": "NOT_STARTED",
                    "mode": "byte" if real_rom_scope else "schema",
                },
                matrix_audit={"status": "NOT_STARTED"},
                tiers=[],
                gate_problems=[*gate_problems, "runtime preflight interrupted"],
            ),
            environment=environment,
            required_problems=required_asset_problems(assets),
        )

    fixture_manifest = _entry.run_fixture_manifest_validation(
        project_root=project_root,
        python_executable=python_executable,
        environment=environment,
        fixture_root=fixture_root,
        validate_bytes=real_rom_scope,
        timeout_seconds=timeout_override or COLLECTION_TIMEOUT_SECONDS,
    )
    if fixture_manifest["status"] != "PASS":
        gate_problems.append(
            "fixture manifest validation failed: "
            f"mode={fixture_manifest['mode']} "
            f"{fixture_manifest.get('reason') or 'unknown error'}"
        )
    elif real_rom_scope:
        gate_problems.extend(
            _entry.fixture_manifest_input_problems(
                project_root / FIXTURE_MANIFEST_RELATIVE_PATH,
                rom_root=rom_root,
                assets=assets,
                expected_sha1=expected_sha1,
            )
        )
        gate_problems.extend(
            _entry.fixture_manifest_provenance_problems(
                project_root / FIXTURE_MANIFEST_RELATIVE_PATH
            )
        )

    matrix_audit = _entry.run_matrix_collection_audit(
        project_root=project_root,
        collections=collections,
    )
    if real_rom_scope:
        if not matrix_audit.get("structural_pass"):
            gate_problems.append("link matrix structural audit failed")
        if not matrix_audit.get("acceptance_matrix_complete"):
            gaps = matrix_audit.get("acceptance_gaps", {})
            gap_counts = ", ".join(
                f"{name}={len(entries)}"
                for name, entries in gaps.items()
                if isinstance(entries, (tuple, list))
            )
            gate_problems.append(
                "strict acceptance matrix declaration is incomplete"
                + (f" ({gap_counts})" if gap_counts else "")
            )
        gate_problems.extend(
            _matrix_execution_problems(
                matrix_audit=matrix_audit,
                required_nodeids_by_tier=required_nodeids_by_tier,
                selected=selected_tiers,
            )
        )

    return PreparedRuntimeGate(
        result=RuntimeGateResult(
            mode=mode,
            runtime=runtime,
            collections=collections,
            fixture_manifest=fixture_manifest,
            matrix_audit=matrix_audit,
            tiers=[],
            gate_problems=gate_problems,
        ),
        environment=environment,
        required_problems=required_asset_problems(assets),
    )


def run_prepared_tier(
    *,
    prepared: PreparedRuntimeGate,
    name: str,
    project_root: Path,
    python_executable: Path,
    required_tests_by_tier: dict[str, frozenset[tuple[str, str]]],
    required_nodeids_by_tier: dict[str, frozenset[str]],
    repeat: int = 5,
    timeout_override: float | None = None,
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
    raw_output_directory: Path | None = None,
    capacity_session: Any | None = None,
    scope: str = "",
) -> TierResult:
    """Execute one planned tier with the already verified runtime environment.

    A completed tier is written to the runtime result's durable
    ``completed_tiers`` mapping before this call returns.  A cancellation
    delivered between the return and the caller's bookkeeping used to leave a
    tier that really ran indistinguishable from a tier that never started, so
    the orchestrator synthesized an empty ``INTERRUPTED`` row and the loaded
    evidence was lost.  A per-call sink cannot fix that: it only lives in the
    frame that invoked this function, so a cancellation escaping *that* frame
    - or the dispatch helper built on top of it - left no reachable copy.  The
    mapping travels with ``prepared.result``, which is published to the
    caller's accumulator before any tier is dispatched, so the CLI's
    interrupted-result finalizer can recover the executed tier too.
    """

    mode = prepared.result.mode
    # Row identity is per runtime scope.  Deriving it from the prepared runtime
    # keeps every dispatch path (including the early-smoke branch, which used to
    # omit it) consistent with the cancellation and finalization paths that
    # already account rows under the runtime mode.
    scope = scope or mode
    runtime_output_directory = raw_output_directory / mode if raw_output_directory else None
    tier: TierResult | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=f"pokered-gate-{mode}-{name}-") as directory:
            tier = _entry.run_tier(
                name=name,
                project_root=project_root,
                python_executable=python_executable,
                environment=prepared.environment,
                required_problems=prepared.required_problems,
                repeat=repeat,
                timeout_override=timeout_override,
                report_directory=Path(directory),
                required_test_keys=required_tests_by_tier.get(name, ()),
                required_nodeids=required_nodeids_by_tier.get(name, ()),
                matrix_workers=matrix_workers,
                matrix_timeout_override=matrix_timeout_override,
                raw_output_directory=runtime_output_directory,
                capacity_session=capacity_session,
                collections=prepared.result.collections,
                scope=scope,
                publish=_completed_tier_sink(prepared),
            )
    except TierInterruptedDuringCleanup:
        # The tier supervisor already captured the completed result; keep it.
        raise
    except KeyboardInterrupt:
        # The return value only exists once ``run_tier`` returned, so a
        # cancellation raised while unwinding the scratch directory would lose
        # it.  Carry the finished tier out instead of reporting it unstarted.
        if tier is None:
            raise
        raise TierInterruptedDuringCleanup(
            tier,
            f"{name} completed but its scratch-directory cleanup was interrupted",
        ) from None
    finally:
        # Runs before this frame returns, even when a signal raises out of the
        # ``return`` below, so a completed tier is never left reachable only
        # from this frame.
        if tier is not None:
            prepared.result.completed_tiers[tier.name] = tier
    return tier


def _unrun_tier(
    name: str,
    reason: str,
    required_nodeids: Iterable[str] = (),
    *,
    status: str = "NOT_STARTED",
    capacity_session: Any | None = None,
    collections: Iterable[CollectionResult] = (),
    project_root: Path | None = None,
    scope: str = "",
) -> TierResult:
    if status in {"BLOCKED", "NOT_STARTED", "INTERRUPTED"} and capacity_session is not None:
        # A row blocked before dispatch must not disappear from capacity
        # accounting: record each planned row and its terminal block decision
        # here, where every pre-dispatch BLOCKED/cancelled/interrupted path
        # converges.  Cancellation terminalizes the row the same way an expiry
        # does, so a queued waiter is never promoted after the run stopped.
        register_blocked_rows(
            capacity_session,
            name=name,
            collections=collections,
            project_root=project_root if project_root is not None else Path("."),
            reason=reason,
            required_nodeids=required_nodeids,
            status=status,
            scope=scope,
        )
    nodes = sorted(set(required_nodeids) | (SMOKE_REQUIRED_NODEIDS if name == "smoke" else set()))
    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=True,
        status=status,
        reason=reason,
        selected_nodeids=nodes,
        case_results=[
            MatrixCaseResult(
                nodeid=node, status=status, returncode=None, duration_seconds=0.0, reason=reason
            )
            for node in nodes
        ],
    )


def _preflight_reason(prepared: PreparedRuntimeGate) -> str:
    result = prepared.result
    reasons = list(result.gate_problems)
    reasons.extend(
        collection.reason or f"{collection.name} collection {collection.status}"
        for collection in result.collections
        if collection.status != "PASS"
    )
    if not result.collections:
        reasons.append("no collection result")
    return "; ".join(reasons)


def _unstarted_preparation(
    mode: str, reason: str, *, interrupted: bool = False
) -> PreparedRuntimeGate:
    return PreparedRuntimeGate(
        result=RuntimeGateResult(
            mode=mode,
            runtime={"pyboy_mode": "not-run"},
            collections=[
                CollectionResult(
                    name="preflight",
                    command=[],
                    status="INTERRUPTED" if interrupted else "NOT_STARTED",
                    returncode=130 if interrupted else None,
                    reason=reason,
                )
            ],
            fixture_manifest={"status": "NOT_STARTED", "mode": "not-run"},
            matrix_audit={"status": "NOT_STARTED"},
            tiers=[],
            gate_problems=[reason],
        ),
        environment={},
        required_problems=[],
    )


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.production_gate as _entry
