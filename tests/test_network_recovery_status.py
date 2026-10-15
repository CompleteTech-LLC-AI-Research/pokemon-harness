"""Recovery termination belongs to the same owned-frame result (#620).

Deterministic adapter controls exercise the actual installer/ownership scopes;
separate authored-runtime controls call real public PyBoy.tick, without TCP or
commercial assets. These controls are not gameplay or native-build proof.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pyboy.utils import WindowEvent

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.ownership import owner_for
from tests.test_serial_backend_boundary import emulator as _emulator_fixture  # noqa: F401


@dataclass
class _Frame:
    result: object = True
    advance: bool = True
    quitting: bool = False
    stopped: bool = False
    error: BaseException | None = None


class _Gate:
    """Observe the real serial gate's enter/exit without replacing its lock."""

    def __init__(self, original):
        self.original = original
        self.depth = 0

    def __enter__(self):
        self.original.__enter__()
        self.depth += 1
        return self

    def __exit__(self, *args):
        self.depth -= 1
        return self.original.__exit__(*args)


class _Backend:
    """Recording protocol adapter: explicit work publication, no transport loop.

    Each held token is an identity, not proof of CPU/ROM consumption. The real
    owner installer decides when to execute a frame and when to release it.
    """

    def __init__(self, path, *, progress_count=1):
        self.path = path
        self.progress_count = progress_count
        self._dispatch_to_owner = True
        self._local_core = SimpleNamespace(internal_clock=0, transfer_enabled=0)
        self.held = None
        self.tokens = []
        self.released = []
        self.events = []
        self.pumps = 0
        self.callback_count = 0
        self.abort = []
        self.closed = []
        self.progress_error = None
        self.finish_error = None
        self.endpoint = None
        self.owner = None

    def publish(self):
        token = object()
        self.tokens.append(token)
        self.held = token
        self.events.append("publish")

    def begin_frame_turn(self, *, leader):
        self.events.append(("begin", leader))

    def debug_snapshot(self):
        return {"pending_edge_requests": int(self.held is not None)}

    def service_pending_edges(self, *, max_edges=None):
        if max_edges is not None:
            assert max_edges == 1
            self.events.append("service-progress")
            if self.progress_error is not None:
                raise self.progress_error
            return 0
        self.pumps += 1
        self.events.append("pump")
        if self.path == "post" and self.pumps == 2:
            self.publish()
        return 0

    def begin_owner_frame(self):
        return self.held

    def finish_owner_frame(self, token):
        self.events.append("finish-owner")
        if token is not None:
            assert token is self.held
            self.released.append(token)
            self.held = None

    def finish_frame_turn(self, *, leader, progress_callback):
        self.events.append(("finish", leader))
        if self.path == "progress":
            for _ in range(self.progress_count):
                if self.held is None:
                    self.publish()
                self.callback_count += 1
                progress_callback()
        if self.finish_error is not None:
            raise self.finish_error

    def abort_frame_turn(self, *, leader):
        self.abort.append(leader)

    def _mark_closed(self, error):
        self.closed.append(error)


class _Endpoint:
    """Scripted original tick; does not implement result aggregation."""

    def __init__(self, frames):
        self.frames = frames
        self.calls = []
        self.frame_count = 0
        self.quitting = False
        self.stopped = False
        self.owner = None
        self.link = None
        self.on_tick = None

    def advance(self, *args, **kwargs):
        assert self.owner.execution.depth >= 2
        assert self.link._serial_gate.depth == 1
        assert self.link._network_tick_active is True
        self.calls.append((args, kwargs))
        assert len(self.calls) <= len(self.frames), "unexpected extra recovery tick"
        frame = self.frames[len(self.calls) - 1]
        if self.on_tick is not None:
            self.on_tick()
        if frame.error is not None:
            raise frame.error
        self.quitting = frame.quitting
        self.stopped = frame.stopped
        if frame.advance:
            self.frame_count += 1
        return frame.result


@contextmanager
def _owned(frames, *, path="post", barrier=False, leader=True, seam="_tick", progress_count=1):
    backend = _Backend(path, progress_count=progress_count)
    endpoint = _Endpoint(frames)
    if seam == "_tick":
        endpoint._tick = endpoint.advance
        endpoint.tick = lambda: endpoint._tick(False, True)
    else:
        endpoint.tick = endpoint.advance
    link = PyBoyLinkSession(network_backend=backend, network_is_internal_clock=leader)
    link._network_frame_barrier = barrier
    link._serial_gate = _Gate(link._serial_gate)
    owner = owner_for(endpoint)
    endpoint.owner, endpoint.link = owner, link
    original = getattr(endpoint, seam)
    link._install_network_tick_owner(endpoint)
    try:
        yield SimpleNamespace(
            endpoint=endpoint,
            backend=backend,
            link=link,
            owner=owner,
            call=endpoint.tick,
        )
    finally:
        assert link._network_tick_active is False
        assert owner.execution.depth == 0
        assert link._serial_gate.depth == 0
        link._restore_network_tick_owner(endpoint)
        assert getattr(endpoint, seam) == original
        assert link._original_ticks == {}
        assert owner.closed is False


