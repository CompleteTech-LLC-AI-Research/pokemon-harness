"""J1-N actual receive error boundaries. Source-authored controls, ALL NOTRUN."""

import array
import json
import socket
from types import SimpleNamespace

import pytest

from pokered_harness.link import listener_handoff_custody as custody
from pokered_harness.link import network_listener_handoff as handoff


class Lock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True

    def __exit__(self, *args):
        self.held = False


@pytest.fixture
def controlled(monkeypatch):
    c = SimpleNamespace(
        now=10.0,
        calls=[],
        sockets=[],
        wrappers=[],
        failure=None,
        stage=None,
        secondary=None,
        secondary_stage=None,
        advance=None,
        short=None,
        frame=None,
        hook=None,
    )
    book = custody.ReceiveBook()
    book.lock = Lock()

    def effect(stage, *args):
        assert not book.lock.held, stage
        c.calls.append((stage, *args))
        if c.hook:
            c.hook(stage)
        if c.advance == stage:
            c.now = 20.0
        if c.stage == stage:
            raise c.failure
        if c.secondary_stage == stage:
            raise c.secondary

    def clock():
        effect("clock")
        return c.now

    class Connection:
        family, type, proto = socket.AF_UNIX, socket.SOCK_SEQPACKET, 0

        def __init__(self):
            self.closes = 0
            c.sockets.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def settimeout(self, seconds):
            effect("timeout", seconds)

        def connect(self, address):
            effect("connect", address)

        def send(self, data):
            kind = json.loads(data)["op"]
            effect(kind, data)
            return 1 if c.short == kind else len(data)

        def recv(self, size):
            effect("recv", size)
            return b'{"ok":true,"port":9000}'

        def recvmsg(self, size, ancillary):
            effect("recvmsg", size, ancillary)
            return c.frame

        def close(self):
            self.closes += 1
            effect("control_close", self)

    class Listener:
        family, type, proto = socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP

        def __init__(self, *, fileno):
            effect("wrap", fileno)
            self.fd, self.closes = fileno, 0
            c.wrappers.append(self)

        def getsockopt(self, *args):
            effect("validate_listening", self)
            return 1

        def getsockname(self):
            effect("validate_address", self)
            return ("127.0.0.1", 9000)

        def close(self):
            self.closes += 1
            effect("listener_close", self)

    def factory(*args):
        effect("construct", args)
        return Connection()

    def rights(*fds):
        return (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds).tobytes())

    c.frame = (b'{"ok":true}', [rights(71)], 0, None)
    c.take = lambda: handoff.take_listener("127.0.0.1", 9000, socket_class=Listener)
    c.names = lambda: [call[0] for call in c.calls]
    c.book, c.rights, c.Listener = book, rights, Listener
    monkeypatch.setenv(handoff.HANDOFF_ENV, "controlled-root")
    monkeypatch.setattr(handoff, "_BOOK", book)
    monkeypatch.setattr(handoff, "_CLOCK", clock)
    monkeypatch.setattr(handoff, "_SOCKET", factory)
    monkeypatch.setattr(custody, "os", SimpleNamespace(close=lambda fd: effect("raw_close", fd)))
    return c


def test_no_owner_zero_effect_and_raw_connect_ordinary_transfer(controlled, monkeypatch):
    c = controlled
    monkeypatch.delenv(handoff.HANDOFF_ENV)
    assert handoff.take_listener("127.0.0.1", 9000) is None
    assert handoff.lease_port() is None
    handoff.release_port(9000)
    assert not c.calls and not c.book.records
    with handoff._connect("controlled") as conn:
        assert conn is c.sockets[0] and not c.book.records and c.book.reserved == 0
        assert conn.closes == 0
    assert conn.closes == 1 and c.names().count("control_close") == 1


@pytest.mark.parametrize("operation", ["lease", "release"])
def test_internal_simple_requests_have_one_checked_control_closer(controlled, operation):
    c = controlled
    if operation == "lease":
        assert handoff.lease_port() == 9000
    else:
        assert handoff.release_port(9000) is None
    assert c.names().count(operation) == c.names().count("control_close") == 1
    assert c.sockets[0].closes == 1 and not c.book.records and c.book.reserved == 0


