"""Session honors an optional backend ownership guard before emulator work."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from pokered_harness.session import InvalidStateError
from tests.test_session import _session


class OwnershipError(RuntimeError):
    code = "paired_session_operation"


class RecordingBackend:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def validate_session_operation(self, operation):
        self.calls.append(operation)
        if self.error is not None:
            raise self.error


def _attach(pyboy, backend):
    pyboy.mb = SimpleNamespace(serial=SimpleNamespace(backend=backend))


def _operation(session, operation, event_names=("wanted",)):
    if operation == "step":
        return session.step(3)
    if operation == "load_state":
        return session.load_state(b"replacement")
    return session.run_until_event(event_names, max_ticks=3, chunk=3)


@pytest.mark.parametrize("operation", ["step", "load_state", "run_until_event"])
def test_guard_rejection_precedes_emulator_and_event_name_side_effects(operation):
    session, pyboy, events = _session()
    session.reset_tick(7)
    pyboy.memory[0xD35E] = 42
    error = OwnershipError("paired provider owns frame advancement and state restore")
    backend = RecordingBackend(error)
    _attach(pyboy, backend)
    effects = []

    def tick(*_args, **_kwargs):
        effects.append("tick")
        pyboy.memory[0xD35E] = 99

    def load(_stream):
        effects.append("load")
        pyboy.memory[0xD35E] = 99

    def names():
        effects.append("event names consumed")
        yield "wanted"

    pyboy.tick, pyboy.load_state = tick, load
    before_events = list(events)
    with pytest.raises(OwnershipError) as raised:
        _operation(session, operation, names())
    assert raised.value is error
    assert raised.value.code == "paired_session_operation"
    assert backend.calls == [operation]
    assert effects == []
    assert session.current_tick() == 7
    assert pyboy.memory[0xD35E] == 42
    assert list(events) == before_events


@pytest.mark.parametrize("operation", ["step", "load_state", "run_until_event"])
def test_allowing_guard_runs_before_operation_changes(operation):
    session, pyboy, _ = _session()
    observations = []

    def validate(name):
        observations.append((name, session.current_tick(), pyboy._saved_state))

    _attach(pyboy, SimpleNamespace(validate_session_operation=validate))
    _operation(session, operation)
    assert observations[0] == (operation, 0, b"")
    if operation == "load_state":
        assert pyboy._saved_state == b"replacement"
        assert len(observations) == 1
    else:
        assert session.current_tick() == 3
        assert pyboy.tick_calls == [(3, False)]
        if operation == "run_until_event":
            assert observations == [("run_until_event", 0, b""), ("step", 0, b"")]


@pytest.mark.parametrize(
    "motherboard",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(serial=None),
        SimpleNamespace(serial=SimpleNamespace()),
        SimpleNamespace(serial=SimpleNamespace(backend=None)),
        SimpleNamespace(serial=SimpleNamespace(backend=object())),
        SimpleNamespace(serial=SimpleNamespace(backend=SimpleNamespace(validate_session_operation=None))),
        SimpleNamespace(serial=SimpleNamespace(backend=SimpleNamespace(validate_session_operation="not callable"))),
    ],
)
def test_absent_or_noncallable_backend_hook_preserves_operations(motherboard):
    session, pyboy, _ = _session()
    if motherboard is not None:
        pyboy.mb = motherboard
    session.step()
    session.load_state(b"replacement")
    result = session.run_until_event("wanted", max_ticks=2)
    assert session.current_tick() == 3
    assert pyboy._saved_state == b"replacement"
    assert not result.reached


def test_paired_guard_does_not_restrict_input_read_save_or_close():
    session, pyboy, _ = _session()
    backend = RecordingBackend(OwnershipError("denied"))
    _attach(pyboy, backend)
    session.press("a")
    session.hold("b")
    session.release("b")
    assert session.read_game_state() is not None
    assert session.event_snapshot() == []
    assert session.current_tick() == 0
    assert session.save_state()
    session.close()
    assert pyboy.stopped
    assert backend.calls == []


def test_ownership_rejection_precedes_empty_event_name_validation():
    session, pyboy, _ = _session()
    _attach(pyboy, RecordingBackend(OwnershipError("denied")))
    with pytest.raises(OwnershipError):
        session.run_until_event([], max_ticks=1)
    # Without the optional ownership guard, the existing validation remains.
    _attach(pyboy, object())
    with pytest.raises(ValueError, match="event_names must be non-empty"):
        session.run_until_event([], max_ticks=1)


@pytest.mark.parametrize("operation", ["step", "load_state", "max_ticks", "chunk"])
def test_existing_payload_and_tick_validation_precedes_backend_hook(operation):
    session, pyboy, _ = _session()
    backend = RecordingBackend(OwnershipError("denied"))
    _attach(pyboy, backend)
    with pytest.raises((ValueError, InvalidStateError)):
        if operation == "step":
            session.step(0)
        elif operation == "load_state":
            session.load_state(b"")
        elif operation == "max_ticks":
            session.run_until_event("wanted", max_ticks=0)
        else:
            session.run_until_event("wanted", max_ticks=1, chunk=0)
    assert backend.calls == []


@pytest.mark.parametrize("operation", ["step", "load_state", "run_until_event"])
def test_unexpected_hook_exception_propagates_unchanged(operation):
    session, pyboy, _ = _session()
    error = ValueError("provider validation failed")
    _attach(pyboy, RecordingBackend(error))
    with pytest.raises(ValueError) as raised:
        _operation(session, operation)
    assert raised.value is error
    assert session.current_tick() == 0
    assert pyboy.tick_calls == []
    assert pyboy._saved_state == b""


@pytest.mark.parametrize("operation", ["step", "load_state", "run_until_event"])
def test_guard_is_invoked_while_emulator_lock_is_held(operation):
    session, pyboy, _ = _session()
    checked = []

    def validate(name):
        contender_acquired = []
        checked_event = threading.Event()

        def contend():
            acquired = session._lock.acquire(blocking=False)
            contender_acquired.append(acquired)
            if acquired:
                session._lock.release()
            checked_event.set()

        thread = threading.Thread(target=contend, daemon=True)
        thread.start()
        assert checked_event.wait(1)
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert contender_acquired == [False]
        checked.append(name)

    _attach(pyboy, SimpleNamespace(validate_session_operation=validate))
    _operation(session, operation)
    assert checked[0] == operation
