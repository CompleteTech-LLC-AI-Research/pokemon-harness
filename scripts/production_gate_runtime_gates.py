"""Runtime gate orchestration split out of ``production_gate_tiers``.

Moved verbatim for the #86 capacity admission feature (#115) so the tier module
stays inside the repository's 1000-line file split bound (#122/#124).  Calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_assets import runtime_modes_for_gate
from scripts.production_gate_capacity import (
    _capacity_tier_reason,
    _dispatch_prepared_tier,
    _record_completed_tier,
    _recover_completed_tier,
)
from scripts.production_gate_matrix_audit import synthetic_optional_skip
from scripts.production_gate_model import (
    DEFAULT_MATRIX_WORKERS,
    AssetRecord,
    PreparedRuntimeGate,
    RuntimeGateResult,
    _valid_execution_plan,
    build_execution_plan,
)
from scripts.production_gate_tiers import (
    _optional_preflight_reason,
    _preflight_reason,
    _unrun_tier,
    _unstarted_preparation,
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
    capacity_session: Any | None = None,
    accumulator: list[RuntimeGateResult] | None = None,
) -> RuntimeGateResult:
    """Run a selected single-runtime scope, retaining its existing tier order.

    The mutable runtime result is published to ``accumulator`` as soon as the
    runtime is prepared, before any tier is dispatched.  A cancellation between
    tiers therefore leaves the completed tiers, counts, diagnostics, and the
    observed runtime identity reachable to the caller instead of being rebuilt
    from scratch as an unstarted run.
    """

    try:
        prepared = _entry.prepare_runtime_gate(
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
    if accumulator is not None and not any(item is prepared.result for item in accumulator):
        # Publish before dispatch: later tiers mutate this same object, so the
        # caller always observes the progress that really happened.
        accumulator.append(prepared.result)
    for name in selected:
        if stop_reason:
            prepared.result.tiers.append(
                _unrun_tier(
                    name,
                    stop_reason,
                    required_nodeids_by_tier.get(name, ()),
                    capacity_session=capacity_session,
                    collections=prepared.result.collections,
                    project_root=project_root,
                    scope=mode,
                )
            )
            continue
        try:
            capacity_reason = _capacity_tier_reason(capacity_session, name)
            if capacity_reason:
                prepared.result.tiers.append(
                    _unrun_tier(
                        name,
                        capacity_reason,
                        required_nodeids_by_tier.get(name, ()),
                        status="BLOCKED",
                        capacity_session=capacity_session,
                        collections=prepared.result.collections,
                        project_root=project_root,
                        scope=mode,
                    )
                )
                continue
            if name in _entry.OPTIONAL_TIERS:
                reason = _optional_preflight_reason(name, assets)
                if reason:
                    prepared.result.tiers.append(synthetic_optional_skip(name, reason))
                    continue
            tier, cancellation = _dispatch_prepared_tier(
                prepared=prepared,
                name=name,
                python_executable=python_executable,
                arguments={
                    "project_root": project_root,
                    "required_tests_by_tier": required_tests_by_tier,
                    "required_nodeids_by_tier": required_nodeids_by_tier,
                    "repeat": repeat,
                    "timeout_override": timeout_override,
                    "matrix_workers": matrix_workers,
                    "matrix_timeout_override": matrix_timeout_override,
                    "raw_output_directory": raw_output_directory,
                    "capacity_session": capacity_session,
                    "scope": mode,
                },
            )
        except KeyboardInterrupt:
            # Nothing about this tier completed, so its rows are recorded as an
            # unstarted cancellation and dispatch stops.  A tier that did
            # complete - even if the interrupt arrived after its evidence was
            # loaded - comes back through ``_dispatch_prepared_tier`` instead of
            # this branch, so no executed evidence is replaced by an empty row.
            completed = _recover_completed_tier(prepared, name)
            if completed is not None:
                # The cancellation landed between the dispatch helper's return
                # and this loop's bookkeeping.  The tier really ran: keep its
                # counts, diagnostics, output, and rows, and record only the
                # cancellation on the run.
                reason = (
                    f"{mode} {name} completed but the run was cancelled "
                    "before its result was recorded"
                )
                _record_completed_tier(prepared, completed, reason)
                stop_reason = f"not started after {mode} {name} INTERRUPTED"
                continue
            prepared.result.tiers.append(
                _unrun_tier(
                    name,
                    f"{mode} {name} was cancelled by an interrupt",
                    required_nodeids_by_tier.get(name, ()),
                    status="INTERRUPTED",
                    capacity_session=capacity_session,
                    collections=prepared.result.collections,
                    project_root=project_root,
                    scope=mode,
                )
            )
            stop_reason = f"not started after {mode} {name} INTERRUPTED"
            continue
        prepared.result.tiers.append(tier)
        if cancellation:
            # The tier ran to completion and its evidence was loaded; only its
            # observer/scratch teardown was cancelled.  Keep the executed tier
            # and its rows, and record the cancellation on the run so it can
            # never be reported as a clean PASS.  Synthesizing an unrun row here
            # is what dropped the loaded failures and double-counted the row.
            prepared.result.cancellation = cancellation
            stop_reason = f"not started after {mode} {name} INTERRUPTED"
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
    capacity_session: Any | None = None,
    accumulator: list[RuntimeGateResult] | None = None,
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
        "capacity_session": capacity_session,
    }
    if not early_smoke and not fail_fast and selected != ("smoke",):
        results = []
        cancel_reason = ""
        for mode in modes:
            if cancel_reason:
                result = _unstarted_preparation(mode, cancel_reason).result
                result.cancellation = cancel_reason
                result.tiers = [
                    _unrun_tier(
                        name,
                        cancel_reason,
                        required_nodeids_by_tier.get(name, ()),
                        capacity_session=capacity_session,
                        collections=result.collections,
                        project_root=project_root,
                        # Rows skipped because an earlier runtime was cancelled
                        # are still this runtime's rows: register them under the
                        # dispatched scope so a cancellation during this
                        # accounting transitions the identity the plan opened
                        # instead of adding a second one under ("", tier).
                        scope=mode,
                    )
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
                    capacity_session=capacity_session,
                    accumulator=accumulator,
                )
                if (
                    any(tier.status == "INTERRUPTED" for tier in result.tiers)
                    or any(item.status == "INTERRUPTED" for item in result.collections)
                    or result.cancellation
                ):
                    cancel_reason = f"not started after {mode} was interrupted"
            result.execution_plan = plan
            results.append(result)
            if accumulator is not None and not any(item is result for item in accumulator):
                accumulator.append(result)
        return tuple(results)

    prepared_runtimes: list[PreparedRuntimeGate] = []
    stop_reason = ""
    cancel_reason = ""
    for mode in modes:
        if cancel_reason:
            prepared = _unstarted_preparation(mode, cancel_reason)
        else:
            try:
                prepared = _entry.prepare_runtime_gate(
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
        if accumulator is not None:
            accumulator.append(prepared.result)
        if early_smoke or selected == ("smoke",):
            reason = _preflight_reason(prepared)
            if reason:
                smoke = _unrun_tier(
                    "smoke",
                    reason,
                    status="NOT_STARTED" if cancel_reason else "BLOCKED",
                    capacity_session=capacity_session,
                    collections=prepared.result.collections,
                    project_root=project_root,
                    scope=mode,
                )
                smoke_cancellation = ""
            else:
                try:
                    capacity_reason = _capacity_tier_reason(capacity_session, "smoke")
                    if capacity_reason:
                        smoke = _unrun_tier(
                            "smoke",
                            capacity_reason,
                            status="BLOCKED",
                            capacity_session=capacity_session,
                            collections=prepared.result.collections,
                            project_root=project_root,
                            scope=mode,
                        )
                        smoke_cancellation = ""
                    else:
                        smoke, smoke_cancellation = _dispatch_prepared_tier(
                            prepared=prepared,
                            name="smoke",
                            python_executable=python_by_mode[mode],
                            arguments=execution_arguments,
                        )
                except KeyboardInterrupt:
                    # Cancellation during smoke admission/observation still has
                    # to account for the smoke rows and stop the plan.  The
                    # dispatch helper recovers a smoke that already loaded its
                    # evidence, but a signal can also land on that helper's own
                    # ``return`` and escape it, so consult the durable mapping
                    # here before declaring the smoke unstarted.
                    completed = _recover_completed_tier(prepared, "smoke")
                    if completed is not None:
                        smoke = completed
                        smoke_cancellation = (
                            f"{mode} smoke completed but the run was cancelled "
                            "before its result was recorded"
                        )
                    else:
                        smoke = _unrun_tier(
                            "smoke",
                            f"{mode} smoke was cancelled by an interrupt",
                            status="INTERRUPTED",
                            capacity_session=capacity_session,
                            collections=prepared.result.collections,
                            project_root=project_root,
                            scope=mode,
                        )
                        smoke_cancellation = ""
            prepared.result.tiers.append(smoke)
            if smoke_cancellation:
                # The smoke tier finished; only its teardown (or the return to
                # this frame) was cancelled.  Keep the executed smoke result and
                # its rows and record the cancellation instead of replacing it
                # with an unrun row.  Dispatch must still stop, because the run
                # was cancelled.
                prepared.result.cancellation = smoke_cancellation
                cancel_reason = stop_reason = f"not started after {mode} smoke INTERRUPTED"
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
                    status="NOT_STARTED" if stop_reason else "BLOCKED",
                    capacity_session=capacity_session,
                    collections=prepared.result.collections,
                    project_root=project_root,
                    scope=mode,
                )
                cancellation = ""
            else:
                # Admission is only meaningful for work that can actually run.
                # Checking cancellation/preflight first avoids spending the
                # admission deadline on tiers that cannot execute.
                try:
                    capacity_reason = _capacity_tier_reason(capacity_session, name)
                    if capacity_reason:
                        tier = _unrun_tier(
                            name,
                            capacity_reason,
                            required_nodeids_by_tier.get(name, ()),
                            status="BLOCKED",
                            capacity_session=capacity_session,
                            collections=prepared.result.collections,
                            project_root=project_root,
                            scope=mode,
                        )
                        cancellation = ""
                    else:
                        tier, cancellation = _dispatch_prepared_tier(
                            prepared=prepared,
                            name=name,
                            python_executable=python_by_mode[mode],
                            arguments=execution_arguments,
                        )
                except KeyboardInterrupt:
                    # Cancellation of this tier still accounts for its rows and
                    # stops the plan; a tier that completed, even if the
                    # interrupt arrived after its evidence was loaded, returns
                    # through ``_dispatch_prepared_tier`` with its executed
                    # evidence instead of reaching this unstarted branch.  A
                    # signal can still land on the dispatch helper's own
                    # ``return`` and escape it, so consult the durable mapping
                    # before declaring the tier unstarted.
                    completed = _recover_completed_tier(prepared, name)
                    if completed is not None:
                        tier = completed
                        cancellation = (
                            f"{mode} {name} completed but the run was cancelled "
                            "before its result was recorded"
                        )
                    else:
                        tier = _unrun_tier(
                            name,
                            f"{mode} {name} was cancelled by an interrupt",
                            required_nodeids_by_tier.get(name, ()),
                            status="INTERRUPTED",
                            capacity_session=capacity_session,
                            collections=prepared.result.collections,
                            project_root=project_root,
                            scope=mode,
                        )
                        cancellation = ""
            prepared.result.tiers.append(tier)
            if cancellation:
                # The tier finished; only its teardown (or the return to this
                # frame) was cancelled.  Keep the executed tier and its rows,
                # record the cancellation, and stop dispatching the remaining
                # tiers.
                prepared.result.cancellation = cancellation
                cancel_reason = stop_reason = f"not started after {mode} {name} INTERRUPTED"
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
        and not result.cancellation
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
