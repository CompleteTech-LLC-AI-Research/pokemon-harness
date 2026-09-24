"""Production-gate integration and CLI behavior for capacity admission."""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gate_capacity
from tests._gate_capacity_support import (
    ROOT,
    FakeMatrixPopen,
    SequenceSampler,
    _runtime_result,
    _standalone_environment,
    gate,
    make_facts,
    make_policy,
    runtime_gate_stubs,
    write_tier_config,
)


def test_rising_pressure_stops_admission_and_retains_existing_failure(tmp_path, monkeypatch):
    import time

    class SlowFailingChild(FakeMatrixPopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.finish = time.monotonic() + 0.05

        def poll(self):
            return self.returncode if time.monotonic() >= self.finish else None

    monkeypatch.setattr(gate.subprocess, "Popen", SlowFailingChild)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=SequenceSampler(
            [
                make_facts(),
                make_facts(),
                make_facts(psi_cpu_some_avg300=100),
            ]
        ),
    )
    nodeids = (
        "tests/test_matrix.py::test_pair[aaa-fail]",
        "tests/test_matrix.py::test_pair[zzz-block-a]",
        "tests/test_matrix.py::test_pair[zzz-block-b]",
    )
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=nodeids,
        matrix_workers=2,
        capacity_session=session,
    )

    assert result.status == "FAIL"
    cases = {case.nodeid: case for case in result.case_results}
    assert cases[nodeids[0]].status == "FAIL"
    assert "MATRIX-OUTPUT" in cases[nodeids[0]].output_tail
    assert cases[nodeids[1]].status == "BLOCKED"
    assert cases[nodeids[2]].status == "BLOCKED"
    reasons = {detail.nodeid: detail.reason for detail in result.failure_details}
    assert any("runtime-identity-marker" in reason for reason in reasons.values())
    lifecycle = session.telemetry.lifecycle
    assert lifecycle["completed"] == 1
    assert lifecycle["not_started"] == 2


def test_matrix_workers_are_capped_by_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    nodeids = tuple(f"tests/test_matrix.py::test_pair[pass-{index}]" for index in range(3))
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=nodeids,
        matrix_workers=3,
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert "--matrix-workers=1" in result.command
    assert all(case.status == "PASS" for case in result.case_results)


def test_capacity_tier_reason_skips_asset_free_tiers(tmp_path):
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=lambda: make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
    )
    assert gate._capacity_tier_reason(None, "trade") == ""
    assert gate._capacity_tier_reason(session, "unit") == ""
    assert gate._capacity_tier_reason(session, "timing") == ""
    reason = gate._capacity_tier_reason(session, "trade")
    assert "capacity" in reason
    assert "blocked" in reason


def test_present_policy_blocks_real_tier_but_runs_unit(tmp_path, runtime_gate_stubs):
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=lambda: make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
    )
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
        required_nodeids_by_tier={},
        capacity_session=session,
    )
    result = results[0]
    statuses = {tier.name: tier.status for tier in result.tiers}
    assert statuses["unit"] == "PASS"
    assert statuses["trade"] == "BLOCKED"
    trade = next(tier for tier in result.tiers if tier.name == "trade")
    assert "capacity" in trade.reason
    assert any(call for call in runtime_gate_stubs if call["name"] == "unit")


def test_absent_policy_preserves_real_tier_dispatch(tmp_path, runtime_gate_stubs):
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
        required_nodeids_by_tier={},
    )
    result = results[0]
    assert all(tier.status == "PASS" for tier in result.tiers)
    assert {call["name"] for call in runtime_gate_stubs} == {"unit", "trade"}


