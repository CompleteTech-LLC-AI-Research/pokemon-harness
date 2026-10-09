"""Budget/preflight producer controls, authored NOTRUN; not probe containment proof."""

import json
import math
import os
import sys
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_runtime_inputs as inputs
from scripts import production_gate_parallel_worker as worker
from scripts.production_gate_parallel_artifacts import TERMINAL_ARTIFACT_NAME
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from tests._parallel_runtime_holder_kernel import Clock
from tests._parallel_runtime_report_support import make_plan


def test_success_expiry_still_services_siblings_and_keeps_only_original_cleanup_allowance():
    clock, service = Clock(), []
    bound = MeasurementBudget(
        1.0, 2.0, clock=clock.monotonic, service=lambda: service.append(clock.now)
    )
    clock.now = 1.0
    with pytest.raises(IdentityDeadline):
        bound.checkpoint()
    assert service == [1.0], "expiry must broadcast sibling service before refusing success"
    assert bound.cleanup_open()
    clock.now = 2.0
    assert not bound.cleanup_open()
    repeated = bound.with_checkpoint(lambda: service.append("additional service"))
    assert not repeated.cleanup_open(), "a nested callback cannot buy new cleanup time"


def test_observed_shared_cleanup_tightening_cannot_relax_on_later_supplier_readback():
    clock = Clock()
    observed = [4.0]
    bound = MeasurementBudget(10.0, 15.0, clock=clock.monotonic, cleanup_limit=lambda: observed[0])
    assert bound.cleanup_bound() == 4.0
    observed[0] = 12.0
    nested = bound.with_checkpoint(lambda: None)
    assert nested.cleanup_bound() == bound.cleanup_bound() == 4.0
    shorter = nested.tighten(deadline=3.0, cleanup_deadline=3.5)
    assert bound.cleanup_bound() == shorter.cleanup_bound() == 3.5
    attempted_extension = shorter.tighten(deadline=50.0, cleanup_deadline=60.0)
    assert attempted_extension.deadline == 3.0 and attempted_extension.cleanup_bound() == 3.5


@pytest.mark.parametrize("unknown", [math.nan, math.inf, -math.inf])
def test_unknown_budget_clock_and_shared_bound_refuse_success(unknown):
    with pytest.raises(ValueError):
        MeasurementBudget(unknown, 2.0)
    with pytest.raises(ValueError):
        MeasurementBudget(1.0, unknown)
    bound = MeasurementBudget(1.0, 2.0, clock=lambda: unknown)
    with pytest.raises(IdentityDeadline, match="clock is unknown"):
        bound.checkpoint()
    with pytest.raises(IdentityDeadline, match="clock is unknown"):
        bound.cleanup_open()
    bound = MeasurementBudget(1.0, 2.0, clock=lambda: 0.0, cleanup_limit=lambda: unknown)
    with pytest.raises(IdentityDeadline, match="cleanup bound is unknown"):
        bound.checkpoint()
    with pytest.raises(ValueError):
        bound.tighten(deadline=unknown, cleanup_deadline=2.0)


