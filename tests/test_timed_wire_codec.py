"""Frame/edge codec contracts and handshake validation (#151).

Split from ``tests/test_timed_wire.py`` for #151 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import socket
import threading
from types import SimpleNamespace

import pytest

from tests._timed_wire_support import (
    BOUND,
    EPOCH,
    HEADER,
    Job,
    channels,
    deadline,
    exchange,
    sockets,
    wire,
)


@pytest.mark.parametrize(
    "message",
    [
        wire.Hello(),
        wire.Progress(0),
        wire.Progress(2**64 - 1),
        wire.EdgeRequest(1, 0, 0, 0),
        wire.EdgeRequest(2**64 - 1, 2**64 - 1, 2**64 - 1, 1),
        wire.EmissionComplete(0, 0),
        wire.EdgeResponse(1, 0, 1),
    ],
)
def test_codec_roundtrip(message):
    frame = wire.Frame(EPOCH, 1, message)
    assert wire.decode_frame(wire.encode_frame(frame)) == frame


@pytest.mark.parametrize("value", [-1, 2**64, True, False, 1.5, "1", None])
@pytest.mark.parametrize(
    "factory",
    [
        lambda n: wire.Frame(EPOCH, n, wire.Hello()),
        lambda n: wire.Frame(EPOCH, 1, wire.Progress(n)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeRequest(n, 0, 0, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeRequest(1, n, 0, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeRequest(1, 0, n, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EmissionComplete(n, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EmissionComplete(0, n)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeResponse(n, 0, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeResponse(1, n, 0)),
    ],
)
def test_codec_strict_uint64(factory, value):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(factory(value))


@pytest.mark.parametrize("bit", [-1, 2, True, False, 0.0, "0"])
@pytest.mark.parametrize("factory", [wire.EdgeRequest, wire.EdgeResponse])
def test_codec_strict_bit(factory, bit):
    with pytest.raises(wire.ProtocolError):
        message = factory(1, 0, 0, bit) if factory is wire.EdgeRequest else factory(1, 0, bit)
        wire.encode_frame(wire.Frame(EPOCH, 1, message))


@pytest.mark.parametrize("epoch", [b"", b"x" * 15, b"x" * 17, "x" * 16])
def test_codec_epoch_length_and_type(epoch):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(epoch, 1, wire.Hello()))


@pytest.mark.parametrize("field,value", [(0, b"NOPE"), (1, 1), (1, 3), (2, 255), (3, 65535)])
def test_codec_rejects_unknown_or_malformed_header(field, value):
    encoded = wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello()))
    fields = list(HEADER.unpack(encoded[: HEADER.size]))
    fields[field] = value
    with pytest.raises(wire.ProtocolError):
        wire.decode_frame(HEADER.pack(*fields) + encoded[HEADER.size :])


def test_codec_requires_exact_frame_length():
    encoded = wire.encode_frame(wire.Frame(EPOCH, 1, wire.Progress(0)))
    for bad in (encoded[:-1], encoded + b"x", encoded[: HEADER.size - 1]):
        with pytest.raises(wire.ProtocolError):
            wire.decode_frame(bad)


def test_handshake_idempotent_and_application_delivery(kind):
    with channels(kind) as (left, right):
        left.handshake(deadline=deadline())
        right.handshake(deadline=deadline())
        exchange(left, right, wire.Progress(0))
        exchange(right, left, wire.Progress(0))
        assert not left.closed and not right.closed


def test_send_requires_handshake(kind):
    with sockets(kind) as (a, _):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            with pytest.raises(wire.WireError):
                channel.send(wire.Progress(0), deadline=deadline())
        finally:
            channel.close()


@pytest.mark.parametrize("peer_epoch,caps", [(b"z" * 16, 0), (EPOCH, 1)])
def test_handshake_rejects_epoch_and_capabilities(kind, peer_epoch, caps):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            encoded = wire.encode_frame(wire.Frame(peer_epoch, 1, wire.Hello()))
            if caps:
                encoded = encoded[:-1] + bytes([caps])
            b.sendall(encoded)
            with pytest.raises(wire.ProtocolError) as caught:
                channel.handshake(deadline=deadline())
            assert channel.closed
            assert caught.value is channel.error
        finally:
            channel.close()


@pytest.mark.parametrize("stage", ["select", "send"])
@pytest.mark.parametrize("reason", ["epoch", "capabilities", "eof", "close"])
@pytest.mark.parametrize("revision", [2, 3])
def test_handshake_writer_preserves_terminal_reason(kind, stage, reason, revision, monkeypatch):
    """Close after the writer's open check, before its real socket operation."""
    entered = threading.Event()
    release = threading.Event()

    def pause_writer():
        entered.set()
        assert release.wait(BOUND), "test did not release handshake writer"

    with sockets(kind) as (a, b):

        class SendGateSocket:
            def __getattr__(self, name):
                return getattr(a, name)

            def send(self, data):
                if stage == "send":
                    pause_writer()
                return a.send(data)

        original_select = wire.select.select

        def gated_select(readable, writable, exceptional, timeout):
            if writable and stage == "select":
                pause_writer()
            return original_select(readable, writable, exceptional, timeout)

        # Patch only this leaf module; reader readiness still uses real select.
        monkeypatch.setattr(wire, "select", SimpleNamespace(select=gated_select))
        channel = wire.TimedWireChannel(SendGateSocket(), epoch=EPOCH, revision=revision)
        writer = Job(lambda: channel.handshake(deadline=deadline()))
        try:
            assert entered.wait(BOUND), "handshake did not reach socket barrier"
            if reason == "close":
                channel.close()
            elif reason == "eof":
                b.shutdown(socket.SHUT_WR)
            else:
                peer_epoch = b"z" * 16 if reason == "epoch" else EPOCH
                valid_caps = 1 if revision == 3 else 0
                encoded = wire.encode_frame(
                    wire.Frame(peer_epoch, 1, wire.Hello(valid_caps), revision)
                )
                if reason == "capabilities":
                    encoded = encoded[:-1] + bytes([1 - valid_caps])
                b.sendall(encoded)
            # Joining proves _terminate has closed the fd, not just set _closed.
            channel._reader.join(BOUND)
            assert not channel._reader.is_alive(), "reader did not terminate"
            assert channel.closed and a.fileno() == -1
            stored = channel.error
            expected = (
                wire.ProtocolError if reason in ("epoch", "capabilities") else wire.ChannelClosed
            )
            assert isinstance(stored, expected)
            release.set()
            with pytest.raises(expected) as caught:
                writer.result()
            assert caught.value is stored, "socket failure masked the stored terminal reason"
            assert channel.error is stored
        finally:
            release.set()
            channel.close()
            writer.thread.join(BOUND + 1)
            assert not writer.thread.is_alive(), "handshake writer leaked"
