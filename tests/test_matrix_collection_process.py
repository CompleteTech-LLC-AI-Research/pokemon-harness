"""Deterministic collection lifecycle controls; no real processes or signals."""

from __future__ import annotations

import errno
import json
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import _matrix_collection_process as owned
from scripts import tcp_link_matrix as matrix


class CollectionModel:
    """Independent leader, descendant, pipe and clock; every OS action is fake."""

    def __init__(self, monkeypatch, *, terminal=True, writer=False):
        self.clock = 0.0
        self.terminal, self.writer = terminal, writer
        self.descendant = writer
        self.events = []
        self.spawn_count = 0
        self.output = []
        self.read_error = None
        self.probe_error = None
        self.signal_errno = None
        self.constructor_error = None
        self.after_create = None
        self.after_read = None
        self.report = None
        self.exit_status = 0
        self.reaped = False
        self.pipe = SimpleNamespace(fileno=lambda: 31338, closed=False)
        self.pipe.close = lambda: setattr(self.pipe, "closed", True)
        self.process = SimpleNamespace(pid=31337, stdout=self.pipe, returncode=None)
        self.handlers = {
            signal.SIGINT: signal.default_int_handler,
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGCHLD: signal.SIG_DFL,
        }
        self.restored = []
        monkeypatch.setattr(owned.sys, "platform", "linux")
        monkeypatch.setattr(owned.sys, "version_info", (3, 11, 17))
        monkeypatch.setattr(owned.threading, "active_count", lambda: 1)
        monkeypatch.setattr(owned.signal, "getsignal", lambda sig: self.handlers[sig])
        monkeypatch.setattr(owned.signal, "signal", self.set_handler)
        monkeypatch.setattr(owned.time, "monotonic", lambda: self.clock)
        monkeypatch.setattr(owned.time, "sleep", self.advance)
        monkeypatch.setattr(owned.os, "set_blocking", lambda *_args: None)
        monkeypatch.setattr(owned.os, "waitid", self.observe)
        monkeypatch.setattr(owned.os, "waitpid", self.reap)
        monkeypatch.setattr(owned.os, "killpg", self.send)
        monkeypatch.setattr(owned.os, "read", self.read)
        monkeypatch.setattr(owned.subprocess, "Popen", self.spawn)
        monkeypatch.setattr(owned.selectors, "DefaultSelector", lambda: self)

    def set_handler(self, sig, handler):
        old = self.handlers[sig]
        self.handlers[sig] = handler
        self.restored.append((sig, handler))
        return old

    def cancel(self, sig):
        self.handlers[sig](sig, None)

    def advance(self, amount):
        self.clock += amount

    def spawn(self, command, **kwargs):
        self.spawn_count += 1
        self.creation_kwargs = kwargs
        if self.after_create:
            self.after_create()
        if self.constructor_error:
            raise self.constructor_error
        if self.report is not None:
            Path(kwargs["env"]["POKERED_GATE_REPORT"]).write_text(json.dumps(self.report))
        return self.process

    def observe(self, *_args):
        if self.probe_error:
            raise self.probe_error
        return (
            SimpleNamespace(
                si_pid=31337, si_code=owned.os.CLD_EXITED, si_status=self.exit_status >> 8
            )
            if self.terminal
            else None
        )

    def reap(self, pid, _flags):
        assert pid == 31337
        self.events.append(("reap", self.clock))
        if self.probe_error:
            raise self.probe_error
        if self.terminal and not self.reaped:
            self.reaped = True
            return pid, self.exit_status
        return 0, 0

    def send(self, pid, sig):
        assert pid == 31337 and not self.reaped
        self.events.append((sig.name, self.clock))
        if self.signal_errno:
            raise OSError(self.signal_errno, "model signal failure")
        self.terminal = True
        if sig == signal.SIGKILL:
            self.descendant = False
            self.writer = False

    def register(self, *_args):
        pass

    def unregister(self, *_args):
        pass

    def select(self, timeout):
        self.advance(timeout)
        return [(SimpleNamespace(fd=31338), 1)]

    def read(self, _fd, size):
        assert size <= 32768
        if self.after_read:
            self.after_read()
        if self.read_error:
            error, self.read_error = self.read_error, None
            raise error
        if self.output:
            return self.output.pop(0)
        if self.writer:
            raise BlockingIOError()
        return b""

    def close(self):
        pass

    def run(self, timeout=0.2):
        scope = owned.CancellationScope(owned.OWNERSHIP_POLICY)
        owner = owned.OwnedCollection(scope)
        self.owner, self.scope = owner, scope
        with scope:
            owner.run(["fake-python"], cwd=Path("."), env={}, timeout=timeout)
        return owner.evidence()


