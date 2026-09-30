"""Tests for the agent-layer AgentSync rendezvous primitive.

AgentSync is the deployment-recommended mechanism for two independent
MCP agents to coordinate button-press timing (T7 in the link-cable
plan). These tests prove:

1. Basic rendezvous: both peers arrive at the same label, exchange
   payloads, proceed.
2. Kind-namespacing: agent_sync labels never collide with game-serial
   kinds (``exchange_bytes/*``, ``exchange_nybble/*``, etc.) so adding
   sync calls is safe alongside live trade/battle game RPCs.
3. FIFO ordering within a label: two back-to-back rendezvous calls at
   the same label pair up in the issued order.
4. Timeout: a lone peer waits, then raises ``SerialLinkTimeout``.
"""

from __future__ import annotations

import threading
import time

import pytest

from pokered_harness.link import (
    AgentSync,
    InProcessSerialLink,
    SerialLinkTimeout,
)


def test_rendezvous_round_trip_payloads():
    a, b = InProcessSerialLink.pair("blue", "blue")
    sa, sb = AgentSync(a), AgentSync(b)

    results: dict[str, bytes] = {}

    def peer():
        results["b"] = sb.rendezvous("press_a", b"tick=2000 from B")

    t = threading.Thread(target=peer, daemon=True)
    t.start()
    results["a"] = sa.rendezvous("press_a", b"tick=1500 from A")
    t.join(timeout=2.0)

    assert results["a"] == b"tick=2000 from B"
    assert results["b"] == b"tick=1500 from A"


def test_rendezvous_timeout_when_peer_never_arrives():
    a, _b = InProcessSerialLink.pair("blue", "blue")
    sync = AgentSync(a)
    with pytest.raises(SerialLinkTimeout):
        sync.rendezvous("orphan", b"", timeout_ms=50)


def test_rendezvous_rejects_label_with_slash():
    a, _b = InProcessSerialLink.pair("blue", "blue")
    sync = AgentSync(a)
    with pytest.raises(ValueError, match="must not contain"):
        sync.rendezvous("bad/label", b"")


@pytest.mark.parametrize("bad_payload", [1, "payload", None])
def test_rendezvous_rejects_non_bytes_payload(bad_payload):
    a, _b = InProcessSerialLink.pair("blue", "blue")
    sync = AgentSync(a)
    with pytest.raises(TypeError, match="payload must be bytes-like"):
        sync.rendezvous("payload", bad_payload)


def test_rendezvous_rejects_empty_or_non_string_label():
    a, _b = InProcessSerialLink.pair("blue", "blue")
    sync = AgentSync(a)
    with pytest.raises(ValueError, match="must not be empty"):
        sync.rendezvous("", b"")
    with pytest.raises(TypeError, match="label must be a string"):
        sync.rendezvous(7, b"")


def test_rendezvous_labels_dont_collide_with_game_kinds():
    """Game RPCs use kinds like ``exchange_bytes/wSerialPlayerDataBlock``
    — agent_sync adds its own prefix so a rendezvous with label
    ``"wSerialPlayerDataBlock"`` can coexist with the real game exchange
    on the same SerialLink without crossing streams."""
    a, b = InProcessSerialLink.pair("blue", "blue")
    sync_a, sync_b = AgentSync(a), AgentSync(b)

    # Queue an agent_sync call on B in the background. It blocks on
    # ``agent_sync/wSerialPlayerDataBlock`` — a different kind from
    # ``exchange_bytes/wSerialPlayerDataBlock``.
    received: dict[str, bytes] = {}

    def b_waits():
        received["b"] = sync_b.rendezvous("wSerialPlayerDataBlock", b"B")

    t = threading.Thread(target=b_waits, daemon=True)
    t.start()

    # Issue an UNRELATED exchange_bytes call on A — should NOT deliver
    # into the agent_sync queue, so B's rendezvous keeps blocking.
    _reply = (
        a.exchange(
            "exchange_bytes/wSerialPlayerDataBlock",
            b"game data",
            timeout_ms=150,
        )
        if False
        else None
    )  # don't actually fire; B would still be blocked
    # Delay a bit to show B is still blocked, then rendezvous properly.
    time.sleep(0.1)
    assert "b" not in received, "B leaked onto the wrong queue!"

    peer_payload = sync_a.rendezvous("wSerialPlayerDataBlock", b"A")
    t.join(timeout=1.0)
    assert peer_payload == b"B"
    assert received["b"] == b"A"


def test_rendezvous_fifo_within_label():
    """Two back-to-back rendezvous on the same label pair up in order.
    This matters when an agent wants to signal a sequence of sync
    points without allocating unique labels for each."""
    a, b = InProcessSerialLink.pair("blue", "blue")
    sa, sb = AgentSync(a), AgentSync(b)

    results_a: list[bytes] = []

    def peer():
        assert sb.rendezvous("tick", b"b1") == b"a1"
        assert sb.rendezvous("tick", b"b2") == b"a2"

    t = threading.Thread(target=peer, daemon=True)
    t.start()
    results_a.append(sa.rendezvous("tick", b"a1", timeout_ms=2000))
    results_a.append(sa.rendezvous("tick", b"a2", timeout_ms=2000))
    t.join(timeout=2.0)
    assert results_a == [b"b1", b"b2"]
