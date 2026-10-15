"""Handler ownership controls without delivering operating-system signals."""

import io
from pathlib import Path

import pytest

from tests._gate_capacity_support import _matrix_row_prepare, gate

# isort: split
# Load the shared facade first, matching the existing capacity test modules.
from scripts import production_gate_capacity as capacity
from scripts import production_gate_matrix as matrix
from scripts import production_gate_tiers as tiers


@pytest.fixture
def registry(monkeypatch):
    class Registry:
        def __init__(self):
            self.deliveries = []
            self.previous = lambda signum, frame: self.deliveries.append(signum)
            self.current = self.previous
            self.transitions = []

        def replace(self, signum, handler):
            assert signum == capacity.signal.SIGINT
            previous = self.current
            self.current = handler
            self.transitions.append(handler)
            return previous

        def deliver(self):
            self.current(capacity.signal.SIGINT, None)

        def assert_restored(self):
            assert self.current is self.previous
            self.deliver()
            assert self.deliveries == [capacity.signal.SIGINT]

    registry = Registry()
    monkeypatch.setattr(capacity.signal, "signal", registry.replace)
    return registry


def _run(tmp_path, **overrides):
    arguments = {
        "name": "unit",
        "project_root": tmp_path,
        "python_executable": Path("python"),
        "environment": {},
        "required_problems": [],
        "repeat": 1,
        "timeout_override": 5,
        "report_directory": tmp_path,
    }
    arguments.update(overrides)
    return gate.run_tier(**arguments)


def _passing(**kwargs):
    return 0, gate.GateReport(gate.Counts(total=1, passed=1)), "PASS-OUTPUT", ["pytest"]


def test_normal_publication_precedes_restoration(tmp_path, monkeypatch, registry):
    monkeypatch.setattr(gate, "run_pytest_once", _passing)
    published = []

    def publish(result):
        assert registry.current is not registry.previous
        published.append(result)

    result = _run(tmp_path, publish=publish)
    assert result.status == "PASS"
    assert published == [result]
    registry.assert_restored()


def test_between_repetitions_restores_and_stops_dispatch(tmp_path, monkeypatch, registry):
    calls = []
    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def run_once(**kwargs):
        calls.append(kwargs["expression"])
        if len(calls) == 2:
            raise KeyboardInterrupt("between repetitions")
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "ORIGINAL-FAILURE"},
                ),
            ),
            "ORIGINAL-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    result = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=Path("python"),
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["timing", "unit"],
        required_tests_by_tier={},
        required_nodeids_by_tier={},
        timeout_override=5,
    )[0]
    partial, unstarted = result.tiers
    assert len(calls) == 2
    assert partial.status == "INTERRUPTED"
    assert partial.counts.total == partial.counts.failed == 1
    assert partial.returncodes == [1]
    assert "ORIGINAL-OUTPUT" in partial.output_tail
    assert any("ORIGINAL-FAILURE" in detail.reason for detail in partial.failure_details)
    assert result.completed_tiers["timing"] is partial
    assert result.cancellation
    assert unstarted.name == "unit" and unstarted.status == "NOT_STARTED"
    registry.assert_restored()


def test_deferred_finalization_interrupt_retains_published_result(tmp_path, monkeypatch, registry):
    monkeypatch.setattr(gate, "run_pytest_once", _passing)
    published = []

    def publish(result):
        published.append(result)
        registry.deliver()

    with pytest.raises(capacity.TierInterruptedDuringCleanup) as caught:
        _run(tmp_path, publish=publish)
    assert caught.value.tier is published[0]
    assert caught.value.tier.counts.passed == 1
    assert "finalization interrupted" in caught.value.reason
    registry.assert_restored()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("store_first", [False, True])
def test_publisher_failure_preserves_exception_and_restores(
    tmp_path, monkeypatch, registry, error_type, store_first
):
    monkeypatch.setattr(gate, "run_pytest_once", _passing)
    original = error_type("publisher failure")
    published = []

    def publish(result):
        assert registry.current is not registry.previous
        if store_first:
            published.append(result)
        registry.deliver()
        raise original

    with pytest.raises(error_type) as caught:
        _run(tmp_path, publish=publish)
    assert caught.value is original
    assert len(published) == int(store_first)
    if store_first:
        assert published[0].counts.passed == 1
    registry.assert_restored()