@pytest.mark.parametrize("file_kind", ["source", "editable"])
def test_real_worker_runtime_pin_hashing_spends_the_inherited_job_budget(
    tmp_path, monkeypatch, file_kind
):
    """Actual main -> pin -> runtime_digest -> real file reader; gate must never start.

    Clock expiry is injected after one actual file read. This exercises the original
    preflight gap, without claiming to supervise a Git/build process or fix timing.
    """

    clock = Clock()
    prefix = tmp_path / "runtime"
    prefix.mkdir()
    name = "module.py" if file_kind == "source" else "editable.pth"
    (prefix / name).write_text("print('never imported')\n")
    pin = inputs.runtime_digest(prefix, tmp_path)
    plan = make_plan(deadline_seconds=10.0, runtime_inputs={"source": pin})
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.text)
    monkeypatch.setattr(
        worker, "time", SimpleNamespace(monotonic=clock.monotonic, time=lambda: 1000.0)
    )
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(prefix=str(prefix), executable=sys.executable, stderr=sys.stderr),
    )
    monkeypatch.delenv("POKERED_PARALLEL_DEADLINE_EPOCH", raising=False)
    monkeypatch.setenv("POKERED_PARALLEL_JOB_DEADLINE_MONOTONIC", "1.0")
    monkeypatch.setenv("POKERED_PARALLEL_CLEANUP_DEADLINE_MONOTONIC", "2.0")
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError("expired preflight started identity/gate work")

    if file_kind == "source":
        read = inputs.sha256_file

        def delayed_read(path, *, checkpoint):
            result = read(path, checkpoint=checkpoint)
            calls.append(path)
            clock.now = 1.1
            checkpoint()
            return result

        monkeypatch.setattr(inputs, "sha256_file", delayed_read)
    else:
        read = inputs.read_evidence_bytes

        def delayed_read(path, *, checkpoint):
            result = read(path, checkpoint=checkpoint)
            calls.append(path)
            clock.now = 1.1
            checkpoint()
            return result

        monkeypatch.setattr(inputs, "read_evidence_bytes", delayed_read)
    monkeypatch.setattr(worker, "measure_identity", forbidden)
    monkeypatch.setattr(worker.subprocess, "run", forbidden)
    status = worker.main(
        [
            "--plan",
            str(plan_path),
            "--job-id",
            "source-job",
            "--job-root",
            str(tmp_path),
            "--project-root",
            str(tmp_path),
            "--rom-root",
            str(tmp_path),
            "--fixture-root",
            str(tmp_path),
        ]
    )
    assert calls == [prefix / name]
    assert status == worker.TIMEOUT_EXIT_STATUS
    assert not (tmp_path / "out" / TERMINAL_ARTIFACT_NAME).exists()


@pytest.mark.parametrize("point", ["within_cleanup", "spent_cleanup", "failure_serialization"])
def test_actual_failure_publication_cannot_renew_spent_cleanup_time(tmp_path, monkeypatch, point):
    (tmp_path / "out").mkdir()
    clock = Clock()
    clock.now = 2.0 if point == "spent_cleanup" else 1.5
    bound = MeasurementBudget(1.0, 2.0, clock=clock.monotonic)
    artifact = {"overall": "PASS", "child_exit_status": 0, "gate_problems": [], "finished_utc": ""}
    handles = []
    opener = worker._private_output_fd
    encode = worker.json.JSONEncoder.iterencode

    def tracked_open(path, flags):
        fd = opener(path, flags)
        handles.append(fd)
        return fd

    def delayed_encode(encoder, value, *args, **kwargs):
        for index, chunk in enumerate(encode(encoder, value, *args, **kwargs)):
            yield chunk
            if point == "failure_serialization" and value["overall"] == "FAIL" and index == 3:
                clock.now = 2.0

    monkeypatch.setattr(worker, "_private_output_fd", tracked_open)
    monkeypatch.setattr(worker.json.JSONEncoder, "iterencode", delayed_encode)
    target = tmp_path / "out" / TERMINAL_ARTIFACT_NAME
    if point == "within_cleanup":
        status = worker.publish_artifact(
            tmp_path,
            artifact,
            checkpoint=bound.checkpoint,
            cleanup_checkpoint=bound.cleanup_checkpoint,
        )
        assert status == worker.TIMEOUT_EXIT_STATUS
        assert json.loads(target.read_text())["overall"] == "FAIL"
    else:
        with pytest.raises(IdentityDeadline, match="cleanup deadline"):
            worker.publish_artifact(
                tmp_path,
                artifact,
                checkpoint=bound.checkpoint,
                cleanup_checkpoint=bound.cleanup_checkpoint,
            )
        assert not target.exists(), "spent cleanup must not publish a success document"
    assert (
        artifact["overall"] == "FAIL"
        and artifact["child_exit_status"] == worker.TIMEOUT_EXIT_STATUS
    )
    assert not target.with_suffix(".tmp").exists()
    for fd in handles:
        with pytest.raises(OSError):
            os.fstat(fd)