@pytest.mark.parametrize("version", [(3, 11, 2), (3, 11, 17), (3, 12, 13), (3, 12, 15)])
def test_admitted_versions_reach_owned_positive_launch(monkeypatch, version):
    model = CollectionModel(monkeypatch)
    monkeypatch.setattr(owned.sys, "version_info", version)
    result = model.run()
    assert model.spawn_count == 1 and result["leader_reaped"] and result["pipe_eof"]
    assert result["cleanup_status"] == "SUPPORTED_ACTIONS_COMPLETE"
    assert result["group_quiescence"] == "UNPROVEN"
    assert [x[0] for x in model.events] == ["SIGTERM", "SIGKILL", "reap"]


@pytest.mark.parametrize(
    "reason", ["version", "windows", "darwin", "thread", "handler", "declaration", "waitid"]
)
def test_unsupported_policy_creates_zero_children(monkeypatch, reason):
    model = CollectionModel(monkeypatch)
    policy = owned.OWNERSHIP_POLICY
    if reason == "version":
        monkeypatch.setattr(owned.sys, "version_info", (3, 12, 14))
    elif reason in ("windows", "darwin"):
        monkeypatch.setattr(owned.sys, "platform", "win32" if reason == "windows" else "darwin")
    elif reason == "thread":
        monkeypatch.setattr(owned.threading, "active_count", lambda: 2)
    elif reason == "handler":
        model.handlers[signal.SIGCHLD] = signal.SIG_IGN
    elif reason == "declaration":
        policy = None
    else:
        monkeypatch.delattr(owned.os, "WNOWAIT")
    with pytest.raises(owned.UnsupportedCollection), owned.CancellationScope(policy):
        model.spawn([])
    assert model.spawn_count == 0 and not model.events


@pytest.mark.parametrize(
    "sig,kind", [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)]
)
@pytest.mark.parametrize("boundary", ["before", "creation", "handoff", "monitor"])
def test_first_cancellation_handoff_and_restoration(monkeypatch, sig, kind, boundary):
    model = CollectionModel(monkeypatch, terminal=False, writer=True)
    scope = owned.CancellationScope(owned.OWNERSHIP_POLICY)
    owner = owned.OwnedCollection(scope)
    if boundary == "creation":
        model.after_create = lambda: model.cancel(sig)
    if boundary == "monitor":
        model.after_read = lambda: model.cancel(sig)
    with pytest.raises(kind) as caught, scope:
        if boundary == "before":
            model.cancel(sig)
        if boundary == "handoff":
            original = model.spawn

            def spawn(*args, **kwargs):
                result = original(*args, **kwargs)
                model.cancel(sig)
                return result

            monkeypatch.setattr(owned.subprocess, "Popen", spawn)
        owner.run(["fake"], cwd=Path("."), env={}, timeout=1)
    if kind is SystemExit:
        assert caught.value.code == 143
    assert model.spawn_count == (0 if boundary == "before" else 1)
    assert owner.result["ownership"] == ("NONE_PROVEN" if boundary == "before" else "REAPED")
    assert model.handlers[signal.SIGINT] == signal.default_int_handler
    assert model.handlers[signal.SIGTERM] == signal.SIG_DFL


@pytest.mark.parametrize(
    "exc", [FileNotFoundError("exec"), MemoryError("handoff"), KeyboardInterrupt()]
)
def test_exceptional_constructor_is_creation_unknown(monkeypatch, exc):
    model = CollectionModel(monkeypatch)
    model.constructor_error = exc
    with pytest.raises(type(exc)) as caught:
        model.run()
    assert caught.value is exc
    assert model.owner.result["ownership"] == "CREATION_UNKNOWN"
    assert model.owner.result["recovery_required"]
    assert model.events == []


def test_leader_exit_and_closed_pipe_do_not_skip_live_descendant(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=True, writer=False)
    model.descendant = True
    result = model.run()
    assert not model.descendant
    assert [x[0] for x in model.events] == ["SIGTERM", "SIGKILL", "reap"]
    assert result["group_quiescence"] == "UNPROVEN"


