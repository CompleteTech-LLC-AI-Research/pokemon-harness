"""Authored cleanup/ownership controls, not an execution or crash certificate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import produce_battle_scenario as producer
from scripts import produce_battle_scenario_capture as capture
from scripts import produce_battle_scenario_recovery as recovery_module
from scripts import produce_battle_scenario_transaction as transaction
from scripts.produce_battle_scenario_recovery import _ReportRecovery
from tests._battle_scenario_fixtures_support import (
    _capture_kwargs,
    _FakeSession,
    _prepared_run,
    _run_with_fake_session,
    _staged_files,
    _writable_scenario,
)
from tests._battle_scenario_producer_cleanup_support import _recovery_handle_faults, _token

PRIOR = b"exact prior report recovery bytes"


def _reopen(directory):
    manifests = list(directory.glob(".*.recovery-*/manifest.json"))
    assert len(manifests) == 1
    recovery = _ReportRecovery.reopen(manifests[0])
    assert recovery.read_verified() == PRIOR
    return recovery


@pytest.mark.parametrize("error_type", [PermissionError, OSError, KeyboardInterrupt, SystemExit])
def test_probe_uncertainty_is_not_foreign_or_absent(tmp_path, monkeypatch, error_type):
    """C01: real inode observation errors cannot become false successful cleanup."""
    path = tmp_path / "fixture"
    path.write_bytes(b"owned")
    token = _token(path)
    real_lstat = os.lstat

    def probe(target, *args, **kwargs):
        if Path(target) == path:
            raise error_type("probe unavailable")
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", probe)
    result = token.withdraw_result()
    assert result.disposition == "unresolved"
    assert result.observation == "unresolved"
    assert result.exception_type == error_type.__name__
    assert path.read_bytes() == b"owned"
    assert token.is_owned() is False  # bool adapter is explicitly not cleanup evidence


@pytest.mark.parametrize("report", [False, True])
def test_absent_fixture_and_report_have_different_recovery_meaning(tmp_path, report):
    """C08 variant: absence does not mean a report's prior bytes were restored."""
    target = tmp_path / "entry"
    if report:
        target.write_bytes(PRIOR)
        token = producer.write_report(target, {"new": True})
    else:
        target.write_bytes(b"fixture")
        token = _token(target)
    target.unlink()
    result = token.withdraw_result()
    assert result.observation == "absent"
    assert result.disposition == ("unresolved" if report else "foreign-preserved")
    assert not target.exists()
    if report:
        assert _reopen(tmp_path).read_verified() == PRIOR


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, OSError])
def test_first_cleanup_failure_does_not_skip_real_second_withdrawal(tmp_path, error_type):
    """C02: action invocation is independent and primary identity survives."""
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    primary = RuntimeError("original operation")
    attempted = []
    with pytest.raises(RuntimeError) as caught, transaction.resource_scope() as scope:
        publication = producer.write_report(report, {"new": True})

        # Added later, therefore attempted first during reverse unwinding.
        def fail():
            attempted.append(1)
            raise error_type("first withdrawal")

        scope.action("first", tmp_path / "other", "first withdrawal", fail)
        assert publication.recovery_metadata() is not None
        raise primary
    assert caught.value is primary and attempted == [1]
    assert report.read_bytes() == PRIOR
    assert _reopen(tmp_path).read_verified() == PRIOR
    assert any(row.get("exception_type") == error_type.__name__ for row in primary.producer_cleanup)


@pytest.mark.parametrize("withdraw_failure", [False, True])
def test_fixture_interruption_and_stage_failure_retain_primary(
    tmp_path, monkeypatch, withdraw_failure
):
    """C03: lower helper removes fixture despite interrupted staging cleanup."""
    real_link, real_unlink = os.link, os.unlink
    primary = KeyboardInterrupt("link returned after creation")
    stage_attempts = []
    real_withdraw = capture._OwnedPublication.withdraw_result

    def withdraw(self):
        if withdraw_failure and self.path == tmp_path / "fixture":
            raise OSError("first fixture withdrawal interrupted")
        return real_withdraw(self)

    monkeypatch.setattr(capture._OwnedPublication, "withdraw_result", withdraw)

    def link(source, destination):
        real_link(source, destination)
        raise primary

    def unlink(path, *args, **kwargs):
        if ".partial-" in Path(path).name:
            stage_attempts.append(Path(path))
            raise SystemExit("stage cleanup interrupted")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(KeyboardInterrupt) as caught:
        capture._write_fixture_exclusive(tmp_path / "fixture", b"bytes")
    assert caught.value is primary
    assert (tmp_path / "fixture").exists() is withdraw_failure
    if withdraw_failure:
        assert any(row.get("exception_type") == "OSError" for row in primary.producer_cleanup)
    assert len(stage_attempts) == 1 and stage_attempts[0].read_bytes() == b"bytes"
    assert any(
        row["path"] == str(stage_attempts[0]) and row["disposition"] == "unresolved"
        for row in primary.producer_cleanup
    )


