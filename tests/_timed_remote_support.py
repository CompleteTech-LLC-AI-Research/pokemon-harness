"""Shared fixtures and helpers for the split timed-remote tests (#162).

Split out of ``tests/test_timed_remote.py`` for #162 with no behavior change.
The constants, the module-scoped ``remote`` fixture, and every helper below are
copied verbatim from the original module.
"""

import hashlib
import importlib
import importlib.machinery
import importlib.util
import queue
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

PRELUDE = struct.Struct(">4sBBBB16sI")
HEADER = struct.Struct(">4sBBH16sQ")
DOMAIN = b"pokered-timed-epoch-v3\0"
ROM = {"red": 1, "blue": 2, "yellow": 3}
SIDE = {"listener": 1, "connector": 2}
BOUND = 3.0
TIMING = {"rearm_budget": 32, "rearm_instruction_cap": 16, "max_edge_lateness": 32}


@pytest.fixture(scope="module")
def remote():
    """Load relative leaf modules without importing the runtime-facing package."""
    name = "_timed_remote_contract"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "src/pokered_harness/link")]
    sys.modules[name] = package
    try:
        yield importlib.import_module(f"{name}.timed_remote")
    finally:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                del sys.modules[key]


class Job:
    """Bounded worker with exceptions returned to the asserting thread."""

    def __init__(self, call):
        self.results = queue.Queue()

        def run():
            try:
                self.results.put((True, call()))
            except BaseException as exc:  # noqa: BLE001 - propagate all worker failures
                self.results.put((False, exc))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(BOUND + 1)
        assert not self.thread.is_alive(), "remote worker exceeded outer bound"
        ok, result = self.results.get_nowait()
        if not ok:
            raise result
        return result


def record(rom_name="blue", side_name="connector", nonce=bytes(16), **changes):
    fields = {
        "magic": b"PKTH",
        "prelude": 1,
        "wire": 3,
        "rom": ROM[rom_name],
        "side": SIDE[side_name],
        "nonce": nonce,
        "caps": 1,
    }
    fields.update(changes)
    return PRELUDE.pack(*fields.values())


def epoch(listener, connector):
    return hashlib.sha256(DOMAIN + listener + connector).digest()[:16]


def hello(session_epoch):
    return HEADER.pack(b"PKTW", 3, 1, 4, session_epoch, 1) + struct.pack(">I", 1)


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        assert part, f"unexpected EOF after {len(data)}/{size} bytes"
        data.extend(part)
    return bytes(data)


@contextmanager
def sockets(kind="socketpair"):
    if kind == "socketpair":
        left, right = socket.socketpair()
    else:
        family = socket.AF_INET6 if kind == "tcp6" else socket.AF_INET
        host = "::1" if kind == "tcp6" else "127.0.0.1"
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            listener.settimeout(BOUND)
            listener.bind((host, 0))
            listener.listen(1)
            left = socket.socket(family, socket.SOCK_STREAM)
            left.settimeout(BOUND)
            left.connect(listener.getsockname())
            right, _ = listener.accept()
    left.settimeout(BOUND)
    right.settimeout(BOUND)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def start(remote, sock, *, side="listener", rom="red", deadline=None, **options):
    return remote.TimedRemoteEndpoint.from_connected_socket(
        sock,
        side=side,
        rom_version=rom,
        deadline=time.monotonic() + BOUND if deadline is None else deadline,
        **(TIMING | options),
    )


@contextmanager
def endpoints(remote, kind="socketpair", listener_rom="red", connector_rom="blue"):
    with sockets(kind) as (left, right):
        deadline = time.monotonic() + BOUND
        worker = Job(lambda: start(remote, left, rom=listener_rom, deadline=deadline))
        a = b = None
        try:
            b = start(remote, right, side="connector", rom=connector_rom, deadline=deadline)
            a = worker.result()
            yield a, b
        finally:
            if a is not None:
                a.close()
            if b is not None:
                b.close()
            left.close()
            right.close()
            worker.thread.join(BOUND + 1)
            assert not worker.thread.is_alive()
