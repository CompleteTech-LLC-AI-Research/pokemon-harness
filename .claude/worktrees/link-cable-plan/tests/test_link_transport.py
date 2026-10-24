"""Tests for the LinkTransport byte-queue data structure."""

from __future__ import annotations

import pytest

from pokered_harness.link import LinkTransport


def test_push_pull_a_to_b_roundtrip():
    t = LinkTransport()
    t.push_a_to_b(0x42)
    t.push_a_to_b(0x43)
    assert t.pull_for_b() == 0x42
    assert t.pull_for_b() == 0x43
    assert t.pull_for_b() == 0x00  # default when empty


def test_push_pull_b_to_a_roundtrip():
    t = LinkTransport()
    t.push_b_to_a(0xAA)
    t.push_b_to_a(0xBB)
    assert t.pull_for_a() == 0xAA
    assert t.pull_for_a() == 0xBB
    assert t.pull_for_a(default=0xFF) == 0xFF


def test_queues_are_independent():
    t = LinkTransport()
    t.push_a_to_b(0x11)
    # Pulling from A's inbox (B→A) should not consume the A→B byte.
    assert t.pull_for_a() == 0x00
    assert t.pull_for_b() == 0x11


def test_exchange_on_empty_queues():
    t = LinkTransport()
    # Both sides present a byte simultaneously with nothing queued: each side
    # receives the byte the other just sent.
    to_a, to_b = t.exchange(0x01, 0x02)
    assert (to_a, to_b) == (0x02, 0x01)
    assert t.pending_a_to_b == 0
    assert t.pending_b_to_a == 0


def test_exchange_preserves_fifo_with_backlog():
    t = LinkTransport()
    t.push_a_to_b(0x10)  # queued for B first
    t.push_b_to_a(0x20)  # queued for A first
    to_a, to_b = t.exchange(0x11, 0x21)
    # A's oldest inbound is 0x20; B's oldest inbound is 0x10.
    assert to_a == 0x20
    assert to_b == 0x10
    # The just-sent bytes remain queued behind what was already there.
    assert t.pending_a_to_b == 1
    assert t.pending_b_to_a == 1
    assert t.pull_for_b() == 0x11
    assert t.pull_for_a() == 0x21


def test_reset_clears_both_queues():
    t = LinkTransport()
    t.push_a_to_b(0x01)
    t.push_b_to_a(0x02)
    t.reset()
    assert t.pending_a_to_b == 0
    assert t.pending_b_to_a == 0
    assert t.pull_for_a() == 0x00
    assert t.pull_for_b() == 0x00


def test_pending_reflects_depth():
    t = LinkTransport()
    assert t.pending_a_to_b == 0
    t.push_a_to_b(1)
    t.push_a_to_b(2)
    t.push_a_to_b(3)
    assert t.pending_a_to_b == 3
    t.pull_for_b()
    assert t.pending_a_to_b == 2


@pytest.mark.parametrize("bad", [-1, 256, 1000])
def test_push_rejects_out_of_range(bad):
    t = LinkTransport()
    with pytest.raises(ValueError):
        t.push_a_to_b(bad)
    with pytest.raises(ValueError):
        t.push_b_to_a(bad)


@pytest.mark.parametrize("bad", ["x", 1.5, None, b"\x00"])
def test_push_rejects_non_int(bad):
    t = LinkTransport()
    with pytest.raises(ValueError):
        t.push_a_to_b(bad)


def test_pull_default_validated():
    t = LinkTransport()
    with pytest.raises(ValueError):
        t.pull_for_a(default=-1)
    with pytest.raises(ValueError):
        t.pull_for_b(default=256)


def test_snapshot_is_independent_copy():
    t = LinkTransport()
    t.push_a_to_b(0x10)
    t.push_b_to_a(0x20)
    snap = t.snapshot()
    assert snap == {"a_to_b": [0x10], "b_to_a": [0x20]}
    snap["a_to_b"].append(0xFF)
    snap["b_to_a"].clear()
    # Internal state unaffected by mutations on the returned dict/lists.
    assert t.pending_a_to_b == 1
    assert t.pending_b_to_a == 1
    assert t.snapshot() == {"a_to_b": [0x10], "b_to_a": [0x20]}