@pytest.mark.parametrize("path", ["post", "progress"])
@pytest.mark.parametrize("leader", [False, True], ids=["follower", "leader"])
@pytest.mark.parametrize("seam", ["_tick", "tick"], ids=["one-frame", "fallback"])
def test_recovery_false_is_the_owned_result(path, leader, seam):
    with _owned(
        [_Frame(), _Frame(False, quitting=True)],
        path=path,
        barrier=path == "progress",
        leader=leader,
        seam=seam,
    ) as case:
        result = case.call()
        assert len(case.endpoint.calls) == 2
        assert case.endpoint.frame_count == 2
        assert case.backend.released == []
        assert case.backend.held is case.backend.tokens[0]
        assert case.backend.callback_count == (1 if path == "progress" else 0)
        assert case.backend.abort == case.backend.closed == []
        assert result is False


@pytest.mark.parametrize("path", ["post", "progress"])
@pytest.mark.parametrize("barrier", [False, True])
def test_all_running_frames_preserve_true(path, barrier):
    if path == "progress" and not barrier:
        # This combination intentionally publishes no callback work.
        frames = [_Frame()]
    else:
        frames = [_Frame(), _Frame()]
    with _owned(frames, path=path, barrier=barrier) as case:
        result = case.call()
        assert len(case.endpoint.calls) == len(frames)
        assert case.endpoint.frame_count == len(frames)
        assert case.backend.released == case.backend.tokens
        assert result is True


@pytest.mark.parametrize("path", ["post", "progress"])
def test_initial_false_stays_false_without_skipping_required_recovery(path):
    with _owned([_Frame(False), _Frame(True)], path=path, barrier=True) as case:
        result = case.call()
        assert len(case.endpoint.calls) == 2
        assert case.endpoint.frame_count == 2
        assert case.backend.released == case.backend.tokens
        assert result is False


def test_false_callback_result_survives_later_true_callback():
    with _owned(
        [_Frame(True), _Frame(False), _Frame(True)],
        path="progress",
        barrier=True,
        progress_count=2,
    ) as case:
        result = case.call()
        assert len(case.endpoint.calls) == 3
        assert case.backend.callback_count == 2
        assert result is False
        assert len(case.backend.tokens) == 1
        assert case.backend.released == case.backend.tokens


@pytest.mark.parametrize(
    "path,stage",
    [
        ("post", "requested"),
        ("post", "recovery"),
        ("post", "finish"),
        ("progress", "requested"),
        ("progress", "recovery"),
        ("progress", "progress"),
        ("progress", "finish"),
    ],
)
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_original_exceptions_keep_abort_and_lifecycle_policies(stage, error_type, path):
    error = error_type("original frame failure")
    frames = [_Frame(error=error)] if stage == "requested" else [_Frame(), _Frame()]
    if stage == "recovery":
        frames[1].error = error
    with _owned(frames, path=path, barrier=True) as case:
        if stage == "progress":
            case.backend.progress_error = error
        if stage == "finish":
            case.backend.finish_error = error
        with pytest.raises(error_type) as caught:
            case.call()
        assert caught.value is error
        assert case.backend.abort == [True]
        assert case.backend.closed == []
        assert case.link._network_tick_active is False
        assert case.owner.execution.depth == 0
        if stage == "requested" or (stage == "progress" and path == "progress"):
            assert len(case.endpoint.calls) == 1
        else:
            assert len(case.endpoint.calls) == 2


@pytest.mark.parametrize("stage", ["requested", "recovery"])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_no_barrier_original_error_is_not_replaced_with_a_boolean(stage, error_type):
    error = error_type("original no-barrier failure")
    frames = [_Frame(error=error)] if stage == "requested" else [_Frame(), _Frame(error=error)]
    with _owned(frames) as case:
        with pytest.raises(error_type) as caught:
            case.call()
        assert caught.value is error
        assert case.backend.abort == []
        assert len(case.backend.closed) == 1
        assert str(case.backend.closed[0]) == (
            "emulator frame aborted with pending serial work"
            if stage == "requested"
            else "emulator byte continuation aborted"
        )
        assert case.link._network_tick_active is False
        assert case.owner.execution.depth == 0