def test_non_fail_fast_cli_retains_completed_runtime_progress(tmp_path, monkeypatch, capsys):
    """Regression: a between-tiers interrupt keeps the completed runtime.

    With ordinary ``--tier trade --tier battle`` selection and no ``--fail-fast``
    the accumulator was appended only after ``run_runtime_gate`` returned.  A
    SIGINT between the failing trade tier and the next tier therefore rebuilt the
    runtime as unstarted: ``failed`` changed from 1 to 0, the failure evidence
    disappeared, and the observed runtime identity became ``not-run``.
    """

    rows = {
        "trade": ("tests/test_matrix.py::test_pair[aaa-fail]",),
        "battle": ("tests/test_matrix.py::test_pair[pending]",),
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
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    monkeypatch.setattr(gate.gate_capacity, "CapacitySession", lambda *_, **__: session)
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda *_: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda *_: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda *_: (rows, ""))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(session.policy.as_dict()))
    # Deliver a real SIGINT after the failing trade tier has been appended but
    # before the next tier starts.  The tier-loop cancellation handlers are not
    # involved, so only publishing the runtime result before dispatch keeps the
    # completed failure and the observed runtime identity.
    target = gate.run_runtime_gate
    source, start = inspect.getsourcelines(target)
    signal_line = start + next(
        index
        for index, line in enumerate(source)
        if line.startswith('        if prepared.result.tiers[-1].status == "INTERRUPTED":')
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

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        returncode = gate.main(
            [
                "--repo-root",
                str(tmp_path),
                "--tier",
                "trade",
                "--tier",
                "battle",
                "--capacity-policy",
                str(policy_path),
                "--format",
                "json",
                "--evidence-dir",
                str(tmp_path / "nonfailfast-evidence"),
            ]
        )
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous)
    payload = json.loads(capsys.readouterr().out)
    assert signalled, "the regression did not deliver its signal"
    assert returncode == 130
    # The observed runtime identity and the executed failure are retained.
    assert payload["runtime"].get("pyboy_revision") == "runtime-identity-marker", payload["runtime"]
    trade = next(tier for tier in payload["tiers"] if tier["name"] == "trade")
    assert trade["counts"]["failed"] == 1, trade
    assert "runtime-identity-marker" in json.dumps(trade), trade
    battle = next(tier for tier in payload["tiers"] if tier["name"] == "battle")
    assert battle["status"] == "INTERRUPTED", battle


def test_capacity_only_aggregate_stop_is_blocked_and_drops_the_queue(tmp_path, monkeypatch):
    """Regression: a capacity-only stop is BLOCKED, not a product FAIL.

    A reviewer reproduced a tier with one passing row and one row queued solely
    because declared pressure was high.  The supervisor reported FAIL with zero
    failed tests, and the abandoned waiter was still queued, so a later release
    promoted it into a live slot and starved the next real row.
    """

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(
            max_concurrent_pairs=1, admission_deadline_seconds=60.0, observation_seconds=0.01
        ),
        repo_root=tmp_path,
        sampler=make_facts,
    )
    rows = (
        "tests/test_matrix.py::test_pair[pass-a]",
        "tests/test_matrix.py::test_pair[queued-b]",
    )

    def pressured_popen(command, **kwargs):
        child = FakeMatrixPopen(command, **kwargs)
        # Once the first pair owns the slot, declared pressure rises and stays
        # high, so the second row can only ever queue.
        session.telemetry._sampler = lambda: make_facts(psi_cpu_some_avg300=99)
        return child

    monkeypatch.setattr(gate.subprocess, "Popen", pressured_popen)
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=rows,
        matrix_workers=2,
        matrix_timeout_override=0.2,
        capacity_session=session,
    )

    cases = {case.nodeid: case.status for case in result.case_results}
    assert cases[rows[0]] == "PASS"
    assert cases[rows[1]] == "BLOCKED"
    assert result.status == "BLOCKED", result.status
    # The abandoned waiter no longer holds queue state and is terminal, so a
    # recovered tier admits the next real row instead of the abandoned one.
    assert session.admission.queued == []
    keys = {
        decision.pair_id.split(":", 2)[-1]: decision.pair_id
        for decision in session.admission.decisions
    }
    assert session.admission.state(keys[rows[1]]) == "expired"
    report = session.report()
    assert report["lifecycle"]["not_started"] == 1
    assert report["lifecycle"]["completed"] == 1
    assert report["status"] == "blocked"
    session.telemetry._sampler = make_facts
    session.observe()
    decision = session.admission.admit("2:next")
    assert decision.status == "ok" and decision.owner is True, decision


def test_asset_blocked_strict_rows_are_registered(tmp_path, monkeypatch):
    """Regression: a directly asset-blocked strict row still accounts its rows."""

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    rows = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=["missing ROM for red"],
        repeat=1,
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=rows,
        matrix_workers=2,
        capacity_session=session,
    )
    assert result.status == "BLOCKED", result.reason
    report = session.report()
    assert report["lifecycle"]["not_started"] == len(rows)
    recorded = {
        decision["pair_id"].split(":", 2)[-1] for decision in report["admission"]["decisions"]
    }
    assert recorded == set(rows)


