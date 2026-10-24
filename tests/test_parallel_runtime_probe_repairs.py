"""Authored NOTRUN source-bound controls for the four 82ba review findings.

These control kernel/reaping inputs and extract the actual embedded reader/final
producer. They do not cover the full live-node lifecycle or pending ECHILD-parent
death case; those actual-process controls remain required after broker integration.
"""

import ast
import hashlib
import hmac
import json
import socket
from types import SimpleNamespace

import pytest

from scripts.production_gate_parallel_probe_bootstrap import ADMISSION_SOURCE, MAIN_SOURCE
from scripts.production_gate_parallel_probe_owner import ProbePeer
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError, decode_probe_frame
from tests import _parallel_runtime_nested_cleanup as nested_module
from tests.test_parallel_runtime_probe_owner import final_body, owner_case
from tests.test_parallel_runtime_probe_protocol import signed_bytes


@pytest.mark.parametrize("tag", ["é" * 64, "g" * 64, "0" * 63, None])
def test_actual_embedded_reader_refuses_bad_mac_without_unwinding(tag):
    names = {"valid_mac", "canonical", "unique_pairs", "decode", "service_control"}
    tree = ast.parse(ADMISSION_SOURCE)
    selected = ast.Module(
        body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names],
        type_ignores=[],
    )
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    refused, retained = [], object()
    namespace = {
        "json": json,
        "hashlib": hashlib,
        "hmac": hmac,
        "MAX_FRAME": 65536,
        "parent": receiver,
        "root": None,
        "key": b"k" * 32,
        "config": {},
        "cleanup_deadline": 120.0,
        "request_stop": refused.append,
        "held": {"controlled-instance": retained},
    }
    try:
        # Execute only selected functions from the repository's trusted source literal.
        exec(compile(selected, "actual-embedded-service-control", "exec"), namespace)  # noqa: S102
        sender.send(json.dumps({"mac": tag}).encode())
        namespace["service_control"]()
        assert refused == ["malformed control frame"]
        assert namespace["held"] == {"controlled-instance": retained}
        assert namespace["cleanup_deadline"] == 120.0
    finally:
        receiver.close()
        sender.close()


def test_actual_final_producer_keeps_late_physical_cleanup_as_failed_operation(tmp_path):
    owner, now = owner_case(tmp_path)
    owner.custody.roles["guardian"] = SimpleNamespace(instance=(201, 3))
    holder = SimpleNamespace(instance=(200, 2), role="holder", sequence=2)
    namespace = {
        "exhausted": lambda: True,
        "held": {},
        "time": SimpleNamespace(monotonic=lambda: 121.0),
        "subreaper_on": lambda: True,
        "custody_errors": [],
        "operation_errors": [],
        "cleanup_deadline": 120.0,
        "job_deadline": 100.0,
        "direct": SimpleNamespace(returncode=0),
        "parent_pid": 100,
        "parent_start": 1,
        "forks": [[201, 3]],
        "adopted": [],
        "reaped": [[201, 3]],
        "killed": [],
    }
    tree = ast.parse(MAIN_SOURCE)
    first = next(
        i
        for i, n in enumerate(tree.body)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "physical_empty" for t in n.targets)
    )
    last = next(
        i
        for i, n in enumerate(tree.body)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "body" for t in n.targets)
    )
    selected = ast.Module(body=tree.body[first : last + 1], type_ignores=[])
    # Exercise the trusted producer statements instead of duplicating their behavior.
    exec(compile(selected, "actual-embedded-final-producer", "exec"), namespace)  # noqa: S102
    body = dict(final_body(owner, "holder", holder.instance, (201, 3)), **namespace["body"])
    assert body["proof"] and body["physical_empty"]
    assert body["timely_cleanup"] is False and body["success"] is False
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    receiver.setblocking(False)
    peer = ProbePeer(receiver, record=holder)
    now[0] = 121.0
    try:
        sender.send(signed_bytes(body, owner.context))
        owner._read(peer)
        assert peer.final == body and owner.custody.failures
        assert not owner.operation_succeeded()
    finally:
        receiver.close()
        sender.close()


@pytest.mark.parametrize("crossing", [100.0, 120.0])
def test_operation_cannot_pass_when_reconciliation_crosses_original_bound(tmp_path, crossing):
    owner, now = owner_case(tmp_path)
    now[0] = 99.0
    holder = SimpleNamespace(role="holder")
    guardian = SimpleNamespace(role="guardian")
    owner.peers = [
        ProbePeer(None, record=holder, final={"success": True}),
        ProbePeer(None, record=guardian, final={"success": True}),
    ]
    owner.custody.roles.update(holder=holder, guardian=guardian, payload=object())

    def reconcile():
        now[0] = crossing
        owner.physical_cleanup_verified_at = crossing
        return True

    owner.reconcile = reconcile
    assert owner.operation_succeeded() is False
    assert owner.physical_cleanup_verified_at == crossing and owner.custody.failures


@pytest.mark.parametrize(
    "raw",
    [b'{"mac":"' + "é".encode() * 64 + b'"}', b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"],
)
def test_root_decoder_contains_unicode_mac_and_deep_json_refusal(tmp_path, raw):
    owner, _ = owner_case(tmp_path)
    with pytest.raises(ProbeProtocolError):
        decode_probe_frame(raw, owner.context)


@pytest.mark.parametrize("unsettled_parent", [False, True])
def test_actual_fixture_settles_parent_before_discharge_of_child_wait(
    tmp_path, monkeypatch, unsettled_parent
):
    fixture = nested_module.NestedCleanup(tmp_path, "controlled-unused-source")
    fixture.listener = fixture.thread = fixture.directory = None
    fixture.cleanup_attempts, fixture.cleanup_deadline = 0, 100.0
    fixture.fds = {"child": 1002, "nested": 1001}
    fixture.fd_bindings = {"child": (1, 2), "nested": (1, 1)}
    fixture.metadata = {"nested": {"pid": 200, "start": 2}, "child": {"pid": 201, "start": 3}}
    fixture.state = lambda name: "gone"
    fixture._held_fd = lambda name: fixture.fds[name]
    waits, released = [], []

    def wait(kind, fd, flags):
        waits.append(fd)
        if fd == 1001 and unsettled_parent:
            raise ChildProcessError("still owed by intermediate parent's parent")
        if fd == 1002:
            assert 1001 in released, "future child wait inspected before parent settled"
        return SimpleNamespace(si_status=0)

    fake_os = SimpleNamespace(
        P_PIDFD=nested_module.os.P_PIDFD,
        WEXITED=nested_module.os.WEXITED,
        WNOHANG=nested_module.os.WNOHANG,
        waitid=wait,
        close=released.append,
    )
    monkeypatch.setattr(nested_module, "os", fake_os)
    monkeypatch.setattr(nested_module, "time", SimpleNamespace(monotonic=lambda: 10.0))
    raw = b"200 (controlled) " + b" ".join([b"Z", b"100", *([b"0"] * 17), b"2"])
    monkeypatch.setattr(nested_module.Path, "read_bytes", lambda path: raw)
    if unsettled_parent:
        with pytest.raises(AssertionError, match="still owes a parent wait"):
            fixture._cleanup()
        assert waits == [1001] and released == []
        assert set(fixture.fds) == {"nested", "child"}
    else:
        fixture._cleanup()
        assert waits == released == [1001, 1002] and fixture.fds == {}
