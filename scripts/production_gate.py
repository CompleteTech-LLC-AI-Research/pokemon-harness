#!/usr/bin/env python3
"""Run the Pokémon harness production acceptance gate.

The gate deliberately runs pytest in separate subprocesses.  This keeps ROM
state, emulator globals, and network listeners isolated between tiers and
makes the reported counts independent of pytest's in-process plugin state.

Examples::

    python scripts/production_gate.py
    python scripts/production_gate.py --unit-only
    python scripts/production_gate.py --tier remote --repeat-timing 5
    python scripts/production_gate.py --runtime-mode both --unit-only
    python scripts/production_gate.py --runtime-mode both --python .venv/bin/python \
        --cython-python .venv-cython/bin/python --unit-only
    python scripts/production_gate.py --unit-only --evidence-dir /tmp/pokered-evidence

The default command is strict for every selected tier, including the
stateful trade and battle acceptance cases. Missing ROMs or derived fixtures
are reported as blocked rather than converted into a green skip.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib  # noqa: F401  (retained facade attribute: gate.hashlib)
import json
import os  # noqa: F401  (retained facade attribute: gate.os)
import re  # noqa: F401  (retained facade attribute: gate.re)
import runpy  # noqa: F401  (retained facade attribute: gate.runpy)
import signal  # noqa: F401  (retained facade attribute: gate.signal)
import subprocess  # noqa: F401  (retained facade attribute: gate.subprocess)
import sys
import tempfile  # noqa: F401  (retained facade attribute: gate.tempfile)
import threading  # noqa: F401  (retained facade attribute: gate.threading)
import time  # noqa: F401  (retained facade attribute: gate.time)
from collections import (  # noqa: F401  (retained facade attributes: gate.Counter, gate.deque)
    Counter,
    deque,
)
from collections.abc import (  # noqa: F401  (retained facade attribute: gate.Iterable)
    Iterable,
    Sequence,
)
from dataclasses import (  # noqa: F401  (retained facade attributes: gate.dataclass, gate.field, gate.replace)
    asdict,
    dataclass,
    field,
    replace,
)
from datetime import (  # noqa: F401  (retained facade attributes: gate.UTC, gate.datetime)
    UTC,
    datetime,
)
from pathlib import (  # noqa: F401  (retained facade attribute: gate.PureWindowsPath)
    Path,
    PureWindowsPath,
)
from typing import Any  # noqa: F401  (retained facade attribute: gate.Any)

try:
    from scripts import gate_capacity
except ImportError:  # pragma: no cover - executed as ``python scripts/production_gate.py``.
    import gate_capacity  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Loaded by path (for example ``pokered_production_gate``) as well as by package
# name; register the loaded module under its canonical name first so the support
# modules' ``import scripts.production_gate as _entry`` resolves to this same
# object instead of a second copy.
sys.modules.setdefault("scripts.production_gate", sys.modules[__name__])


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=project_root_from_script())
    parser.add_argument("--rom-root", type=Path)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument(
        "--capacity-policy",
        type=Path,
        help=(
            "path to a versioned JSON capacity policy; when omitted the report "
            "records capacity_policy: unavailable and gate behavior is unchanged"
        ),
    )
    parser.add_argument(
        "--python", dest="python_executable", type=Path, default=Path(sys.executable)
    )
    parser.add_argument(
        "--cython-python",
        dest="cython_python_executable",
        type=Path,
        help=("interpreter for the Cython runtime when --runtime-mode both; defaults to --python"),
    )
    parser.add_argument(
        "--tier",
        action="append",
        choices=tuple(TIER_EXPRESSIONS),
        help="run only this tier; repeat the option to select multiple tiers",
    )
    parser.add_argument(
        "--unit-only",
        action="store_true",
        help="run the ROM-free unit tier and its fivefold timing regression",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help=(
            "run runtime/import checks, public MCP lifecycle and nine timed ROM "
            "orientations only; requires pinned assets and does not qualify gameplay"
        ),
    )
    stopping = parser.add_mutually_exclusive_group()
    stopping.add_argument(
        "--fail-fast",
        dest="fail_fast",
        action="store_true",
        default=None,
        help="stop dispatching long tiers after a failure (default for the complete gate)",
    )
    stopping.add_argument(
        "--keep-going",
        dest="fail_fast",
        action="store_false",
        help="continue selected tiers after failures, retaining every failure",
    )
    parser.add_argument(
        "--repeat-timing",
        type=int,
        default=5,
        help="number of timing-tier repetitions (minimum 5; default: 5)",
    )
    parser.add_argument(
        "--matrix-workers",
        type=int,
        default=DEFAULT_MATRIX_WORKERS,
        help=(
            "parallel subprocess workers for strict trade/battle matrix rows "
            f"(default: {DEFAULT_MATRIX_WORKERS})"
        ),
    )
    parser.add_argument(
        "--runtime-mode",
        type=normalize_runtime_mode,
        choices=RUNTIME_MODE_CHOICES,
        default="source",
        help=(
            "PyBoy runtime to require for every selected tier: source uses the "
            "vendored Python modules; cython uses installed extension modules; "
            "both runs every selected tier under each explicit runtime "
            "(dual is accepted as an alias) (default: source)"
        ),
    )
    parser.add_argument(
        "--matrix-timeout-seconds",
        type=float,
        help=(
            "hard aggregate timeout for each strict trade/battle matrix tier; "
            "otherwise it is derived from row timeout and worker count"
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="override the per-run timeout for every selected tier",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help=(
            "write a sanitized gate-report.json, gate-report.txt, and "
            "evidence-manifest.json to this directory"
        ),
    )
    parser.add_argument(
        "--raw-output-dir",
        type=Path,
        help=(
            "retain complete, unredacted pytest stdout/stderr in a new private directory; "
            "must be separate from --evidence-dir"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repeat_timing < 5:
        parser.error("--repeat-timing must be at least 5")
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.matrix_workers <= 0:
        parser.error("--matrix-workers must be positive")
    if args.matrix_timeout_seconds is not None and args.matrix_timeout_seconds <= 0:
        parser.error("--matrix-timeout-seconds must be positive")
    if args.unit_only and args.tier:
        parser.error("--unit-only cannot be combined with --tier")
    if args.smoke_only and (args.unit_only or args.tier):
        parser.error("--smoke-only cannot be combined with --unit-only or --tier")
    if args.cython_python_executable is not None and args.runtime_mode != "both":
        parser.error("--cython-python requires --runtime-mode both")

    raw_output_directory = None
    if args.raw_output_dir is not None:
        raw_output_directory = args.raw_output_dir.expanduser().resolve()
        if args.evidence_dir is not None:
            evidence_directory = args.evidence_dir.expanduser().resolve()
            if raw_output_directory.is_relative_to(evidence_directory):
                parser.error("--raw-output-dir must be outside --evidence-dir")
        try:
            raw_output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        except OSError as exc:
            parser.error(
                f"--raw-output-dir requires a new writable directory: {type(exc).__name__}"
            )

    project_root = args.repo_root.expanduser().resolve()
    # Do not call ``resolve()`` here: POSIX virtualenv interpreters are often
    # symlinks to the system interpreter, and resolving would silently drop
    # the environment containing pytest/PyBoy.
    python_executable = _python_path_from_argument(args.python_executable, project_root)
    cython_python_executable = (
        _python_path_from_argument(args.cython_python_executable, project_root)
        if args.cython_python_executable is not None
        else None
    )
    rom_root = find_rom_root(project_root, args.rom_root)
    fixture_root = find_fixture_root(project_root, args.fixture_root)
    expected_sha1 = parse_expected_sha1(project_root / "VERSIONS.md")
    assets = inspect_assets(rom_root, fixture_root, expected_sha1)
    required_tests_by_tier, tier_config_error = load_required_test_keys(project_root)
    configuration_problems: list[str] = []
    if tier_config_error:
        configuration_problems.append(tier_config_error)
    required_nodeids_by_tier, nodeid_config_error = load_required_nodeids(project_root)
    if nodeid_config_error:
        configuration_problems.append(nodeid_config_error)

    capacity_session = None
    capacity_payload = gate_capacity.unavailable_capacity(
        "no --capacity-policy supplied; capacity admission is not enforced"
    )
    if args.capacity_policy is not None:
        capacity_path = _path_from_project_root(project_root, args.capacity_policy)
        policy, policy_error = gate_capacity.load_capacity_policy(capacity_path)
        if policy is None:
            capacity_payload = gate_capacity.unavailable_capacity(
                f"capacity policy blocked: {policy_error}"
            )
            capacity_payload["status"] = "blocked"
            configuration_problems.append(f"capacity policy blocked: {policy_error}")
        else:
            capacity_session = gate_capacity.CapacitySession(policy, repo_root=project_root)

    early_smoke = not (args.unit_only or args.tier or args.smoke_only)
    fail_fast = early_smoke if args.fail_fast is None else args.fail_fast
    if args.smoke_only:
        selected = ["smoke"]
    elif args.unit_only:
        selected = ["unit", "timing"]
    elif args.tier:
        selected = list(dict.fromkeys(args.tier))
    else:
        selected = list(DEFAULT_TIERS)
    accumulated_runtime_results: list[RuntimeGateResult] = []
    try:
        runtime_results = run_runtime_gates(
            runtime_mode=args.runtime_mode,
            project_root=project_root,
            python_executable=python_executable,
            cython_python_executable=cython_python_executable,
            rom_root=rom_root,
            fixture_root=fixture_root,
            expected_sha1=expected_sha1,
            assets=assets,
            selected=selected,
            required_tests_by_tier=required_tests_by_tier,
            required_nodeids_by_tier=required_nodeids_by_tier,
            configuration_problems=configuration_problems,
            repeat=args.repeat_timing,
            timeout_override=args.timeout_seconds,
            matrix_workers=args.matrix_workers,
            matrix_timeout_override=args.matrix_timeout_seconds,
            raw_output_directory=raw_output_directory,
            early_smoke=early_smoke,
            fail_fast=fail_fast,
            capacity_session=capacity_session,
            accumulator=accumulated_runtime_results,
        )
    except KeyboardInterrupt:
        # An operator SIGINT must still produce the evidence bundle, but it must
        # never erase work that already ran.  Results accumulated before the
        # interrupt keep their own counts, diagnostics, runtime identity, and
        # rows; only genuinely unfinished tiers are finalized as interrupted.
        interrupt_reason = "gate cancelled by an interrupt"
        interrupt_plan = build_execution_plan(
            runtime_modes_for_gate(args.runtime_mode),
            selected,
            early_smoke=early_smoke,
            fail_fast=fail_fast,
        )
        if accumulated_runtime_results:
            runtime_results = _finalize_interrupted_results(
                accumulated=accumulated_runtime_results,
                modes=runtime_modes_for_gate(args.runtime_mode),
                plan=interrupt_plan,
                reason=interrupt_reason,
                required_nodeids_by_tier=required_nodeids_by_tier,
                capacity_session=capacity_session,
                project_root=project_root,
            )
        else:
            # Interrupted before the first runtime produced a result: record
            # every expected runtime as interrupted with its unrun tiers.
            interrupted_results = []
            for mode in runtime_modes_for_gate(args.runtime_mode):
                interrupted = _unstarted_preparation(
                    mode, interrupt_reason, interrupted=True
                ).result
                interrupted.execution_plan = interrupt_plan
                interrupted.cancellation = interrupt_reason
                interrupted.tiers = [
                    _unrun_tier(
                        step["tier"],
                        interrupt_reason,
                        required_nodeids_by_tier.get(step["tier"], ()),
                        status="INTERRUPTED",
                        capacity_session=capacity_session,
                        collections=interrupted.collections,
                        project_root=project_root,
                        scope=mode,
                    )
                    for step in interrupt_plan["steps"]
                    if step["mode"] == mode
                ]
                interrupted_results.append(interrupted)
            runtime_results = tuple(interrupted_results)

    expected_modes = runtime_modes_for_gate(args.runtime_mode)
    observed_modes = tuple(result.mode for result in runtime_results)
    if observed_modes != expected_modes:
        reason = (
            "runtime execution results do not match the requested modes: "
            f"expected={','.join(expected_modes)} observed={','.join(observed_modes) or 'none'}"
        )
        results = list(runtime_results)
        plan = build_execution_plan(
            expected_modes, selected, early_smoke=early_smoke, fail_fast=fail_fast
        )
        for mode in expected_modes:
            if mode not in observed_modes:
                missing = _unstarted_preparation(mode, reason).result
                missing.execution_plan = plan
                missing.tiers = [
                    _unrun_tier(
                        step["tier"],
                        reason,
                        required_nodeids_by_tier.get(step["tier"], ()),
                        capacity_session=capacity_session,
                        project_root=project_root,
                        scope=mode,
                    )
                    for step in plan["steps"]
                    if step["mode"] == mode
                ]
                results.append(missing)
        for result in results:
            if reason not in result.gate_problems:
                result.gate_problems.append(reason)
        runtime_results = tuple(
            sorted(
                results,
                key=lambda result: (
                    expected_modes.index(result.mode)
                    if result.mode in expected_modes
                    else len(expected_modes)
                ),
            )
        )
    dual_runtime = len(expected_modes) > 1 or len(runtime_results) > 1
    if not dual_runtime:
        result = runtime_results[0]
        runtime = result.runtime
        collections = result.collections
        fixture_manifest = result.fixture_manifest
        matrix_audit = result.matrix_audit
        tiers = result.tiers
        gate_problems = result.gate_problems
        overall = "PASS" if runtime_gate_passes(result) else "FAIL"
    else:
        runtime = {}
        collections = []
        fixture_manifest = {}
        matrix_audit = {}
        tiers = []
        gate_problems = [
            f"{result.mode}: {problem}"
            for result in runtime_results
            for problem in result.gate_problems
        ]
        overall = "PASS" if runtime_gates_pass(runtime_results) else "FAIL"
    if capacity_session is not None:
        if set(selected) & REQUIRED_TIER_ASSETS:
            with contextlib.suppress(Exception):
                capacity_session.ensure_started()
            failed = sum(
                tier.status == "FAIL" for result in runtime_results for tier in result.tiers
            )
            capacity_payload = capacity_session.report(failed=failed)
        else:
            # Asset-free selections dispatch no emulator pairs, so the capacity
            # section must not contradict a unit-only PASS with a blocked status.
            capacity_payload = capacity_session.not_applicable_report()
    if args.capacity_policy is not None and capacity_payload.get("status") not in {
        "ok",
        "not_applicable",
    }:
        overall = "FAIL"
        gate_problems.append(f"capacity prerequisite: {capacity_payload.get('status')}")
    evidence_error = ""
    if args.evidence_dir is not None:
        evidence_dir = args.evidence_dir.expanduser()
        if not evidence_dir.is_absolute():
            evidence_dir = (Path.cwd() / evidence_dir).resolve()
        if dual_runtime:
            evidence_payload = build_dual_evidence_payload(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                assets=assets,
                runtime_results=runtime_results,
                overall=overall,
                requested_mode="both",
                capacity=capacity_payload,
            )
        else:
            evidence_payload = build_evidence_payload(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                runtime=runtime,
                assets=assets,
                collections=collections,
                tiers=tiers,
                gate_problems=gate_problems,
                overall=overall,
                fixture_manifest=fixture_manifest,
                matrix_audit=matrix_audit,
                execution_plan=runtime_results[0].execution_plan,
                capacity=capacity_payload,
                cancellation=runtime_results[0].cancellation,
            )
        try:
            write_evidence_bundle(evidence_dir, evidence_payload)
        except (OSError, TypeError, ValueError) as exc:
            evidence_error = (
                f"could not write evidence bundle to {evidence_dir}: {type(exc).__name__}: {exc}"
            )
            gate_problems.append(evidence_error)
            overall = "FAIL"

    if args.format == "json":
        if dual_runtime:
            payload = {
                "project_root": str(project_root),
                "rom_root": str(rom_root),
                "fixture_root": str(fixture_root),
                "runtime_mode": "both",
                "runtimes": [_runtime_result_json(result) for result in runtime_results],
                "assets": [asdict(asset) for asset in assets],
                "gate_problems": gate_problems,
                "overall": overall,
                "capacity": capacity_payload,
            }
        else:
            payload = {
                "project_root": str(project_root),
                "rom_root": str(rom_root),
                "fixture_root": str(fixture_root),
                "runtime": runtime,
                "collections": [asdict(collection) for collection in collections],
                "assets": [asdict(asset) for asset in assets],
                "tiers": [_jsonable_tier(tier) for tier in tiers],
                "gate_problems": gate_problems,
                "cancellation": runtime_results[0].cancellation,
                "overall": overall,
                "fixture_manifest": fixture_manifest,
                "matrix_audit": matrix_audit,
                "capacity": capacity_payload,
                **(
                    {"execution_plan": runtime_results[0].execution_plan}
                    if runtime_results[0].execution_plan
                    else {}
                ),
            }
        if evidence_error:
            payload["evidence_error"] = evidence_error
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if dual_runtime:
            output = render_dual_text(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                assets=assets,
                runtime_results=runtime_results,
                overall=overall,
                capacity=capacity_payload,
            )
        else:
            output = render_text(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                runtime=runtime,
                assets=assets,
                collections=collections,
                tiers=tiers,
                gate_problems=gate_problems,
                overall=overall,
                fixture_manifest=fixture_manifest,
                matrix_audit=matrix_audit,
                execution_plan=runtime_results[0].execution_plan,
                capacity=capacity_payload,
                cancellation=runtime_results[0].cancellation,
            )
        print(output)
    if (
        any(tier.status == "INTERRUPTED" for result in runtime_results for tier in result.tiers)
        or any(result.cancellation for result in runtime_results)
        or any(
            collection.status == "INTERRUPTED"
            for result in runtime_results
            for collection in result.collections
        )
    ):
        return 130
    return 0 if overall == "PASS" else 1


# The split modules' names are re-exported verbatim so the gate's module surface
# (and every ``gate.<name>`` monkeypatch target the gate tests rely on) is unchanged.
from scripts.production_gate_assets import (
    _path_from_env,  # noqa: F401  (retained facade attribute: gate._path_from_env)
    _path_from_project_root,
    _python_path_from_argument,
    find_fixture_root,
    find_rom_root,
    inspect_assets,
    normalize_runtime_mode,
    parse_expected_pyboy_revision,  # noqa: F401  (retained facade attribute: gate.parse_expected_pyboy_revision)
    parse_expected_pyboy_version,  # noqa: F401  (retained facade attribute: gate.parse_expected_pyboy_version)
    parse_expected_sha1,
    required_asset_problems,  # noqa: F401  (retained facade attribute: gate.required_asset_problems)
    runtime_modes_for_gate,
    sha1_of_file,  # noqa: F401  (retained facade attribute: gate.sha1_of_file)
)
from scripts.production_gate_capacity import (
    CAPACITY_STREAM_FILENAME,  # noqa: F401  (retained facade attribute: gate.CAPACITY_STREAM_FILENAME)
    MAX_CAPACITY_SAMPLES,  # noqa: F401  (retained facade attribute: gate.MAX_CAPACITY_SAMPLES)
    TierInterruptedDuringCleanup,  # noqa: F401  (retained facade attribute: gate.TierInterruptedDuringCleanup)
    _capacity_execution,  # noqa: F401  (retained facade attribute: gate._capacity_execution)
    _capacity_text_lines,  # noqa: F401  (retained facade attribute: gate._capacity_text_lines)
    _capacity_tier_reason,  # noqa: F401  (retained facade attribute: gate._capacity_tier_reason)
    _completed_tier_sink,  # noqa: F401  (retained facade attribute: gate._completed_tier_sink)
    _defer_sigint_while_finalizing,  # noqa: F401  (retained facade attribute: gate._defer_sigint_while_finalizing)
    _DeferredSigintGuard,  # noqa: F401  (retained facade attribute: gate._DeferredSigintGuard)
    _dispatch_prepared_tier,  # noqa: F401  (retained facade attribute: gate._dispatch_prepared_tier)
    _finalize_interrupted_results,
    _record_completed_tier,  # noqa: F401  (retained facade attribute: gate._record_completed_tier)
    _recover_completed_tier,  # noqa: F401  (retained facade attribute: gate._recover_completed_tier)
    _safe_capacity,  # noqa: F401  (retained facade attribute: gate._safe_capacity)
    _safe_capacity_sample,  # noqa: F401  (retained facade attribute: gate._safe_capacity_sample)
    _tier_row_manifest,  # noqa: F401  (retained facade attribute: gate._tier_row_manifest)
    planned_tier_nodeids,  # noqa: F401  (retained facade attribute: gate.planned_tier_nodeids)
    register_blocked_rows,  # noqa: F401  (retained facade attribute: gate.register_blocked_rows)
)
from scripts.production_gate_evidence import (
    _evidence_roots,  # noqa: F401  (retained facade attribute: gate._evidence_roots)
    _portable_path,  # noqa: F401  (retained facade attribute: gate._portable_path)
    _prepare_root_replacements,  # noqa: F401  (retained facade attribute: gate._prepare_root_replacements)
    _safe_asset,  # noqa: F401  (retained facade attribute: gate._safe_asset)
    _safe_collection,  # noqa: F401  (retained facade attribute: gate._safe_collection)
    _safe_command,  # noqa: F401  (retained facade attribute: gate._safe_command)
    _safe_diagnostic,  # noqa: F401  (retained facade attribute: gate._safe_diagnostic)
    _safe_fixture_manifest,  # noqa: F401  (retained facade attribute: gate._safe_fixture_manifest)
    _safe_matrix_audit,  # noqa: F401  (retained facade attribute: gate._safe_matrix_audit)
    _safe_runtime,  # noqa: F401  (retained facade attribute: gate._safe_runtime)
    _safe_tier,  # noqa: F401  (retained facade attribute: gate._safe_tier)
    build_dual_evidence_payload,
    build_evidence_payload,
    verify_evidence_bundle,  # noqa: F401  (retained facade attribute: gate.verify_evidence_bundle)
    write_evidence_bundle,
)
from scripts.production_gate_execution import (
    _communicate_after_termination,  # noqa: F401  (retained facade attribute: gate._communicate_after_termination)
    _process_creation_kwargs,  # noqa: F401  (retained facade attribute: gate._process_creation_kwargs)
    _pytest_console_script,  # noqa: F401  (retained facade attribute: gate._pytest_console_script)
    _retain_raw_output,  # noqa: F401  (retained facade attribute: gate._retain_raw_output)
    _run_collection_command,  # noqa: F401  (retained facade attribute: gate._run_collection_command)
    _terminate_process,  # noqa: F401  (retained facade attribute: gate._terminate_process)
    fixture_manifest_input_problems,  # noqa: F401  (retained facade attribute: gate.fixture_manifest_input_problems)
    fixture_manifest_provenance_problems,  # noqa: F401  (retained facade attribute: gate.fixture_manifest_provenance_problems)
    run_collection_preflight,  # noqa: F401  (retained facade attribute: gate.run_collection_preflight)
    run_fixture_manifest_validation,  # noqa: F401  (retained facade attribute: gate.run_fixture_manifest_validation)
    run_pytest_once,  # noqa: F401  (retained facade attribute: gate.run_pytest_once)
)
from scripts.production_gate_matrix import (
    _add_counts,  # noqa: F401  (retained facade attribute: gate._add_counts)
    _drain_matrix_stream,  # noqa: F401  (retained facade attribute: gate._drain_matrix_stream)
    _kill_matrix_process,  # noqa: F401  (retained facade attribute: gate._kill_matrix_process)
    _matrix_execution_problems,  # noqa: F401  (retained facade attribute: gate._matrix_execution_problems)
    _matrix_report_path,  # noqa: F401  (retained facade attribute: gate._matrix_report_path)
    run_matrix_tier,  # noqa: F401  (retained facade attribute: gate.run_matrix_tier)
)
from scripts.production_gate_matrix_audit import (
    _audited_matrix_nodeids,  # noqa: F401  (retained facade attribute: gate._audited_matrix_nodeids)
    _required_nodeid_problems,  # noqa: F401  (retained facade attribute: gate._required_nodeid_problems)
    _required_test_problems,  # noqa: F401  (retained facade attribute: gate._required_test_problems)
    _test_key_from_nodeid,  # noqa: F401  (retained facade attribute: gate._test_key_from_nodeid)
    run_matrix_collection_audit,  # noqa: F401  (retained facade attribute: gate.run_matrix_collection_audit)
    synthetic_optional_skip,  # noqa: F401  (retained facade attribute: gate.synthetic_optional_skip)
)
from scripts.production_gate_model import (
    _ABSOLUTE_PATH_RE,  # noqa: F401  (retained facade attribute: gate._ABSOLUTE_PATH_RE)
    _BYTE_LITERAL_RE,  # noqa: F401  (retained facade attribute: gate._BYTE_LITERAL_RE)
    _CREDENTIAL_TEXT_RE,  # noqa: F401  (retained facade attribute: gate._CREDENTIAL_TEXT_RE)
    _EVIDENCE_OMITTED,  # noqa: F401  (retained facade attribute: gate._EVIDENCE_OMITTED)
    _LONG_TOKEN_RE,  # noqa: F401  (retained facade attribute: gate._LONG_TOKEN_RE)
    _PATH_RE,  # noqa: F401  (retained facade attribute: gate._PATH_RE)
    _PYBOY_RE,  # noqa: F401  (retained facade attribute: gate._PYBOY_RE)
    _QUOTED_ABSOLUTE_PATH_RE,  # noqa: F401  (retained facade attribute: gate._QUOTED_ABSOLUTE_PATH_RE)
    _SHA1_RE,  # noqa: F401  (retained facade attribute: gate._SHA1_RE)
    _SPACED_ABSOLUTE_PATH_RE,  # noqa: F401  (retained facade attribute: gate._SPACED_ABSOLUTE_PATH_RE)
    _SYMBOL_PATH_RE,  # noqa: F401  (retained facade attribute: gate._SYMBOL_PATH_RE)
    _SYMBOL_SHA1_RE,  # noqa: F401  (retained facade attribute: gate._SYMBOL_SHA1_RE)
    _URI_CREDENTIAL_RE,  # noqa: F401  (retained facade attribute: gate._URI_CREDENTIAL_RE)
    _WINDOWS_ABSOLUTE_PATH_RE,  # noqa: F401  (retained facade attribute: gate._WINDOWS_ABSOLUTE_PATH_RE)
    CERTIFIED_FIXTURE_IDS,  # noqa: F401  (retained facade attribute: gate.CERTIFIED_FIXTURE_IDS)
    COLLECTION_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: gate.COLLECTION_TIMEOUT_SECONDS)
    CYTHON_RUNTIME_MODULES,  # noqa: F401  (retained facade attribute: gate.CYTHON_RUNTIME_MODULES)
    DEFAULT_MATRIX_WORKERS,
    DEFAULT_TIERS,
    DEFAULT_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: gate.DEFAULT_TIMEOUT_SECONDS)
    EVIDENCE_MANIFEST_FILENAME,  # noqa: F401  (retained facade attribute: gate.EVIDENCE_MANIFEST_FILENAME)
    EVIDENCE_REPORT_FILENAME,  # noqa: F401  (retained facade attribute: gate.EVIDENCE_REPORT_FILENAME)
    EVIDENCE_SCHEMA_VERSION,  # noqa: F401  (retained facade attribute: gate.EVIDENCE_SCHEMA_VERSION)
    EVIDENCE_TEXT_FILENAME,  # noqa: F401  (retained facade attribute: gate.EVIDENCE_TEXT_FILENAME)
    FIXTURE_MANIFEST_RELATIVE_PATH,  # noqa: F401  (retained facade attribute: gate.FIXTURE_MANIFEST_RELATIVE_PATH)
    GATE_CONTROLLED_ENVIRONMENT,  # noqa: F401  (retained facade attribute: gate.GATE_CONTROLLED_ENVIRONMENT)
    KNOWN_ROM_FILES,  # noqa: F401  (retained facade attribute: gate.KNOWN_ROM_FILES)
    KNOWN_SYMBOL_FILES,  # noqa: F401  (retained facade attribute: gate.KNOWN_SYMBOL_FILES)
    MATRIX_AGGREGATE_GRACE_SECONDS,  # noqa: F401  (retained facade attribute: gate.MATRIX_AGGREGATE_GRACE_SECONDS)
    MATRIX_CASE_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: gate.MATRIX_CASE_TIMEOUT_SECONDS)
    MATRIX_CLEANUP_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: gate.MATRIX_CLEANUP_TIMEOUT_SECONDS)
    MATRIX_READER_JOIN_TIMEOUT_SECONDS,  # noqa: F401  (retained facade attribute: gate.MATRIX_READER_JOIN_TIMEOUT_SECONDS)
    MAX_FAILED_OUTPUT_CHARS,  # noqa: F401  (retained facade attribute: gate.MAX_FAILED_OUTPUT_CHARS)
    MAX_FAILURE_DETAIL_CHARS,  # noqa: F401  (retained facade attribute: gate.MAX_FAILURE_DETAIL_CHARS)
    MAX_FAILURE_DETAILS,  # noqa: F401  (retained facade attribute: gate.MAX_FAILURE_DETAILS)
    MAX_FAILURE_DETAILS_CHARS,  # noqa: F401  (retained facade attribute: gate.MAX_FAILURE_DETAILS_CHARS)
    MAX_FAILURE_NODEID_CHARS,  # noqa: F401  (retained facade attribute: gate.MAX_FAILURE_NODEID_CHARS)
    MAX_ITERATION_FAILURE_CHARS,  # noqa: F401  (retained facade attribute: gate.MAX_ITERATION_FAILURE_CHARS)
    MAX_ITERATION_FAILURES,  # noqa: F401  (retained facade attribute: gate.MAX_ITERATION_FAILURES)
    OPTIONAL_TIERS,  # noqa: F401  (retained facade attribute: gate.OPTIONAL_TIERS)
    PYBOY_RUNTIME_MODULES,  # noqa: F401  (retained facade attribute: gate.PYBOY_RUNTIME_MODULES)
    PYTEST_GATE_ARGUMENTS,  # noqa: F401  (retained facade attribute: gate.PYTEST_GATE_ARGUMENTS)
    REQUIRED_FIXTURES,  # noqa: F401  (retained facade attribute: gate.REQUIRED_FIXTURES)
    REQUIRED_TIER_ASSETS,
    RUNTIME_MODE_ALIASES,  # noqa: F401  (retained facade attribute: gate.RUNTIME_MODE_ALIASES)
    RUNTIME_MODE_CHOICES,
    RUNTIME_MODES,  # noqa: F401  (retained facade attribute: gate.RUNTIME_MODES)
    SMOKE_REQUIRED_NODEIDS,  # noqa: F401  (retained facade attribute: gate.SMOKE_REQUIRED_NODEIDS)
    SMOKE_REQUIRED_TESTS,  # noqa: F401  (retained facade attribute: gate.SMOKE_REQUIRED_TESTS)
    SMOKE_SELECTORS,  # noqa: F401  (retained facade attribute: gate.SMOKE_SELECTORS)
    TIER_DESCRIPTIONS,  # noqa: F401  (retained facade attribute: gate.TIER_DESCRIPTIONS)
    TIER_EXPRESSIONS,
    AssetRecord,  # noqa: F401  (retained facade attribute: gate.AssetRecord)
    CollectionResult,  # noqa: F401  (retained facade attribute: gate.CollectionResult)
    Counts,  # noqa: F401  (retained facade attribute: gate.Counts)
    FailureDetail,  # noqa: F401  (retained facade attribute: gate.FailureDetail)
    GateReport,  # noqa: F401  (retained facade attribute: gate.GateReport)
    MatrixCaseResult,  # noqa: F401  (retained facade attribute: gate.MatrixCaseResult)
    PreparedRuntimeGate,  # noqa: F401  (retained facade attribute: gate.PreparedRuntimeGate)
    RuntimeGateResult,
    TierResult,  # noqa: F401  (retained facade attribute: gate.TierResult)
    _asset_key,  # noqa: F401  (retained facade attribute: gate._asset_key)
    _execution_plan_lines,  # noqa: F401  (retained facade attribute: gate._execution_plan_lines)
    _normalize_nodeid,  # noqa: F401  (retained facade attribute: gate._normalize_nodeid)
    _safe_execution_plan,  # noqa: F401  (retained facade attribute: gate._safe_execution_plan)
    _valid_execution_plan,  # noqa: F401  (retained facade attribute: gate._valid_execution_plan)
    build_execution_plan,
)
from scripts.production_gate_render import (
    _evidence_file_metadata,  # noqa: F401  (retained facade attribute: gate._evidence_file_metadata)
    _render_dual_evidence_text,  # noqa: F401  (retained facade attribute: gate._render_dual_evidence_text)
    _runtime_result_json,
    render_dual_text,
    render_evidence_text,  # noqa: F401  (retained facade attribute: gate.render_evidence_text)
    render_text,
)
from scripts.production_gate_runtime import (
    _asset_record_for_path,  # noqa: F401  (retained facade attribute: gate._asset_record_for_path)
    _load_gate_report,  # noqa: F401  (retained facade attribute: gate._load_gate_report)
    _reason_counter,  # noqa: F401  (retained facade attribute: gate._reason_counter)
    _resolve_child_path,  # noqa: F401  (retained facade attribute: gate._resolve_child_path)
    _runtime_identity_problems,  # noqa: F401  (retained facade attribute: gate._runtime_identity_problems)
    build_test_environment,  # noqa: F401  (retained facade attribute: gate.build_test_environment)
    environment_policy_problems,  # noqa: F401  (retained facade attribute: gate.environment_policy_problems)
    load_gate_report,  # noqa: F401  (retained facade attribute: gate.load_gate_report)
    load_required_nodeids,
    load_required_test_keys,
    probe_runtime,  # noqa: F401  (retained facade attribute: gate.probe_runtime)
    runtime_problems,  # noqa: F401  (retained facade attribute: gate.runtime_problems)
)
from scripts.production_gate_runtime_gates import (
    run_runtime_gate,  # noqa: F401  (retained facade attribute: gate.run_runtime_gate)
    run_runtime_gates,
    runtime_gate_passes,
    runtime_gates_pass,
)
from scripts.production_gate_text import (
    _bounded_failure_text,  # noqa: F401  (retained facade attribute: gate._bounded_failure_text)
    _failure_detail_lines,  # noqa: F401  (retained facade attribute: gate._failure_detail_lines)
    _failure_excerpt,  # noqa: F401  (retained facade attribute: gate._failure_excerpt)
    _format_counts,  # noqa: F401  (retained facade attribute: gate._format_counts)
    _jsonable_tier,
    _payload_counts_text,  # noqa: F401  (retained facade attribute: gate._payload_counts_text)
    _retain_failure_detail,  # noqa: F401  (retained facade attribute: gate._retain_failure_detail)
    _safe_text,  # noqa: F401  (retained facade attribute: gate._safe_text)
)
from scripts.production_gate_tiers import (
    _optional_preflight_reason,  # noqa: F401  (retained facade attribute: gate._optional_preflight_reason)
    _preflight_reason,  # noqa: F401  (retained facade attribute: gate._preflight_reason)
    _unrun_tier,
    _unstarted_preparation,
    prepare_runtime_gate,  # noqa: F401  (retained facade attribute: gate.prepare_runtime_gate)
    run_prepared_tier,  # noqa: F401  (retained facade attribute: gate.run_prepared_tier)
    run_tier,  # noqa: F401  (retained facade attribute: gate.run_tier)
)

if __name__ == "__main__":
    raise SystemExit(main())
