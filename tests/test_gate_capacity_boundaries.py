"""Return-boundary interrupts and row-identity finalization."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from scripts import gate_capacity
from tests._gate_capacity_support import (
    FakeMatrixPopen,
    _matrix_row_prepare,
    _real_sigint_at_line,
    gate,
    make_facts,
    make_policy,
)


@pytest.mark.parametrize("fail_fast", [False, True])
def test_return_boundary_interrupt_keeps_the_executed_tier(tmp_path, monkeypatch, fail_fast):
    """Regression: a SIGINT between the call and its bookkeeping kept nothing.

    A reviewer delivered a real SIGINT on the final ``return`` of
    ``run_prepared_tier``.  The completed :class:`TierResult` only existed in
    that frame, so the orchestrator synthesized an empty ``INTERRUPTED`` row:
    ``failed`` dropped from 1 to 0 and the diagnostics, output, and runtime
    identity vanished.  The tier must be published before the cancellation can
    escape the call.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate.run_prepared_tier, "    return tier\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=fail_fast,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False
    # One planned row, one admission identity, and one completed lifecycle
    # entry: the completed tier must not be re-registered as unstarted.
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states
    assert session.telemetry.lifecycle["completed"] == 1, session.telemetry.lifecycle


def test_batch_tier_finalization_interrupt_keeps_loaded_failure_evidence(tmp_path, monkeypatch):
    """Regression: an ordinary batch tier lost its loaded failure on SIGINT.

    A reviewer delivered a real SIGINT after ``run_tier`` had loaded a failing
    child report but before the batch tier returned.  Only matrix tiers had the
    deferral guard, so the failure counts, diagnostics, and captured output were
    replaced by an empty ``INTERRUPTED`` row.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def run_once(**kwargs):
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "REAL-FAILED-OUTCOME"},
                ),
            ),
            "RETAIN-FAILED-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    with _real_sigint_at_line(gate.run_tier, "    if omitted_failures:\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["unit"],
            required_tests_by_tier={},
            required_nodeids_by_tier={},
            timeout_override=5,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "RETAIN-FAILED-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("REAL-FAILED-OUTCOME" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result


def test_fail_fast_dispatch_and_finalization_share_one_row_identity(tmp_path, monkeypatch):
    """Regression: dispatch and finalization used different runtime scopes.

    The fail-fast and early-smoke branches called ``run_prepared_tier`` without
    the runtime scope, so admission identities used ``("", tier)`` while the
    cancellation and finalization paths used ``(mode, tier)``.  A SIGINT during
    the pre-dispatch blocked-row accounting therefore recorded the same planned
    row twice under two identities.  The scope is now derived from the prepared
    runtime, so the row is transitioned instead of duplicated.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def prepare(**kwargs):
        prepared = _matrix_row_prepare({"trade": (row,)})(**kwargs)
        prepared.required_problems = ["required asset is unavailable"]
        return prepared

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    original_mark = session.telemetry.mark
    signalled: list[str] = []

    def mark_then_interrupt(pair_id, state):
        original_mark(pair_id, state)
        if not signalled:
            signalled.append(pair_id)
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(session.telemetry, "mark", mark_then_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=True,
        )[0]
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states
    (pair_id,) = session.telemetry.pair_states
    assert pair_id.endswith(row), pair_id
    # The row was registered under the dispatched runtime scope, so neither
    # dispatch nor cancellation allocated a second identity for it.
    assert set(session._tier_run_ids) == {("source", "trade")}, session._tier_run_ids
    assert {key[0] for key in session._tier_blocked_rows} == {"source"}, session._tier_blocked_rows
    assert result.tiers[0].status == "INTERRUPTED", result.tiers[0]


def test_dispatch_return_boundary_interrupt_keeps_the_executed_tier(tmp_path, monkeypatch):
    """Regression: a SIGINT on the dispatch helper's own ``return`` dropped it.

    The round-9 review delivered a real SIGINT on ``_dispatch_prepared_tier``'s
    final line.  The completed :class:`TierResult` had already been loaded, but
    the fail-fast branch only recovered evidence the helper itself returned, so
    the signal was indistinguishable from a tier that never started: the loaded
    failure (``failed`` 1 -> 0), diagnostics, output, and runtime identity were
    replaced by an empty ``INTERRUPTED`` row.  Every dispatch caller now
    consults the durable mapping on the runtime result.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate._dispatch_prepared_tier, '    return tier, ""\n') as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=True,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False
    # The executed row keeps its single admission identity and is not
    # re-registered as an unstarted interruption.
    assert set(session._tier_run_ids) == {("source", "trade")}, session._tier_run_ids
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states


def test_matrix_supervisor_return_boundary_keeps_loaded_case_evidence(tmp_path, monkeypatch):
    """Regression: a SIGINT on ``run_matrix_tier``'s own ``return`` erased it.

    The assembled matrix tier only existed in the supervisor's frame.  A
    cancellation delivered after the finalization guard was restored but before
    the value reached ``run_tier`` escaped the whole dispatch chain, so the
    orchestrator replaced the executed rows (``failed`` 1) with an empty
    ``INTERRUPTED`` row.  The supervisor now publishes the assembled tier into
    the runtime's durable ``completed_tiers`` mapping while the guard is still
    installed.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate.run_matrix_tier, "    return tier_result\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False


