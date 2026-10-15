"""Authored ROM-free deadline controls; execution requires separate admission."""

from __future__ import annotations

import json

import pytest

from scripts import produce_battle_scenario as producer
from scripts import produce_battle_scenario_transaction as transaction
from tests._battle_scenario_fixtures_support import (
    _capture_kwargs,
    _FakeSession,
    _prepared_run,
    _run_with_fake_session,
    _writable_scenario,
)


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.failure = None

    def __call__(self):
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        return self.now


def _prepared(tmp_path, report=True):
    prepared = _prepared_run(tmp_path)
    prepared.update(out=tmp_path / "out.state", root=tmp_path)
    if report:
        prepared["report"] = tmp_path / "report.json"
    return prepared


def _at_phase(monkeypatch, clock, target, change):
    real_admit = transaction._OperationClock.admit
    reached = []

    def admit(self, phase):
        if phase == target and not reached:
            reached.append(phase)
            change(clock)
        return real_admit(self, phase)

    monkeypatch.setattr(transaction._OperationClock, "admit", admit)
    return reached


@pytest.mark.parametrize("report", [False, True])
@pytest.mark.parametrize("final", [180.0, 181.0])
def test_original_deadline_survives_delayed_measurement(tmp_path, monkeypatch, report, final):
    """D01: the 0/30/179/181 schedule must not become a 30-origin window."""
    prepared = _prepared(tmp_path, report)
    clock = _Clock()
    session = _FakeSession()

    def measure(**_kwargs):
        clock.now = 30.0
        return {"mode": "source", "measured": True, "executable": "/test/python"}

    monkeypatch.setattr(producer, "measure_runtime_identity", measure)
    monkeypatch.setattr(producer, "_default_session_factory", lambda **_kwargs: session)
    real_link = producer.os.link

    def link(source, destination):
        real_link(source, destination)
        clock.now = 179.0

    monkeypatch.setattr(producer.os, "link", link)
    reached = _at_phase(monkeypatch, clock, "capture final admission",
                        lambda value: setattr(value, "now", final))
    with pytest.raises(producer.CaptureBoundsExceeded):
        _run_with_fake_session(prepared, clock=clock,
                              capture=producer.capture_battle_scenario)
    assert reached and session.closed
    assert not prepared["out"].exists()
    assert not prepared.get("report", tmp_path / "absent").exists()