@pytest.mark.parametrize("path", ["post", "progress"])
@pytest.mark.parametrize("state", ["paused", "stopped", "quitting"])
def test_incomplete_or_quitting_recovery_does_not_release_held_token(path, state):
    recovery = _Frame(
        result=state == "paused",
        advance=state == "quitting",
        quitting=state == "quitting",
        stopped=state == "stopped",
    )
    with _owned([_Frame(), recovery], path=path, barrier=True) as case:
        result = case.call()
        assert len(case.endpoint.calls) == 2
        assert len(case.backend.tokens) == 1
        assert case.backend.held is case.backend.tokens[0]
        assert case.backend.released == []
        assert result is (state == "paused")


@pytest.mark.parametrize("path", ["post", "progress"])
def test_terminal_barrier_failure_is_not_converted_to_false_or_success(path):
    error = TimeoutError("held response never settled")
    with _owned([_Frame(), _Frame(False, advance=False)], path=path, barrier=True) as case:
        case.backend.finish_error = error
        with pytest.raises(TimeoutError) as caught:
            case.call()
        assert caught.value is error
        assert len(case.endpoint.calls) == 2
        assert case.backend.held is case.backend.tokens[0]
        assert case.backend.released == []
        assert case.backend.abort == [True]


@pytest.mark.parametrize("seam", ["_tick", "tick"], ids=["one-frame", "fallback"])
def test_truthy_adapter_result_is_unchanged_without_termination(seam):
    # Preserve a truthy duck-typed adapter's original requested return value.
    # Native _tick exposes an integer status; its zero must count as False.
    sentinel = object()
    with _owned([_Frame(sentinel), _Frame(True)], seam=seam) as case:
        result = case.call()
        assert len(case.endpoint.calls) == 2
        assert result is sentinel


@pytest.mark.parametrize("path", ["post", "progress"])
@pytest.mark.parametrize("quit_event", [False, True], ids=["running", "QUIT"])
def test_real_public_tick_reports_delayed_quit_during_recovery(
    _emulator_fixture,  # noqa: F811 - the shared fixture owns runtime cleanup.
    monkeypatch,
    path,
    quit_event,
):
    pyboy = _emulator_fixture
    backend = _Backend(path)
    link = PyBoyLinkSession(network_backend=backend, network_is_internal_clock=True)
    link._network_frame_barrier = path == "progress"
    calls = []
    results = []
    original = pyboy._tick

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        value = original(*args, **kwargs)
        results.append(value)
        return value

    monkeypatch.setattr(pyboy, "_tick", observe)
    owner = owner_for(pyboy)
    link._install_network_tick_owner(pyboy)
    try:
        if quit_event:
            pyboy.send_input(WindowEvent.QUIT, delay=1)
        frame_before = pyboy.frame_count
        result = pyboy.tick(1, False, False)
        assert len(calls) == 2
        assert results == [True, not quit_event]
        assert pyboy.frame_count - frame_before == 2
        assert link._network_tick_active is False
        assert owner.execution.depth == 0
        assert bool(result) is (not quit_event)
        if quit_event:
            assert backend.held is backend.tokens[0]
            assert backend.released == []
        else:
            assert backend.released == backend.tokens
    finally:
        link._restore_network_tick_owner(pyboy)
        assert pyboy._tick is observe
        assert link._original_ticks == {}


@pytest.mark.parametrize("path", ["post", "progress"])
def test_native_integer_zero_stops_without_public_quitting_attribute(path):
    # Native declares _tick as int64_t, while quitting is cdef-private. Model
    # that externally invisible quit flag without building/importing native.
    with _owned([_Frame(1), _Frame(0)], path=path, barrier=True) as case:
        result = case.call()
        assert len(case.endpoint.calls) == 2
        assert case.endpoint.frame_count == 2
        assert case.endpoint.quitting is False
        assert result is False
        assert case.backend.held is case.backend.tokens[0]
        assert case.backend.released == []


def test_termination_flag_does_not_leak_into_next_owned_invocation():
    with _owned([_Frame(), _Frame(False), _Frame(True)]) as case:
        assert case.call() is False
        # First invocation left its token held. A real later successful frame
        # may release it, without carrying a previous invocation's result.
        assert len(case.endpoint.calls) == 2
        assert case.backend.held is case.backend.tokens[0]
        assert case.call() is True
        assert len(case.endpoint.calls) == 3
        assert case.backend.released == case.backend.tokens
        assert case.backend.held is None


@pytest.mark.parametrize("barrier", [False, True])
def test_recursive_entry_still_rejects_and_clears_outer_lifecycle(barrier):
    with _owned([_Frame()], barrier=barrier) as case:
        case.endpoint.on_tick = case.call
        with pytest.raises(RuntimeError, match="recursive network emulator execution"):
            case.call()
        assert len(case.endpoint.calls) == 1
        assert case.endpoint.frame_count == 0
        assert case.link._network_tick_active is False
        assert case.owner.execution.depth == 0
        assert case.link._serial_gate.depth == 0
        assert case.backend.abort == ([True] if barrier else [])
        assert len(case.backend.closed) == (0 if barrier else 1)
