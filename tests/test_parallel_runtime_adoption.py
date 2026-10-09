"""Actual subreaper fault boundaries, authored only until execution is admitted.

These controls launch no children. They inject faults at the restoration syscall
or readback while all other operations reach libc, and independently restore the
test process's original flag. Their result cannot prove process containment.
"""

import ctypes

import pytest

from scripts.production_gate_parallel_adoption import (
    PR_GET_CHILD_SUBREAPER,
    PR_SET_CHILD_SUBREAPER,
    read_subreaper,
)
from scripts.production_gate_parallel_processes import OrchestratorAdoption


class RestorationFault:
    def __init__(self, libc, point):
        self.libc, self.point = libc, point
        self.armed = True
        self.calls = []

    def prctl(self, operation, *args):
        self.calls.append(operation)
        if self.armed:
            if self.point == "set" and operation == PR_SET_CHILD_SUBREAPER:
                ctypes.set_errno(1)
                return -1
            if self.point == "read" and operation == PR_GET_CHILD_SUBREAPER:
                ctypes.set_errno(5)
                return -1
            if self.point == "unchanged" and operation == PR_SET_CHILD_SUBREAPER:
                return 0  # dishonest success must be rejected by actual kernel readback
        return self.libc.prctl(operation, *args)


@pytest.mark.parametrize(
    "previous,point", [(0, "set"), (1, "set"), (0, "read"), (1, "read"), (0, "unchanged")]
)
@pytest.mark.parametrize("body_failure", [False, True])
def test_restoration_fault_preserves_error_and_retry_obligation(previous, point, body_failure):
    libc = ctypes.CDLL(None, use_errno=True)
    original_flag = read_subreaper(libc)
    owner = OrchestratorAdoption()
    original_error = ValueError("controlled original body failure")
    fault = None
    try:
        assert libc.prctl(PR_SET_CHILD_SUBREAPER, previous, 0, 0, 0) == 0
        assert read_subreaper(libc) == previous
        expected = ValueError if body_failure else RuntimeError
        message = "controlled original body failure" if body_failure else "restoration unproven"
        with pytest.raises(expected, match=message) as raised, owner:
            assert owner.active and owner.verify()
            fault = RestorationFault(owner._libc, point)
            owner._libc = fault
            if body_failure:
                raise original_error
        if body_failure:
            assert raised.value is original_error
            assert any("restoration unproven" in note for note in raised.value.__notes__)
        assert owner.restoration_pending is True
        assert owner.restoration_problem in owner.reason
        assert owner.active is True, "unknown restoration cannot clear active bookkeeping"
        assert PR_SET_CHILD_SUBREAPER in fault.calls
        if point != "set":
            assert PR_GET_CHILD_SUBREAPER in fault.calls
        assert read_subreaper(libc) == (previous if point == "read" else 1)
        with pytest.raises(RuntimeError, match="prior subreaper ownership is unresolved"):
            owner.__enter__()
        assert owner._previous == previous, "re-entry must not overwrite the original flag"
        fault.armed = False
        owner.__exit__()
        assert read_subreaper(libc) == previous
        assert not owner.restoration_pending and not owner.active
        assert owner.restoration_problem == ""
        owner.__exit__()  # discharged restoration is idempotent
    finally:
        # Outer rescue is not a successful product restoration result. It runs even
        # if an assertion above fails, and does not invoke a changed project helper.
        assert libc.prctl(PR_SET_CHILD_SUBREAPER, original_flag, 0, 0, 0) == 0
        assert read_subreaper(libc) == original_flag


def test_failed_enable_retains_the_original_flag_until_checked_restoration(monkeypatch):
    libc = ctypes.CDLL(None, use_errno=True)
    original_flag = read_subreaper(libc)
    fault = RestorationFault(libc, "set")
    owner = OrchestratorAdoption()
    try:
        monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: fault)
        owner.__enter__()
        assert not owner.active
        assert "not established" in owner.reason
        assert owner.restoration_pending
        assert owner._previous == original_flag
        fault.armed = False
        owner.__exit__()
        assert read_subreaper(libc) == original_flag
        assert not owner.restoration_pending and not owner.active
    finally:
        assert libc.prctl(PR_SET_CHILD_SUBREAPER, original_flag, 0, 0, 0) == 0
        assert read_subreaper(libc) == original_flag
