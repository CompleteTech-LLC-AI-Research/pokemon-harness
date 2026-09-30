"""Shared ROM-free builders for the split emulated-time modules (#141).

``tests/test_emulated_time.py`` was 1481 lines. The builders that more than
one split module needs live here so the split files never duplicate them; the
production contract under test stays in ``pokered_harness.link.emulated_time``.
"""

from __future__ import annotations

import pytest

from pokered_harness.link.emulated_time import (
    CoordinatorClosed,
    EmulatedTimeCoordinator,
)


def coordinator(**overrides):
    options = {
        "epoch": "epoch-1",
        "raw_cpu_clock": 1000,
        "rearm_budget": 32,
        "max_edge_lateness": 128,
    }
    options.update(overrides)
    c = EmulatedTimeCoordinator(**options)
    c.record_peer_progress(epoch=options["epoch"], sequence=1, committed_half_cycles=0)
    return c


def advance(c, raw, cycles, instructions=1):
    permit = c.reserve(cycles)
    assert permit is not None
    assert permit.cpu_cycles == cycles
    return c.commit(permit, raw_cpu_clock=raw + cycles, instructions=instructions)


def exhausted():
    c = coordinator()
    advance(c, 1000, 128)
    assert c.reserve(1) is None
    return c


def assert_terminal(c):
    assert c.snapshot().closed
    with pytest.raises(CoordinatorClosed):
        c.reserve(1)


def delivery_batch(c):
    c.receive_edge(epoch="epoch-1", sequence=1, at_half_cycle=0, payload=b"first")
    c.receive_edge(epoch="epoch-1", sequence=2, at_half_cycle=0, payload=b"second")
    c.advance_watermark(epoch="epoch-1", sequence=2, through_half_cycle=0)
    ready = c.pop_ready_edges()
    assert len(ready) == 2
    assert ready[0].batch_token == ready[1].batch_token
    return ready[0].batch_token