def test_assembly_failure_restores_before_propagation(tmp_path, monkeypatch, registry):
    monkeypatch.setattr(gate, "run_pytest_once", _passing)
    original = RuntimeError("assembly failure")

    def fail_assembly(**kwargs):
        raise original

    monkeypatch.setattr(tiers, "TierResult", fail_assembly)
    with pytest.raises(RuntimeError) as caught:
        _run(tmp_path)
    assert caught.value is original
    registry.assert_restored()


def test_nested_and_sequential_ownership(tmp_path, monkeypatch, registry):
    monkeypatch.setattr(gate, "run_pytest_once", _passing)
    outer = capacity._defer_sigint_while_finalizing()
    outer_handler = registry.current
    try:
        first = _run(tmp_path)
        assert first.status == "PASS"
        assert registry.current is outer_handler
        registry.deliver()
        assert outer.triggered
    finally:
        outer.restore()
    assert registry.current is registry.previous
    second = _run(tmp_path)
    assert second.status == "PASS"
    registry.assert_restored()


def test_between_repeat_cancellation_keeps_priority_and_restores_outer(
    tmp_path, monkeypatch, registry
):
    calls = []
    published = []

    def run_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise KeyboardInterrupt("original repetition cancellation")
        return _passing(**kwargs)

    def publish(result):
        published.append(result)
        registry.deliver()

    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    outer = capacity._defer_sigint_while_finalizing()
    outer_handler = registry.current
    try:
        with pytest.raises(capacity.TierInterruptedDuringCleanup) as caught:
            _run(tmp_path, name="timing", publish=publish)
        assert caught.value.tier is published[0]
        assert caught.value.tier.counts.passed == 1
        assert caught.value.tier.status == "INTERRUPTED"
        assert "cancelled between timing repetitions" in caught.value.reason
        assert registry.current is outer_handler
        assert not outer.triggered
        registry.deliver()
        assert outer.triggered
    finally:
        outer.restore()
    assert registry.current is registry.previous
    monkeypatch.setattr(gate, "run_pytest_once", _passing)
    assert _run(tmp_path).status == "PASS"
    registry.assert_restored()


MATRIX_ROW = "tests/test_matrix.py::test_pair[aaa-fail]"