def test_report_staging_failure_does_not_skip_outer_fixture(tmp_path, monkeypatch):
    """C04: stage cleanup failure cannot strand another real publication."""
    primary = OSError("report rename")
    real_replace, real_unlink = os.replace, os.unlink
    fixture = tmp_path / "fixture"
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    attempts = []

    def replace(source, target):
        if Path(target) == report:
            raise primary
        return real_replace(source, target)

    def unlink(path, *args, **kwargs):
        if ".report.json.partial-" in Path(path).name:
            attempts.append(Path(path))
            raise KeyboardInterrupt("report stage")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(OSError) as caught, transaction.resource_scope():
        capture._write_fixture_exclusive(fixture, b"fixture")
        producer.write_report(report, {"new": True})
    assert caught.value is primary and len(attempts) == 1
    assert not fixture.exists() and report.read_bytes() == PRIOR
    _reopen(tmp_path)


def test_restore_staging_cleanup_does_not_mask_restore_failure(tmp_path, monkeypatch):
    """C05: canonical prior survives restore and restore-stage cleanup errors."""
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    token = producer.write_report(report, {"new": True})
    current = report.read_bytes()
    real_unlink = os.unlink

    def replace(*_args):
        raise PermissionError("restore replace refused")

    def unlink(path, *args, **kwargs):
        if ".partial-" in Path(path).name:
            raise KeyboardInterrupt("restore stage cleanup")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    result = token.withdraw_result()
    assert result.disposition == "unresolved"
    assert result.exception_type == "PermissionError"
    assert "restore replace refused" in result.error
    assert any(
        row.get("exception_type") == "KeyboardInterrupt" and ".partial-" in row["path"]
        for row in result.cleanup
    )
    assert report.read_bytes() == current
    _reopen(tmp_path)


@pytest.mark.parametrize("prior_failure", [False, True])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_session_cleanup_interrupt_never_becomes_success(tmp_path, prior_failure, error_type):
    """C06: close exactly once, retaining an earlier primary when present."""
    primary = producer.CapturePreconditionFailed("drive failed")
    close_failure = error_type("close interrupted")

    class Session(_FakeSession):
        closes = 0

        def step(self, frames=1, **kwargs):
            if prior_failure:
                # A declared refusal is retained rather than translated by capture.
                raise primary
            return super().step(frames, **kwargs)

        def close(self):
            self.closes += 1
            raise close_failure

    session = Session()
    expected = producer.CapturePreconditionFailed if prior_failure else error_type
    with pytest.raises(expected) as caught:
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path, _writable_scenario(), session_factory=lambda **_kwargs: session
            )
        )
    if prior_failure:
        assert caught.value is primary
        assert any(
            row.get("exception_type") == error_type.__name__
            for row in caught.value.producer_cleanup
        )
    else:
        assert caught.value is close_failure
    assert session.closes == 1 and not (tmp_path / "out.state").exists()


def test_broken_repr_and_add_note_do_not_skip_real_cleanup(tmp_path):
    """C07: diagnostic production is isolated before the next real action."""

    class Primary(RuntimeError):
        def add_note(self, _note):
            raise SystemExit("note failed")

    class Secondary(OSError):
        def __repr__(self):
            raise KeyboardInterrupt("repr failed")

        def __str__(self):
            raise KeyboardInterrupt("str failed")

    primary = Primary("original")
    path = tmp_path / "owned"
    path.write_bytes(b"owned")
    with pytest.raises(Primary) as caught, transaction.resource_scope() as scope:
        scope.publication(_token(path))

        def fail():
            raise Secondary()

        scope.action("failure", tmp_path / "unresolved", "broken diagnostic", fail)
        raise primary
    assert caught.value is primary and not path.exists()
    assert any(row["phase"] == "diagnostic attachment" for row in primary.producer_cleanup)