@pytest.mark.parametrize("stage", ["construct", "timeout", "connect", "take", "recvmsg"])
@pytest.mark.parametrize("kind", [OSError, ValueError, KeyboardInterrupt])
def test_intended_control_effect_failure_preserves_original_identity(controlled, stage, kind):
    c = controlled
    primary = kind("original " + stage)
    c.stage, c.failure = stage, primary
    with pytest.raises(kind) as caught:
        c.take()
    assert caught.value is primary and c.names().count(stage) >= 1
    assert not c.wrappers and not any(n in c.names() for n in ("ack", "raw_close"))
    if stage in ("construct", "recvmsg"):
        record = next(iter(c.book.records.values()))
        assert record.first_error is primary and record.unknown
        assert record.raw_tuple is None and not record.rights
    else:
        assert c.sockets[0].closes <= 1


def test_normal_return_retained_before_ancillary_or_json_decode(controlled, monkeypatch):
    c = controlled
    real = custody.ReceiveRecord.classify
    published = []

    def hook(stage):
        if stage == "timeout":
            record = next(iter(c.book.records.values()))
            assert record.control is c.sockets[0] and record.control_state == "open"
            published.append(record)

    c.hook = hook

    def classify(record):
        assert record.raw_tuple is c.frame
        assert c.book.records[record.number] is record
        assert record.worst == socket.CMSG_SPACE(custody.INT_SIZE) // custody.INT_SIZE
        return real(record)

    monkeypatch.setattr(custody.ReceiveRecord, "classify", classify)
    result = c.take()
    assert published and all(record.control is c.sockets[0] for record in published)
    assert result is c.wrappers[0] and result.closes == 0
    assert c.names().count("ack") == c.names().count("control_close") == 1
    assert not c.book.records and c.book.reserved == 0


@pytest.mark.parametrize(
    "mode",
    [
        "json",
        "false",
        "none",
        "extra",
        "other",
        "other_after",
        "data_trunc",
        "anc_trunc",
        "tail",
        "negative",
        "duplicate",
    ],
)
def test_rejected_frames_dispose_only_known_original_rights(controlled, mode):
    c = controlled
    data, ancillary, flags, address = c.frame
    if mode == "json":
        data = b"{"
    elif mode == "false":
        data = b'{"ok":false}'
    elif mode == "none":
        ancillary = []
    elif mode == "extra":
        ancillary = [c.rights(71, 72)]
    elif mode in ("other", "other_after"):
        ancillary = [(999, 999, b"opaque"), c.rights(71)]
        if mode == "other_after":
            ancillary.reverse()
    elif mode == "data_trunc":
        flags = socket.MSG_TRUNC
    elif mode == "anc_trunc":
        flags = socket.MSG_CTRUNC
    elif mode == "tail":
        level, kind, payload = c.rights(71)
        ancillary = [(level, kind, payload + b"x")]
    elif mode == "negative":
        ancillary = [c.rights(-1)]
    else:
        ancillary = [c.rights(71, 71)]
    c.frame = (data, ancillary, flags, address)
    with pytest.raises(handoff.HandoffError):
        c.take()
    assert not c.wrappers and "ack" not in c.names()
    closed = [call[1] for call in c.calls if call[0] == "raw_close"]
    expected = (
        [] if mode in ("none", "negative", "duplicate") else [71, 72] if mode == "extra" else [71]
    )
    assert closed == expected and len(closed) == len(set(closed))
    if mode in ("anc_trunc", "tail", "negative", "duplicate", "other", "other_after"):
        record = next(iter(c.book.records.values()))
        assert record.unknown and record.raw_tuple is c.frame
    else:
        assert not c.book.records and c.book.reserved == 0


@pytest.mark.parametrize("fd", [0, 1, 2])
def test_known_low_nonnegative_right_is_not_guessed_invalid(controlled, fd):
    c = controlled
    c.frame = (b'{"ok":false}', [c.rights(fd)], 0, None)
    with pytest.raises(handoff.HandoffError):
        c.take()
    assert [call[1] for call in c.calls if call[0] == "raw_close"] == [fd]
    assert not c.book.records


