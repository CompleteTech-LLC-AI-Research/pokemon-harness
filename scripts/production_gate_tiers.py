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
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_assets import required_asset_problems, runtime_modes_for_gate
from scripts.production_gate_matrix import (
    _matrix_execution_problems,
    _required_nodeid_problems,
    _required_test_problems,
    run_matrix_tier,
    synthetic_optional_skip,
)
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
    _valid_execution_plan,
    build_execution_plan,
)
from scripts.production_gate_text import _bounded_failure_text, _retain_failure_detail


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
        )

    required = name not in _entry.OPTIONAL_TIERS
    if required and name in REQUIRED_TIER_ASSETS and required_problems:
        reason = "required assets unavailable: " + "; ".join(required_problems)
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=True,
            status="BLOCKED",
            reason=reason,
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
            problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
        if report.collection_skips:
            problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
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

    if omitted_failures:
        iteration_failures.append(f"[...{omitted_failures} additional failure entries omitted...]")
    if failed_output:
        output_tail = failed_output

    duration = time.monotonic() - started
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    if 130 in returncodes:
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

    return TierResult(
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
) -> TierResult:
    """Execute one planned tier with the already verified runtime environment."""

    mode = prepared.result.mode
    runtime_output_directory = raw_output_directory / mode if raw_output_directory else None
    with tempfile.TemporaryDirectory(prefix=f"pokered-gate-{mode}-{name}-") as directory:
        return _entry.run_tier(
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
        )


def _unrun_tier(
    name: str, reason: str, required_nodeids: Iterable[str] = (), *, status: str = "NOT_STARTED"
) -> TierResult:
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


def run_runtime_gate(
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
    repeat: int = 5,
    timeout_override: float | None = None,
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
    raw_output_directory: Path | None = None,
) -> RuntimeGateResult:
    """Run a selected single-runtime scope, retaining its existing tier order."""

    try:
        prepared = prepare_runtime_gate(
            mode=mode,
            project_root=project_root,
            python_executable=python_executable,
            rom_root=rom_root,
            fixture_root=fixture_root,
            expected_sha1=expected_sha1,
            assets=assets,
            selected=selected,
            required_tests_by_tier=required_tests_by_tier,
            required_nodeids_by_tier=required_nodeids_by_tier,
            configuration_problems=configuration_problems,
            timeout_override=timeout_override,
            raw_output_directory=raw_output_directory,
        )
    except KeyboardInterrupt:
        prepared = _unstarted_preparation(
            mode,
            f"{mode} preflight interrupted",
            interrupted=True,
        )
    stop_reason = (
        f"not started after {mode} preflight was interrupted"
        if any(item.status == "INTERRUPTED" for item in prepared.result.collections)
        else ""
    )
    for name in selected:
        if stop_reason:
            prepared.result.tiers.append(
                _unrun_tier(
                    name,
                    stop_reason,
                    required_nodeids_by_tier.get(name, ()),
                )
            )
            continue
        if name in _entry.OPTIONAL_TIERS:
            reason = _optional_preflight_reason(name, assets)
            if reason:
                prepared.result.tiers.append(synthetic_optional_skip(name, reason))
                continue
        prepared.result.tiers.append(
            run_prepared_tier(
                prepared=prepared,
                name=name,
                project_root=project_root,
                python_executable=python_executable,
                required_tests_by_tier=required_tests_by_tier,
                required_nodeids_by_tier=required_nodeids_by_tier,
                repeat=repeat,
                timeout_override=timeout_override,
                matrix_workers=matrix_workers,
                matrix_timeout_override=matrix_timeout_override,
                raw_output_directory=raw_output_directory,
            )
        )
        if prepared.result.tiers[-1].status == "INTERRUPTED":
            stop_reason = f"not started after {mode} {name} INTERRUPTED"
    return prepared.result