def test_expired_cleanup_restores_prior_without_new_success_window(tmp_path, monkeypatch):
    """X01/X02: expired restoration is cleanup, not normal guarded publication."""
    prepared = _prepared_run(tmp_path)
    prepared.update(out=tmp_path / "out.state", root=tmp_path, report=tmp_path / "report.json")
    prepared["report"].write_bytes(PRIOR)
    now = [0.0]
    real_writer = producer.write_report
    attempted = []

    def writer(path, payload):
        result = real_writer(path, payload)
        now[0] = 181.0
        return result

    real_withdraw = capture._OwnedPublication.withdraw_result

    def withdraw(self):
        attempted.append(self.path)
        return real_withdraw(self)

    monkeypatch.setattr(producer, "write_report", writer)
    monkeypatch.setattr(capture._OwnedPublication, "withdraw_result", withdraw)
    with pytest.raises(producer.CaptureBoundsExceeded) as caught:
        _run_with_fake_session(prepared, clock=lambda: now[0])
    assert prepared["report"].read_bytes() == PRIOR
    assert not prepared["out"].exists()
    assert attempted.count(prepared["out"]) == 1
    assert attempted.count(prepared["report"]) == 1
    recovery = _reopen(tmp_path)
    manifests = json.loads(recovery.manifest.read_text())
    assert "publication outcome unknown" in manifests["phase"]
    assert all(
        row.get("disposition") != "removed"
        for row in caught.value.producer_cleanup
        if row.get("path") == str(prepared["report"])
    )


def test_failure_ledger_error_is_secondary_and_canonical_prior_survives(tmp_path, monkeypatch):
    """X02 variant: the durable-ledger failure is explicit, never clean completion."""
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    real_open = os.open
    primary = KeyboardInterrupt("operation interrupted")

    def open_file(path, flags, *args, **kwargs):
        if Path(path).name.startswith("cleanup-"):
            raise PermissionError("ledger unavailable")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(KeyboardInterrupt) as caught, transaction.resource_scope():
        producer.write_report(report, {"new": True})
        raise primary
    assert caught.value is primary and report.read_bytes() == PRIOR
    _reopen(tmp_path)
    assert any(
        row["phase"] == "failure ledger" and row["disposition"] == "unresolved"
        for row in primary.producer_cleanup
    )


def test_nested_transfer_does_not_withdraw_twice(tmp_path, monkeypatch):
    """Required token-transfer variant uses real inner/outer helper unwinding."""
    path = tmp_path / "fixture"
    attempts = []
    real = capture._OwnedPublication.withdraw_result

    def withdraw(self):
        attempts.append(self.path)
        return real(self)

    monkeypatch.setattr(capture._OwnedPublication, "withdraw_result", withdraw)
    with pytest.raises(OSError, match="outer"), transaction.resource_scope() as outer:
        with transaction.resource_scope():
            token = capture._write_fixture_exclusive(path, b"fixture")
        outer.publication(token)  # dedup reuses the transferred action
        raise OSError("outer")
    assert attempts.count(path) == 1 and not path.exists()
    assert _staged_files(tmp_path) == []


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-noop"])
def test_positive_owned_cleanup_rejects_noop_control(tmp_path, monkeypatch, unsafe):
    """C09: this paired positive oracle rejects skipping all cleanup."""
    path = tmp_path / "owned"
    path.write_bytes(b"owned")
    if unsafe:
        monkeypatch.setattr(capture._OwnedPublication, "withdraw_result", lambda self: None)

    def exercise():
        with pytest.raises(OSError), transaction.resource_scope() as scope:
            scope.publication(_token(path))
            raise OSError("primary")
        assert not path.exists(), "no-op cleanup left owned bytes"

    if unsafe:
        with pytest.raises(AssertionError, match="no-op cleanup"):
            exercise()
    else:
        exercise()


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-probe-false"])
def test_probe_oracle_rejects_false_foreign_control(tmp_path, monkeypatch, unsafe):
    """Reduced unsafe control models the old OSError-to-false classification."""
    path = tmp_path / "owned"
    path.write_bytes(b"owned")
    token = _token(path)
    if unsafe:
        monkeypatch.setattr(
            capture._OwnedPublication, "observe_ownership", lambda self: ("observed-foreign", None)
        )
    else:
        monkeypatch.setattr(
            capture._OwnedPublication,
            "observe_ownership",
            lambda self: ("unresolved", PermissionError("probe")),
        )

    def exercise():
        result = token.withdraw_result()
        assert result.disposition == "unresolved", "probe was falsely reported foreign"
        assert path.read_bytes() == b"owned"

    if unsafe:
        with pytest.raises(AssertionError, match="falsely reported foreign"):
            exercise()
    else:
        exercise()