@pytest.mark.parametrize("kind", [OSError, KeyboardInterrupt])
def test_wrapper_no_return_never_authorizes_raw_close_or_retry(controlled, kind):
    c = controlled
    primary = kind("wrapper may have consumed original fd")
    c.stage, c.failure = "wrap", primary
    with pytest.raises(kind) as caught:
        c.take()
    record = next(iter(c.book.records.values()))
    assert caught.value is record.first_error is primary
    assert record.rights[0].fd == 71 and record.rights[0].state == "unknown"
    before = list(c.calls)
    record.fail(primary)
    with pytest.raises(RuntimeError):
        record.classify()
    with pytest.raises(RuntimeError):
        record.wrap(c.Listener)
    with pytest.raises(RuntimeError):
        record.transfer_control()
    assert "raw_close" not in c.names() and not c.wrappers
    assert c.names().count("wrap") == 1 and c.calls == before


@pytest.mark.parametrize("stage", ["validate_listening", "validate_address", "ack"])
@pytest.mark.parametrize("kind", [OSError, ValueError, KeyboardInterrupt])
def test_validation_or_ACK_error_checked_listener_close_preserves_primary(controlled, stage, kind):
    c = controlled
    primary = kind("original " + stage)
    c.stage, c.failure = stage, primary
    with pytest.raises(kind) as caught:
        c.take()
    assert caught.value is primary and c.names().count(stage) == 1
    assert c.wrappers[0].closes == c.sockets[0].closes == 1
    assert "raw_close" not in c.names() and not c.book.records
    if stage != "ack":
        assert c.names().index("listener_close") < c.names().index("nack")
    else:
        assert c.names().count("ack") == 1 and "nack" not in c.names()


@pytest.mark.parametrize("secondary_stage", ["listener_close", "nack", "control_close"])
def test_secondary_BaseException_cannot_replace_validation_primary(controlled, secondary_stage):
    c = controlled
    primary, secondary = ValueError("original validation"), KeyboardInterrupt("secondary")
    c.stage, c.failure = "validate_address", primary
    c.secondary_stage, c.secondary = secondary_stage, secondary
    with pytest.raises(ValueError) as caught:
        c.take()
    assert caught.value is primary and any("KeyboardInterrupt" in n for n in primary.__notes__)
    assert c.names().count(secondary_stage) == 1
    if secondary_stage != "nack":
        record = next(iter(c.book.records.values()))
        before = list(c.calls)
        record.fail(primary)
        assert record.first_error is primary and record.unknown and c.calls == before


def test_short_ACK_is_not_retried_and_known_listener_is_closed(controlled):
    c = controlled
    c.short = "ack"
    with pytest.raises(OSError, match="short listener handoff"):
        c.take()
    assert c.names().count("ack") == c.wrappers[0].closes == c.sockets[0].closes == 1
    assert "nack" not in c.names() and not c.book.records


@pytest.mark.parametrize("kind", [OSError, KeyboardInterrupt])
def test_ACK_success_then_control_close_failure_retains_primary_and_listener_custody(
    controlled, kind
):
    c = controlled
    primary = kind("after original control close effect")
    c.stage, c.failure = "control_close", primary
    with pytest.raises(kind) as caught:
        c.take()
    record = next(iter(c.book.records.values()))
    assert caught.value is record.first_error is primary
    assert c.names().count("ack") == c.names().count("control_close") == 1
    assert record.control is c.sockets[0] and record.control_state == "unknown"
    assert record.rights[0].wrapper is c.wrappers[0] and c.wrappers[0].closes == 1
    assert record.rights[0].state == "closed" and record.unknown
    before = list(c.calls)
    record.fail(primary)
    assert c.calls == before and c.book.reserved == record.worst


@pytest.mark.parametrize("stage", ["connect", "take", "recvmsg", "validate_address", "ack"])
def test_original_entry_cutoff_forbids_next_close_or_send_effect(controlled, stage):
    c = controlled
    c.advance = stage
    with pytest.raises(TimeoutError):
        c.take()
    record = next(iter(c.book.records.values()))
    assert record.deadline == 20.0 and record.unknown
    assert "control_close" not in c.names() and "listener_close" not in c.names()
    assert "nack" not in c.names()
    if stage != "ack":
        assert "ack" not in c.names()
    assert c.names().count(stage) == 1


