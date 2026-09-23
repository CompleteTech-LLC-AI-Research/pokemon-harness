"""Shared helpers for the split MCP timed-remote test modules.

Split from ``tests/test_mcp_timed_remote.py`` for #148 with no behavior change;
the constants, context managers, helpers and the ``failure_snapshots`` fixture
are moved verbatim into one module that the split test modules import.
"""

import threading
import time
from contextlib import contextmanager

import pytest

from pokered_harness.mcp_timed_owner import TimedOwner, TimedOwnerPolicy
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text

BOUND = 5.0
PAIR_OWNER_COUNT, PAIR_CALLS_PER_OWNER = 2, 1
PAIR_WORK_CAPACITY_S = PAIR_OWNER_COUNT * PAIR_CALLS_PER_OWNER * (BOUND + 1)
POLICY = {
    "rearm_budget": 32,
    "rearm_instruction_cap": 16,
    "max_edge_lateness": 32,
    "quantum_cycles": 256,
    "operation_timeout": 3.0,
    "max_wait_attempts": 10000,
    "inbound_capacity": 1024,
    "queue_capacity": 2,
    "request_timeout": 5.0,
    "lock_timeout": 1.0,
    "close_timeout": 2.0,
}


@contextmanager
def authored(tmp_path, name, *, close=True):
    from pyboy import PyBoy
    from pyboy.core import cpu, mb, serial

    boot = bytearray(256)
    boot[:6] = bytes([0x31, 0x00, 0xD0, 0xC3, 0xFC, 0x00])
    boot[252:] = bytes([0x3E, 0x01, 0xE0, 0x50])
    cartridge = bytearray(0x8000)
    cartridge[0x100:0x103] = bytes([0xC3, 0x50, 0x01])
    cartridge[0x134:0x13B] = b"ROUTING"
    cartridge[0x150:0x15A] = bytes([0xF3, 0x3E, 0x91, 0xE0, 0x40, 0xAF, 0xE0, 0x02, 0x18, 0xFB])
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 0xFF
    rom, bootstrap = tmp_path / f"{name}.gb", tmp_path / f"{name}.boot"
    rom.write_bytes(cartridge)
    bootstrap.write_bytes(boot)
    game = PyBoy(str(rom), bootrom=str(bootstrap), window="null", sound_emulated=False)
    session = Session(pyboy=game, symbols=load_sym_text(""))
    try:
        assert type(game.mb) is mb.Motherboard
        assert type(game.mb.cpu) is cpu.CPU
        assert type(game.mb.serial) is serial.Serial
        assert game.mb.cpu.retired_instructions == 0
        game.set_emulation_speed(0)
        session.step(1, render=False)
        assert session.current_tick() == game.frame_count == 1
        yield session, game
    finally:
        if close and not session.closed:
            session.close(save=False, timeout_s=BOUND)


@contextmanager
def owned(tmp_path, name="owner", **overrides):
    with authored(tmp_path, name, close=False) as (session, game):
        owner = TimedOwner(session, TimedOwnerPolicy(**(POLICY | overrides)))
        try:
            yield owner, session, game
        finally:
            assert owner.close(timeout=BOUND), "persistent owner did not settle"


def result(request):
    return request.result(timeout=BOUND)


def result_until(request, deadline):
    """Collect a request against one absolute workload deadline."""
    remaining = deadline - time.monotonic()
    return request.result(timeout=max(1e-6, remaining))


@contextmanager
def linked(tmp_path, **overrides):
    with (
        owned(tmp_path, "listener", **overrides) as left,
        owned(tmp_path, "connector", **overrides) as right,
    ):
        listener = left[0].listen("127.0.0.1", 0, "red")
        address = listener.ready.result(timeout=BOUND)
        assert address["state"] == "listening"
        assert 1 <= address["port"] <= 65535
        connector = right[0].connect("127.0.0.1", address["port"], "blue")
        result(connector)
        result(listener)
        yield left, right