def test_expiry_before_close_still_closes_session_once(tmp_path):
    """D09/C06: a normal guard must not prevent mandatory close after expiry."""
    now = [0.0]

    class Session(_FakeSession):
        closes = 0

        def save_state(self):
            now[0] = 180.0
            return super().save_state()

        def close(self):
            self.closes += 1
            super().close()

    session = Session()
    with pytest.raises(producer.CaptureBoundsExceeded):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                clock=lambda: now[0],
                session_factory=lambda **_kwargs: session,
            )
        )
    assert session.closed and session.closes == 1
    assert not (tmp_path / "out.state").exists()


def test_combined_expiry_diagnostic_failure_and_successful_restoration(tmp_path, monkeypatch):
    """Mandatory combined control: expired normal context must not stop recovery."""

    class Primary(producer.CaptureBoundsExceeded):
        def add_note(self, _text):
            raise KeyboardInterrupt("diagnostic delivery failed")

    report = tmp_path / "report.json"
    fixture = tmp_path / "fixture"
    report.write_bytes(PRIOR)
    now = [0.0]
    primary = Primary("deadline expiry")

    @transaction.operation_entry("run")
    def operation(**_kwargs):
        transaction.bind_operation(180.0)
        capture._write_fixture_exclusive(fixture, b"fixture")
        producer.write_report(report, {"new": True})

        def interrupt():
            raise SystemExit("first cleanup interrupted")

        transaction.active_scope().action(
            "first", tmp_path / "unresolved", "first cleanup", interrupt
        )
        now[0] = 181.0
        raise primary

    with pytest.raises(Primary) as caught:
        operation(clock=lambda: now[0])
    assert caught.value is primary and report.read_bytes() == PRIOR
    assert not fixture.exists()
    assert any(row["phase"] == "diagnostic attachment" for row in primary.producer_cleanup)
    assert any(row.get("disposition") == "restored" for row in primary.producer_cleanup)
    _reopen(tmp_path)