@pytest.mark.parametrize("exhaustion", ["transactions", "rights"])
def test_pre_effect_reservation_is_padding_aware_and_unknown_never_recycles(controlled, exhaustion):
    c = controlled
    primary = OSError("receive did not return")
    c.stage, c.failure = "recvmsg", primary
    worst = socket.CMSG_SPACE(custody.INT_SIZE) // custody.INT_SIZE
    if exhaustion == "rights":
        c.book.right_limit = worst
    else:
        c.book.transaction_limit = 1
    with pytest.raises(OSError) as caught:
        c.take()
    assert caught.value is primary and c.book.reserved == worst
    before = list(c.calls)
    with pytest.raises(handoff.HandoffError, match="capacity"):
        c.take()
    # Entry clock is permitted, but no allocation/receive/close follows refusal.
    assert c.calls[len(before) :] == [("clock",)] and len(c.book.records) == 1


def test_reentrant_receive_reserves_before_first_receive_effect(controlled):
    c = controlled
    c.book.transaction_limit = 1
    refused = []

    def hook(stage):
        if stage == "recvmsg":
            c.hook = None
            before = len(c.calls)
            with pytest.raises(handoff.HandoffError):
                c.take()
            refused.append(c.calls[before:])

    c.hook = hook
    result = c.take()
    assert refused == [[("clock",)]] and result is c.wrappers[0]
    assert c.names().count("construct") == c.names().count("recvmsg") == 1
    assert not c.book.records


def test_original_validation_and_publication_boundaries_remain_distinct(controlled):
    c = controlled
    listener = c.take()
    assert listener is c.wrappers[0] and listener.closes == 0
    assert c.names().index("ack") < c.names().index("control_close")
    assert not c.book.records  # ordinary raw return only, NOT physical/caller-publication proof
    listener.close()
    assert listener.closes == 1


@pytest.mark.parametrize("caller", ["network", "serial", "mcp", "timed", "timed_owner"])
def test_actual_configured_refusal_never_authorizes_ordinary_bind(controlled, monkeypatch, caller):
    # The actual facade must reject its controlled normal recvmsg tuple, before any
    # real caller fallback factory/bind. Regressing to None hits counted inert guards.
    c = controlled
    c.frame = (b'{"ok":false}', [], 0, None)
    forbidden = {"factory": 0, "bind": 0}

    class Fallback:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def setsockopt(self, *args):
            pass

        def setblocking(self, value):
            pass

        def close(self):
            pass

        def bind(self, *args):
            forbidden["bind"] += 1
            raise AssertionError("forbidden caller fallback bind")

    def factory(*args, **kwargs):
        forbidden["factory"] += 1
        return Fallback()

    def guard(module):
        # Replace this caller's module reference, not shared socket.socket or the
        # controlled handoff._SOCKET. Timed socket_class is never reached on refusal.
        values = {
            name: getattr(socket, name)
            for name in (
                "AF_INET",
                "AF_INET6",
                "SOCK_STREAM",
                "IPPROTO_TCP",
                "SOL_SOCKET",
                "SO_REUSEADDR",
            )
        }
        monkeypatch.setattr(module, "socket", SimpleNamespace(**values, socket=factory))

    if caller == "network":
        from pokered_harness.link import network_backend

        guard(network_backend)
        with pytest.raises(handoff.HandoffError) as caught:
            network_backend.NetworkBackend.listen(9000, accept_timeout_s=0.1)
    elif caller == "serial":
        from pokered_harness.link import serial_link

        guard(serial_link)
        with pytest.raises(handoff.HandoffError) as caught:
            serial_link.TcpSerialLink.listen(9000, "blue", accept_timeout_s=0.1)
    elif caller == "mcp":
        from pokered_harness import mcp_server_remote_contract as remote

        guard(remote)
        with pytest.raises(remote.McpHarnessError) as caught:
            remote._bind_listener("127.0.0.1", 9000)
        assert caught.value.code == "listen_failed"
        assert isinstance(caught.value.__cause__, handoff.HandoffError)
    elif caller == "timed":
        from pokered_harness.link import timed_remote

        guard(timed_remote)
        monkeypatch.setattr(timed_remote, "time", SimpleNamespace(monotonic=lambda: c.now))
        with pytest.raises(handoff.HandoffError) as caught:
            timed_remote.TimedRemoteEndpoint.listen(9000, rom_version="blue", deadline=20.0)
    else:
        from pokered_harness import mcp_timed_owner

        guard(mcp_timed_owner)
        owner = SimpleNamespace(
            _endpoint=None,
            _condition=Lock(),
            policy=SimpleNamespace(endpoint_options=dict),
            _publish=lambda **kwargs: c.calls.append(("publish", kwargs)),
        )
        request = SimpleNamespace(
            args=("127.0.0.1", 9000, "blue"), deadline=20.0, cancel_event=None, kind="listen"
        )
        with pytest.raises(handoff.HandoffError) as caught:
            mcp_timed_owner.TimedOwner._open(owner, request)
    refusal = caught.value.__cause__ if caller == "mcp" else caught.value
    assert type(refusal) is handoff.HandoffError
    assert str(refusal) == "listener reply requires exactly one descriptor"
    assert type(refusal.__cause__) is ValueError and str(refusal.__cause__) == str(refusal)
    assert c.names().count("recvmsg") == c.names().count("take") == 1
    assert c.sockets[0].closes == 1 and not c.book.records
    assert forbidden == {"factory": 0, "bind": 0}
    assert not c.wrappers and "wrap" not in c.names() and "ack" not in c.names()