def run_runtime_gates(
    *,
    runtime_mode: str,
    project_root: Path,
    python_executable: Path,
    cython_python_executable: Path | None = None,
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
    assets: list[AssetRecord],
    selected: Sequence[str],
    required_tests_by_tier: dict[str, frozenset[tuple[str, str]]],
    required_nodeids_by_tier: dict[str, frozenset[str]],
    configuration_problems: Iterable[str] = (),
    repeat: int = 5,
    timeout_override: float | None = None,
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
    raw_output_directory: Path | None = None,
    early_smoke: bool = False,
    fail_fast: bool = False,
) -> tuple[RuntimeGateResult, ...]:
    """Execute one declared plan without borrowing results from another run.

    Full CLI gates probe both runtimes and run both additional smokes before
    long tiers. A smoke failure still permits the other runtime's smoke, so a
    source failure cannot hide native status. The original qualification rows
    remain required and run separately; smoke passes do not replace them.
    """

    modes = runtime_modes_for_gate(runtime_mode)
    selected = tuple(selected)
    plan = build_execution_plan(modes, selected, early_smoke=early_smoke, fail_fast=fail_fast)
    python_by_mode = {
        "source": python_executable,
        "cython": cython_python_executable or python_executable,
    }
    preparation_arguments = {
        "project_root": project_root,
        "rom_root": rom_root,
        "fixture_root": fixture_root,
        "expected_sha1": expected_sha1,
        "assets": assets,
        "selected": selected,
        "required_tests_by_tier": required_tests_by_tier,
        "required_nodeids_by_tier": required_nodeids_by_tier,
        "configuration_problems": tuple(configuration_problems),
        "timeout_override": timeout_override,
        "raw_output_directory": raw_output_directory,
    }
    execution_arguments = {
        "project_root": project_root,
        "required_tests_by_tier": required_tests_by_tier,
        "required_nodeids_by_tier": required_nodeids_by_tier,
        "repeat": repeat,
        "timeout_override": timeout_override,
        "matrix_workers": matrix_workers,
        "matrix_timeout_override": matrix_timeout_override,
        "raw_output_directory": raw_output_directory,
    }
    if not early_smoke and not fail_fast and selected != ("smoke",):
        results = []
        cancel_reason = ""
        for mode in modes:
            if cancel_reason:
                result = _unstarted_preparation(mode, cancel_reason).result
                result.tiers = [
                    _unrun_tier(name, cancel_reason, required_nodeids_by_tier.get(name, ()))
                    for name in selected
                ]
            else:
                result = _entry.run_runtime_gate(
                    mode=mode,
                    python_executable=python_by_mode[mode],
                    **preparation_arguments,
                    repeat=repeat,
                    matrix_workers=matrix_workers,
                    matrix_timeout_override=matrix_timeout_override,
                )
                if any(tier.status == "INTERRUPTED" for tier in result.tiers) or any(
                    item.status == "INTERRUPTED" for item in result.collections
                ):
                    cancel_reason = f"not started after {mode} was interrupted"
            result.execution_plan = plan
            results.append(result)
        return tuple(results)

    prepared_runtimes: list[PreparedRuntimeGate] = []
    stop_reason = ""
    cancel_reason = ""
    for mode in modes:
        if cancel_reason:
            prepared = _unstarted_preparation(mode, cancel_reason)
        else:
            try:
                prepared = prepare_runtime_gate(
                    mode=mode,
                    python_executable=python_by_mode[mode],
                    **preparation_arguments,
                )
            except KeyboardInterrupt:
                prepared = _unstarted_preparation(
                    mode,
                    f"{mode} preflight interrupted",
                    interrupted=True,
                )
        if any(item.status == "INTERRUPTED" for item in prepared.result.collections):
            cancel_reason = stop_reason = f"not started after {mode} preflight was interrupted"
        prepared.result.execution_plan = plan
        prepared_runtimes.append(prepared)
        if early_smoke or selected == ("smoke",):
            reason = _preflight_reason(prepared)
            smoke = (
                _unrun_tier("smoke", reason, status="NOT_STARTED" if cancel_reason else "BLOCKED")
                if reason
                else run_prepared_tier(
                    prepared=prepared,
                    name="smoke",
                    python_executable=python_by_mode[mode],
                    **execution_arguments,
                )
            )
            prepared.result.tiers.append(smoke)
            if smoke.status == "INTERRUPTED":
                cancel_reason = stop_reason = f"not started after {mode} smoke INTERRUPTED"
            if fail_fast and smoke.status != "PASS" and not stop_reason:
                stop_reason = f"not started after {mode} smoke {smoke.status}"

    if selected == ("smoke",):
        return tuple(prepared.result for prepared in prepared_runtimes)

    for prepared in prepared_runtimes:
        mode = prepared.result.mode
        preflight_reason = _preflight_reason(prepared)
        for name in selected:
            if stop_reason or preflight_reason:
                tier = _unrun_tier(
                    name,
                    stop_reason or f"{mode} preflight failed: {preflight_reason}",
                    required_nodeids_by_tier.get(name, ()),
                )
            else:
                tier = run_prepared_tier(
                    prepared=prepared,
                    name=name,
                    python_executable=python_by_mode[mode],
                    **execution_arguments,
                )
            prepared.result.tiers.append(tier)
            if tier.status == "INTERRUPTED":
                stop_reason = f"not started after {mode} {name} INTERRUPTED"
            if fail_fast and tier.status != "PASS" and not stop_reason:
                stop_reason = f"not started after {mode} {name} {tier.status}"
    return tuple(prepared.result for prepared in prepared_runtimes)


def runtime_gate_passes(result: RuntimeGateResult) -> bool:
    """Return whether one runtime result satisfies every existing gate rule."""

    if result.execution_plan:
        if not _valid_execution_plan(result.execution_plan):
            return False
        expected = [
            step["tier"] for step in result.execution_plan["steps"] if step["mode"] == result.mode
        ]
        if [tier.name for tier in result.tiers] != expected:
            return False
    tier_ok = all(
        tier.status == "PASS" if tier.required else tier.status in {"PASS", "SKIP"}
        for tier in result.tiers
    )
    return (
        result.runtime.get("pyboy_mode") == result.mode
        and bool(result.collections)
        and bool(result.tiers)
        and not result.gate_problems
        and all(collection.status == "PASS" for collection in result.collections)
        and tier_ok
    )


def runtime_gates_pass(results: Iterable[RuntimeGateResult]) -> bool:
    """Aggregate runtime results fail-closed, including an empty result set."""

    result_list = tuple(results)
    plans = [result.execution_plan for result in result_list if result.execution_plan]
    if plans and (
        len(plans) != len(result_list)
        or any(not _valid_execution_plan(plan) for plan in plans)
        or any(plan != plans[0] for plan in plans)
        or [result.mode for result in result_list] != plans[0].get("runtime_modes")
    ):
        return False
    return bool(result_list) and all(runtime_gate_passes(result) for result in result_list)


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.production_gate as _entry
