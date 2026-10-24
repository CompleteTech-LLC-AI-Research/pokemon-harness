"""Authored NOTRUN owner-lifetime controls; kernel state is deliberately controlled."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_probe_session as session_module
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError


def test_pending_scope_prevents_root_restore_or_resource_release(monkeypatch):
    session = session_module.ProbeSession()
    monkeypatch.setattr(session_module, "_LIVE_SESSIONS", [session])
    session.broker = SimpleNamespace(pending=lambda: True)
    calls = []
    session.adoption = SimpleNamespace(__exit__=lambda *args: calls.append("restore"))
    with pytest.raises(ProbeProtocolError, match="retain inputs and adoption"):
        session.close()
    assert session_module._LIVE_SESSIONS == [session] and not session.closed and not calls


def test_attach_preserves_job_bounds_and_original_cleanup_latch():
    session = session_module.ProbeSession()
    session.adoption = SimpleNamespace(verify=lambda: True)
    runner = lambda *args, **kwargs: None
    session.broker = SimpleNamespace(run=runner)
    cleanup = [110.0]
    budget = MeasurementBudget(100.0, 120.0, clock=lambda: 10.0, cleanup_limit=lambda: cleanup[0])
    attached = session.attach(budget)
    assert attached.deadline == 100.0 and attached.cleanup_bound() == 110.0
    cleanup[0] = 120.0
    assert attached.cleanup_bound() == budget.cleanup_bound() == 110.0
    assert attached.probe_runner is runner


def test_restoration_refusal_keeps_same_root_owner_for_retry(monkeypatch):
    session = session_module.ProbeSession()
    monkeypatch.setattr(session_module, "_LIVE_SESSIONS", [session])
    calls = []

    def restore(*args):
        calls.append("restore")
        raise RuntimeError("controlled restoration refusal")

    session.adoption = SimpleNamespace(__exit__=restore)
    with pytest.raises(RuntimeError, match="restoration refusal"):
        session.close()
    assert session_module._LIVE_SESSIONS == [session] and not session.closed
    session.adoption.__exit__ = lambda *args: calls.append("verified restore")
    session.close()
    assert session.closed and session_module._LIVE_SESSIONS == []
    assert calls == ["restore", "verified restore"]


def test_refused_new_owner_close_cannot_restore_existing_owner(monkeypatch):
    existing, session = object(), session_module.ProbeSession()
    monkeypatch.setattr(session_module, "_LIVE_SESSIONS", [existing])
    calls = []
    session.adoption = SimpleNamespace(__exit__=lambda *args: calls.append("foreign restore"))
    with pytest.raises(ProbeProtocolError, match="previous root probe owner"):
        session.__enter__()
    session.close()
    assert session_module._LIVE_SESSIONS == [existing] and not calls


def test_cancel_services_owned_scopes_before_refusing_measurement():
    session = session_module.ProbeSession()
    calls = []
    scope = SimpleNamespace(refuse=lambda reason: calls.append(reason))
    session.broker = SimpleNamespace(scopes=[scope], service=lambda: calls.append("service"))
    session.interrupt.set()
    with pytest.raises(IdentityDeadline, match="continuing custody retained"):
        session.checkpoint()
    assert calls == ["CLI cancellation; success permanently refused", "service"]


def test_operation_cap_tightens_success_without_renewing_cleanup():
    now, budgets = [10.0], []

    def runner(command, **kwargs):
        budgets.append(kwargs["budget"])
        now[0] = 15.0
        return object()

    original = MeasurementBudget(100.0, 120.0, clock=lambda: now[0], probe_runner=runner)
    with pytest.raises(IdentityDeadline, match="frozen deadline"):
        original.run_probe(["controlled-unused"], environment={}, cwd=Path("/"), max_seconds=5.0)
    assert budgets[0].deadline == 15.0 and original.deadline == 100.0
    assert budgets[0].cleanup_bound() == original.cleanup_bound() == 120.0


def test_report_temporary_inputs_outlive_pending_scope_until_physical_retirement(
    tmp_path, monkeypatch
):
    session, pending = session_module.ProbeSession(), [True]
    monkeypatch.setattr(session_module, "_LIVE_SESSIONS", [session])
    session.broker = SimpleNamespace(pending=lambda: pending[0])
    restored = []
    session.adoption = SimpleNamespace(__exit__=lambda *args: restored.append(True))
    directory = TemporaryDirectory(dir=tmp_path)
    path = Path(directory.name)
    session.retain_temporary(directory)
    with pytest.raises(ProbeProtocolError, match="retain inputs and adoption"):
        session.close()
    assert path.is_dir() and session.temporary_inputs[0][0] is directory and not restored
    pending[0] = False
    session.close()
    assert not path.exists() and not session.temporary_inputs and restored == [True]


def test_changed_temporary_binding_refuses_cleanup_and_adoption_restore(tmp_path, monkeypatch):
    session = session_module.ProbeSession()
    monkeypatch.setattr(session_module, "_LIVE_SESSIONS", [session])
    restored = []
    session.adoption = SimpleNamespace(__exit__=lambda *args: restored.append(True))
    directory = TemporaryDirectory(dir=tmp_path)
    session.retain_temporary(directory)
    changed = SimpleNamespace(lstat=lambda: SimpleNamespace(st_dev=-1, st_ino=-1))
    monkeypatch.setattr(session_module, "Path", lambda name: changed)
    try:
        with pytest.raises(ProbeProtocolError, match="temporary input directory changed"):
            session.close()
        assert session.temporary_inputs[0][0] is directory and not restored and not session.closed
    finally:
        directory.cleanup()  # controlled test directory; no measured process was launched