@pytest.mark.parametrize("kind", [OSError, KeyboardInterrupt])
def test_rejected_raw_close_after_effect_is_single_use_and_quarantined(controlled, kind):
    c = controlled
    c.frame = (b'{"ok":false}', [c.rights(71)], 0, None)
    secondary = kind("after raw close effect")
    c.stage, c.failure = "raw_close", secondary
    with pytest.raises(handoff.HandoffError) as caught:
        c.take()
    record = next(iter(c.book.records.values()))
    assert record.first_error is caught.value and record.rights[0].state == "unknown"
    assert any(kind.__name__ in note for note in caught.value.__notes__)
    assert c.names().count("raw_close") == 1 and c.sockets[0].closes == 1
    before = list(c.calls)
    record.fail(caught.value)
    assert c.calls == before and record.raw_tuple is c.frame


def test_default_quota_and_postreturn_parser_interruption_keep_original_tuple(
    controlled, monkeypatch
):
    c = controlled
    assert c.book.transaction_limit == 32 and c.book.right_limit == 256
    primary = KeyboardInterrupt("after normal raw tuple before parser")

    def classify(record):
        assert record.raw_tuple is c.frame and record.phase == "raw_returned"
        raise primary

    monkeypatch.setattr(custody.ReceiveRecord, "classify", classify)
    with pytest.raises(KeyboardInterrupt) as caught:
        c.take()
    record = next(iter(c.book.records.values()))
    assert caught.value is record.first_error is primary
    assert record.raw_tuple is c.frame and record.unknown and not record.rights
    assert "raw_close" not in c.names() and c.book.reserved == record.worst


def test_ancillary_padding_budget_is_not_advertised_one_right(controlled):
    c = controlled
    worst = socket.CMSG_SPACE(custody.INT_SIZE) // custody.INT_SIZE
    assert worst > 1
    c.frame = (b'{"ok":true}', [c.rights(*range(71, 71 + worst + 1))], 0, None)
    with pytest.raises(handoff.HandoffError):
        c.take()
    record = next(iter(c.book.records.values()))
    assert record.worst == len(record.rights) == worst and record.unknown
    assert record.raw_tuple is c.frame and c.book.reserved == worst
    assert [call[1] for call in c.calls if call[0] == "raw_close"] == list(range(71, 71 + worst))


@pytest.mark.parametrize("opaque_first", [False, True])
def test_unsupported_ancillary_keeps_original_tuple_and_quota_after_known_close(
    controlled, opaque_first
):
    c = controlled
    opaque = (999, 999, b"opaque-not-a-descriptor")
    ancillary = [opaque, c.rights(71)] if opaque_first else [c.rights(71), opaque]
    c.frame = (b'{"ok":true}', ancillary, 0, None)
    with pytest.raises(handoff.HandoffError) as caught:
        c.take()
    record = next(iter(c.book.records.values()))
    assert record.first_error is caught.value and record.raw_tuple is c.frame
    assert (
        opaque in record.raw_tuple[1]
        and record.unknown == "unsupported listener ancillary ownership"
    )
    assert record.rights[0].state == "closed" and record.control_state == "closed"
    assert c.book.reserved == record.worst and c.names().count("raw_close") == 1
    assert [call[1] for call in c.calls if call[0] == "raw_close"] == [71]
    before = list(c.calls)
    record.fail(caught.value)
    with pytest.raises(RuntimeError):
        record.classify()
    assert c.calls == before and c.book.reserved == record.worst
    assert c.book.records[record.number] is record and record.raw_tuple is c.frame
