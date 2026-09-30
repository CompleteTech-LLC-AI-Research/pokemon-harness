"""Interrupt and cancellation handling during capacity observation."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import signal
import sys

import pytest

from scripts import gate_capacity
from tests._gate_capacity_support import (
    FakeMatrixPopen,
    _runtime_result,
    gate,
    make_facts,
    make_policy,
)


def test_cancelled_tiers_skip_capacity_admission_waits(tmp_path, monkeypatch):
    """A reviewed regression: cancellation must not spend admission deadlines."""

    import time

    calls = []

    def tier(**kwargs):
        calls.append(kwargs["name"])
        if kwargs["name"] == "smoke":
            return gate.TierResult(
                name="smoke",
                description="smoke",
                expression="smoke",
                required=True,
                status="INTERRUPTED",
                counts=gate.Counts(total=0),
            )
        return gate.TierResult(
            name=kwargs["name"],
            description=kwargs["name"],
            expression=kwargs["name"],
            required=True,
            status="PASS",
            counts=gate.Counts(total=1, passed=1),
        )

    monkeypatch.setattr(
        gate, "build_test_environment", lambda *_a, runtime_mode: {"M": runtime_mode}
    )
    monkeypatch.setattr(gate, "probe_runtime", lambda _p, _r, env: {"pyboy_mode": env["M"]})
    monkeypatch.setattr(gate, "runtime_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **_k: [])
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_k: [
            gate.CollectionResult("python-module", [], "PASS", 0, nodeids=()),
            gate.CollectionResult("pytest-console", [], "PASS", 0, nodeids=()),
        ],
    )
    monkeypatch.setattr(
        gate, "run_fixture_manifest_validation", lambda **_k: {"status": "PASS", "mode": "byte"}
    )
    monkeypatch.setattr(gate, "fixture_manifest_input_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "fixture_manifest_provenance_problems", lambda *_a: [])
    monkeypatch.setattr(
        gate,
        "run_matrix_collection_audit",
        lambda **_k: {
            "status": "PASS",
            "structural_pass": True,
            "acceptance_matrix_complete": True,
            "audited_nodeids": {},
            "groups": {},
        },
    )
    monkeypatch.setattr(gate, "run_tier", tier)

    admissions = []

    class CountingSession(gate_capacity.CapacitySession):
        def wait_until_available(self, **_kwargs):
            admissions.append(time.monotonic())
            return super().wait_until_available(**_kwargs)

    session = CountingSession(
        make_policy(admission_deadline_seconds=1.0),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    started = time.monotonic()
    results = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["local", "remote"],
        required_tests_by_tier={},
        required_nodeids_by_tier={},
        capacity_session=session,
        early_smoke=True,
        fail_fast=True,
    )
    elapsed = time.monotonic() - started
    statuses = {tier.name: tier.status for tier in results[0].tiers}
    smoke = next(t for t in results[0].tiers if t.name == "smoke")
    assert statuses["smoke"] == "INTERRUPTED", smoke.reason
    # The remaining real-ROM tier cannot execute after cancellation, so it must
    # not pay a capacity admission deadline before reporting NOT_STARTED.
    assert statuses["local"] == "NOT_STARTED"
    assert statuses["remote"] == "NOT_STARTED"
    # Only the smoke tier can still execute; every cancelled tier must skip the
    # admission deadline entirely instead of waiting once per runtime.
    assert len(admissions) == 1, admissions
    assert elapsed < 1.0


def test_cancelled_admission_wait_records_rows_and_keeps_prior_results(
    tmp_path, runtime_gate_stubs
):
    """Regression: a SIGINT while waiting for capacity must not lose evidence.

    A reviewer delivered a real SIGINT during the admission wait and observed
    ``KeyboardInterrupt`` escaping the gate: no report, and the pending row
    absent from lifecycle accounting.  Cancellation has to stop dispatch,
    account for the rows that never ran, and preserve the tiers that already
    finished.
    """

    rows = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )

    class InterruptingSession(gate_capacity.CapacitySession):
        def wait_until_available(self, **_kwargs):
            if not self.admission.decisions:
                raise KeyboardInterrupt
            return super().wait_until_available(**_kwargs)

    session = InterruptingSession(
        make_policy(admission_deadline_seconds=5.0),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    try:
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["unit", "trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": rows},
            capacity_session=session,
        )
    except KeyboardInterrupt:
        pytest.fail("cancellation escaped the gate instead of finalizing evidence")

    statuses = {tier.name: tier.status for tier in results[0].tiers}
    assert statuses["unit"] == "PASS", results[0].tiers
    assert statuses["trade"] == "INTERRUPTED", results[0].tiers
    # Every row the cancelled tier would have dispatched is terminal, and no
    # queued waiter survives to be promoted into a later slot.
    assert session.telemetry.lifecycle["interrupted"] == len(rows)
    assert session.admission.queued == []
    recorded = {decision.pair_id.split(":", 2)[-1] for decision in session.admission.decisions}
    assert recorded == set(rows)
    keys = {
        decision.pair_id.split(":", 2)[-1]: decision.pair_id
        for decision in session.admission.decisions
    }
    assert all(session.admission.state(keys[nodeid]) == "expired" for nodeid in rows)


def test_interrupt_during_capacity_observation_records_rows(tmp_path, runtime_gate_stubs):
    """Regression: cancellation during observation setup is not lost either."""

    rows = ("tests/test_matrix.py::test_pair[yellow-red]",)

    class ObservationInterrupt(gate_capacity.CapacitySession):
        def ensure_started(self):
            raise KeyboardInterrupt

    session = ObservationInterrupt(
        make_policy(admission_deadline_seconds=5.0),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    try:
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": rows},
            capacity_session=session,
        )
    except KeyboardInterrupt:
        pytest.fail("cancellation escaped observation setup")

    tier = results[0].tiers[0]
    assert tier.name == "trade"
    assert tier.status == "INTERRUPTED", tier.reason
    assert session.telemetry.lifecycle["interrupted"] == len(rows)
    assert [decision.pair_id.split(":", 2)[-1] for decision in session.admission.decisions] == list(
        rows
    )


def test_sigint_during_observer_shutdown_keeps_completed_tier(tmp_path, monkeypatch):
    """Regression: a SIGINT during observer shutdown must keep the tier result.

    A reviewer delivered a real SIGINT while the capacity observer was being
    joined, *after* the tier had already finished and loaded its failure
    report.  The previous handlers replaced that executed tier with an empty
    ``INTERRUPTED`` row, so the loaded failure disappeared and the same planned
    row was registered twice (completed and interrupted).  The completed tier,
    its counts, and its failure details must survive, the cancellation must be
    recorded separately, and no row may be accounted for twice.
    """

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    signalled: list[bool] = []
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"]},
                collections=[gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,))],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
                gate_problems=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)

    class SignalOnShutdown(gate_capacity.CapacitySession):
        @contextlib.contextmanager
        def monitoring(self):
            with super().monitoring():
                yield
            if not signalled:
                # A genuine SIGINT, delivered only after the tier completed and
                # its report was loaded, exactly during observer teardown.
                signalled.append(True)
                os.kill(os.getpid(), signal.SIGINT)

    session = SignalOnShutdown(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    try:
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
        )
    except KeyboardInterrupt:  # pragma: no cover - regression guard
        pytest.fail("cancellation escaped the gate instead of finalizing evidence")
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    result = results[0]
    assert result.cancellation, result
    # Exactly one tier: the executed one.  No empty replacement row was added.
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.status == "FAIL", tier.reason
    assert tier.counts.failed == 1, tier.counts
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    # A cancelled run can never be reported as a clean PASS.
    assert gate.runtime_gate_passes(result) is False
    # A cancellation after an otherwise passing tier must still fail the run,
    # even when every executed row passed.
    cancelled_pass = _runtime_result("source")
    cancelled_pass.cancellation = "observer shutdown interrupted after a passing tier"
    assert gate.runtime_gate_passes(cancelled_pass) is False
    # A cancellation recorded on a completed tier must stop the outer runtime
    # plan: no later runtime may be prepared, waited for, or dispatched.

    def cancelled_cleanup(**kwargs):
        prepared = gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"], "pyboy_revision": "runtime-identity-marker"},
                collections=[gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,))],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )
        prepared.result.tiers.append(
            gate.TierResult(
                name="trade",
                description="trade",
                expression="trade",
                required=True,
                status="PASS",
                counts=gate.Counts(total=1, passed=1),
            )
        )
        prepared.result.cancellation = "observer shutdown interrupted after a passing tier"
        return prepared

    calls: list[str] = []
    monkeypatch.setattr(gate, "prepare_runtime_gate", cancelled_cleanup)

    def record_dispatch(command, **kwargs):
        calls.append(kwargs["env"].get("pyboy_mode", ""))
        return FakeMatrixPopen(command, **kwargs)

    monkeypatch.setattr(gate.subprocess, "Popen", record_dispatch)
    dual = gate.run_runtime_gates(
        runtime_mode="both",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["trade"],
        required_tests_by_tier={},
        required_nodeids_by_tier={"trade": (row,)},
        timeout_override=5,
    )
    assert [result.cancellation for result in dual] == [
        "observer shutdown interrupted after a passing tier",
        "not started after source was interrupted",
    ], [(result.mode, result.cancellation) for result in dual]
    assert dual[1].tiers[0].status == "NOT_STARTED", dual[1].tiers
    # The single row is accounted for once, not once as completed and again as
    # interrupted.
    assert sum(session.telemetry.lifecycle.values()) == 1, session.telemetry.lifecycle


def test_outer_interrupt_keeps_completed_tier_counts_and_identity(tmp_path, monkeypatch, capsys):
    """Regression: the outer SIGINT fallback must not erase completed rows.

    A reviewer delivered a real SIGINT while the outer plan accounted for a
    fail-fast-stopped row after a completed trade failure.  The CLI fallback
    rebuilt every runtime from scratch, so the executed trade row became an
    empty ``INTERRUPTED`` result with no counts, diagnostics, or runtime
    identity, and its lifecycle entry was duplicated.  Completed results must
    survive the outer cancellation boundary and only unfinished rows may be
    finalized.
    """

    rows = {
        "trade": ("tests/test_matrix.py::test_pair[aaa-fail]",),
        "battle": ("tests/test_matrix.py::test_battle[pending]",),
    }

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={
                    "pyboy_mode": kwargs["mode"],
                    "pyboy_revision": "runtime-identity-marker",
                },
                collections=[
                    gate.CollectionResult(
                        "python-module",
                        [],
                        "PASS",
                        0,
                        nodeids=tuple(rows["trade"]) + tuple(rows["battle"]),
                    )
                ],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=make_facts,
    )
    original_mark = session.telemetry.mark
    signalled: list[str] = []

    def mark_then_interrupt(pair_id, state):
        original_mark(pair_id, state)
        if ":battle:" in pair_id and state == "not_started" and not signalled:
            signalled.append(pair_id)
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(session.telemetry, "mark", mark_then_interrupt)
    monkeypatch.setattr(gate.gate_capacity, "CapacitySession", lambda *_, **__: session)
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda *_: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda *_: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda *_: (rows, ""))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(session.policy.as_dict()))
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        returncode = gate.main(
            [
                "--repo-root",
                str(tmp_path),
                "--tier",
                "trade",
                "--tier",
                "battle",
                "--fail-fast",
                "--capacity-policy",
                str(policy_path),
                "--format",
                "json",
                "--evidence-dir",
                str(tmp_path / "fallback-evidence"),
            ]
        )
    finally:
        signal.signal(signal.SIGINT, previous)
    payload = json.loads(capsys.readouterr().out)
    assert signalled and returncode == 130
    assert session.telemetry.lifecycle["completed"] == 1, session.telemetry.lifecycle
    # One planned row, one lifecycle entry: finalizing the partially registered
    # tier must transition the existing admission identity rather than
    # allocating a second one for the same planned row.
    assert len(session.telemetry.pair_states) == 2, session.telemetry.pair_states
    row_ids = [pair_id.split(":", 2)[-1] for pair_id in session.telemetry.pair_states]
    assert len(row_ids) == len(set(row_ids)), session.telemetry.pair_states
    trade = next(t for t in payload["tiers"] if t["name"] == "trade")
    assert trade["counts"]["failed"] == 1, trade
    assert "runtime-identity-marker" in json.dumps(trade), trade
    battle = next(t for t in payload["tiers"] if t["name"] == "battle")
    assert battle["status"] == "INTERRUPTED", battle


def test_matrix_finalization_interrupt_keeps_loaded_failure_evidence(tmp_path, monkeypatch):
    """Regression: an interrupt during matrix assembly must keep the evidence.

    A reviewer delivered a real SIGINT after the failing child report had been
    loaded but before ``run_matrix_tier`` returned.  The tier was replaced by an
    empty ``INTERRUPTED`` row, so ``failed`` dropped from 1 to 0 and the
    diagnostics and output vanished, while the same planned row was recorded a
    second time.  Assembly is in-memory only, so the cancellation must be
    deferred until the complete tier result exists and then carried out.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"], "pyboy_revision": "runtime-identity-marker"},
                collections=[gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,))],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    # Deliver a real SIGINT exactly at the in-memory assembly boundary, after
    # the failing child report has been loaded.
    target = inspect.unwrap(gate.run_matrix_tier)
    source, start = inspect.getsourcelines(target)
    signal_line = start + next(
        index
        for index, line in enumerate(source)
        if line.startswith("    case_results = [case_results_by_nodeid")
    )
    signalled: list[bool] = []

    def trace(frame, event, arg):
        if (
            not signalled
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == signal_line
        ):
            signalled.append(True)
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
        )
    except KeyboardInterrupt:  # pragma: no cover - regression guard
        pytest.fail("assembly cancellation escaped instead of finalizing evidence")
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    result = results[0]
    assert result.cancellation, result
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    # The executed failure, its diagnostics, and its output survive assembly.
    assert tier.counts.failed == 1, tier.counts
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert gate.runtime_gate_passes(result) is False
    # Exactly one admission identity for the single planned row.
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states
