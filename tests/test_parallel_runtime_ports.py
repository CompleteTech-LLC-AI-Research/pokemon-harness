"""Authored controls: a job-scoped port reservation held until the listener binds (#107)."""

import os
import socket
import subprocess
import sys

import pytest

from tests import _parallel_runtime_port_support as ports

CHILD = "from tests._parallel_runtime_port_support import reserve_port\nprint(reserve_port())\n"


@pytest.fixture
def job_range(tmp_path, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1] + 1
    monkeypatch.setenv(ports.PORT_BASE_ENV, str(base))
    monkeypatch.setenv(ports.PORT_COUNT_ENV, "8")
    monkeypatch.setenv(ports.PORT_LOCK_ENV, str(tmp_path / "locks"))
    monkeypatch.setattr(ports, "_HELD", {})
    return base


def test_outside_a_parallel_job_nothing_is_reserved(monkeypatch):
    for name in (ports.PORT_BASE_ENV, ports.PORT_COUNT_ENV, ports.PORT_LOCK_ENV):
        monkeypatch.delenv(name, raising=False)
    assert ports.reserve_port() is None


def test_ports_come_from_the_job_range_and_are_never_handed_out_twice(job_range):
    first, second = ports.reserve_port(), ports.reserve_port()
    assert first != second
    assert all(job_range <= port < job_range + 8 for port in (first, second))


def test_the_reservation_is_held_across_processes_until_the_holder_ends(job_range, tmp_path):
    held = {ports.reserve_port(), ports.reserve_port()}
    env = {**os.environ}
    other = int(
        subprocess.run(
            [sys.executable, "-c", CHILD], env=env, capture_output=True, text=True, check=True
        ).stdout
    )
    assert other not in held and job_range <= other < job_range + 8


def test_a_port_with_a_live_listener_is_skipped(job_range):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", job_range))
        except OSError:
            pytest.skip("range start is not bindable on this host")
        listener.listen(1)
        assert ports.reserve_port() != job_range


def test_release_makes_the_lowest_port_available_again(job_range):
    first = ports.reserve_port()
    ports.release_port(first)
    assert ports.reserve_port() == first


def test_exhausting_the_range_fails_closed(job_range, monkeypatch):
    monkeypatch.setenv(ports.PORT_COUNT_ENV, "1")
    ports.reserve_port()
    with pytest.raises(RuntimeError, match="no loopback port"):
        ports.reserve_port()