def test_timeout_retains_failure_after_positive_forced_cleanup(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=False, writer=True)
    with pytest.raises(subprocess.TimeoutExpired):
        model.run()
    assert not model.descendant and model.reaped
    assert model.owner.result["cleanup_status"] == "SUPPORTED_ACTIONS_COMPLETE"
    assert model.owner.result["leader_reaped"] and model.owner.result["pipe_eof"]


@pytest.mark.parametrize("number", [errno.ESRCH, errno.EPERM])
def test_absent_group_is_benign_but_permission_failure_is_not(monkeypatch, number):
    model = CollectionModel(monkeypatch)
    model.signal_errno = number
    if number == errno.EPERM:
        with pytest.raises(OSError):
            model.run()
    else:
        assert model.run()["cleanup_status"] == "SUPPORTED_ACTIONS_COMPLETE"
    assert {x["result"] for x in model.owner.result["actions"]} == (
        {"absent_at_observation"} if number == errno.ESRCH else {"failed"}
    )
    assert model.owner.result["group_quiescence"] == "UNPROVEN"


def test_lost_child_identity_never_signals_reused_numeric_group(monkeypatch):
    model = CollectionModel(monkeypatch)
    model.probe_error = ChildProcessError("foreign/reaped identity")
    with pytest.raises(ChildProcessError):
        model.run()
    assert not any(name.startswith("SIG") for name, _clock in model.events)
    assert model.owner.result["ownership"] == "OWNERSHIP_UNKNOWN"


def test_eof_while_leader_runs_is_not_execution_completion(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=False, writer=False)
    with pytest.raises(subprocess.TimeoutExpired):
        model.run(timeout=0.2)
    assert model.events[0][1] >= 0.2


def test_terminal_leader_with_writer_starts_cleanup_without_execution_timeout(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=True, writer=True)
    result = model.run(timeout=300)
    assert result["deadlines"]["start"] == 0
    assert not model.writer and model.reaped


def test_continuous_output_has_bounded_memory_and_escalation_fairness(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=False, writer=True)
    model.output = [b"x" * 32768] * 100
    with pytest.raises(subprocess.TimeoutExpired):
        model.run()
    assert len(model.owner.tail) <= 16384
    assert model.owner.result["truncated"]
    deadlines = model.owner.result["deadlines"]
    kill = next(at for name, at in model.events if name == "SIGKILL")
    assert deadlines["grace"] <= kill < deadlines["escalation"]
    assert deadlines["final"] - deadlines["escalation"] == 3


def test_unicode_tail_split_malformed_and_truncated_prefix(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=False)
    model.output = [b"\xe2", b"\x82\xac", b"\xff"]
    with pytest.raises(subprocess.TimeoutExpired):
        model.run()
    result = model.owner.evidence()
    assert "€" in result["output_tail"] and result["decoding_replaced"]
    model.owner.tail = bytearray(b"\x82\xac" + b"a" * 18000)
    assert len(model.owner.evidence()["output_tail"]) <= 4000


def test_budget_reserves_reap_and_never_resets_on_cancellation(monkeypatch):
    model = CollectionModel(monkeypatch)
    model.after_read = lambda: model.cancel(signal.SIGTERM)
    with pytest.raises(SystemExit):
        model.run()
    d = model.owner.result["deadlines"]
    assert d == {"start": 0.0, "grace": 1.0, "escalation": 2.0, "final": 5.0}
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]
    assert next(at for name, at in model.events if name == "reap") < d["final"]


def test_escalation_deadline_miss_is_not_a_late_signal(monkeypatch):
    model = CollectionModel(monkeypatch)
    original = model.send

    def slow_send(pid, sig):
        original(pid, sig)
        model.advance(2)

    monkeypatch.setattr(owned.os, "killpg", slow_send)
    with pytest.raises(owned.CollectionCleanupError):
        model.run()
    assert [name for name, _ in model.events] == ["SIGTERM", "reap"]
    assert model.owner.result["deadlines"]["final"] == 5


@pytest.mark.parametrize("exc", [OSError("read"), KeyboardInterrupt(), SystemExit(7)])
def test_original_failure_survives_secondary_signal_and_pipe_errors(monkeypatch, exc):
    model = CollectionModel(monkeypatch, terminal=False, writer=True)
    model.read_error = exc
    model.signal_errno = errno.EPERM
    with pytest.raises(type(exc)) as caught:
        model.run()
    assert caught.value is exc and caught.value.__traceback__ is not None
    assert model.owner.result["errors"] and model.scope.secondary


