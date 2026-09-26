"""Shared helpers for the split timed-ROM-pair probe test modules.

Split from ``tests/test_probe_timed_rom_pair.py`` for #139 with no behavior
change; the ``arguments`` helper, the ``FakeSession``/``FakeEndpoint``/``Harness``
doubles, the spawn/startup seam helpers and the menu-owner helpers are moved
verbatim into one module that the split test modules import. The
``spawned_probe_args`` fixture is re-exported through ``tests/conftest.py``.
"""

import json
import multiprocessing
import os
import signal
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_timed_rom_pair as probe


def arguments(*extra):
    return probe.parse_args(
        [
            "--operation-timeout",
            "1",
            "--rearm-budget",
            "32",
            "--rearm-instruction-cap",
            "16",
            "--max-edge-lateness",
            "32",
            "--output",
            "/tmp/unused-probe-unit-output.json",
            *extra,
        ]
    )


class FakeSession:
    """Model ownership and partial advancement without claiming native evidence."""

    def __init__(self, harness, family):
        self.harness = harness
        self.family = family
        self.owner = threading.get_ident()
        self.calls = []
        self.endpoint = None
        self.closed = False
        self._pyboy = SimpleNamespace(
            frame_count=10,
            mb=SimpleNamespace(
                cpu=SimpleNamespace(cycles=100, retired_instructions=20),
                serial=SimpleNamespace(SB=0, SC=0, bits_remaining=0),
                lcd=SimpleNamespace(LY=7, clock=32),
                double_speed=False,
                execution_governor=None,
            ),
        )

    def record(self, name):
        assert threading.get_ident() == self.owner
        self.calls.append(name)

    def load_state(self, data):
        self.record("load")
        assert data == b"authored fixture placeholder"
        assert self.endpoint is None

    def bind_timed_execution(self, endpoint, **kwargs):
        self.record("bind")
        assert endpoint.pyboy is self._pyboy
        self.endpoint = endpoint

    def unbind_timed_execution(self, endpoint, **kwargs):
        self.record("unbind")
        assert endpoint is self.endpoint
        endpoint.close()
        self.endpoint = None

    def current_tick(self):
        return self._pyboy.frame_count

    @contextmanager
    def locked(self, **kwargs):
        self.record("locked")
        yield

    def step(self, count, *, render):
        self.record("step")
        assert render is False
        assert self.endpoint is not None
        self.harness.in_step.wait(timeout=2)
        behavior = self.harness.behavior[self.family]
        if behavior == "no_progress":
            return
        if behavior == "complete":
            self._pyboy.frame_count += count
            self._pyboy.mb.cpu.cycles += 100 * count
            return
        if behavior == "fail":
            self._pyboy.frame_count += 1
            self._pyboy.mb.cpu.cycles += 60
            raise RuntimeError("injected partial public call")
        assert self.endpoint.cancel_event.wait(2), "supervisor did not cancel"
        self._pyboy.frame_count += 1
        raise RuntimeError("interrupted public call")

    def close(self, *, save=False, **kwargs):
        self.record("close")
        assert save is False
        assert self.endpoint is None, "must not worker-stop a bound governor"
        self.closed = True


class FakeEndpoint:
    def __init__(self, harness, sock, **kwargs):
        self.harness = harness
        self.sock = sock
        self.family = kwargs["rom_version"]
        self.owner = threading.get_ident()
        self.cancel_event = kwargs["cancel_event"]
        self.cancel_threads = []
        self.pyboy = None
        self.closed = False
        self.metadata = {"family": self.family}
        assert kwargs["quantum_cycles"] == 256
        assert kwargs["operation_timeout"] == 1

    def attach(self, pyboy, *, deadline):
        assert threading.get_ident() == self.owner
        self.pyboy = pyboy

    def snapshot(self):
        assert threading.get_ident() == self.owner
        return {"local_half_cycles": 2 * self.pyboy.mb.cpu.cycles}

    def cancel(self):
        self.cancel_threads.append(threading.get_ident())
        self.cancel_event.set()

    def close(self):
        assert threading.get_ident() == self.owner
        assert self.cancel_event.is_set()
        if self.family == self.harness.close_failure:
            raise RuntimeError("injected detach failure")
        self.closed = True
        self.sock.close()