def test_stage_creation_interruption_reports_uncertain_remaining_entry(tmp_path, monkeypatch):
    """An interrupted open without returned identity cannot authorize blind unlink."""
    real_open = os.open
    primary = KeyboardInterrupt("open interrupted after creation")
    paths = []

    def open_file(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if ".partial-" in Path(path).name:
            os.close(descriptor)  # diagnostic shim owns its unreturned descriptor
            paths.append(Path(path))
            raise primary
        return descriptor

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(KeyboardInterrupt) as caught:
        capture._write_fixture_exclusive(tmp_path / "fixture", b"fixture")
    assert caught.value is primary and len(paths) == 1
    assert paths[0].exists() and not (tmp_path / "fixture").exists()
    assert any(
        row["path"] == str(paths[0]) and row["disposition"] == "unresolved"
        for row in primary.producer_cleanup
    )


def test_owned_fixture_foreign_replacement_and_real_cleanup_positive(tmp_path):
    """C09/C01 positive controls preserve foreign bytes and actually remove owned bytes."""
    foreign = tmp_path / "foreign"
    owned = tmp_path / "owned"
    foreign.write_bytes(b"first")
    token = _token(foreign)
    competitor = tmp_path / "competitor"
    competitor.write_bytes(b"competitor")
    os.replace(competitor, foreign)
    result = token.withdraw_result()
    assert result.observation == "observed-foreign"
    assert result.disposition == "foreign-preserved" and foreign.read_bytes() == b"competitor"
    owned.write_bytes(b"owned")
    result = _token(owned).withdraw_result()
    assert result.disposition == "removed" and not owned.exists()


@pytest.mark.parametrize("unsafe", [False, True], ids=["candidate", "unsafe-reset-window"])
def test_paired_original_deadline_rejects_reset_window(tmp_path, monkeypatch, unsafe):
    """Reduced unsafe timing control; not execution of the historical parent."""
    now = [0.0]
    prepared = _prepared_run(tmp_path)
    prepared.update(out=tmp_path / "out.state", root=tmp_path)
    real_admit = transaction._OperationClock.admit
    reached = []

    def admit(self, phase):
        if phase == "capture final admission" and not reached:
            reached.append(phase)
            now[0] = 181.0
            if unsafe:
                self.started = 30.0
                self.deadline = 210.0
        return real_admit(self, phase)

    monkeypatch.setattr(transaction._OperationClock, "admit", admit)

    def exercise():
        refused = False
        try:
            _run_with_fake_session(prepared, clock=lambda: now[0])
        except producer.CaptureBoundsExceeded:
            refused = True
        assert refused and not prepared["out"].exists(), "reset deadline admitted late output"

    if unsafe:
        with pytest.raises(AssertionError, match="reset deadline"):
            exercise()
    else:
        exercise()
    assert reached


def test_deadline_arithmetic_overflow_is_refused_before_session(tmp_path):
    kwargs = _capture_kwargs(
        tmp_path,
        _writable_scenario(),
        clock=lambda: 1e308,
        session_factory=lambda **_kwargs: pytest.fail("no session"),
    )
    kwargs["bounds"]["max_wall_seconds"] = 1e308
    with pytest.raises(producer.ScenarioRefusal, match="arithmetic"):
        producer.capture_battle_scenario(**kwargs)
    assert not (tmp_path / "out.state").exists()


def test_standalone_report_does_not_invent_wall_budget(tmp_path):
    """No active operation means the legacy report helper is standalone."""
    report = tmp_path / "report.json"
    token = producer.write_report(report, {"standalone": True})
    assert transaction.active_scope() is None
    assert json.loads(report.read_text()) == {"standalone": True}
    assert token.withdraw_result().disposition == "removed"
    assert not report.exists()


def test_reentrant_operation_inherits_deadline_without_parent_registry(tmp_path):
    now = [0.0]

    def clock():
        return now[0]

    scopes = []

    @transaction.operation_entry("run")
    def inner(**_kwargs):
        transaction.bind_operation(1000.0)
        scope = transaction.active_scope()
        scopes.append(scope)
        assert scope.timing.started == 30.0
        assert scope.timing.deadline == 180.0
        return {}

    @transaction.operation_entry("run")
    def outer(**_kwargs):
        transaction.bind_operation(180.0)
        parent = transaction.active_scope()
        scopes.append(parent)
        now[0] = 30.0
        inner(clock=clock)
        assert transaction.active_scope() is parent
        assert parent.timing.started == 0.0 and parent.timing.deadline == 180.0
        return {}

    result = outer(clock=clock)
    assert scopes[0] is not scopes[1] and scopes[0].actions is not scopes[1].actions
    assert result["operation_timing"]["wall_seconds"] == 30.0
    assert transaction.active_scope() is None


def test_producer_revision_expiry_is_not_a_shorter_capture_duration(tmp_path, monkeypatch):
    now = [0.0]
    session = _FakeSession()

    def revision(_root):
        now[0] = 181.0

    monkeypatch.setattr(producer, "_producer_revision", revision)
    with pytest.raises(producer.CaptureBoundsExceeded):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                clock=lambda: now[0],
                session_factory=lambda **_kwargs: session,
            )
        )
    assert session.closed and not (tmp_path / "out.state").exists()


def test_tighter_explicit_deadline_cannot_be_reset(tmp_path):
    now = [0.0]
    session = _FakeSession()

    def factory(**_kwargs):
        now[0] = 5.0
        return session

    with pytest.raises(producer.CaptureBoundsExceeded):
        producer.capture_battle_scenario(
            **_capture_kwargs(
                tmp_path,
                _writable_scenario(),
                deadline=5.0,
                clock=lambda: now[0],
                session_factory=factory,
            )
        )
    assert session.closed and not (tmp_path / "out.state").exists()


def test_context_reset_failure_cannot_replace_an_existing_primary(tmp_path, monkeypatch):
    real = transaction._ACTIVE
    primary = RuntimeError("original body failure")
    path = tmp_path / "owned"
    path.write_bytes(b"owned")

    class ResetFailure:
        def get(self):
            return real.get()

        def set(self, value):
            return real.set(value)

        def reset(self, token):
            real.reset(token)
            raise SystemExit("context reset interrupted")

    monkeypatch.setattr(transaction, "_ACTIVE", ResetFailure())
    with pytest.raises(RuntimeError) as caught, transaction.resource_scope() as scope:
        scope.publication(_token(path))
        raise primary
    assert caught.value is primary and not path.exists()
    assert real.get() is None
    assert any("context reset failed" in note for note in primary.__notes__)