def test_repeated_cancellation_does_not_restart_or_launch(monkeypatch):
    model = CollectionModel(monkeypatch)
    model.after_read = lambda: (model.cancel(signal.SIGINT), model.cancel(signal.SIGTERM))
    with pytest.raises(KeyboardInterrupt):
        model.run()
    assert model.spawn_count == 1 and model.scope.signal_count == 2
    assert model.owner.evidence()["recovery_required"]
    assert model.owner.result["deadlines"]["final"] == 5


def test_adapter_never_signals_arbitrary_supplied_popen(monkeypatch):
    model = CollectionModel(monkeypatch)
    assert owned.reap_leader_only(model.process)
    assert [name for name, _ in model.events] == ["reap"]
    model.probe_error = ChildProcessError()
    assert owned.reap_leader_only(model.process) is False


def test_postreap_report_error_cannot_restart_group_cleanup(monkeypatch, tmp_path):
    model = CollectionModel(monkeypatch)
    audit, command = matrix.collect_nodeids(
        tmp_path, Path("fake"), ownership_policy=owned.OWNERSHIP_POLICY
    )
    assert command[:3] == ["fake", "-m", "pytest"]
    assert not audit["structural_pass"] and audit["collection_cleanup"]["leader_reaped"]
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]


def test_cleanup_cancellation_keeps_installed_handlers_and_restores_both(monkeypatch):
    model = CollectionModel(monkeypatch)
    original = model.send

    def cancel_during_cleanup(pid, sig):
        original(pid, sig)
        model.cancel(signal.SIGTERM)

    monkeypatch.setattr(owned.os, "killpg", cancel_during_cleanup)
    with pytest.raises(SystemExit) as caught:
        model.run()
    assert caught.value.code == 143
    assert model.handlers[signal.SIGINT] == signal.default_int_handler
    assert model.handlers[signal.SIGTERM] == signal.SIG_DFL
    assert model.reaped and model.spawn_count == 1


def test_structural_success_is_not_group_quiescence(monkeypatch, tmp_path):
    model = CollectionModel(monkeypatch)
    monkeypatch.setattr(
        matrix, "_collection_report_details", lambda *_args, **_kwargs: ([], [], [], [])
    )
    monkeypatch.setattr(
        matrix, "audit_collection", lambda *_args, **_kwargs: {"structural_pass": True}
    )
    model.report = {}
    audit, _command = matrix.collect_nodeids(
        tmp_path, Path("fake"), ownership_policy=owned.OWNERSHIP_POLICY
    )
    assert audit["structural_pass"]
    assert audit["collection_cleanup"]["group_quiescence"] == "UNPROVEN"
    assert audit["collection_cleanup"]["cleanup_status"] == "SUPPORTED_ACTIONS_COMPLETE"


@pytest.mark.parametrize(
    "sig,kind", [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)]
)
def test_report_phase_cancellation_after_reap_never_signals_again(monkeypatch, tmp_path, sig, kind):
    model = CollectionModel(monkeypatch)
    model.report = {}

    def parse(*_args, **_kwargs):
        assert model.reaped
        model.cancel(sig)
        return [], [], [], []

    monkeypatch.setattr(matrix, "_collection_report_details", parse)
    with pytest.raises(kind):
        matrix.collect_nodeids(tmp_path, Path("fake"), ownership_policy=owned.OWNERSHIP_POLICY)
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]
    assert model.handlers[signal.SIGTERM] == signal.SIG_DFL


def test_restore_failure_attempts_both_and_retains_original_exception(monkeypatch):
    model = CollectionModel(monkeypatch)
    original = model.set_handler
    failure = KeyboardInterrupt("primary")
    attempts = []

    def fail_restore(sig, handler):
        if handler in (signal.default_int_handler, signal.SIG_DFL):
            attempts.append(sig)
            if sig == signal.SIGINT:
                raise OSError("restore failed")
        return original(sig, handler)

    monkeypatch.setattr(owned.signal, "signal", fail_restore)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        owned.CancellationScope(owned.OWNERSHIP_POLICY),
    ):
        raise failure
    assert caught.value is failure
    assert attempts == [signal.SIGINT, signal.SIGTERM]
    assert model.handlers[signal.SIGTERM] == signal.SIG_DFL


