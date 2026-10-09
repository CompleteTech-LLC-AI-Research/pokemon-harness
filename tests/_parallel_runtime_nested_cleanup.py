"""Independent outer ownership of this trusted nested fixture, never product proof.

The fixture hands its own and its direct child's pidfds to this test before
publishing readiness. Validation binds those descriptors to the private handshake,
peer credentials, exact argv, parent and start identities. Assertion/setup failures
cannot skip acquiring ownership. Only these held instances can be signalled.
"""

import array
import json
import os
import secrets
import select
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

from scripts.production_gate_parallel_processes import OrchestratorAdoption, read_stat
from tests._parallel_runtime_nested_retention import finish_nested_cleanup, pending_nested_cleanup
from tests._parallel_runtime_nested_startup import NESTED_CHILD_SOURCE


class NestedCleanup:
    def __init__(
        self, root, source, *, startup_mode="normal", handoff_pause=None, reject_handoff=False
    ):
        self.root, self.source = Path(root), source
        self.token = secrets.token_hex(32)
        self.stop = threading.Event()
        self.fds, self.metadata, self.problem = {}, {}, ""
        self.fd_bindings = {}
        self.adoption = OrchestratorAdoption()
        self.startup_mode, self.handoff_pause = startup_mode, handoff_pause
        self.reject_handoff = reject_handoff

    def __enter__(self):
        if pending_nested_cleanup():
            raise AssertionError("prior outer fixture cleanup is unresolved; admission refused")
        self.owner_thread = threading.current_thread()
        self.directory = self.listener = self.thread = None
        self.cleanup_deadline = None
        self.cleanup_attempts = 0
        try:
            self.adoption.__enter__()
            self.directory = tempfile.TemporaryDirectory(prefix="poke-nested-")
            self.endpoint = str(Path(self.directory.name) / "owner.sock")
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            if not self.adoption.verify():
                raise AssertionError("outer fixture subreaper is not established")
            self.listener.bind(self.endpoint)
            self.listener.listen(1)
            self.listener.settimeout(0.05)
            self.thread = threading.Thread(target=self._receive, name="owned-nested-pidfds")
            self.thread.start()
        except BaseException as original:
            self.problem = f"fixture setup failed: {type(original).__name__}: {original}"
            finish_nested_cleanup(self, original)
            raise
        return self

    def _validate(self, fd, instance, argv, parent=None):
        pid, start = instance["pid"], instance["start"]
        info = Path(f"/proc/self/fdinfo/{fd}").read_text()
        identity = next(line.split()[1] for line in info.splitlines() if line.startswith("Pid:"))
        assert int(identity) == pid, "received descriptor is not the declared pidfd"
        first = read_stat(pid)
        assert first is not None and first[0] == start, "received instance start is unproven"
        assert parent is None or first[1] == parent, "fixture child belongs to another parent"
        command = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]
        assert command == argv, "received instance is not this fixture"
        again = read_stat(pid)
        assert again is not None and again[:2] == first[:2], "instance changed while validating"

    def _receive(self):
        received = []
        try:
            while not self.stop.is_set():
                try:
                    connection, _ = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            else:
                return
            with connection:
                connection.settimeout(1.0)
                raw, ancillary, flags, _ = connection.recvmsg(
                    8192, socket.CMSG_SPACE(2 * array.array("i").itemsize)
                )
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        values = array.array("i")
                        values.frombytes(data[: len(data) - len(data) % values.itemsize])
                        received.extend(values)
                assert not flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)
                assert len(received) == 2, "fixture must hand off both ownership descriptors"
                body = json.loads(raw)
                assert body["token"] == self.token, "wrong fixture handshake"
                assert body["mode"] == self.startup_mode, "undeclared startup mode"
                if self.handoff_pause is not None:
                    limit = time.monotonic() + 1.5
                    while not self.handoff_pause.wait(0.01):
                        if self.stop.is_set() or time.monotonic() >= limit:
                            raise AssertionError("controlled handoff pause expired")
                assert not self.reject_handoff, "controlled rejection before ownership"
                peer, _, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                assert body["nested"]["pid"] == peer, "descriptor sender is not nested parent"
                job = str(Path(body["job_root"]).resolve())
                assert Path(job).is_relative_to(self.root.resolve()), (
                    "fixture job is outside its root"
                )
                parent_argv = [sys.executable, "-c", self.source, job, self.endpoint, self.token]
                if self.startup_mode != "normal":
                    parent_argv.append(self.startup_mode)
                self._validate(
                    received[0],
                    body["nested"],
                    parent_argv,
                )
                controls = body["control_fds"]
                assert set(controls) == {"release", "confirm"}
                assert all(type(value) is int and value >= 0 for value in controls.values())
                self._validate(
                    received[1],
                    body["child"],
                    [
                        sys.executable,
                        "-c",
                        NESTED_CHILD_SOURCE,
                        str(controls["release"]),
                        str(controls["confirm"]),
                        str(peer),
                    ],
                    parent=peer,
                )
                bindings = [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in received]
                self.fd_bindings = dict(zip(("nested", "child"), bindings, strict=True))
                self.fds = dict(zip(("nested", "child"), received, strict=True))
                received = []  # ownership transferred atomically; later errors still owe outer cleanup
                self.metadata = {k: body[k] for k in ("nested", "child", "job_root")}
                connection.sendall(b"owned")
        except Exception as exc:  # noqa: BLE001 - reported by state()/outer cleanup, never a PASS
            self.problem = f"fixture ownership handshake failed: {type(exc).__name__}: {exc}"
        finally:
            for fd in received:
                os.close(fd)

    def _held_fd(self, name):
        if name not in self.fds:
            raise AssertionError(self.problem or "fixture ownership handshake never completed")
        fd = self.fds[name]
        stat = os.fstat(fd)
        assert (stat.st_dev, stat.st_ino) == self.fd_bindings[name], "owned descriptor changed"
        return fd

    def state(self, name):
        poller = select.poll()
        poller.register(self._held_fd(name), select.POLLIN)
        events = poller.poll(0)
        assert not any(flags & (select.POLLERR | select.POLLNVAL) for _, flags in events)
        return "gone" if any(flags & select.POLLIN for _, flags in events) else "held"

    def __exit__(self, *exc):
        original = exc[1] if len(exc) > 1 and isinstance(exc[1], BaseException) else None
        finish_nested_cleanup(self, original)

    def retry_cleanup(self):
        """Explicit same-owner retry, without refreshing the original wait allowance."""

        finish_nested_cleanup(self)

    def _cleanup(self):
        problems, actions = [], []
        self.cleanup_attempts += 1
        if self.cleanup_deadline is None:
            self.cleanup_deadline = time.monotonic() + 2.0
        self.stop.set()
        try:
            if self.listener is not None:
                self.listener.close()
        except Exception as error:  # noqa: BLE001 - later resources still owe cleanup
            problems.append(f"listener close failed: {type(error).__name__}: {error}")
        try:
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join(timeout=max(0.0, self.cleanup_deadline - time.monotonic()))
        except Exception as error:  # noqa: BLE001
            problems.append(f"receiver join failed: {type(error).__name__}: {error}")
        if self.thread is not None and self.thread.is_alive():
            problems.append("owned handshake thread did not stop within 2 seconds")
        else:
            for name in ("child", "nested"):
                if name not in self.fds:
                    continue
                fd = self.fds[name]
                try:
                    before = self.state(name)
                    if before == "held":
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    while self.state(name) != "gone" and time.monotonic() < self.cleanup_deadline:
                        time.sleep(0.01)
                    assert self.state(name) == "gone", "owned fixture survived outer cleanup"
                    try:
                        reaped = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
                        assert reaped is not None, "owned fixture reaping is still pending"
                        reaping = "outer waitid completed"
                    except ChildProcessError:
                        # Exit is already proven by this held pidfd. ECHILD proves
                        # there is no wait obligation for this test-process owner;
                        # an external parent may still own the exited zombie.
                        reaping = "ECHILD; no outer wait obligation"
                    assert self._held_fd(name) == fd, "descriptor changed before release"
                    os.close(fd)
                    del self.fds[name]
                    del self.fd_bindings[name]
                    actions.append(
                        {"name": name, "before": before, "gone": True, "reaping": reaping}
                    )
                except Exception as error:  # noqa: BLE001 - failed ownership is retained for explicit retry
                    problems.append(f"{name}: {type(error).__name__}: {error}")
        if not self.fds and (self.thread is None or not self.thread.is_alive()):
            try:
                if self.directory is not None:
                    self.directory.cleanup()
            except Exception as error:  # noqa: BLE001
                problems.append(f"directory cleanup failed: {type(error).__name__}: {error}")
        receipt = {
            "scope": "outer fixture cleanup, never product proof",
            "metadata": self.metadata,
            "handshake_problem": self.problem,
            "actions": actions,
            "problems": problems,
            "retained_instances": sorted(self.fds),
            "cleanup_deadline_monotonic": self.cleanup_deadline,
            "attempt": self.cleanup_attempts,
        }
        try:
            with (self.root / f"outer-nested-cleanup-attempt-{self.cleanup_attempts}.json").open(
                "x"
            ) as stream:
                json.dump(receipt, stream)
            (self.root / "outer-nested-cleanup.json").write_text(json.dumps(receipt))
        except Exception as error:  # noqa: BLE001 - restoration in __exit__ must still run
            problems.append(f"cleanup receipt write failed: {type(error).__name__}: {error}")
        if problems:
            raise AssertionError("; ".join(problems))