@pytest.mark.parametrize("phase", ["capture final admission", "run final admission"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_final_clock_interrupt_rolls_back_real_publications(tmp_path, monkeypatch, phase, error_type):
    """D02/D03: the clock itself raises after fixture/report publication."""
    prepared = _prepared(tmp_path)
    prior = b"prior report"
    prepared["report"].write_bytes(prior)
    clock = _Clock()
    failure = error_type("final clock")
    reached = _at_phase(monkeypatch, clock, phase,
                        lambda value: setattr(value, "failure", failure))
    with pytest.raises(error_type) as caught:
        _run_with_fake_session(prepared, clock=clock)
    assert caught.value is failure and reached
    assert not prepared["out"].exists()
    assert prepared["report"].read_bytes() == prior


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_invalid_final_sample_cannot_admit_output(tmp_path, monkeypatch, value):
    """D01 variant: NaN and backward samples cannot evade comparison."""
    prepared = _prepared(tmp_path)
    clock = _Clock()
    reached = _at_phase(monkeypatch, clock, "run final admission",
                        lambda timer: setattr(timer, "now", value))
    with pytest.raises(producer.ScenarioRefusal, match="nonfinite or backward"):
        _run_with_fake_session(prepared, clock=clock)
    assert reached and not prepared["out"].exists()
    assert not prepared["report"].exists()


@pytest.mark.parametrize("origin", [float("nan"), float("inf")])
def test_nonfinite_origin_is_refused_before_resources(tmp_path, origin):
    clock = _Clock()
    clock.now = origin
    prepared = _prepared(tmp_path)
    with pytest.raises(producer.ScenarioRefusal, match="finite"):
        _run_with_fake_session(prepared, clock=clock)
    assert not prepared["out"].exists()
    assert transaction.active_scope() is None


@pytest.mark.parametrize("phase", [
    "prior report copy", "prior report payload sync", "prior report manifest sync",
    "recovery verification", "recovery directory sync", "report staging close",
    "report publication",
])
def test_normal_report_work_stops_at_deadline(tmp_path, monkeypatch, phase):
    """D05/D06: normal copy and sync must not create a new success window."""
    prepared = _prepared(tmp_path)
    prior = b"persisted prior"
    prepared["report"].write_bytes(prior)
    clock = _Clock()
    reached = _at_phase(monkeypatch, clock, phase,
                        lambda timer: setattr(timer, "now", 180.0))
    with pytest.raises(producer.CaptureBoundsExceeded):
        _run_with_fake_session(prepared, clock=clock)
    assert reached and prepared["report"].read_bytes() == prior
    assert not prepared["out"].exists()


@pytest.mark.parametrize("phase", ["fixture digest", "record preparation", "state save"])
def test_capture_slow_phases_still_use_original_deadline(tmp_path, monkeypatch, phase):
    clock = _Clock()
    session = _FakeSession()
    reached = _at_phase(monkeypatch, clock, phase,
                        lambda timer: setattr(timer, "now", 180.0))
    with pytest.raises(producer.CaptureBoundsExceeded):
        producer.capture_battle_scenario(**_capture_kwargs(
            tmp_path, _writable_scenario(), clock=clock,
            session_factory=lambda **_kwargs: session))
    assert reached and session.closed
    assert not (tmp_path / "out.state").exists()


def test_scoped_report_and_final_returned_total_are_distinct(tmp_path, monkeypatch):
    """D04/D08: nonzero exact values reject zero duration and blanket refusal."""
    prepared = _prepared(tmp_path)
    clock = _Clock()
    real_link = producer.os.link
    real_writer = producer.write_report

    def link(source, target):
        real_link(source, target)
        clock.now = 5.0

    def writer(path, payload):
        clock.now = 8.0
        publication = real_writer(path, payload)
        clock.now = 12.0
        return publication

    monkeypatch.setattr(producer.os, "link", link)
    monkeypatch.setattr(producer, "write_report", writer)  # legacy two-argument patch
    plan = _run_with_fake_session(prepared, clock=clock)
    written = json.loads(prepared["report"].read_text())
    assert plan["capture"] == written["capture"]
    assert written["capture"]["wall_seconds"] == 5.0
    assert written["report_timing"]["wall_seconds"] == 5.0
    assert written["report_timing"]["final_total_observed"] is False
    assert "operation_timing" not in written
    assert plan["operation_timing"]["wall_seconds"] == 12.0
    assert plan["operation_timing"]["report_finalized"] is True
    assert prepared["out"].read_bytes() == b"fake-bounded-capture-state"
    assert transaction.active_scope() is None


@pytest.mark.parametrize("report", [False, True])
def test_timely_positive_run_and_direct_capture(tmp_path, report):
    clock = _Clock()
    prepared = _prepared(tmp_path, report)
    plan = _run_with_fake_session(prepared, clock=clock)
    assert prepared["out"].read_bytes() == b"fake-bounded-capture-state"
    assert plan["operation_timing"]["report_finalized"] is report
    directory = tmp_path / "direct"
    directory.mkdir()
    session = _FakeSession()
    record = producer.capture_battle_scenario(**_capture_kwargs(
        directory, _writable_scenario(), clock=clock,
        session_factory=lambda **_kwargs: session))
    assert record["wall_seconds_scope"] == "operation origin through fixture admission"
    assert session.closed and (directory / "out.state").read_bytes() == session.payload


def test_nested_clock_domain_mismatch_resets_context(tmp_path):
    clock = _Clock()
    prepared = _prepared(tmp_path)

    def capture(**kwargs):
        kwargs["clock"] = _Clock()
        return producer.capture_battle_scenario(
            **kwargs, session_factory=lambda **_kwargs: _FakeSession())

    with pytest.raises(producer.ScenarioRefusal, match="clock domain"):
        _run_with_fake_session(prepared, clock=clock, capture=capture)
    assert transaction.active_scope() is None
    assert not prepared["out"].exists()


def test_reentrant_run_has_own_resources_and_tighter_deadline(tmp_path):
    """D07: successful independent nested run is not parent's rollback resource."""
    clock = _Clock()
    outer_dir = tmp_path / "outer"
    inner_dir = tmp_path / "inner"
    outer_dir.mkdir()
    inner_dir.mkdir()
    outer = _prepared(outer_dir)
    inner = _prepared(inner_dir, False)
    observed = []

    def capture(**kwargs):
        parent = transaction.active_scope()
        inner["scenario"]["capture_bounds"]["max_wall_seconds"] = 1000.0
        plan = _run_with_fake_session(inner, clock=clock)
        assert transaction.active_scope() is parent
        observed.append(plan)
        raise OSError("outer failure")

    with pytest.raises(OSError, match="outer failure"):
        _run_with_fake_session(outer, clock=clock, capture=capture)
    assert observed and inner["out"].exists()
    assert not outer["out"].exists()
    assert transaction.active_scope() is None


def test_explicit_deadline_never_extends_direct_capture_window(tmp_path):
    """A caller's later deadline cannot enlarge the declared capture bound."""
    clock = _Clock()
    session = _FakeSession()

    def factory(**_kwargs):
        clock.now = 180.0
        return session

    with pytest.raises(producer.CaptureBoundsExceeded):
        producer.capture_battle_scenario(**_capture_kwargs(
            tmp_path, _writable_scenario(), deadline=1000.0, clock=clock,
            session_factory=factory))
    assert session.closed and not (tmp_path / "out.state").exists()


@pytest.mark.parametrize("limit", [float("nan"), float("inf"), 1e309])
def test_nonfinite_explicit_deadline_refuses(tmp_path, limit):
    with pytest.raises(producer.ScenarioRefusal, match="finite"):
        producer.capture_battle_scenario(**_capture_kwargs(
            tmp_path, _writable_scenario(), deadline=limit,
            session_factory=lambda **_kwargs: _FakeSession()))
    assert not (tmp_path / "out.state").exists()


@pytest.mark.parametrize("inner_failure", [False, True], ids=["inner-success", "caught-inner-failure"])
@pytest.mark.parametrize("outer_time", [80.0, 120.0], ids=["backward-refused", "timely-positive"])
def test_reentrant_every_observation_survives_caught_failure(
    tmp_path, inner_failure, outer_time
):
    """F2: real nested runs share observations, never origins or resource lists."""
    from scripts import produce_battle_scenario_capture as capture_module

    outer_dir, inner_dir = tmp_path / "outer", tmp_path / "inner"
    outer_dir.mkdir()
    inner_dir.mkdir()
    outer, inner = _prepared(outer_dir, False), _prepared(inner_dir, False)
    extra = outer_dir / "separately-owned"
    clock = _Clock()
    primary = RuntimeError("inner operation failed after observing 100")
    scopes, inner_results = [], []

    def inner_capture(**kwargs):
        scopes.append(transaction.active_scope())
        result = producer.capture_battle_scenario(
            **kwargs, session_factory=lambda **_kwargs: _FakeSession())
        clock.now = 100.0
        transaction.normal_guard("inner observation before possible failure")
        if inner_failure:
            raise primary
        return result

    def outer_capture(**kwargs):
        parent = transaction.active_scope()
        scopes.append(parent)
        capture_module._write_fixture_exclusive(extra, b"outer resource")
        clock.now = 30.0
        try:
            inner_results.append(_run_with_fake_session(
                inner, clock=clock, capture=inner_capture))
        except RuntimeError as caught:
            assert inner_failure and caught is primary
        assert transaction.active_scope() is parent
        assert scopes[0] is not scopes[1] and scopes[0].actions is not scopes[1].actions
        assert parent.timing.started == 0.0 and parent.timing.deadline == 180.0
        assert scopes[1].timing.started == 30.0 and scopes[1].timing.deadline == 180.0
        assert parent.timing.observations is scopes[1].timing.observations
        assert parent.timing.observations.last == 100.0
        clock.now = outer_time
        return producer.capture_battle_scenario(
            **kwargs, session_factory=lambda **_kwargs: _FakeSession())

    if outer_time < 100.0:
        with pytest.raises(producer.ScenarioRefusal, match="backward"):
            _run_with_fake_session(outer, clock=clock, capture=outer_capture)
        assert not outer["out"].exists() and not extra.exists()
    else:
        plan = _run_with_fake_session(outer, clock=clock, capture=outer_capture)
        assert plan["operation_timing"]["wall_seconds"] == 120.0
        assert outer["out"].read_bytes() == b"fake-bounded-capture-state"
        assert extra.read_bytes() == b"outer resource"
    assert inner["out"].exists() is (not inner_failure)
    if not inner_failure:
        assert inner["out"].read_bytes() == b"fake-bounded-capture-state"
        assert inner_results[0]["operation_timing"]["wall_seconds"] == 70.0
    assert transaction.active_scope() is None