def test_preflight_failure_registers_blocked_rows_for_every_tier(tmp_path, monkeypatch):
    """Regression: fail-fast/preflight exits must not hide planned rows."""

    runtime_gate_stubs.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [
            gate.CollectionResult("python-module", [], "FAIL", 1, reason="collection failed")
        ],
    )
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    rows = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )
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
        early_smoke=True,
        fail_fast=True,
    )
    statuses = {tier.name: tier.status for tier in results[0].tiers}
    assert statuses["trade"] in {"BLOCKED", "NOT_STARTED"}, statuses
    report = session.report()
    recorded = {
        decision["pair_id"].split(":", 2)[-1] for decision in report["admission"]["decisions"]
    }
    assert set(rows) <= recorded, recorded
    assert report["lifecycle"]["not_started"] >= len(rows)


def test_parser_accepts_capacity_policy():
    args = gate.build_parser().parse_args(["--capacity-policy", "policy.json"])
    assert args.capacity_policy == Path("policy.json")


def test_main_without_policy_records_unavailable_capacity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _path: ({}, ""))

    def fake_runtime_gates(**_kwargs):
        result = _runtime_result("source")
        result.execution_plan = gate.build_execution_plan(
            ("source",), ("unit",), early_smoke=False, fail_fast=False
        )
        return [result]

    monkeypatch.setattr(gate, "run_runtime_gates", fake_runtime_gates)
    evidence = tmp_path / "evidence"
    returncode = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--tier",
            "unit",
            "--format",
            "json",
            "--evidence-dir",
            str(evidence),
        ]
    )
    capsys.readouterr()
    assert returncode == 0
    report = json.loads((evidence / "gate-report.json").read_text(encoding="utf-8"))
    assert report["capacity"]["status"] == "unavailable"
    assert report["capacity"]["capacity_policy"] == "unavailable"
    text = (evidence / "gate-report.txt").read_text(encoding="utf-8")
    assert "capacity-policy: unavailable" in text


def test_main_with_policy_asset_free_reports_not_applicable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _path: ({}, ""))

    def fake_runtime_gates(**_kwargs):
        result = _runtime_result("source")
        result.execution_plan = gate.build_execution_plan(
            ("source",), ("unit",), early_smoke=False, fail_fast=False
        )
        return [result]

    monkeypatch.setattr(gate, "run_runtime_gates", fake_runtime_gates)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(make_policy(effective_cpus=99).as_dict()), encoding="utf-8")
    evidence = tmp_path / "evidence"
    returncode = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--tier",
            "unit",
            "--capacity-policy",
            str(policy_path),
            "--format",
            "json",
            "--evidence-dir",
            str(evidence),
        ]
    )
    capsys.readouterr()
    assert returncode == 0
    report = json.loads((evidence / "gate-report.json").read_text(encoding="utf-8"))
    assert report["overall"] == "PASS"
    assert report["capacity"]["status"] == "not_applicable"
    assert report["capacity"]["availability"]["status"] == "not_applicable"
    assert report["capacity"]["admission"]["admitted"] == 0
    text = (evidence / "gate-report.txt").read_text(encoding="utf-8")
    assert "capacity-policy: not_applicable" in text


def test_standalone_gate_help_runs_without_package_layout():
    # Regression for the dynamic qualification_runner load: the loaded module
    # must be registered in ``sys.modules`` before ``exec_module`` so the
    # sibling module's annotated ``@dataclass`` can resolve its own globals.
    completed = subprocess.run(
        [sys.executable, "scripts/production_gate.py", "--help"],
        cwd=ROOT,
        env=_standalone_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Traceback" not in completed.stderr
    assert "usage:" in completed.stdout


def test_matrix_retries_unavailable_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    sampler = SequenceSampler(
        [make_facts(cpu_quota_cores=1), make_facts(cpu_quota_cores=1), make_facts()]
    )
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=1), repo_root=tmp_path, sampler=sampler
    )
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=("tests/test_matrix.py::test_pair[pass]",),
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert sampler.calls >= 3
    assert session.admission.admitted_count == 1


def test_observer_samples_while_matrix_owner_runs(tmp_path, monkeypatch):
    import time

    class SlowChild(FakeMatrixPopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.finish = time.monotonic() + 0.15

        def poll(self):
            return self.returncode if time.monotonic() >= self.finish else None

    monkeypatch.setattr(gate.subprocess, "Popen", SlowChild)
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01), repo_root=tmp_path, sampler=make_facts
    )
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=("tests/test_matrix.py::test_pair[pass]",),
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert len(session.telemetry.samples) >= 3
    assert not session._monitor_thread.is_alive()