def test_tier_supervisor_return_boundary_keeps_loaded_failure_evidence(tmp_path, monkeypatch):
    """Regression: a SIGINT on ``run_tier``'s own ``return`` lost the batch tier.

    An ordinary batch tier was assembled and its finalization guard restored,
    but the completed :class:`TierResult` still only existed in that frame.  A
    cancellation landing between ``restore()`` and ``run_prepared_tier``
    receiving the value therefore discarded the loaded failure, its diagnostics,
    and its captured output, leaving an empty ``INTERRUPTED`` row.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def run_once(**kwargs):
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "BATCH-RETURN-FAILURE"},
                ),
            ),
            "RETAIN-BATCH-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    with _real_sigint_at_line(gate.run_tier, "    return tier_result\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["unit"],
            required_tests_by_tier={},
            required_nodeids_by_tier={},
            timeout_override=5,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    tier = result.tiers[0]
    # The tier did execute and did fail; only the cancellation is separate, so a
    # real failure is never laundered into an empty ``INTERRUPTED`` row.
    assert tier.status == "FAIL", tier.status
    assert tier.counts.failed == 1, tier.counts
    assert "RETAIN-BATCH-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("BATCH-RETURN-FAILURE" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result


def test_cancellation_between_timing_repetitions_keeps_loaded_iteration(tmp_path, monkeypatch):
    """Regression: a SIGINT between timing repetitions discarded iteration 1.

    The timing tier repeats its expression at least five times.  A cancellation
    delivered after the first repetition's child report was loaded, but before
    the second repetition started, escaped past the repetition loop and threw
    away the counts, diagnostics, output tail and return codes already
    collected.  The loop now records the cancellation so the partial tier is
    finalized with its loaded evidence.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    iterations: list[int] = []

    def run_once(**kwargs):
        iterations.append(len(iterations) + 1)
        if len(iterations) > 1:
            # The first repetition has already been loaded; deliver the
            # cancellation on the boundary to the next one.
            os.kill(os.getpid(), signal.SIGINT)
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "TIMING-ITERATION-FAILURE"},
                ),
            ),
            "RETAIN-TIMING-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["timing"],
            required_tests_by_tier={},
            required_nodeids_by_tier={},
            timeout_override=5,
        )[0]
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert len(iterations) == 2, iterations
    tier = result.tiers[0]
    assert tier.name == "timing", tier.name
    assert tier.status == "INTERRUPTED", tier.status
    assert tier.counts.total == 1, tier.counts
    assert tier.counts.failed == 1, tier.counts
    assert "RETAIN-TIMING-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("TIMING-ITERATION-FAILURE" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result


def test_capacity_gated_matrix_dispatch_threads_the_publish_sink(tmp_path, monkeypatch):
    """Regression: the capacity-gated dispatch dropped the runtime's sink.

    Real-ROM tiers that are not dispatched from a strict nodeid manifest hand
    their rows to ``run_matrix_tier`` from inside ``run_tier``.  That call site
    did not forward the publish sink, so the very same return-boundary
    cancellation recovered for trade/battle erased the executed rows here.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "planned_tier_nodeids", lambda *args, **kwargs: ((row,), ""))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    published: list[gate.TierResult] = []
    with (
        _real_sigint_at_line(gate.run_matrix_tier, "    return tier_result\n") as signalled,
        pytest.raises(KeyboardInterrupt),
    ):
        gate.run_tier(
            name="local",
            project_root=tmp_path,
            python_executable=Path("python"),
            environment={},
            required_problems=[],
            repeat=1,
            timeout_override=5,
            report_directory=tmp_path,
            required_nodeids=(row,),
            matrix_workers=1,
            capacity_session=session,
            collections=(gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,)),),
            publish=published.append,
        )

    assert signalled, "the regression did not deliver its signal"
    assert len(published) == 1, published
    (tier,) = published
    assert tier.name == "local", tier.name
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail


def test_caller_append_boundary_interrupt_is_finalized_with_the_executed_tier(
    tmp_path, monkeypatch
):
    """Regression: a SIGINT before the caller's append erased loaded evidence.

    The round-9 review showed the completed tier was only reachable from the
    dispatching frame: a signal delivered between the dispatch helper's return
    and ``run_runtime_gates``' ``prepared.result.tiers.append(tier)`` escaped
    the whole orchestration, so the CLI fallback rebuilt the runtime from the
    plan and produced an empty ``INTERRUPTED`` tier with ``failed`` 0.  The
    finalizer now recovers the executed tier through the runtime result's
    durable mapping.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    accumulated: list[gate.RuntimeGateResult] = []
    append_line = "            prepared.result.tiers.append(tier)\n"
    with (
        _real_sigint_at_line(gate.run_runtime_gates, append_line) as signalled,
        pytest.raises(KeyboardInterrupt),
    ):
        gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=True,
            accumulator=accumulated,
        )

    assert signalled, "the regression did not deliver its signal"
    assert len(accumulated) == 1, accumulated
    plan = gate.build_execution_plan(("source",), ["trade"], early_smoke=False, fail_fast=True)
    finalized = gate._finalize_interrupted_results(
        accumulated=accumulated,
        modes=("source",),
        plan=plan,
        reason="gate cancelled by an interrupt",
        required_nodeids_by_tier={"trade": (row,)},
        capacity_session=session,
        project_root=tmp_path,
    )

    assert len(finalized) == 1, finalized
    result = finalized[0]
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False
    assert set(session._tier_run_ids) == {("source", "trade")}, session._tier_run_ids
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states