def test_partial_handler_install_failure_never_launches_and_restores(monkeypatch):
    model = CollectionModel(monkeypatch)
    original = model.set_handler

    def fail_install(sig, handler):
        if sig == signal.SIGTERM and handler != signal.SIG_DFL:
            raise OSError("install failed")
        return original(sig, handler)

    monkeypatch.setattr(owned.signal, "signal", fail_install)
    with pytest.raises(OSError), owned.CancellationScope(owned.OWNERSHIP_POLICY):
        model.spawn([])
    assert model.spawn_count == 0
    assert model.handlers[signal.SIGINT] == signal.default_int_handler


def test_temp_cleanup_failure_does_not_replace_postreap_primary(monkeypatch, tmp_path):
    model = CollectionModel(monkeypatch)
    model.report = {}
    failure = SystemExit(23)
    original_temp = matrix.tempfile.TemporaryDirectory

    class FailingTemporaryDirectory:
        def __init__(self, **kwargs):
            self.real = original_temp(**kwargs)
            self.name = self.real.name

        def cleanup(self):
            self.real.cleanup()
            raise OSError("secondary temporary cleanup")

    def fail_parse(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(matrix.tempfile, "TemporaryDirectory", FailingTemporaryDirectory)
    monkeypatch.setattr(matrix, "_collection_report_details", fail_parse)
    with pytest.raises(SystemExit) as caught:
        matrix.collect_nodeids(tmp_path, Path("fake"), ownership_policy=owned.OWNERSHIP_POLICY)
    assert caught.value is failure
    assert any("secondary temporary cleanup" in note for note in caught.value.__notes__)
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]


def test_closed_pipe_does_not_count_as_eof_or_success(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=True, writer=True)
    original = model.send

    def keep_writer(pid, sig):
        original(pid, sig)
        model.writer = True

    monkeypatch.setattr(owned.os, "killpg", keep_writer)
    with pytest.raises(owned.CollectionCleanupError):
        model.run()
    assert model.pipe.closed and not model.owner.result["pipe_eof"]
    assert model.owner.result["leader_reaped"]
    assert model.clock >= model.owner.result["deadlines"]["final"]


def test_primary_read_error_does_not_skip_other_local_cleanup(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=False, writer=True)
    primary = OSError("original read failure")
    model.read_error = primary
    attempts = []

    def fail_selector_close():
        attempts.append("selector")
        raise SystemExit("secondary close")

    def close_pipe():
        attempts.append("pipe")
        model.pipe.closed = True

    monkeypatch.setattr(model, "close", fail_selector_close)
    model.pipe.close = close_pipe
    with pytest.raises(OSError) as caught:
        model.run()
    assert caught.value is primary
    assert attempts == ["selector", "pipe"]
    assert model.pipe.closed


def test_changed_recording_handler_revokes_signalling_authority(monkeypatch):
    model = CollectionModel(monkeypatch, terminal=False)
    model.after_create = lambda: model.handlers.update({signal.SIGTERM: signal.SIG_IGN})
    with pytest.raises(owned.CollectionCleanupError):
        model.run()
    assert not any(name.startswith("SIG") for name, _ in model.events)


def test_nonzero_child_is_not_masked_by_later_cleanup_error(monkeypatch):
    model = CollectionModel(monkeypatch)
    model.exit_status = 7 << 8
    model.signal_errno = errno.EPERM
    with pytest.raises(owned.CollectionCleanupError, match="exited unsuccessfully"):
        model.run()
    assert model.process.returncode == 7
    assert model.scope.secondary


def test_invalid_timeout_is_positive_precreation_none(monkeypatch):
    model = CollectionModel(monkeypatch)
    scope = owned.CancellationScope(owned.OWNERSHIP_POLICY)
    owner = owned.OwnedCollection(scope)
    with pytest.raises(owned.UnsupportedCollection), scope:
        owner.run(["fake"], cwd=Path("."), env={}, timeout=float("nan"))
    assert owner.result["ownership"] == "NONE_PROVEN"
    assert owner.result["cleanup_status"] == "NOT_NEEDED"
    assert model.spawn_count == 0 and not model.events


def test_worker_thread_context_is_refused_before_launch(monkeypatch):
    model = CollectionModel(monkeypatch)
    monkeypatch.setattr(owned.threading, "current_thread", lambda: object())
    with (
        pytest.raises(owned.UnsupportedCollection),
        owned.CancellationScope(owned.OWNERSHIP_POLICY),
    ):
        model.spawn([])
    assert model.spawn_count == 0