class Harness:
    def __init__(self, blue="complete", yellow="wait", *, close_failure=None):
        self.behavior = {"blue": blue, "yellow": yellow}
        self.close_failure = close_failure
        self.in_step = threading.Barrier(2)
        self.sessions = {}
        self.endpoints = {}

    def assets(self, version, repo_root):
        family = version.split("_")[0]
        return {
            "rom": family,
            "sym": "unused",
            "state": b"authored fixture placeholder",
            "family": family,
            "pins": {},
            "provenance": {"synthetic": True},
        }

    def session(self, rom, sym, **kwargs):
        session = FakeSession(self, rom)
        self.sessions[rom] = session
        return session

    def endpoint(self, sock, **kwargs):
        endpoint = FakeEndpoint(self, sock, **kwargs)
        self.endpoints[endpoint.family] = endpoint
        return endpoint

    def run(self, *extra, endpoint_factory=None):
        return probe.run_pair(
            arguments("--frame-limit", "1", "--pair-timeout", "0.5", *extra),
            session_factory=self.session,
            endpoint_factory=endpoint_factory or self.endpoint,
            asset_resolver=self.assets,
        )


def _spawn_diagnostic_owner(
    args_dict,
    index,
    sock,
    cancel_event,
    done_event,
    barrier,
    deadline,
    overall,
    report_sender,
    stderr_path,
):
    """Serializable synthetic owner; never loads assets or claims native progress."""
    scenario = args_dict["test_scenario"]
    record = {
        "side": ("listener", "connector")[index],
        "calls": [],
        "cleanup": [],
        "errors": [],
        "termination": "cancelled_or_deadline",
        "test_pid": os.getpid(),
        "test_executable": sys.executable,
        "test_repo_root": args_dict["repo_root"],
        "test_input_profile": args_dict["input_profile"],
        "test_tcp_nodelay": sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY),
    }
    try:
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(bytes([index]))
        assert sock.recv(1) == bytes([1 - index])
        if scenario in ("ignore_cancel", "partial_report"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            Path(stderr_path).with_suffix(".checkpoint.json").write_text(
                json.dumps({"synthetic_checkpoint": True, "phase": "before_unresponsive_wait"})
            )
        barrier.wait(timeout=max(0.001, deadline - time.monotonic()))
        if scenario in ("ignore_cancel", "partial_report"):
            if scenario == "partial_report":
                os.write(report_sender.fileno(), struct.pack("!i", 1024) + b"{")
                # A dead/stuck child may own a shared condition indefinitely.
                abandoned_event = multiprocessing.get_context("spawn").Event()
                abandoned_event._cond.acquire()
            # Parent must kill this process; test fixture also reaps on assertion failure.
            time.sleep(30)
            return
        if index == 0:
            record["errors"].append("injected failure before endpoint publication")
            record["termination"] = "owner_failure"
            done_event.set()
        record["test_cancel_seen"] = cancel_event.wait(max(0, overall - time.monotonic()))
        assert record["test_cancel_seen"]
        record["cleanup"].append("synthetic_socket_closed")
        if index == 0 and scenario == "invalid_json":
            report_sender.send_bytes(b"not JSON")
        elif index == 0 and scenario == "oversize":
            report_sender.send_bytes(b"x" * (probe.MAX_REPORT_BYTES + 1))
        else:
            report_sender.send_bytes(json.dumps(record).encode())
    finally:
        sock.close()
        report_sender.close()


@pytest.fixture
def spawned_probe_args(tmp_path):
    """Retain a final emergency reap so a failed assertion cannot leak test children."""
    before = {child.pid for child in multiprocessing.active_children()}
    args = arguments(
        "--owner-mode",
        "process",
        "--repo-root",
        str(tmp_path),
        "--pair-timeout",
        "3",
        "--cleanup-timeout",
        "1",
        "--overall-timeout",
        "8",
    )
    try:
        yield args
    finally:
        for child in multiprocessing.active_children():
            if child.pid not in before:
                child.kill()
                child.join(2)


class _NoSharedEventContext:
    """Fail immediately if the supervisor tries to use child-lockable Events."""

    def __init__(self, *, fail_second_start=False, block_before_owner=False):
        self.context = multiprocessing.get_context("spawn")
        self.fail_second_start = fail_second_start
        self.block_before_owner = block_before_owner
        self.process_count = 0
        self.startup = {}
        self.connections = []
        if fail_second_start:
            self.ready_receiver, self.ready_sender = self.context.Pipe(duplex=False)
            self.connections.extend((self.ready_receiver, self.ready_sender))
            if block_before_owner:
                self.gate_receiver, self.gate_sender = self.context.Pipe(duplex=False)
                self.connections.extend((self.gate_receiver, self.gate_sender))

    def close(self):
        for connection in self.connections:
            connection.close()

    def __getattr__(self, name):
        return getattr(self.context, name)

    def Event(self):
        pytest.fail("supervisor must not depend on multiprocessing.Event locks")

    def Process(self, **kwargs):
        self.process_count += 1
        if self.fail_second_start and self.process_count == 2:
            deadline, overall = kwargs["args"][6:8]
            return _FailedStartProcess(self, min(deadline, overall))
        if self.fail_second_start:
            if self.block_before_owner:
                kwargs["args"] = (kwargs["args"], self.ready_sender, self.gate_receiver)
                kwargs["target"] = _spawn_block_before_owner
            else:
                kwargs["kwargs"] = {"ready_sender": self.ready_sender}
        return self.context.Process(**kwargs)


class _FailedStartProcess:
    def __init__(self, context, deadline):
        self.context = context
        self.deadline = deadline

    def start(self):
        self.context.startup["pair_deadline"] = self.deadline
        if not self.context.ready_receiver.poll(max(0, self.deadline - time.monotonic())):
            raise TimeoutError("first owner readiness missed existing pair deadline")
        self.context.startup.update(json.loads(self.context.ready_receiver.recv_bytes(4096)))
        self.context.startup["injected_monotonic"] = time.monotonic()
        if self.context.startup["injected_monotonic"] >= self.deadline:
            raise TimeoutError("first owner readiness arrived after existing pair deadline")
        raise OSError("injected second owner start failure")


def _spawn_block_before_owner(owner_args, ready_sender, gate_receiver):
    """Hold a real spawned child before owner entry until termination or overall bound."""
    ready_sender.send_bytes(
        json.dumps(
            {
                "phase": "before_owner_entry",
                "ready_monotonic": time.monotonic(),
                "pid": os.getpid(),
            }
        ).encode()
    )
    ready_sender.close()
    # The parent retains the write end but never releases this gate. No sleep,
    # shared Event, owner report, or cooperative cancellation is involved.
    if gate_receiver.poll(max(0, owner_args[7] - time.monotonic())):
        raise AssertionError("pre-owner startup gate unexpectedly released")
    gate_receiver.close()


def _spawn_wait_for_startup_cancel(
    args_dict,
    index,
    sock,
    cancel_event,
    done_event,
    barrier,
    deadline,
    overall,
    report_sender,
    stderr_path,
    *,
    ready_sender,
):
    """Owner starts without a peer, waits for supervisor cancellation, then reports."""
    ready = time.monotonic()
    ready_sender.send_bytes(
        json.dumps(
            {
                "phase": "cancellation_wait",
                "ready_monotonic": ready,
                "pid": os.getpid(),
            }
        ).encode()
    )
    ready_sender.close()
    observed = cancel_event.wait(max(0, overall - time.monotonic()))
    record = {
        "side": "listener",
        "calls": [],
        "cleanup": [],
        "errors": [],
        "termination": "cancelled_or_deadline",
        "test_cancel_seen": observed,
        "test_ready_monotonic": ready,
        "test_cancel_seen_monotonic": time.monotonic(),
    }
    report_sender.send_bytes(json.dumps(record).encode())
    report_sender.close()
    sock.close()


def _print_startup_evidence(context, args, result, started, returned):
    owner = result["owners"][0]
    print(
        json.dumps(
            {
                **context.startup,
                "started": started,
                "cancelled_monotonic": result["cancelled_monotonic"],
                "cancel_seen_monotonic": owner.get("test_cancel_seen_monotonic"),
                "returned": returned,
                "elapsed_s": returned - started,
                "cleanup_elapsed_s": returned - result["cancelled_monotonic"],
                "pair_timeout": args.pair_timeout,
                "cleanup_timeout": args.cleanup_timeout,
                "overall_timeout": args.overall_timeout,
                "exitcode": owner["exitcode"],
                "forced_termination": owner["forced_termination"],
                "cancel_seen": owner.get("test_cancel_seen"),
                "live_process_count": len(result["processes_alive"]),
                "live_reader_count": len(result["report_readers_alive"]),
            },
            sort_keys=True,
        )
    )


class _ReadOnlyProbeMemory:
    def __getitem__(self, address):
        mapped_io = {
            0xFFFF: 0x01,  # IE
            0xFF0F: 0x00,  # IF
            0xFF00: 0xCF,  # JOYP
            0xFF44: 0x07,  # LY
            0xFF41: 0x80,  # STAT
            0xFF40: 0x91,  # LCDC
        }
        if address in mapped_io:
            return mapped_io[address]
        return address & 255

    def __setitem__(self, address, value):
        pytest.fail("menu diagnostic attempted a memory write")


class _MenuSession(FakeSession):
    def __init__(self, harness, family, behavior):
        super().__init__(harness, family)
        self.behavior = behavior
        self.inputs = []
        self.steps = []
        self._pyboy.memory = _ReadOnlyProbeMemory()
        self._pyboy.register_file = SimpleNamespace(
            A=1, F=0xB0, B=0, C=0x13, D=0, E=0xD8, HL=0xC123, SP=0xDFFE, PC=0x1234
        )
        input_addresses = {
            name: 0xFF80 + index
            for index, name in enumerate(
                (
                    "hLoadedROMBank",
                    "hJoyInput",
                    "hJoyPressed",
                    "hJoyHeld",
                    "hJoyLast",
                    "wJoyIgnore",
                    "wStatusFlags5",
                    "wWalkCounter",
                    "hVBlankOccurred",
                    "hSerialReceivedNewData",
                    "hSerialSendData",
                    "hSerialReceiveData",
                )
            )
        }

        def address(name):
            if behavior == "missing_symbol" or (
                behavior == "observation_failure" and self._pyboy.frame_count > 10
            ):
                raise KeyError(name)
            return input_addresses.get(name, 0xC000 + len(name))

        self.symbols = SimpleNamespace(addr_of=address)

    def press(self, button, *, duration=1):
        self.record("press")
        assert self.endpoint is not None
        self.inputs.append(
            (self._pyboy.frame_count - 10, button, duration, self._pyboy.mb.cpu.cycles)
        )
        if self.behavior == "press_failure":
            raise RuntimeError("injected input admission failure")

    def step(self, count, *, render):
        self.record("step")
        self.steps.append(count)
        assert render is False
        if self.behavior == "zero":
            return
        if self.behavior == "interrupt_before_frame":
            self._pyboy.mb.cpu.cycles += 4
            raise RuntimeError("interrupted before complete frame")
        advance = 2 if self.behavior == "overadvance" else count
        self._pyboy.frame_count += advance
        self._pyboy.mb.cpu.cycles += 100 * advance
        if self.behavior == "interrupt_after_frame":
            raise RuntimeError("interrupted after actual frame")


def _run_menu_owner(*extra, behavior="complete", profile="menu", checkpoint=None):
    """One synthetic owner runs production scheduling without a peer CPU or ROM."""
    from tests.test_timed_menu_frame_bound import FakeClock

    clock = FakeClock(step=0.0)
    clock_deadline_base = clock()
    args = arguments(
        "--input-profile",
        profile,
        "--listener-chunk",
        "1",
        "--connector-chunk",
        "1",
        "--frame-limit",
        "77",
        *extra,
    )
    harness = Harness()
    session = _MenuSession(harness, "blue", behavior)
    cancelled = threading.Event()

    class Done:
        def set(self):
            cancelled.set()

    records = [{"side": "listener", "calls": [], "cleanup": [], "errors": []}, {}]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as connector:
            accepted, _ = listener.accept()
            try:
                probe._run_owner(
                    0,
                    args,
                    records,
                    [accepted, connector],
                    cancelled,
                    Done(),
                    threading.Barrier(1),
                    [None, None],
                    threading.Lock(),
                    # The rows using this helper assert exact step counts, exact
                    # menu-schedule offsets and exact terminal semantics. Against
                    # the real clock a 3-second deadline makes those assertions a
                    # measurement of host speed instead of the frame bound (#252).
                    # `#255` added the `clock` seam for precisely this reason; the
                    # milestone rows use it and these two helpers still do not.
                    # A step of 0 models an arbitrarily fast host, so the owner
                    # terminates on the frame limit and the exact counts hold on
                    # any machine. The deadline branch is still exercised
                    # separately by tests/test_timed_menu_frame_bound.py.
                    clock_deadline_base + 3,
                    clock_deadline_base + 5,
                    lambda *args, **kwargs: session,
                    harness.endpoint,
                    harness.assets,
                    checkpoint,
                    clock=clock,
                )
            finally:
                accepted.close()
    return session, records[0]