@pytest.fixture
def matrix_workers(monkeypatch):
    """Synchronous in-memory worker, stream and report; no process or thread."""
    started = []

    class Process:
        def __init__(self, command, **kwargs):
            assert command[3] == MATRIX_ROW
            started.append(command[3])
            self.stdout = io.StringIO("MATRIX-ORIGINAL-OUTPUT")

        def poll(self):
            return 1

    class Reader:
        def __init__(self, *, target, args, **kwargs):
            self.target = target
            self.args = args
            self.ident = None

        def start(self):
            self.ident = 1
            self.target(*self.args)

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    report = gate.GateReport(
        gate.Counts(total=1, failed=1),
        nodeids=(MATRIX_ROW,),
        failed_records=(
            {"nodeid": MATRIX_ROW, "outcome": "failed", "reason": "MATRIX-ORIGINAL-FAILURE"},
        ),
    )
    monkeypatch.setattr(matrix.subprocess, "Popen", Process)
    monkeypatch.setattr(matrix.threading, "Thread", Reader)
    monkeypatch.setattr(matrix.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(matrix.time, "sleep", lambda duration: None)
    monkeypatch.setattr(matrix, "_process_creation_kwargs", dict)
    monkeypatch.setattr(matrix, "_load_gate_report", lambda *args, **kwargs: report)
    return started


def _run_matrix(tmp_path, **overrides):
    arguments = {
        "name": "trade",
        "project_root": tmp_path,
        "python_executable": Path("python"),
        "environment": {},
        "required_problems": [],
        "timeout_override": 5,
        "report_directory": tmp_path,
        "required_nodeids": (MATRIX_ROW,),
    }
    arguments.update(overrides)
    return gate.run_matrix_tier(**arguments)


def _assert_matrix_failure(result):
    assert result.status == "FAIL"
    assert result.counts.total == result.counts.failed == 1
    assert result.returncodes == [1]
    assert result.selected_nodeids == [MATRIX_ROW]
    assert len(result.case_results) == 1
    assert result.case_results[0].status == "FAIL"
    assert "MATRIX-ORIGINAL-OUTPUT" in result.output_tail
    assert any("MATRIX-ORIGINAL-FAILURE" in detail.reason for detail in result.failure_details)


def test_matrix_normal_publication_precedes_restore(tmp_path, registry, matrix_workers):
    published = {}

    def publish(result):
        assert registry.current is not registry.previous
        published[result.name] = result

    result = _run_matrix(tmp_path, publish=publish)
    assert published["trade"] is result
    _assert_matrix_failure(result)
    assert matrix_workers == [MATRIX_ROW]
    registry.assert_restored()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("store_first", [False, True])
def test_matrix_publisher_failure_restores_primary_exception(
    tmp_path, registry, matrix_workers, error_type, store_first
):
    original = error_type("matrix publisher failure")
    published = {}

    def publish(result):
        assert registry.current is not registry.previous
        if store_first:
            published[result.name] = result
        registry.deliver()
        raise original

    with pytest.raises(error_type) as caught:
        _run_matrix(tmp_path, publish=publish)
    assert caught.value is original
    assert bool(published) == store_first
    if store_first:
        _assert_matrix_failure(published["trade"])
    assert matrix_workers == [MATRIX_ROW]
    registry.assert_restored()


def test_matrix_assembly_failure_restores(tmp_path, monkeypatch, registry, matrix_workers):
    original = RuntimeError("matrix assembly failure")
    published = []

    def fail_assembly(**kwargs):
        raise original

    monkeypatch.setattr(matrix, "TierResult", fail_assembly)
    with pytest.raises(RuntimeError) as caught:
        _run_matrix(tmp_path, publish=published.append)
    assert caught.value is original
    assert not published
    assert matrix_workers == [MATRIX_ROW]
    registry.assert_restored()


def test_matrix_finalization_cancellation_keeps_failure_and_stops_dispatch(
    tmp_path, monkeypatch, registry, matrix_workers
):
    real_sink = tiers._completed_tier_sink

    def interrupting_sink(prepared):
        publish = real_sink(prepared)

        def store_then_interrupt(result):
            assert registry.current is not registry.previous
            publish(result)
            registry.deliver()

        return store_then_interrupt

    def unexpected_batch(**kwargs):
        pytest.fail("a later tier was dispatched after matrix cancellation")

    monkeypatch.setattr(tiers, "_completed_tier_sink", interrupting_sink)
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (MATRIX_ROW,)}))
    monkeypatch.setattr(gate, "run_pytest_once", unexpected_batch)
    result = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=Path("python"),
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["trade", "unit"],
        required_tests_by_tier={},
        required_nodeids_by_tier={"trade": (MATRIX_ROW,)},
        timeout_override=5,
    )[0]
    failed, unstarted = result.tiers
    _assert_matrix_failure(failed)
    assert result.completed_tiers["trade"] is failed
    assert "matrix finalization interrupted" in result.cancellation
    assert unstarted.name == "unit" and unstarted.status == "NOT_STARTED"
    assert matrix_workers == [MATRIX_ROW]
    registry.assert_restored()


@pytest.mark.parametrize("interrupt_finalization", [False, True])
def test_matrix_nested_then_sequential_ownership(
    tmp_path, registry, matrix_workers, interrupt_finalization
):
    published = []
    outer = capacity._defer_sigint_while_finalizing()
    outer_handler = registry.current

    def publish(result):
        published.append(result)
        if interrupt_finalization:
            registry.deliver()

    try:
        if interrupt_finalization:
            with pytest.raises(capacity.TierInterruptedDuringCleanup) as caught:
                _run_matrix(tmp_path, publish=publish)
            assert caught.value.tier is published[0]
        else:
            assert _run_matrix(tmp_path, publish=publish) is published[0]
        _assert_matrix_failure(published[0])
        assert registry.current is outer_handler
        assert not outer.triggered
        registry.deliver()
        assert outer.triggered
    finally:
        outer.restore()
    assert registry.current is registry.previous
    _assert_matrix_failure(_run_matrix(tmp_path))
    assert matrix_workers == [MATRIX_ROW, MATRIX_ROW]
    registry.assert_restored()