def test_expiry_failed_restore_and_failed_ledger_remain_failure(tmp_path, monkeypatch):
    """X02 complete variant: deadline, restoration and ledger failures stay separate."""
    prepared = _prepared_run(tmp_path)
    prepared.update(out=tmp_path / "out.state", root=tmp_path, report=tmp_path / "report.json")
    prepared["report"].write_bytes(PRIOR)
    now = [0.0]
    real_writer, real_open = producer.write_report, os.open

    def writer(path, payload):
        token = real_writer(path, payload)
        now[0] = 181.0
        return token

    def restore(*_args):
        raise PermissionError("restoration unavailable")

    def open_file(path, flags, *args, **kwargs):
        if Path(path).name.startswith("cleanup-"):
            raise OSError("ledger unavailable")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(producer, "write_report", writer)
    monkeypatch.setattr(capture, "_write_bytes_atomically", restore)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(producer.CaptureBoundsExceeded) as caught:
        _run_with_fake_session(prepared, clock=lambda: now[0])
    assert not prepared["out"].exists()
    assert json.loads(prepared["report"].read_text())["capture"]["producer"] == (
        "scripts/produce_battle_scenario.py"
    )
    _reopen(tmp_path)
    rows = caught.value.producer_cleanup
    assert any(row.get("exception_type") == "PermissionError" for row in rows)
    assert any(
        row["phase"] == "failure ledger" and row["disposition"] == "unresolved" for row in rows
    )