def test_actual_registry_runpy_declarations_load_without_scripts_path(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    clean_path = [
        str(root),
        *[p for p in sys.path if Path(p or ".").resolve() not in (root, root / "scripts")],
    ]
    monkeypatch.setattr(sys, "path", clean_path)
    monkeypatch.delitem(sys.modules, "_matrix_collection_process", raising=False)
    registry = runpy.run_path(str(root / "tests" / "_tier_config.py"))
    declarations = registry["_MATRIX"]
    assert declarations["_COLLECTION_PROCESS"] is None
    assert declarations["required_matrix_nodeids"]() == matrix.required_matrix_nodeids()
    assert registry["TIER_REQUIRED_NODEIDS"]["trade"] == matrix.STRICT_TRADE_NODEIDS
    assert "test_matrix_collection_process.py" in registry["UNIT_MODULES"]
    assert str(root / "scripts") not in sys.path


def test_direct_file_and_package_lifecycle_loaders_need_no_scripts_path(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if Path(p or ".").resolve() != root / "scripts"]
    )
    direct = runpy.run_path(
        str(root / "scripts" / "tcp_link_matrix.py"), run_name="_matrix_declaration"
    )
    assert direct["_COLLECTION_PROCESS"] is None
    helper = direct["_load_collection_process"]()
    assert Path(helper.__file__).resolve() == root / "scripts" / "_matrix_collection_process.py"
    assert helper.OWNERSHIP_POLICY == owned.OWNERSHIP_POLICY
    assert matrix._load_collection_process() is owned


@pytest.mark.parametrize("after_reap", [False, True])
def test_final_pipe_failure_preserves_delayed_owned_reap(monkeypatch, after_reap):
    model = CollectionModel(monkeypatch, terminal=False, writer=True)
    terminal_at = None
    pipe_error = OSError("final-phase pipe failure")
    pipe_failed_at = None
    original_read = model.read

    def delayed_signal(pid, sig):
        nonlocal terminal_at
        assert pid == 31337 and not model.reaped
        model.events.append((sig.name, model.clock))
        if sig == signal.SIGKILL:
            terminal_at = model.clock + 0.5
            model.descendant = False

    def delayed_observation(*_args):
        if terminal_at is not None and model.clock >= terminal_at:
            model.terminal = True
        return (
            SimpleNamespace(si_pid=31337, si_code=owned.os.CLD_EXITED, si_status=0)
            if model.terminal
            else None
        )

    def failing_final_read(fd, size):
        nonlocal pipe_failed_at
        if (
            terminal_at is not None
            and not model.owner.signals_enabled
            and model.reaped == after_reap
            and pipe_failed_at is None
        ):
            pipe_failed_at = model.clock
            raise pipe_error
        return original_read(fd, size)

    monkeypatch.setattr(owned.os, "killpg", delayed_signal)
    monkeypatch.setattr(owned.os, "waitid", delayed_observation)
    monkeypatch.setattr(owned.os, "read", failing_final_read)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        model.run(timeout=0.2)
    assert caught.value is model.scope.primary
    assert pipe_failed_at is not None and model.reaped
    reap_at = next(at for name, at in model.events if name == "reap")
    kill_at = next(at for name, at in model.events if name == "SIGKILL")
    assert kill_at < reap_at < model.owner.result["deadlines"]["final"]
    assert (pipe_failed_at < reap_at) if not after_reap else (reap_at <= pipe_failed_at)
    assert model.owner.result["ownership"] == "REAPED"
    assert model.owner.result["leader_reaped"] and not model.owner.result["pipe_eof"]
    assert model.owner.result["cleanup_status"] == "FAILED"
    assert any("final-phase pipe failure" in error for error in model.owner.result["errors"])
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]