def test_nonmatrix_tier_rows_are_admitted_and_observed(tmp_path, monkeypatch):
    """An ordinary real-ROM tier admits each planned pair, not one batch slot."""

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    row = "tests/test_rom_boot.py::test_pair[red-color]"
    write_tier_config(tmp_path)
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01), repo_root=tmp_path, sampler=make_facts
    )
    result = gate.run_tier(
        name="local",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        repeat=1,
        capacity_session=session,
        collections=(gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,)),),
    )
    assert result.status == "PASS", result.reason
    assert session.report()["status"] == "ok"
    # One slot per planned pair, counted once even though dispatch is per row.
    assert session.admission.admitted_count == 1
    assert session.telemetry.lifecycle["completed"] == 1
    assert len(session.telemetry.samples) >= 3
    assert [case.status for case in result.case_results] == ["PASS"]


def test_observer_hang_cannot_hold_cleanup_or_modify_final_report(tmp_path):
    import threading
    import time

    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def sampler():
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            release.wait(5)
        return make_facts()

    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01), repo_root=tmp_path, sampler=sampler
    )
    try:
        with session.monitoring():
            assert entered.wait(1)
            start = time.monotonic()
        assert time.monotonic() - start < 1
        final = session.report(failed=1)
        assert final["status"] == "failed"
        assert final["collection_failures"] == 1
    finally:
        release.set()
        session._monitor_thread.join(1)
    assert session.report(failed=1) == final


def test_complete_sanitized_stream_is_persisted_and_verified(tmp_path):
    session = gate_capacity.CapacitySession(make_policy(), repo_root=tmp_path, sampler=make_facts)
    for _ in range(270):
        session.observe()
    session.telemetry.samples[-1].problems = [str(tmp_path / "private-fixture")]
    payload = gate.build_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[],
        gate_problems=[],
        overall="FAIL",
        capacity=session.report(),
    )
    evidence = tmp_path / "evidence"
    gate.write_evidence_bundle(evidence, payload)
    report = json.loads((evidence / gate.EVIDENCE_REPORT_FILENAME).read_text())
    capacity = report["capacity"]
    content = (evidence / gate.CAPACITY_STREAM_FILENAME).read_bytes()
    assert str(tmp_path).encode() not in content
    assert len(json.loads(content)) == 270
    assert len(capacity["samples"]) == 256
    assert capacity["samples_omitted"] == 14
    assert "sample_stream" not in capacity
    import hashlib

    assert capacity["sample_reference"]["sha256"] == hashlib.sha256(content).hexdigest()
    gate.verify_evidence_bundle(evidence)
    (evidence / gate.CAPACITY_STREAM_FILENAME).write_bytes(content + b" ")
    with pytest.raises(ValueError, match="mismatch"):
        gate.verify_evidence_bundle(evidence)


def test_main_cannot_pass_with_unresolved_capacity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "run_runtime_gates", lambda **kwargs: [_runtime_result("source")])
    monkeypatch.setattr(
        gate_capacity.qualification_runner, "collect_facts", lambda *args: make_facts()
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(make_policy().as_dict()))
    code = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--tier",
            "smoke",
            "--capacity-policy",
            str(policy_path),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["overall"] == "FAIL"
    assert report["capacity"]["status"] == "blocked"