def test_final_clock_and_independent_cleanup_failures_keep_all_ownership(tmp_path, monkeypatch):
    """X01: one final-clock primary with probe, action and stage failures."""
    fixture, stage, report = (
        tmp_path / name for name in ("fixture", ".extra.partial-x", "report.json")
    )
    report.write_bytes(PRIOR)
    primary = KeyboardInterrupt("final clock interrupted")
    armed, pending = [False], [None]
    real_lstat, real_unlink = os.lstat, os.unlink
    attempts = []

    def clock():
        if pending[0] is not None:
            error, pending[0] = pending[0], None
            raise error
        return 0.0

    def lstat(path, *args, **kwargs):
        if armed[0] and Path(path) == fixture:
            attempts.append("probe")
            raise PermissionError("ownership probe unavailable")
        return real_lstat(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if Path(path) == stage:
            attempts.append("stage")
            raise SystemExit("staging cleanup interrupted")
        return real_unlink(path, *args, **kwargs)

    @transaction.operation_entry("run")
    def operation(**_kwargs):
        transaction.bind_operation(180.0)
        capture._write_fixture_exclusive(fixture, b"fixture")
        producer.write_report(report, {"new": True})
        stage.write_bytes(b"stage")
        owner = transaction.active_scope()
        token = _token(stage)
        owner.action("extra stage", stage, "staging cleanup", token.withdraw_result)

        def first():
            attempts.append("first")
            raise SystemExit("first independent cleanup interrupted")

        owner.action("first", tmp_path / "unresolved", "first cleanup", first)
        armed[0], pending[0] = True, primary
        return {}

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(KeyboardInterrupt) as caught:
        operation(clock=clock)
    assert caught.value is primary
    assert attempts == ["first", "stage", "probe"]
    assert fixture.read_bytes() == b"fixture" and stage.read_bytes() == b"stage"
    assert report.read_bytes() == PRIOR
    rows = primary.producer_cleanup
    assert any(row.get("exception_type") == "PermissionError" for row in rows)
    assert any(row.get("exception_type") == "SystemExit" for row in rows)
    assert any(row.get("disposition") == "restored" for row in rows)
    _reopen(tmp_path)
    assert transaction.active_scope() is None


@pytest.mark.parametrize(
    "phase",
    [
        "prior report copy",
        "prior report payload sync",
        "prior report manifest sync",
        "recovery verification",
        "recovery directory sync",
        "report publication",
    ],
)
@pytest.mark.parametrize("occurrence", [1, 2], ids=["before-work", "after-work"])
def test_normal_recovery_guards_bracket_real_blocking_work(
    tmp_path, monkeypatch, phase, occurrence
):
    """D05/D06: reach both sides of real copy/readback/sync/rename work."""
    prepared = _prepared_run(tmp_path)
    prepared.update(out=tmp_path / "out.state", root=tmp_path, report=tmp_path / "report.json")
    prepared["report"].write_bytes(PRIOR)
    now, seen = [0.0], []
    real_admit = transaction._OperationClock.admit

    def admit(self, label):
        if label == phase:
            seen.append(label)
            if len(seen) == occurrence:
                now[0] = 180.0
        return real_admit(self, label)

    monkeypatch.setattr(transaction._OperationClock, "admit", admit)
    with pytest.raises(producer.CaptureBoundsExceeded):
        _run_with_fake_session(prepared, clock=lambda: now[0])
    assert len(seen) >= occurrence
    assert prepared["report"].read_bytes() == PRIOR
    assert not prepared["out"].exists()
    assert transaction.active_scope() is None


def _recovery_run(tmp_path):
    prepared = _prepared_run(tmp_path)
    prepared.update(out=tmp_path / "out.state", root=tmp_path, report=tmp_path / "report.json")
    prepared["report"].write_bytes(PRIOR)
    return prepared


@pytest.mark.parametrize(
    "name,phase", [("backup", "prior report copy"), ("manifest", "prior report manifest sync")]
)
@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt, SystemExit])
def test_guard_primary_survives_recovery_stream_and_source_close(
    tmp_path, monkeypatch, name, phase, close_type
):
    """F1: actual absolute admission failure, followed by two failing closes."""
    prepared = _recovery_run(tmp_path)
    _, attempts, _ = _recovery_handle_faults(
        monkeypatch, prepared["report"], close_roles=(name, "source"), close_type=close_type
    )
    now, original = [0.0], []
    real_guard = recovery_module.normal_guard

    def guard(label):
        if label == phase:
            now[0] = 180.0
        try:
            return real_guard(label)
        except producer.CaptureBoundsExceeded as error:
            original.append(error)
            raise

    monkeypatch.setattr(recovery_module, "normal_guard", guard)
    with pytest.raises(producer.CaptureBoundsExceeded) as caught:
        _run_with_fake_session(prepared, clock=lambda: now[0])
    assert original and caught.value is original[0]
    assert attempts.count((name, "stream")) == attempts.count(("source", "stream")) == 1
    assert not prepared["out"].exists() and prepared["report"].read_bytes() == PRIOR
    retained = list(tmp_path.glob(".*.recovery-*"))
    assert len(retained) == 1
    assert any(str(retained[0]) in note for note in caught.value.__notes__)
    rows = caught.value.producer_cleanup
    assert sum(row.get("exception_type") == close_type.__name__ for row in rows) >= 2
    assert any(row["path"] == str(prepared["report"]) for row in rows)
    if name == "manifest":
        _reopen(tmp_path)


@pytest.mark.parametrize("name", ["source", "backup", "manifest"])
def test_recovery_close_without_earlier_failure_becomes_primary(tmp_path, monkeypatch, name):
    """F1 positive body: a first close failure prevents report replacement."""
    prepared = _recovery_run(tmp_path)
    handles, attempts, _ = _recovery_handle_faults(
        monkeypatch, prepared["report"], close_roles=(name,), close_type=KeyboardInterrupt
    )
    with pytest.raises(KeyboardInterrupt, match=f"{name} close failed") as caught:
        _run_with_fake_session(prepared)
    assert caught.value is handles["close_failures"][0]
    assert attempts.count((name, "stream")) == 1
    assert prepared["report"].read_bytes() == PRIOR and not prepared["out"].exists()
    retained = list(tmp_path.glob(".*.recovery-*"))
    assert len(retained) == 1 and any(str(retained[0]) in n for n in caught.value.__notes__)
    if name == "source":
        _reopen(tmp_path)