def block_owner(owner):
    entered, release = threading.Event(), threading.Event()

    def operation(session):
        entered.set()
        assert release.wait(BOUND), "test barrier was not released"
        return threading.get_ident()

    request = owner.submit(operation)
    assert entered.wait(BOUND)
    return request, release


@pytest.fixture
def failure_snapshots(monkeypatch):
    """Replace accounting only; real owner, binding and cleanup remain in use."""
    from pokered_harness.link.timed_remote import TimedRemoteEndpoint

    endpoints, snapshots, reads = {}, {}, []
    original_attach = TimedRemoteEndpoint.attach
    original_snapshot = TimedRemoteEndpoint.snapshot

    def attach(endpoint, game, *, deadline):
        answer = original_attach(endpoint, game, deadline=deadline)
        endpoints[game] = endpoint
        return answer

    def snapshot(endpoint):
        identity = threading.get_ident()
        reads.append(identity)
        assert identity == endpoint._owner, "foreign native snapshot read"
        if endpoint in snapshots:
            return snapshots[endpoint]
        return original_snapshot(endpoint)

    monkeypatch.setattr(TimedRemoteEndpoint, "attach", attach)
    monkeypatch.setattr(TimedRemoteEndpoint, "snapshot", snapshot)
    return endpoints, snapshots, reads


def late_accounting(epoch, *, cycles=40):
    """Produce failure via a real guard on a separate, deterministic coordinator."""
    from pokered_harness.link.emulated_time import EmulatedTimeCoordinator, EmulatedTimeError

    coordinator = EmulatedTimeCoordinator(
        epoch=epoch,
        raw_cpu_clock=100,
        rearm_budget=32,
        max_edge_lateness=32,
        quantum_cycles=256,
    )
    coordinator.record_peer_progress(epoch=epoch, sequence=1, committed_half_cycles=0)
    permit = coordinator.reserve(cycles)
    assert permit is not None
    coordinator.commit(permit, raw_cpu_clock=100 + cycles, instructions=1)
    with pytest.raises(EmulatedTimeError, match="^edge lateness exceeds bound$"):
        coordinator.receive_edge(epoch=epoch, sequence=1, at_half_cycle=0, payload=b"x")
    return coordinator.snapshot()


def publish_late_accounting(owner, game, failure_snapshots, *, cycles=40):
    endpoints, snapshots, _ = failure_snapshots
    endpoint = endpoints[game]
    snapshot = late_accounting(endpoint.epoch.hex(), cycles=cycles)

    def publish(session):
        snapshots[endpoint] = snapshot

    result(owner.submit(publish))
    return owner.status()


def assert_lateness_failure(status, *, generation, epoch, cycles=40):
    expected = {
        "reason": "edge lateness exceeds bound",
        "terminal_reason": "edge lateness exceeds bound",
        "phase": "receive_edge.lateness",
        "epoch": epoch,
        "local_half_cycles": cycles * 2,
        "peer_half_cycles": 0,
        "raw_cpu_clock": 100 + cycles,
        "observed_raw_cpu_clock": 100 + cycles,
        "edge_sequence": 1,
        "edge_at_half_cycle": 0,
        "measured_lateness_half_cycles": cycles * 2,
        "allowed_lateness_half_cycles": 64,
        "excess_half_cycles": cycles * 2 - 64,
        "emission_complete_half_cycle": -1,
    }
    assert status["failure"] == expected
    assert status["accounting"]["failure"] == expected
    assert status["last_failure"] == {
        "generation": generation,
        "epoch": epoch,
        "failure": expected,
    }
    with pytest.raises(TypeError):
        status["failure"]["reason"] = "overwritten"
    with pytest.raises(TypeError):
        status["last_failure"]["generation"] = -1
    with pytest.raises(TypeError):
        status["last_failure"]["failure"]["excess_half_cycles"] = 0