def test_unit_tier_is_not_capacity_gated(tmp_path, monkeypatch):
    """A ROM-free tier must keep its batch dispatch under a capacity policy."""

    observed = []

    def run_once(**kwargs):
        observed.append(kwargs)
        return (
            0,
            gate.GateReport(
                gate.Counts(total=1, passed=1), nodeids=("tests/test_unit.py::test_x",)
            ),
            "",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts(psi_cpu_some_avg300=99)
    )
    result = gate.run_tier(
        name="unit",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        repeat=1,
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert len(observed) == 1
    assert session.admission.admitted_count == 0


def test_second_pair_never_starts_while_observed_capacity_is_blocked(tmp_path, monkeypatch):
    """Regression: an ordinary tier must re-check admission between pairs.

    A reviewer reproduced a two-pair local batch whose second pair started
    after the observer saw blocked pressure, and the whole tier still reported
    PASS.  With per-pair admission the second row must be recorded BLOCKED and
    the tier must report BLOCKED instead of borrowing the first pair's pass.
    """

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    write_tier_config(
        tmp_path,
        markers={
            "test_rom_boot.py::test_pair[ok]": ("real_rom",),
            "test_rom_boot.py": ("real_rom",),
        },
        tier_rules={"local": (("real_rom",), ())},
    )
    rows = (
        "tests/test_rom_boot.py::test_pair[ok]",
        "tests/test_rom_boot.py::test_pair[second]",
    )
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=make_facts,
    )
    started: list[str] = []

    def recording_popen(command, **kwargs):
        started.append(command[3])
        if len(started) == 1:
            # Once the first pair is active, declared pressure spikes.
            session.telemetry._sampler = lambda: make_facts(psi_cpu_some_avg300=99)
            session.observe()
        return FakeMatrixPopen(command, **kwargs)

    monkeypatch.setattr(gate.subprocess, "Popen", recording_popen)
    result = gate.run_tier(
        name="local",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        repeat=1,
        capacity_session=session,
        collections=(gate.CollectionResult("python-module", [], "PASS", 0, nodeids=rows),),
    )
    assert started == [rows[0]], started
    cases = {case.nodeid: case.status for case in result.case_results}
    assert cases[rows[0]] == "PASS"
    assert cases[rows[1]] == "BLOCKED"
    assert result.status == "BLOCKED", result.status
    # The unstarted row is terminal for capacity accounting and never passed.
    assert session.telemetry.lifecycle["not_started"] == 1
    assert any(
        decision.status in {"blocked", "expired"} for decision in session.admission.decisions
    )


def test_preflight_blocked_rows_are_recorded_in_lifecycle(tmp_path, monkeypatch):
    """Regression: pre-dispatch BLOCKED rows must not vanish from accounting.

    A reviewer reproduced two BLOCKED matrix rows with ``not_started=0`` and no
    admission decisions.  Every row the tier would have dispatched must be
    registered as a terminal blocked decision.
    """

    runtime_gate_stubs.__wrapped__(tmp_path, monkeypatch)
    write_tier_config(
        tmp_path,
        markers={"test_matrix.py": ("real_rom", "trade_acceptance")},
        tier_rules={"trade": (("real_rom", "trade_acceptance"), ())},
    )
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.01),
        repo_root=tmp_path,
        sampler=lambda: make_facts(psi_cpu_some_avg300=99),
    )
    rows = (
        "tests/test_matrix.py::test_pair[a]",
        "tests/test_matrix.py::test_pair[b]",
    )
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
    assert results[0].tiers[0].status == "BLOCKED"
    report = session.report()
    assert report["lifecycle"]["not_started"] == len(rows)
    recorded = {
        decision["pair_id"].split(":", 2)[-1] for decision in report["admission"]["decisions"]
    }
    assert recorded == set(rows)
    assert all(decision["status"] == "blocked" for decision in report["admission"]["decisions"])


def test_zero_interval_collector_cannot_delay_active_child_deadline(tmp_path, monkeypatch):
    import threading
    import time

    release = threading.Event()
    calls = 0
    main_thread = threading.get_ident()
    collection_threads = []
    killed_after = []

    def sampler():
        nonlocal calls
        calls += 1
        collection_threads.append(threading.get_ident())
        if calls > 1:
            release.wait(2)
        return make_facts()

    class StuckChild(FakeMatrixPopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = time.monotonic()

        def poll(self):
            return None

    def kill(process, **kwargs):
        killed_after.append(time.monotonic() - process.started)
        return True

    monkeypatch.setattr(gate.subprocess, "Popen", StuckChild)
    monkeypatch.setattr(gate, "_kill_matrix_process", kill)
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0, admission_deadline_seconds=0.4),
        repo_root=tmp_path,
        sampler=sampler,
    )
    try:
        result = gate.run_matrix_tier(
            name="trade",
            project_root=tmp_path,
            python_executable=Path("python"),
            environment={},
            required_problems=[],
            timeout_override=0.03,
            report_directory=tmp_path,
            required_nodeids=(
                "tests/test_matrix.py::test_pair[first]",
                "tests/test_matrix.py::test_pair[second]",
            ),
            matrix_workers=2,
            capacity_session=session,
        )
        assert result.status == "FAIL"
        assert len(killed_after) == 2
        assert max(killed_after) < 0.2
        assert main_thread not in collection_threads
    finally:
        release.set()
        session._monitor_thread.join(1)
