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
from tests._parallel_runtime_nested_startup import NESTED_CHILD_SOURCE


class NestedCleanup:
    def __init__(
        self, root, source, *, startup_mode="normal", handoff_pause=None, reject_handoff=False
    ):
        self.root, self.source = Path(root), source
        self.token = secrets.token_hex(32)
        self.stop = threading.Event()
        self.fds, self.metadata, self.problem = {}, {}, ""
        self.adoption = OrchestratorAdoption()
        self.startup_mode, self.handoff_pause = startup_mode, handoff_pause
        self.reject_handoff = reject_handoff

    def __enter__(self):
        self.directory = self.listener = self.thread = None
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
            try:
                self._cleanup()
            except Exception as cleanup_error:  # noqa: BLE001 - retain both failures
                original.add_note(f"outer cleanup also failed: {cleanup_error}")
            finally:
                self.adoption.__exit__()
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
                self.fds = dict(zip(("nested", "child"), received, strict=True))
                received = []  # ownership transferred atomically; later errors still owe outer cleanup
                self.metadata = {k: body[k] for k in ("nested", "child", "job_root")}
                connection.sendall(b"owned")
        except Exception as exc:  # noqa: BLE001 - reported by state()/outer cleanup, never a PASS
            self.problem = f"fixture ownership handshake failed: {type(exc).__name__}: {exc}"
        finally:
            for fd in received:
                os.close(fd)

    def state(self, name):
        if name not in self.fds:
            raise AssertionError(self.problem or "fixture ownership handshake never completed")
        poller = select.poll()
        poller.register(self.fds[name], select.POLLIN)
        events = poller.poll(0)
        assert not any(flags & (select.POLLERR | select.POLLNVAL) for _, flags in events)
        return "gone" if any(flags & select.POLLIN for _, flags in events) else "held"

    def __exit__(self, *exc):
        try:
            self._cleanup()
        finally:
            self.adoption.__exit__(*exc)

    def _cleanup(self):
        problems, actions = [], []
        self.stop.set()
        try:
            if self.listener is not None:
                self.listener.close()
        except Exception as error:  # noqa: BLE001 - later resources still owe cleanup
            problems.append(f"listener close failed: {type(error).__name__}: {error}")
        try:
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join(timeout=2.0)
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
                    limit = time.monotonic() + 2.0
                    while self.state(name) != "gone" and time.monotonic() < limit:
                        time.sleep(0.01)
                    assert self.state(name) == "gone", "owned fixture survived outer cleanup"
                    actions.append({"name": name, "before": before, "gone": True})
                except Exception as error:  # noqa: BLE001 - keep cleanup errors separate from product facts
                    problems.append(f"{name}: {type(error).__name__}: {error}")
            for name, fd in self.fds.items():
                try:
                    os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
                except ChildProcessError:
                    pass  # its owning product holder already reaped it, or still owns the zombie
                except OSError as error:
                    problems.append(f"{name}: wait failed: {error}")
                finally:
                    try:
                        os.close(fd)
                    except OSError as error:
                        problems.append(f"{name}: descriptor close failed: {error}")
            self.fds.clear()  # no stale numeric descriptors remain eligible for later cleanup
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
        }
        try:
            (self.root / "outer-nested-cleanup.json").write_text(json.dumps(receipt))
        except Exception as error:  # noqa: BLE001 - restoration in __exit__ must still run
            problems.append(f"cleanup receipt write failed: {type(error).__name__}: {error}")
        if problems:
            raise AssertionError("; ".join(problems))
