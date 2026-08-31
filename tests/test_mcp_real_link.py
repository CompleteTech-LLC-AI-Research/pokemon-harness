"""Real-ROM MCP link lifecycle smoke for the production backend.

This intentionally covers the MCP dispatch path, not only the lower-level
TCP transport.  It must attach the pinned PyBoy serial core on both sides and
must not fall back to the legacy semantic endpoint.
"""

from __future__ import annotations

import contextlib
import io
import socket
import time

import pytest

from pokered_harness.config import load_versions
from pokered_harness.mcp_server import LinkState, dispatch_tool
from pokered_harness.session import Session
from tests._rom_assets import rom_path, sym_path


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_mcp_remote_link_attaches_native_serial_backend() -> None:
    rom = rom_path("yellow")
    sym = sym_path("yellow")
    if not (rom.is_file() and sym.is_file()):
        pytest.skip("Yellow ROM and symbols are not available")

    pins = load_versions("VERSIONS.md")
    expected_sha = pins.sha1_for_path(rom)
    assert expected_sha is not None
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        listener_session = Session.from_files(
            rom,
            sym,
            expected_rom_sha1=expected_sha,
            expected_pyboy_version=pins.pyboy_version,
        )
        connector_session = Session.from_files(
            rom,
            sym,
            expected_rom_sha1=expected_sha,
            expected_pyboy_version=pins.pyboy_version,
        )

    listener_link = LinkState(primary_version="yellow")
    connector_link = LinkState(primary_version="yellow")
    port = _free_port()
    try:
        dispatch_tool(
            listener_session,
            "link_listen",
            {"port": port, "rom_version": "yellow"},
            link=listener_link,
        )
        connected = dispatch_tool(
            connector_session,
            "link_connect",
            {
                "host": "127.0.0.1",
                "port": port,
                "rom_version": "yellow",
                "timeout_s": 15,
            },
            link=connector_link,
        )

        deadline = time.monotonic() + 5.0
        listener_status = None
        while time.monotonic() < deadline:
            listener_status = dispatch_tool(
                listener_session, "link_status", {}, link=listener_link
            )
            if listener_status["remote_mode"] == "connected":
                break
            time.sleep(0.02)

        assert connected["peer_rom_version"] == "yellow"
        assert listener_status is not None
        assert listener_status["remote_mode"] == "connected"
        assert listener_status["remote_peer_rom_version"] == "yellow"
        assert listener_link.network_session is not None
        assert connector_link.network_session is not None
        assert listener_link.remote_endpoint is None
        assert connector_link.remote_endpoint is None
        assert listener_session._serial_hooks == []
        assert connector_session._serial_hooks == []
    finally:
        dispatch_tool(listener_session, "link_disconnect", {}, link=listener_link)
        dispatch_tool(connector_session, "link_disconnect", {}, link=connector_link)
        connector_session.close()
        listener_session.close()