def test_skipped_runtime_rows_keep_the_plan_row_identity(tmp_path, monkeypatch):
    """Regression: skipped rows were registered under the empty runtime scope.

    The round-9 review interrupted the non-fail-fast dual-runtime branch while
    it registered the rows of the runtime that never started.  That accounting
    called ``_unrun_tier`` without the runtime scope, so the skipped native row
    was admitted as ``("", tier)`` while the CLI's finalizer used
    ``(mode, tier)``.  A second SIGINT therefore left three lifecycle rows for
    two planned rows.  Both paths must agree on the plan row identity.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    accumulated: list[gate.RuntimeGateResult] = []
    armed = {"value": False}
    signalled: list[str] = []
    original_mark = session.telemetry.mark

    def mark_then_interrupt(pair_id, state):
        original_mark(pair_id, state)
        if armed["value"] and not signalled:
            signalled.append(pair_id)
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(session.telemetry, "mark", mark_then_interrupt)

    def interrupted_runtime(**kwargs):
        mode = kwargs["mode"]
        result = gate._unstarted_preparation(mode, "source was interrupted").result
        result.cancellation = "source was interrupted"
        result.tiers = [
            gate._unrun_tier(
                "trade",
                "source was interrupted",
                (row,),
                status="INTERRUPTED",
                capacity_session=session,
                collections=result.collections,
                project_root=tmp_path,
                scope=mode,
            )
        ]
        # Only the runtime that is skipped *because* the first one stopped may
        # deliver the second signal; the first runtime's own rows are recorded
        # normally.
        armed["value"] = True
        accumulated.append(result)
        return result

    monkeypatch.setattr(gate, "run_runtime_gate", interrupted_runtime)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            gate.run_runtime_gates(
                runtime_mode="both",
                project_root=tmp_path,
                python_executable=Path("python"),
                cython_python_executable=Path("python"),
                rom_root=tmp_path / "rom",
                fixture_root=tmp_path / "fixtures",
                expected_sha1={},
                assets=[],
                selected=["trade"],
                required_tests_by_tier={},
                required_nodeids_by_tier={"trade": (row,)},
                timeout_override=5,
                capacity_session=session,
                accumulator=accumulated,
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    plan = gate.build_execution_plan(
        ("source", "cython"), ["trade"], early_smoke=False, fail_fast=False
    )
    finalized = gate._finalize_interrupted_results(
        accumulated=accumulated,
        modes=("source", "cython"),
        plan=plan,
        reason="gate cancelled by an interrupt",
        required_nodeids_by_tier={"trade": (row,)},
        capacity_session=session,
        project_root=tmp_path,
    )

    # Two planned rows, two admission identities: the skipped runtime's row is
    # transitioned by the finalizer instead of being allocated a second one.
    assert set(session._tier_run_ids) == {("source", "trade"), ("cython", "trade")}, (
        session._tier_run_ids
    )
    assert {key[0] for key in session._tier_blocked_rows} == {"source", "cython"}, (
        session._tier_blocked_rows
    )
    assert len(session.telemetry.pair_states) == 2, session.telemetry.pair_states
    assert [result.mode for result in finalized] == ["source", "cython"], finalized
    assert all(tier.status == "INTERRUPTED" for result in finalized for tier in result.tiers)


def test_early_smoke_return_boundary_interrupt_keeps_the_executed_smoke(tmp_path, monkeypatch):
    """Regression: the early-smoke branch dropped a smoke that already ran.

    The additional smoke is dispatched through the same helper as the selected
    tiers, so the same SIGINT on the helper's ``return`` used to make the
    early-smoke branch overwrite the executed smoke row with an empty
    ``INTERRUPTED`` one.  The branch now consults the runtime result's durable
    mapping before declaring the smoke unstarted.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"smoke": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate._dispatch_prepared_tier, '    return tier, ""\n') as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            early_smoke=True,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["smoke", "trade"], result.tiers
    smoke = result.tiers[0]
    # The dispatched smoke's own decision is retained.  Before the fix the
    # branch replaced it with a synthesized INTERRUPTED row whose status and
    # reason were indistinguishable from a smoke that never ran.
    assert smoke.status == "BLOCKED", smoke
    assert smoke.reason.startswith("capacity could not enumerate planned rows for tier smoke:"), (
        smoke.reason
    )
    assert result.cancellation, result
    assert result.tiers[1].status == "NOT_STARTED", result.tiers[1]
    assert gate.runtime_gate_passes(result) is False