@pytest.mark.parametrize("fsync_failure", [False, True])
def test_recovery_directory_fsync_and_close_keep_first_failure(
    tmp_path, monkeypatch, fsync_failure
):
    prepared = _recovery_run(tmp_path)
    handles, attempts, _ = _recovery_handle_faults(
        monkeypatch, prepared["report"], close_roles=("directory",), close_type=SystemExit
    )
    real_sync = os.fsync
    primary = OSError("directory fsync failed")

    def sync(descriptor):
        if fsync_failure and handles.get(descriptor) == "directory":
            raise primary
        return real_sync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError if fsync_failure else SystemExit) as caught:
        _run_with_fake_session(prepared)
    if fsync_failure:
        assert caught.value is primary
        assert any(row.get("exception_type") == "SystemExit" for row in primary.producer_cleanup)
    else:
        assert caught.value is handles["close_failures"][0]
    assert attempts.count(("directory", "descriptor")) == 1
    assert attempts.count(("source", "stream")) == 1
    assert prepared["report"].read_bytes() == PRIOR and not prepared["out"].exists()
    _reopen(tmp_path)


@pytest.mark.parametrize("name", ["source", "backup", "manifest"])
def test_fdopen_failure_keeps_raw_descriptor_owned_until_once_close(tmp_path, monkeypatch, name):
    prepared = _recovery_run(tmp_path)
    _, attempts, primary = _recovery_handle_faults(
        monkeypatch,
        prepared["report"],
        fdopen_role=name,
        close_roles=(name,),
        close_type=SystemExit,
    )
    with pytest.raises(OSError) as caught:
        _run_with_fake_session(prepared)
    assert caught.value is primary
    assert attempts.count((name, "descriptor")) == 1
    assert attempts.count((name, "stream")) == 0
    assert any(row.get("exception_type") == "SystemExit" for row in primary.producer_cleanup)
    assert prepared["report"].read_bytes() == PRIOR and not prepared["out"].exists()
    retained = list(tmp_path.glob(".*.recovery-*"))
    assert len(retained) == (0 if name == "source" else 1)
    if retained:
        assert any(str(retained[0]) in note for note in primary.__notes__)


@pytest.mark.parametrize("diagnostic_type", [OSError, KeyboardInterrupt])
def test_recovery_description_failure_preserves_primary_and_snapshot(
    tmp_path, monkeypatch, diagnostic_type
):
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    primary = KeyboardInterrupt("original replacement interruption")
    real_replace = os.replace

    def replace_then_interrupt(source, target):
        real_replace(source, target)
        raise primary

    def refuse_restore(*_args):
        raise OSError("restore unavailable")

    def broken_description(_self):
        raise diagnostic_type("description unavailable")

    monkeypatch.setattr(capture.os, "replace", replace_then_interrupt)
    monkeypatch.setattr(capture, "_write_bytes_atomically", refuse_restore)
    monkeypatch.setattr(_ReportRecovery, "describe", broken_description)
    with pytest.raises(KeyboardInterrupt) as caught:
        producer.write_report(report, {"new": True})
    assert caught.value is primary
    retained = _reopen(tmp_path)
    assert json.loads(report.read_bytes()) == {"new": True}
    assert any("recovery description unavailable" in note for note in primary.__notes__)
    assert any(str(retained.manifest) in note for note in primary.__notes__)
    assert any(row.get("disposition") == "unresolved" for row in primary.producer_cleanup)


@pytest.mark.parametrize("ledger_type", [OSError, KeyboardInterrupt])
def test_canonical_recovery_note_survives_failed_ledger_after_restore(
    tmp_path, monkeypatch, ledger_type
):
    report = tmp_path / "report.json"
    report.write_bytes(PRIOR)
    primary = KeyboardInterrupt("original replacement interruption")
    real_replace, real_open = os.replace, os.open
    replacements, ledger_attempts = [], []

    def replace_then_interrupt(source, target):
        real_replace(source, target)
        replacements.append(Path(target))
        if len(replacements) == 1:
            raise primary

    def refuse_ledger(path, *args, **kwargs):
        if Path(path).name.startswith("cleanup-"):
            ledger_attempts.append(Path(path))
            raise ledger_type("ledger unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(capture.os, "replace", replace_then_interrupt)
    monkeypatch.setattr(transaction.os, "open", refuse_ledger)
    with pytest.raises(KeyboardInterrupt) as caught:
        producer.write_report(report, {"new": True})
    assert caught.value is primary
    assert replacements == [report, report]
    assert report.read_bytes() == PRIOR
    retained = _reopen(tmp_path)
    assert len(ledger_attempts) == 1
    assert any(str(retained.manifest) in note for note in primary.__notes__)
    assert any("failure ledger incomplete" in note for note in primary.__notes__)
    assert any(row.get("phase") == "failure ledger" for row in primary.producer_cleanup)
