"""Stable MCP codes for explicitly recognized provider/runtime failures."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import mcp.types as mcp_types
import pytest
from pyboy.core.serial import Serial, SerialBackendError

from pokered_harness import mcp_server
from pokered_harness.link.pyboy_link_session import PairedSessionOperationError, PyBoyLinkSession
from pokered_harness.mcp_timed_owner import TimedOwnerError
from tests.test_session import _session


def _request(session, name="step", arguments=None):
    server = mcp_server.build_server(session)
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name=name, arguments={"count": 1} if arguments is None else arguments
        )
    )
    return asyncio.run(handler(request)).root


def _assert_error(response, code, message, error_type):
    assert response.isError is True
    expected = {
        "ok": False,
        "error": {"code": code, "message": message, "type": error_type},
    }
    assert response.structuredContent == expected
    assert len(response.content) == 1
    assert json.loads(response.content[0].text) == expected


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("step", {"count": 1}),
        ("run_until_event", {"event_names": ["wanted"], "max_ticks": 1}),
        ("load_state", {"data": base64.b64encode(b"state").decode("ascii")}),
    ],
)
def test_real_paired_provider_guard_has_structured_mcp_error(name, arguments):
    primary, primary_pyboy, _ = _session()
    peer, peer_pyboy, _ = _session()
    for pyboy in (primary_pyboy, peer_pyboy):
        pyboy.mb = SimpleNamespace(serial=Serial(False), cgb_mode=False, tick=lambda: False)
    provider = PyBoyLinkSession.local()
    try:
        provider.attach(primary_pyboy)
        provider.attach(peer_pyboy)
        assert provider.paired
        with pytest.raises(PairedSessionOperationError):
            primary_pyboy.mb.serial.backend.validate_session_operation(name)
        response = _request(primary, name, arguments)
        _assert_error(
            response,
            "paired_session_operation",
            f"{name} is unsupported while locally paired; use the pair owner or detach first",
            "PairedSessionOperationError",
        )
        assert primary.current_tick() == peer.current_tick() == 0
        assert primary_pyboy.tick_calls == peer_pyboy.tick_calls == []
        assert primary_pyboy._saved_state == b""
    finally:
        provider.detach_all()
        primary.close()
        peer.close()


def test_latched_serial_error_preserves_internal_cause_but_returns_safe_message():
    private_detail = "private backend detail: credential=DO_NOT_EXPOSE"
    original_error = RuntimeError(private_detail)

    def fail_edge(_bit, _role):
        raise original_error

    core = Serial(False, backend=SimpleNamespace(on_edge=fail_edge))
    core.set_SB(0xA5)
    core.set_SC(0x81)
    core.tick(core.clock_target)
    assert core.backend_failed
    captured = []
    session, pyboy, _ = _session()
    pyboy.mb = SimpleNamespace(serial=core)

    def tick(*_args, **_kwargs):
        try:
            core.check_error()
        except SerialBackendError as error:
            captured.append(error)
            raise

    pyboy.tick = tick
    try:
        response = _request(session)
        _assert_error(
            response,
            "serial_backend_error",
            "serial backend failed; recreate session before resuming",
            "SerialBackendError",
        )
        assert len(captured) == 1
        assert captured[0].__cause__ is original_error
        assert private_detail not in response.model_dump_json()
        assert session.current_tick() == 0
    finally:
        session.close()


@pytest.mark.parametrize("spoofed_code", ["paired_session_operation", "serial_backend_error"])
def test_unrecognized_exception_code_is_not_trusted(spoofed_code):
    # Neither arbitrary .code attributes nor matching class names establish
    # membership in the two explicitly supported exception types.
    fake_type = type("SerialBackendError", (RuntimeError,), {"code": spoofed_code})
    error = fake_type("ordinary internal failure")
    session, pyboy, _ = _session()

    def tick(*_args, **_kwargs):
        raise error

    pyboy.tick = tick
    try:
        response = _request(session)
        _assert_error(response, "internal_error", str(error), "SerialBackendError")
    finally:
        session.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("timed_peer_mismatch", "timed_peer_mismatch"),
        ("timed_deadline", "timed_deadline"),
        ("private_internal_code", "internal_error"),
    ],
)
def test_timed_owner_error_uses_only_published_codes(code, expected):
    assert mcp_server._error_code(TimedOwnerError(code, "timed failure")) == expected


def test_old_runtime_without_serial_backend_error_still_imports():
    # Use a subprocess so a compatibility import does not replace exception
    # class identities in the live MCP module used by other tests.
    code = """
import builtins
original_import = builtins.__import__
def without_new_error(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "pyboy.core.serial" and "SerialBackendError" in (fromlist or ()):
        raise ImportError("older runtime has no SerialBackendError")
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = without_new_error
from pokered_harness import mcp_server
assert mcp_server._SERIAL_BACKEND_ERRORS == ()
assert mcp_server._error_code(RuntimeError("older runtime")) == "internal_error"
print("compatible")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "compatible"
