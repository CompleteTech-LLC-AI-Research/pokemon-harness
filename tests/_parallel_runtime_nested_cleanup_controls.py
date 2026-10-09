"""Actual listener/subreaper fault controls; authored only until execution is admitted."""

import ctypes
from pathlib import Path

import pytest

from tests import _parallel_runtime_nested_cleanup as nested


def subreaper_flag():
    flag = ctypes.c_int()
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(37, ctypes.byref(flag), 0, 0, 0) == 0
    return flag.value


def faulted_nested_cleanup(tmp_path, monkeypatch, point):
    """Inject into real resource setup/teardown, then read the kernel flag back.

    No fixture child is started. This verifies resource cleanup and restoration,
    not product containment or the separate pre-handoff child-ownership gap.
    """

    before = subreaper_flag()
    owner = nested.NestedCleanup(tmp_path, "unused source: no fixture child launched")

    def fail(*args, **kwargs):
        raise RuntimeError(f"controlled {point}")

    class DirectoryFault:
        def __init__(self, directory):
            self.directory, self.name = directory, directory.name

        def cleanup(self):
            self.directory.cleanup()
            fail()

    class ListenerFault:
        def __init__(self, listener):
            self.listener = listener

        def __getattr__(self, name):
            return getattr(self.listener, name)

        def close(self):
            self.listener.close()
            fail()

    if point == "directory_setup":
        monkeypatch.setattr(nested.tempfile, "TemporaryDirectory", fail)
    elif point == "socket_setup":
        monkeypatch.setattr(nested.socket, "socket", fail)
    elif point in ("thread_setup", "started_thread_setup"):
        start = nested.threading.Thread.start

        def faulted_start(thread):
            if point == "started_thread_setup":
                start(thread)
            fail()

        monkeypatch.setattr(nested.threading.Thread, "start", faulted_start)
    elif point == "directory_teardown":
        create = nested.tempfile.TemporaryDirectory
        monkeypatch.setattr(
            nested.tempfile,
            "TemporaryDirectory",
            lambda *args, **kwargs: DirectoryFault(create(*args, **kwargs)),
        )
    elif point == "listener_teardown":
        create = nested.socket.socket
        monkeypatch.setattr(
            nested.socket, "socket", lambda *args, **kwargs: ListenerFault(create(*args, **kwargs))
        )
    elif point == "receipt_teardown":
        write = Path.write_text

        def faulted_write(path, *args, **kwargs):
            if path == tmp_path / "outer-nested-cleanup.json":
                fail()
            return write(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", faulted_write)
    else:
        raise AssertionError(f"unknown fault point: {point}")

    expected = RuntimeError if point.endswith("setup") else AssertionError
    try:
        with pytest.raises(expected, match=f"controlled {point}"), owner:
            assert subreaper_flag() == 1
            assert owner.thread.is_alive()
    finally:
        assert subreaper_flag() == before, "outer test leaked a changed kernel subreaper flag"
        assert owner.adoption.active is False
        if owner.listener is not None:
            assert owner.listener.fileno() == -1, "owned listener descriptor survived cleanup"
        if owner.thread is not None:
            assert not owner.thread.is_alive(), "owned receiver survived cleanup"
        if owner.directory is not None:
            assert not Path(owner.directory.name).exists(), "owned temporary directory survived"
        assert owner.fds == {}
