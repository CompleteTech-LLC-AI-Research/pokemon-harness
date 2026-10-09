"""Authored controls: one live capacity authority, fail closed (#107.2, #107.4, finding 3)."""

import pytest

from scripts.production_gate_parallel_capacity import (
    CapacityError,
    LiveCapacity,
    PairBudget,
    capacity_loss_probe,
    require_parallel_capacity,
)
from tests._parallel_runtime_report_support import make_plan


class Decision:
    def __init__(self, status, reason=""):
        self.status, self.reason = status, reason


class FakeAdmission:
    def __init__(self, free=4):
        self.free, self.active = free, {}

    def admit(self, pair_id):
        if len(self.active) >= self.free:
            return Decision("queued", "no slot")
        self.active[pair_id] = True
        return Decision("ok")

    def release(self, pair_id):
        self.active.pop(pair_id, None)


class FakeSession:
    def __init__(self, status="ok", free=4):
        self.status, self.availability_reasons = status, ["fake"]
        self.admission = FakeAdmission(free)

    def wait_until_available(self):
        return self.status

    def maybe_observe(self):
        return self.status

    def capacity_reason(self):
        return f"capacity {self.status}"


@pytest.mark.parametrize("bad", [0, -1, None, True, 2.0, "2"])
def test_the_budget_refuses_unknown_or_zero_totals(bad):
    with pytest.raises(CapacityError):
        PairBudget(bad)


def test_the_budget_refuses_duplicate_and_excess_reservations():
    budget = PairBudget(2)
    budget.reserve("a", 1)
    with pytest.raises(CapacityError, match="duplicate"):
        budget.reserve("a", 1)
    with pytest.raises(CapacityError, match="exceeds"):
        budget.reserve("b", 2)


def test_the_probe_reports_an_insufficient_first_reading_instead_of_adopting_it_as_a_floor():
    assert capacity_loss_probe(4, measure=lambda: 6)() == 4
    assert capacity_loss_probe(4, measure=lambda: 0)() == 0
    assert capacity_loss_probe(4, measure=lambda: 2)() == 2

    def unreadable():
        raise OSError("quota")

    assert capacity_loss_probe(4, measure=unreadable)() is None


def test_admission_goes_through_the_session_and_records_observations():
    session = FakeSession()
    live = LiveCapacity(PairBudget(4), session=session, probe=capacity_loss_probe(2, lambda: 8))
    live.admit("source-job", 2)
    assert len(session.admission.active) == 2 and live.budget.reserved_total == 2
    assert live.check() == ""
    assert {item["event"] for item in live.observations} >= {"admission", "pair", "probe"}
    live.release("source-job")
    assert not session.admission.active and live.budget.reserved_total == 0


@pytest.mark.parametrize("status", ["unknown", "blocked", "pressure"])
def test_unknown_or_unavailable_capacity_refuses_dispatch_and_releases(status):
    live = LiveCapacity(PairBudget(4), session=FakeSession(status))
    with pytest.raises(CapacityError):
        live.admit("source-job", 1)
    assert live.budget.reserved_total == 0


def test_a_queued_admission_is_a_refusal_not_a_silent_wait():
    live = LiveCapacity(PairBudget(4), session=FakeSession(free=1))
    with pytest.raises(CapacityError, match="queued"):
        live.admit("source-job", 2)
    assert live.budget.reserved_total == 0


def test_a_lost_reservation_or_a_worsening_status_is_reported_by_the_monitor():
    session = FakeSession()
    live = LiveCapacity(PairBudget(4), session=session)
    live.admit("source-job", 2)
    session.admission.active.pop("source-job/pair-1")
    assert "reservation lost" in live.check()
    session.status = "unknown"
    assert "unknown" in live.check()


def test_insufficient_initial_quota_refuses_before_any_dispatch():
    live = LiveCapacity(PairBudget(4), probe=capacity_loss_probe(2, lambda: 1))
    with pytest.raises(CapacityError, match="below the reserved"):
        live.admit("source-job", 2)


def test_parallel_dispatch_needs_a_pair_per_job_of_the_widest_phase():
    plan = make_plan(early_smoke=True, tiers=("unit", "timing"))
    require_parallel_capacity(plan, PairBudget(2))
    with pytest.raises(CapacityError, match="differs"):
        require_parallel_capacity(plan, PairBudget(4))
