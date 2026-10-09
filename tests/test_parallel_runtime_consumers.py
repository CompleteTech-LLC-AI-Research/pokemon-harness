"""Authored controls: migrated listener consumers keep their real contracts (#107 V11).

Both controls call the REAL production helper (the MCP trade server environment builder, the serial
link pair helper over TcpSerialLink.listen) and inspect what comes out; nothing is parsed as text and
nothing here starts a ROM, an emulator or a gate.
"""

import errno
import socket
import time
from pathlib import Path

import pytest

from scripts.production_gate_parallel_ports import HANDOFF_ENV, ListenerBroker
from tests._serial_link_pair import make_tcp_pair

INHERITED_CONTROLS = (
    "POKERED_GATE_REPORT",
    "POKERED_ARBITER_KEY",
    "POKERED_ARBITER_NAME",
    "POKERED_ROM_ROOT",
    "POKERED_PARALLEL_JOB_TOKEN",
    "POKERED_PARALLEL_PORT_BASE",
    "POKERED_SECRET_CONTROL",
    "POKERED_TRADE_RUNTIME_MODE",
)
CONTRACT = {
    HANDOFF_ENV,
    "POKERED_ROM_PATH",
    "POKERED_ROM_VERSION",
    "POKERED_ROM_SHA1",
    "POKERED_SYM_PATH",
    "POKERED_SYM_SHA1",
    "POKERED_VERSIONS_PATH",
    "POKERED_PEER_ROM_PATH",
    "POKERED_PEER_ROM_VERSION",
    "POKERED_PEER_ROM_SHA1",
    "POKERED_PEER_SYM_PATH",
    "POKERED_PEER_SYM_SHA1",
    "POKERED_PEER_STATE_PATH",
    "POKERED_PEER_STATE_SHA1",
}


def asset(name):
    return {
        "rom": Path(f"/assets/{name}.gbc"),
        "sym": Path(f"/assets/{name}.sym"),
        "family": name,
        "pins": {"expected_rom_sha1": "1" * 40, "expected_symbol_sha1": "2" * 40},
        "provenance": {"registry": {"sha1": "3" * 40}},
    }


@pytest.mark.parametrize("runtime_mode", ["source", "cython"])
def test_the_trade_server_environment_keeps_exactly_the_handoff_variable_and_every_pin(
    monkeypatch, runtime_mode
):
    from tests import _mcp_trade_records_rom_support as support

    for name in INHERITED_CONTROLS:
        monkeypatch.setenv(name, "must-not-leak")
    monkeypatch.setenv(HANDOFF_ENV, "abstract-owner-name")
    monkeypatch.setenv("POKERED_PEER_STATE_PATH", "inherited-must-be-overridden")
    env = support._server_env(asset("red"), asset("blue"), runtime_mode, Path("/assets/peer.state"))
    assert env[HANDOFF_ENV] == "abstract-owner-name", "the safe handoff reaches the real server"
    assert not [name for name in INHERITED_CONTROLS if name in env], "no other control leaks"
    assert env["POKERED_ROM_SHA1"] == "1" * 40 and env["POKERED_SYM_SHA1"] == "2" * 40
    assert env["POKERED_PEER_STATE_PATH"] == "/assets/peer.state"
    assert env["POKERED_PEER_STATE_SHA1"] == "3" * 40, (
        "the peer pin is the contract's, not inherited"
    )
    assert env["POKERED_ROM_VERSION"] == "red" and env["POKERED_PEER_ROM_VERSION"] == "blue"
    assert {name for name in env if name.startswith("POKERED_")} == CONTRACT, "exactly the contract"


def test_without_an_owner_the_trade_server_environment_has_no_handoff_variable(monkeypatch):
    from tests import _mcp_trade_records_rom_support as support

    monkeypatch.delenv(HANDOFF_ENV, raising=False)
    env = support._server_env(asset("red"), None, "source", None)
    assert HANDOFF_ENV not in env and env["POKERED_ROM_PATH"] == "/assets/red.gbc"


def wait_for(broker, port, wanted, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if broker.state(port) == wanted:
            return True
        time.sleep(0.02)
    return False


def test_the_serial_link_pair_takes_over_the_socket_the_owner_retained(monkeypatch):
    from tests import test_serial_link as serial

    broker = ListenerBroker().start()
    monkeypatch.setenv(HANDOFF_ENV, broker.name)
    try:
        port = (
            serial._free_port()
        )  # the consumer's own helper: it must lease, not hand out a number
        assert broker.state(port) == "leased"
        rival = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        rival.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with pytest.raises(OSError) as refused:
                rival.bind(("127.0.0.1", port))
            assert refused.value.errno == errno.EADDRINUSE, "no bind-close-rebind window exists"
        finally:
            rival.close()
        server, client = make_tcp_pair(
            port, "blue", "blue"
        )  # real helper over TcpSerialLink.listen
        try:
            assert wait_for(broker, port, "acknowledged"), "the real listener adopted the socket"
        finally:
            client.close()
            server.close()
    finally:
        broker.close()


def test_standalone_the_serial_link_helper_still_hands_out_an_ordinary_free_port(monkeypatch):
    from tests import test_serial_link as serial

    for name in (HANDOFF_ENV, "POKERED_PARALLEL_PORT_BASE", "POKERED_PARALLEL_PORT_COUNT"):
        monkeypatch.delenv(name, raising=False)
    port = serial._free_port()
    assert isinstance(port, int) and 0 < port < 65536
