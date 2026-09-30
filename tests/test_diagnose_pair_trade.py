"""ROM-free tests for the bounded pair-trade diagnostic driver."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "diagnose_pair_trade.py"
_SPEC = importlib.util.spec_from_file_location("diagnose_pair_trade", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
diagnose = importlib.util.module_from_spec(_SPEC)
sys.modules["diagnose_pair_trade"] = diagnose
_SPEC.loader.exec_module(diagnose)


class _FakeSession:
    def __init__(self, side: int) -> None:
        self.side = side
        self._pyboy = object()
        self.inputs: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.closed = 0
        self.state = SimpleNamespace(
            overworld=SimpleNamespace(
                map_id=0xEF,
                x=side + 1,
                y=4,
                walk_counter=0,
                direction="up",
            )
        )

    def press(self, *args: object, **kwargs: object) -> None:
        self.inputs.append((args, kwargs))

    def read_game_state(self):
        return self.state

    def close(self, *, save: bool = False) -> None:
        assert save is False
        self.closed += 1


class _FakeLink:
    def __init__(self) -> None:
        self.attached: list[object] = []
        self.calls: list[tuple[str, int, tuple[object, ...], dict[str, object]]] = []
        self.closed = 0

    def attach(self, pyboy: object) -> None:
        self.attached.append(pyboy)

    def step_interleaved(self, frames: int, *args: object, **kwargs: object) -> None:
        self.calls.append(("step_interleaved", frames, args, kwargs))

    def step(self, frames: int, *args: object, **kwargs: object) -> None:
        self.calls.append(("step", frames, args, kwargs))

    def close(self) -> None:
        self.closed += 1


class _RecordingWriter:
    instances: ClassVar[list[_RecordingWriter]] = []
    fail_frame: int | None = None

    def __init__(self, output_root, *, max_count, selected_frame_ordinals):
        self.output_root = Path(output_root)
        self.max_count = max_count
        self.selected_frame_ordinals = tuple(sorted(selected_frame_ordinals))
        self.records: list[tuple[object, int, dict[str, object]]] = []
        type(self).instances.append(self)

    def capture(self, link, frame_ordinal, *, metadata):
        if frame_ordinal == type(self).fail_frame:
            raise RuntimeError("synthetic checkpoint failure")
        self.records.append((link, frame_ordinal, metadata))
        return self.output_root / f"checkpoint-{frame_ordinal}"


def _install_counters(_a, _b):
    return {"Trade": [0, 0]}


def test_proxy_splits_only_at_exact_requested_frame_ends():
    target = _FakeLink()
    captures: list[int] = []
    deadline = diagnose._Deadline(10, clock=lambda: 0.0)
    proxy = diagnose._CaptureLinkProxy(
        target,
        capture=lambda ordinal: captures.append(ordinal),
        selected_frame_ordinals=(3, 5),
        deadline=deadline,
    )

    proxy.step_interleaved(6, chunk_cycles=256)

    assert captures == [0, 3, 5]
    assert [(method, frames) for method, frames, _args, _kwargs in target.calls] == [
        ("step_interleaved", 3),
        ("step_interleaved", 2),
        ("step_interleaved", 1),
    ]
    assert proxy.frame_ordinal == 6


def test_driver_preserves_natural_input_schedule_and_capture_frames(tmp_path, monkeypatch):
    _RecordingWriter.instances.clear()
    _RecordingWriter.fail_frame = None
    sessions: list[_FakeSession] = []

    def open_session(version: str, *, state_path=None):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        return session

    link = _FakeLink()

    def drive_to(a, b, pair):
        a.press("up", duration=6)
        b.press("up", duration=6)
        pair.step_interleaved(5, chunk_cycles=256)
        pair.step(3)
        return {"final_map_a": 0xEF, "final_map_b": 0xEF}

    def drive_trade(a, b, pair, *, counters, trade_budget_frames, step_frames):
        assert trade_budget_frames == 40
        assert step_frames == 4
        a.press("a", duration=4)
        b.press("a", duration=4)
        pair.step_interleaved(4, chunk_cycles=256)
        return {"add_mon": [1, 1], "counters": counters}

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", lambda: {"test": True})
    result = diagnose.run_pair_trade(
        tmp_path,
        capture_frame_ordinals=(2, 7),
        max_captures=3,
        deadline_seconds=10,
        trade_budget_frames=40,
        step_frames=4,
        open_session=open_session,
        link_factory=lambda: link,
        install_trade_diag_counters=_install_counters,
        drive_to_trade_center=drive_to,
        drive_trade=drive_trade,
        clock=lambda: 0.0,
    )

    assert result["result"] == "completed_inspection"
    assert result["captured_frames"] == [0, 2, 7]
    assert result["acceptance_evidence"] is False
    assert link.closed == 1
    assert [session.closed for session in sessions] == [1, 1]
    writer = _RecordingWriter.instances[-1]
    assert [frame for _link, frame, _metadata in writer.records] == [0, 2, 7]
    assert [entry["operation"] for entry in writer.records[1][2]["input_transcript"]] == [
        "press",
        "press",
    ]
    assert [entry["operation"] for entry in writer.records[-1][2]["input_transcript"]] == [
        "press",
        "press",
    ]
    assert result["input_count"] == 4
    assert [entry["operation"] for entry in result["input_transcript"]] == [
        "press",
        "press",
        "press",
        "press",
    ]
    assert [(method, frames) for method, frames, _args, _kwargs in link.calls] == [
        ("step_interleaved", 1),
        ("step_interleaved", 1),
        ("step_interleaved", 1),
        ("step_interleaved", 1),
        ("step_interleaved", 1),
        ("step", 1),
        ("step", 1),
        ("step", 1),
        ("step_interleaved", 1),
        ("step_interleaved", 1),
        ("step_interleaved", 1),
        ("step_interleaved", 1),
    ]
    terminal = json.loads((tmp_path / diagnose.TERMINAL_FILENAME).read_text())
    assert terminal["stage"] == "complete"
    assert terminal["result"] == "completed_inspection"


def test_capture_failure_still_writes_terminal_and_cleans_up(tmp_path, monkeypatch):
    _RecordingWriter.instances.clear()
    _RecordingWriter.fail_frame = 2
    sessions: list[_FakeSession] = []

    def open_session(version: str, *, state_path=None):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        return session

    link = _FakeLink()

    def drive_to(a, b, pair):
        a.press("a", duration=4)
        b.press("a", duration=4)
        pair.step_interleaved(4)
        return {}

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)
    result = diagnose.run_pair_trade(
        tmp_path,
        capture_frame_ordinals=(2,),
        max_captures=2,
        deadline_seconds=10,
        open_session=open_session,
        link_factory=lambda: link,
        install_trade_diag_counters=_install_counters,
        drive_to_trade_center=drive_to,
        drive_trade=lambda *args, **kwargs: {},
        clock=lambda: 0.0,
    )

    assert result["result"] == "error"
    assert result["error"]["message"] == "synthetic checkpoint failure"
    assert link.closed == 1
    assert [session.closed for session in sessions] == [1, 1]
    terminal = json.loads((tmp_path / diagnose.TERMINAL_FILENAME).read_text())
    assert terminal["result"] == "error"
    assert terminal["stage"] == "trade_center"


def test_capture_bound_is_rejected_before_opening_sessions(tmp_path):
    with pytest.raises(diagnose.CaptureConfigurationError):
        diagnose.run_pair_trade(
            tmp_path,
            capture_frame_ordinals=(1, 2),
            max_captures=2,
            open_session=lambda *_args, **_kwargs: pytest.fail("must not open session"),
        )


def test_second_session_open_failure_closes_first_and_link(tmp_path, monkeypatch):
    _RecordingWriter.instances.clear()
    sessions: list[_FakeSession] = []
    link = _FakeLink()

    def open_session(version: str, *, state_path=None):
        if version == "yellow":
            raise RuntimeError("synthetic yellow open failure")
        session = _FakeSession(0)
        sessions.append(session)
        return session

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)
    result = diagnose.run_pair_trade(
        tmp_path,
        max_captures=1,
        deadline_seconds=10,
        open_session=open_session,
        link_factory=lambda: link,
        clock=lambda: 0.0,
    )

    assert result["result"] == "error"
    assert result["error"]["message"] == "synthetic yellow open failure"
    assert link.closed == 1
    assert [session.closed for session in sessions] == [1]


def test_cleanup_failure_makes_cli_exit_unsuccessful(monkeypatch, tmp_path):
    result = {
        "result": "completed_inspection",
        "cleanup_errors": [{"operation": "link.close", "error": "boom"}],
    }
    monkeypatch.setattr(diagnose, "run_pair_trade", lambda *args, **kwargs: result)
    assert diagnose.main(["--output-dir", str(tmp_path)]) == 1


def test_invalid_output_is_rejected_without_creating_files(tmp_path):
    with pytest.raises(ValueError, match="outside the source checkout"):
        diagnose.run_pair_trade(diagnose.REPO_ROOT / "diagnostic-output")
    assert not (diagnose.REPO_ROOT / "diagnostic-output").exists()

    with pytest.raises(ValueError, match="must not be empty"):
        diagnose.run_pair_trade("")

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / diagnose.TERMINAL_FILENAME).write_text("keep\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        diagnose.run_pair_trade(existing)
    assert (existing / diagnose.TERMINAL_FILENAME).read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
def test_signal_cancellation_restores_handlers_and_writes_terminal(tmp_path, monkeypatch, signum):
    _RecordingWriter.instances.clear()
    _RecordingWriter.fail_frame = None
    sessions: list[_FakeSession] = []
    link = _FakeLink()

    def open_session(version: str, *, state_path=None):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        return session

    def cancel_from_natural_driver(*_args, **_kwargs):
        os.kill(os.getpid(), signum)
        raise AssertionError("signal handler should raise DiagnosticCancelled")

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)
    before = signal.getsignal(signum)
    result = diagnose.run_pair_trade(
        tmp_path,
        max_captures=1,
        deadline_seconds=10,
        open_session=open_session,
        link_factory=lambda: link,
        install_trade_diag_counters=_install_counters,
        drive_to_trade_center=cancel_from_natural_driver,
        clock=lambda: 0.0,
    )

    assert result["result"] == "cancelled"
    assert result["cancellation_requested"] is True
    assert result["cancellation_signal"] == signal.Signals(signum).name
    assert signal.getsignal(signum) is before
    assert link.closed == 1
    assert [session.closed for session in sessions] == [1, 1]
    terminal = json.loads((tmp_path / diagnose.TERMINAL_FILENAME).read_text())
    assert terminal["terminal_phase"] == "final"
    assert terminal["result"] == "cancelled"


def test_signal_during_metadata_is_reported_and_handlers_restore(tmp_path, monkeypatch):
    opened = False

    def helper_that_swallows_signal():
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except diagnose.DiagnosticCancelled:
            return object()
        raise AssertionError("SIGINT handler should raise DiagnosticCancelled")

    def open_session(*_args, **_kwargs):
        nonlocal opened
        opened = True
        return _FakeSession(0)

    before = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(diagnose, "_safe_import_real_helpers", helper_that_swallows_signal)
    result = diagnose.run_pair_trade(
        tmp_path,
        max_captures=1,
        deadline_seconds=10,
        open_session=open_session,
        clock=lambda: 0.0,
    )

    assert result["result"] == "cancelled"
    assert result["cancellation_signal"] == "SIGINT"
    assert opened is False
    assert signal.getsignal(signal.SIGINT) is before
    terminal = json.loads((tmp_path / diagnose.TERMINAL_FILENAME).read_text())
    assert terminal["terminal_phase"] == "final"
    assert terminal["result"] == "cancelled"


def test_signal_handlers_are_not_installed_from_worker_thread():
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    cancellation = diagnose._SignalCancellation()
    completed = threading.Event()

    def install_from_worker():
        cancellation.install()
        cancellation.restore()
        completed.set()

    worker = threading.Thread(target=install_from_worker)
    worker.start()
    worker.join()

    assert completed.is_set()
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == before


def test_signal_handlers_restore_when_terminal_report_building_fails(tmp_path, monkeypatch):
    sessions: list[_FakeSession] = []
    link = _FakeLink()
    cancellations: list[object] = []

    def open_session(*_args, **_kwargs):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        cancellation = cancellations[0]
        cancellation.requested = True
        cancellation.signum = 2**31
        return session

    class InvalidReportSignum(diagnose._SignalCancellation):
        def install(self):
            super().install()
            cancellations.append(self)

    before = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(diagnose, "_SignalCancellation", InvalidReportSignum)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)

    with pytest.raises(ValueError, match="not a valid Signals"):
        diagnose.run_pair_trade(
            tmp_path,
            max_captures=1,
            deadline_seconds=10,
            open_session=open_session,
            link_factory=lambda: link,
            install_trade_diag_counters=_install_counters,
            drive_to_trade_center=lambda *_args, **_kwargs: {
                "final_map_a": 0xEF,
                "final_map_b": 0xEF,
            },
            drive_trade=lambda *args, **kwargs: {},
            clock=lambda: 0.0,
        )

    assert link.closed == 1
    assert [session.closed for session in sessions] == [1, 1]
    assert {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    } == before


def test_cancellation_during_final_write_is_reconciled(tmp_path, monkeypatch):
    _RecordingWriter.instances.clear()
    _RecordingWriter.fail_frame = None
    sessions: list[_FakeSession] = []
    link = _FakeLink()

    def open_session(*_args, **_kwargs):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        return session

    def drive_to(*_args, **_kwargs):
        return {"final_map_a": 0xEF, "final_map_b": 0xEF}

    writes: list[str] = []
    real_write = diagnose._write_json_atomic

    def write_with_cancellation(path, value):
        writes.append(value["terminal_phase"])
        if value["terminal_phase"] == "final" and writes.count("final") == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        return real_write(path, value)

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)
    monkeypatch.setattr(diagnose, "_write_json_atomic", write_with_cancellation)
    result = diagnose.run_pair_trade(
        tmp_path,
        max_captures=1,
        deadline_seconds=10,
        open_session=open_session,
        link_factory=lambda: link,
        install_trade_diag_counters=_install_counters,
        drive_to_trade_center=drive_to,
        drive_trade=lambda *args, **kwargs: {},
        clock=lambda: 0.0,
    )

    assert result["result"] == "cancelled"
    assert result["cancellation_signal"] == "SIGTERM"
    assert writes == ["cleanup_pending", "final", "final"]
    assert link.closed == 1
    assert [session.closed for session in sessions] == [1, 1]
    terminal = json.loads((tmp_path / diagnose.TERMINAL_FILENAME).read_text())
    assert terminal["result"] == "cancelled"
    assert terminal["cancellation_requested"] is True


def test_provisional_terminal_precedes_cleanup_failure(tmp_path, monkeypatch):
    _RecordingWriter.instances.clear()
    _RecordingWriter.fail_frame = None
    sessions: list[_FakeSession] = []

    class FailingCloseLink(_FakeLink):
        terminal_during_close: dict[str, object] | None = None

        def close(self) -> None:
            self.closed += 1
            self.terminal_during_close = json.loads(
                (tmp_path / diagnose.TERMINAL_FILENAME).read_text()
            )
            raise RuntimeError("synthetic cleanup failure")

    link = FailingCloseLink()

    def open_session(version: str, *, state_path=None):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        return session

    def drive_to(*_args, **_kwargs):
        return {"final_map_a": 0xEF, "final_map_b": 0xEF}

    phases: list[str] = []
    real_write = diagnose._write_json_atomic

    def record_write(path, value):
        phases.append(value["terminal_phase"])
        return real_write(path, value)

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)
    monkeypatch.setattr(diagnose, "_write_json_atomic", record_write)
    result = diagnose.run_pair_trade(
        tmp_path,
        max_captures=1,
        deadline_seconds=10,
        open_session=open_session,
        link_factory=lambda: link,
        install_trade_diag_counters=_install_counters,
        drive_to_trade_center=drive_to,
        drive_trade=lambda *args, **kwargs: {},
        clock=lambda: 0.0,
    )

    assert result["result"] == "completed_inspection"
    assert phases == ["cleanup_pending", "final"]
    assert result["cleanup_errors"]
    assert link.terminal_during_close is not None
    assert link.terminal_during_close["terminal_phase"] == "cleanup_pending"
    assert (
        json.loads((tmp_path / diagnose.TERMINAL_FILENAME).read_text())["terminal_phase"] == "final"
    )


def test_cleanup_runs_when_provisional_report_assembly_fails(tmp_path, monkeypatch):
    _RecordingWriter.instances.clear()
    _RecordingWriter.fail_frame = None
    sessions: list[_FakeSession] = []
    link = _FakeLink()
    clock_failed = False

    def open_session(*_args, **_kwargs):
        session = _FakeSession(len(sessions))
        sessions.append(session)
        return session

    def bad_clock():
        if clock_failed:
            raise RuntimeError("synthetic clock failure")
        return 0.0

    def drive_to(*_args, **_kwargs):
        nonlocal clock_failed
        clock_failed = True
        return {"final_map_a": 0xEF, "final_map_b": 0xEF}

    monkeypatch.setattr(diagnose, "PairCheckpointWriter", _RecordingWriter)
    monkeypatch.setattr(diagnose, "_version_and_asset_metadata", dict)
    with pytest.raises(RuntimeError, match="synthetic clock failure"):
        diagnose.run_pair_trade(
            tmp_path,
            max_captures=1,
            deadline_seconds=10,
            open_session=open_session,
            link_factory=lambda: link,
            install_trade_diag_counters=_install_counters,
            drive_to_trade_center=drive_to,
            drive_trade=lambda *args, **kwargs: {},
            clock=bad_clock,
        )

    assert link.closed == 1
    assert [session.closed for session in sessions] == [1, 1]


def test_deadline_is_checked_between_one_frame_owner_chunks():
    now = [0.0]
    target = _FakeLink()
    deadline = diagnose._Deadline(1.0, clock=lambda: now[0])
    proxy = diagnose._CaptureLinkProxy(
        target,
        capture=lambda _ordinal: None,
        selected_frame_ordinals=(),
        deadline=deadline,
        max_operation_frames=1,
    )

    def advance_one(_frames, *_args, **_kwargs):
        target.calls.append(("step_interleaved", 1, (), {}))
        now[0] += 2.0

    target.step_interleaved = advance_one
    with pytest.raises(diagnose.DeadlineExceeded, match="wall deadline"):
        proxy.step_interleaved(3)
    assert len(target.calls) == 1