@pytest.mark.parametrize(
    "sig,kind", [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)]
)
def test_cancellation_at_actual_postassignment_checkpoint(monkeypatch, sig, kind):
    model = CollectionModel(monkeypatch)
    checkpoint = owned.CancellationScope.checkpoint
    injected = False

    def postassignment(scope, *, raise_primary=True):
        nonlocal injected
        if (
            not injected
            and model.spawn_count == 1
            and model.owner.process is model.process
            and model.owner.result["ownership"] == "OWNED_UNREAPED"
            and not model.owner.cleanup_started
        ):
            injected = True
            assert model.events == []
            model.cancel(sig)
        return checkpoint(scope, raise_primary=raise_primary)

    monkeypatch.setattr(owned.CancellationScope, "checkpoint", postassignment)
    with pytest.raises(kind) as caught:
        model.run()
    assert injected and model.spawn_count == 1
    if sig == signal.SIGTERM:
        assert caught.value.code == 143
    assert model.owner.process is model.process and model.owner.result["ownership"] == "REAPED"
    assert model.owner.result["deadlines"] == {
        "start": 0.0,
        "grace": 1.0,
        "escalation": 2.0,
        "final": 5.0,
    }
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]
    assert model.handlers[signal.SIGINT] == signal.default_int_handler
    assert model.handlers[signal.SIGTERM] == signal.SIG_DFL


@pytest.mark.parametrize(
    "sig,kind", [(signal.SIGINT, KeyboardInterrupt), (signal.SIGTERM, SystemExit)]
)
@pytest.mark.parametrize("boundary", ["observation", "reap"])
def test_first_cancellation_in_final_observation_or_reap(monkeypatch, sig, kind, boundary):
    model = CollectionModel(monkeypatch)
    original = model.observe if boundary == "observation" else model.reap
    injected = False

    def cancel_final(*args):
        nonlocal injected
        if model.owner.cleanup_started and not model.owner.signals_enabled and not injected:
            assert model.owner.process is model.process and not model.reaped
            injected = True
            model.cancel(sig)
        return original(*args)

    monkeypatch.setattr(
        owned.os, "waitid" if boundary == "observation" else "waitpid", cancel_final
    )
    with pytest.raises(kind) as caught:
        model.run()
    assert injected and model.reaped and model.spawn_count == 1
    if sig == signal.SIGTERM:
        assert caught.value.code == 143
    assert model.owner.result["ownership"] == "REAPED"
    assert model.owner.result["deadlines"] == {
        "start": 0.0,
        "grace": 1.0,
        "escalation": 2.0,
        "final": 5.0,
    }
    assert [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]
    assert model.handlers[signal.SIGINT] == signal.default_int_handler
    assert model.handlers[signal.SIGTERM] == signal.SIG_DFL


def test_complete_valid_report_uses_actual_parser_and_matrix_auditor(monkeypatch, tmp_path):
    model = CollectionModel(monkeypatch)
    groups = (
        matrix.LOCAL_VERSION_PAIR_NODEIDS,
        matrix.REMOTE_VERSION_PAIR_NODEIDS,
        matrix.REMOTE_REVERSED_ROLE_NODEIDS,
        matrix.TIMED_MCP_SMOKE_NODEIDS,
        matrix.LOCAL_VARIANT_NODEIDS,
        matrix.STRICT_TRADE_NODEIDS,
        matrix.STRICT_BATTLE_NODEIDS,
    )
    nodeids = sorted(set().union(*groups))
    model.report = {
        "exitstatus": 0,
        "collection_only": True,
        "counts": {name: 0 for name in matrix._COLLECTION_REPORT_FIELDS},
        "tests": [],
        "nodeids": nodeids,
        "collected": len(nodeids),
        "collection_errors": [],
        "collection_skips": [],
    }
    audit, command = matrix.collect_nodeids(
        tmp_path, Path("fake"), ownership_policy=owned.OWNERSHIP_POLICY
    )
    assert command[:3] == ["fake", "-m", "pytest"]
    assert audit["structural_pass"] and audit["acceptance_matrix_complete"]
    assert audit["collected"] == len(nodeids) and not audit["collection_errors"]
    assert not audit["collection_skips"] and not audit["duplicate_nodeids"]
    assert all(
        group["expected"] == group["present"] and not group["missing"]
        for group in audit["groups"].values()
    )
    assert audit["runtime"] == "not-run"
    assert audit["collection_cleanup"]["group_quiescence"] == "UNPROVEN"
    assert audit["collection_cleanup"]["cleanup_status"] == "SUPPORTED_ACTIONS_COMPLETE"
    assert model.reaped and [name for name, _ in model.events] == ["SIGTERM", "SIGKILL", "reap"]


@pytest.mark.parametrize("deadline", [float("inf"), float("-inf"), float("nan")])
def test_adapter_nonfinite_deadline_fails_closed_without_wait_or_signal(monkeypatch, deadline):
    model = CollectionModel(monkeypatch)
    assert owned.reap_leader_only(model.process, deadline) is False
    assert model.events == []
