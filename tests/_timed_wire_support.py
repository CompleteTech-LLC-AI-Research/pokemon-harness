"""Shared wire constants, socket helpers and channel fixtures for the timed-wire tests (#151).

Split from ``tests/test_timed_wire.py`` for #151 with no behavior
change. Every constant, helper, class and fixture below is copied
verbatim from the original module.
"""

from __future__ import annotations

import importlib.util
import queue
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src/pokered_harness/link/timed_wire.py"
SPEC = importlib.util.spec_from_file_location("_tested_timed_wire", SOURCE)
wire = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wire
SPEC.loader.exec_module(wire)
EPOCH = bytes(range(16))
HEADER = struct.Struct(">4sBBH16sQ")
BOUND = 2.0


def deadline():
    return time.monotonic() + BOUND


class Job:
    """Capture worker failures and bound every join, including failed tests."""

    def __init__(self, call):
        self.results = queue.Queue()

        def run():
            try:
                self.results.put((True, call()))
            except BaseException as exc:  # noqa: BLE001
                # Propagate pytest outcomes and worker failures to the test thread.
                self.results.put((False, exc))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(BOUND + 1)
        assert not self.thread.is_alive(), "wire worker exceeded outer guard"
        ok, value = self.results.get_nowait()
        if not ok:
            raise value
        return value


@contextmanager
def sockets(kind):
    if kind == "socketpair":
        left, right = socket.socketpair()
    else:
        with socket.socket() as listener:
            listener.settimeout(BOUND)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            left = socket.create_connection(listener.getsockname(), timeout=BOUND)
            right, _ = listener.accept()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


@pytest.fixture(params=["socketpair", "tcp"])
def kind(request):
    return request.param


@contextmanager
def channels(kind, **options):
    with sockets(kind) as (a, b):
        left = wire.TimedWireChannel(a, epoch=EPOCH, **options)
        right = wire.TimedWireChannel(b, epoch=EPOCH, **options)
        try:
            first = Job(lambda: left.handshake(deadline=deadline()))
            right.handshake(deadline=deadline())
            first.result()
            yield left, right
        finally:
            left.close()
            right.close()


def exchange(left, right, message):
    sent = left.send(message, deadline=deadline())
    got = right.receive(deadline=deadline())
    assert sent is None
    assert got == message
    return left._outgoing.sequence


class FragmentSocket:
    """Force partial I/O while retaining real socket readiness and EOF."""

    def __init__(self, sock):
        self.sock = sock
        self.read_entered = threading.Event()
        self.write_entered = threading.Event()

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def recv(self, size, *args):
        self.read_entered.set()
        return self.sock.recv(min(size, 1), *args)

    def send(self, data, *args):
        self.write_entered.set()
        return self.sock.send(data[:1], *args)


def wait_state(channel, predicate):
    with channel._condition:
        assert channel._condition.wait_for(predicate, BOUND), "reader did not reach barrier"


class ReturnGateSocket(FragmentSocket):
    """Put bytes on the wire, then hold the writer before send returns."""

    def __init__(self, sock):
        super().__init__(sock)
        self.armed = False
        self.delivered = threading.Event()
        self.release = threading.Event()

    def send(self, data, *args):
        count = self.sock.send(data, *args)
        if self.armed:
            self.delivered.set()
            assert self.release.wait(BOUND), "test did not release writer"
        return count

    def shutdown(self, how):
        self.release.set()
        return self.sock.shutdown(how)


@contextmanager
def gated_channels(kind, **options):
    with sockets(kind) as (a, b):
        gate = ReturnGateSocket(a)
        left, right = (
            wire.TimedWireChannel(gate, epoch=EPOCH, **options),
            wire.TimedWireChannel(b, epoch=EPOCH, **options),
        )
        try:
            hello = Job(lambda: left.handshake(deadline=deadline()))
            right.handshake(deadline=deadline())
            hello.result()
            gate.armed = True
            yield left, right, gate
        finally:
            gate.release.set()
            left.close()
            right.close()
