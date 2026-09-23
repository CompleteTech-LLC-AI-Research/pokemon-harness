"""Shared wire constants and fake cores for the network-backend tests (#128).

Split from ``tests/test_network_backend.py`` for #128 with no behavior
change. Every constant and class below is copied verbatim from the original
module.
"""

from __future__ import annotations

_OP_EDGE_REQ = 0x10


_OP_EDGE_RESP = 0x11


_OP_SYNC = 0x20


_OP_EXCHANGE = 0x30


class _CompletingSlaveCore:
    def __init__(self) -> None:
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.transfer_enabled = 0
        self.SB = peer_bit & 1
        return True
