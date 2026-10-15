"""Deterministic custody adapters; real dispatcher/provider remain in use."""

from contextlib import contextmanager
import threading

from pokered_harness import mcp_server
from pokered_harness.mcp_server import dispatch_tool
from tests.test_mcp_local_runtime_contract import _sessions


class SyntheticControl(BaseException):
    pass


ERRORS = [RuntimeError, KeyboardInterrupt, SystemExit, SyntheticControl]
CHECKPOINTS = [
    "constructor", "attach1", "attach2", "lock_release", "publish_before", "publish_after"
]


def outcome(function):
    try:
        return function(), None
    except BaseException as error:  # relay process-control objects, never signal the host
        return None, error


def command(primary, link, name, arguments=None):
    return dispatch_tool(primary, name, arguments or {}, link=link)


def resources(primary, peer):
    return tuple(session._pyboy.mb.serial.backend for session in (primary, peer))


def assert_terminal(link):
    assert not link._local_pair_in_progress
    assert link._local_setup_done.is_set()
    assert link._local_setup_deadline is None


class ReleaseFault:
    def __init__(self, lock, fail):
        self.lock = lock
        self.fail = fail

    def acquire(self, *args, **kwargs):
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        return self.lock.release()

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, *_args):
        self.lock.release()
        self.fail()


def branch_fault(monkeypatch, checkpoint, error):
    primary, peer, link = _sessions()
    before = resources(primary, peer)
    provider = mcp_server.PyBoyLinkSession.local()
    witnesses = []
    armed = True

    def fail():
        nonlocal armed
        if armed:
            armed = False
            witnesses.append(checkpoint)
            raise error

    def construct():
        if checkpoint == "constructor":
            fail()
        return provider

    monkeypatch.setattr(mcp_server.PyBoyLinkSession, "local", construct)
    attach = provider.attach
    attached = []

    def attach_then_fault(emulator):
        result = attach(emulator)
        attached.append(emulator)
        if checkpoint == f"attach{len(attached)}":
            fail()
        return result

    monkeypatch.setattr(provider, "attach", attach_then_fault)
    if checkpoint == "lock_release":
        link._pair_lock = ReleaseFault(link._pair_lock, fail)
    if checkpoint.startswith("publish_"):
        base = type(link)

        class PublicationFault(base):
            def __setattr__(self, name, value):
                if name == "local_link_session" and value is provider:
                    if checkpoint == "publish_before":
                        fail()
                    super().__setattr__(name, value)
                    if checkpoint == "publish_after":
                        fail()
                else:
                    super().__setattr__(name, value)

        monkeypatch.setattr(link, "__class__", PublicationFault)
    return primary, peer, link, provider, before, witnesses


class Actors:
    """All waits bounded and all release gates opened before joins."""

    def __init__(self):
        self.gates = []
        self.threads = []
        self.outcomes = {}

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def start(self, name, function):
        assert len(self.threads) < 3

        def run():
            self.outcomes[name] = outcome(function)

        thread = threading.Thread(target=run, name=name)
        self.threads.append(thread)
        thread.start()
        return thread

    def close(self):
        for gate in self.gates:
            gate.set()
        for thread in self.threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in self.threads)
        assert len(self.outcomes) == len(self.threads)


@contextmanager
def owned_actors():
    actors = Actors()
    try:
        yield actors
    finally:
        actors.close()


class RecordingLock:
    """Finite lock adapter with explicit acquisition outcomes, no sleeps."""

    def __init__(self, lock, *, refuse=False, advance=None):
        self.lock = lock
        self.refuse = refuse
        self.advance = advance
        self.timeouts = []
        self.acquired = 0
        self.released = 0

    def acquire(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        if self.advance is not None:
            self.advance()
        if self.refuse:
            return False
        ok = self.lock.acquire(*args, **kwargs)
        self.acquired += int(bool(ok))
        return ok

    def release(self):
        self.released += 1
        return self.lock.release()

    def __enter__(self):
        assert self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class OwnerReleaseFault:
    """Actually release the canonical lock, then raise once after body failure."""

    def __init__(self, lock, error, ledger):
        self.lock = lock
        self.error = error
        self.ledger = ledger
        self.armed = False
        self.acquired = 0
        self.released = 0

    def acquire(self, *args, **kwargs):
        result = self.lock.acquire(*args, **kwargs)
        self.acquired += int(bool(result))
        return result

    def release(self):
        self.lock.release()
        self.released += 1
        if self.armed:
            self.armed = False
            self.ledger.append(self.error)
            raise self.error

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()
