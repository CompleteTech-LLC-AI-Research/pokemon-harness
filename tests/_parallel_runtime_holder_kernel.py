"""Controlled kernel observations for executing the holder's actual producer code.

The tests compile selected AST statements from HOLDER_SOURCE rather than duplicating
its decisions. Every process, signal, proc file and wait is supplied by this model;
it never launches or signals a real process. Real lineage still needs separate tests.
"""

import ast
import ctypes
import hashlib
import hmac
import io
import json
import signal
from types import SimpleNamespace

from scripts.production_gate_parallel_holder import HOLDER_SOURCE


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Kernel:
    """Only H's direct children are visible; a killed nested child can be reparented."""

    def __init__(self, children=(), *, listing="normal", unpinnable=False, nested=False):
        self.clock = Clock()
        self.children = {pid: True for pid in children}
        self.listing, self.unpinnable, self.nested = listing, unpinnable, nested
        self.signals, self.reaped, self.closed = [], [], []
        self.exhausted = True

    def stat(self, pid, field):
        if field == 19:
            return pid + 10
        return 4242 if pid in self.children else 100

    def open(self, path, *args, **kwargs):
        if path.endswith("/children"):
            if self.listing == "failed":
                raise PermissionError("controlled proc failure")
            return io.StringIO("" if self.listing == "empty" else " ".join(map(str, self.children)))
        return open(path, *args, **kwargs)

    def pidfd_open(self, pid):
        if self.unpinnable:
            raise PermissionError("controlled pidfd refusal")
        return pid + 10000

    def send(self, fd, signum):
        pid = fd - 10000
        assert pid in self.children, "producer signalled a process not owned by this holder"
        self.signals.append((pid, signum))
        if signum == signal.SIGKILL:
            self.children[pid] = False
            if self.nested and pid == 7001:
                self.children[7002] = True

    def waitid(self, kind, value, options):
        if kind == 3:  # fake P_PIDFD
            pid = value - 10000
            assert self.children[pid] is False
            del self.children[pid]
            self.reaped.append(pid)
            return SimpleNamespace(si_pid=pid)
        if not self.children and self.exhausted:
            raise ChildProcessError("controlled ECHILD")
        dead = next((pid for pid, alive in self.children.items() if not alive), None)
        if dead is None:
            return None
        if not options & 4:  # fake WNOWAIT
            del self.children[dead]
            self.reaped.append(dead)
        return SimpleNamespace(si_pid=dead)

    def sweep(self):
        nodes = ast.parse(HOLDER_SOURCE).body
        start = next(
            i
            for i, node in enumerate(nodes)
            if isinstance(node, ast.FunctionDef) and node.name == "children_now"
        )
        end = next(
            i
            for i, node in enumerate(nodes)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "code" for t in node.targets)
        )
        state = {
            "guardian": {"pid": 5000, "start": 5010},
            "guardian_status": 0,
            "forks": [[5000, 5010]],
            "adopted": [],
            "killed": [],
            "reaped": [],
            "reaped_unheld": [],
            "unknown": [],
            "remaining": [],
            "incomplete": False,
            "echild": False,
            "never_started": False,
            "guardian_killed": False,
        }
        result = {}
        namespace = {
            "os": SimpleNamespace(
                listdir=lambda path: ["4242"],
                path=SimpleNamespace(exists=lambda p: True),
                pidfd_open=self.pidfd_open,
                close=self.closed.append,
                waitid=self.waitid,
                P_PIDFD=3,
                P_ALL=0,
                P_PID=1,
                WEXITED=1,
                WNOHANG=2,
                WNOWAIT=4,
            ),
            "open": self.open,
            "stat_field": self.stat,
            "me": 4242,
            "grace": 0.1,
            "guardian": SimpleNamespace(pid=5000, returncode=0),
            "state": state,
            "time": self.clock,
            "signal": SimpleNamespace(
                SIGTERM=signal.SIGTERM, SIGKILL=signal.SIGKILL, pidfd_send_signal=self.send
            ),
            "select": SimpleNamespace(
                select=lambda fds, w, e, timeout: (
                    [fd for fd in fds if not self.children.get(fd - 10000, False)],
                    [],
                    [],
                )
            ),
            "subreaper_on": lambda: True,
            "final": lambda **fields: result.update(dict(state, **fields)),
        }
        exec(  # noqa: S102 - execute fixed repository producer AST with controlled kernel doubles
            compile(
                ast.Module(body=nodes[start:end], type_ignores=[]), "holder-actual-sweep", "exec"
            ),
            namespace,
        )
        return result


def startup(tmp_path, *, owner_present=True, pdeath=True, subreaper=True):
    """Run actual startup through its pre-fork checks, with Popen prohibited."""

    nodes = ast.parse(HOLDER_SOURCE).body
    end = next(
        i
        for i, node in enumerate(nodes)
        if isinstance(node, ast.Try)
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Popen"
            for n in ast.walk(node)
        )
    )
    selected = [
        node
        for node in nodes[:end]
        if not isinstance(node, ast.Import)
        and not (isinstance(node, ast.FunctionDef) and node.name == "stat_field")
    ]
    flag, calls = ctypes.c_int(0), []

    def prctl(number, *args):
        calls.append(number)
        if number == 1:
            return 0 if pdeath else -1
        if number == 36:
            flag.value = int(subreaper)
            return 0 if subreaper else -1
        if number == 37:
            args[0]._obj.value = flag.value
            return 0
        raise AssertionError(number)

    argv = [
        "holder",
        str(tmp_path / "holder.json"),
        str(tmp_path / "guardian.json"),
        "0.1",
        "10",
        "11",
        "unused guardian source",
        "30",
        "100",
        "110",
        "--cgroup=",
        "worker",
    ]
    sys = SimpleNamespace(argv=argv, exit=lambda code: (_ for _ in ()).throw(SystemExit(code)))
    namespace = {
        "ctypes": SimpleNamespace(
            CDLL=lambda *a, **k: SimpleNamespace(prctl=prctl),
            c_int=ctypes.c_int,
            byref=ctypes.byref,
        ),
        "os": SimpleNamespace(
            getppid=lambda: 100 if owner_present else 1,
            getpid=lambda: 4242,
            read=lambda *a: b"6b" * 32,
            close=lambda fd: None,
            write=lambda *a: None,
            replace=__import__("os").replace,
            pipe=lambda: (20, 21),
        ),
        "sys": sys,
        "time": Clock(),
        "hashlib": hashlib,
        "hmac": hmac,
        "json": json,
        "signal": SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIGINT=signal.SIGINT,
            SIGHUP=signal.SIGHUP,
            signal=lambda *a: None,
        ),
        "stat_field": lambda pid, field: pid + 10 if field == 19 else None,
    }
    exec(  # noqa: S102 - execute fixed repository producer AST with controlled kernel doubles
        compile(ast.Module(body=selected, type_ignores=[]), "holder-actual-startup", "exec"),
        namespace,
    )
    return namespace, calls


def wedged_guardian_wait():
    """Execute the actual holder wait when TERM and KILL do not produce an exit."""

    nodes = ast.parse(HOLDER_SOURCE).body
    start = next(
        i
        for i, node in enumerate(nodes)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Tuple)
        and any(isinstance(t, ast.Name) and t.id == "sent_term" for t in node.targets[0].elts)
    )
    end = next(
        i
        for i, node in enumerate(nodes)
        if isinstance(node, ast.FunctionDef) and node.name == "children_now"
    )
    clock, calls, state = Clock(), [], {"guardian_killed": False}
    namespace = {
        "guardian": SimpleNamespace(
            poll=lambda: None,
            returncode=None,
            send_signal=lambda number: calls.append((number, clock.now)),
        ),
        "time": clock,
        "began": 0.0,
        "stopped": [signal.SIGTERM],
        "max_seconds": 30,
        "grace": 0.1,
        "signal": signal,
        "state": state,
    }
    exec(  # noqa: S102 - execute fixed repository producer AST with controlled kernel doubles
        compile(
            ast.Module(body=nodes[start:end], type_ignores=[]),
            "holder-actual-guardian-wait",
            "exec",
        ),
        namespace,
    )
    return state, calls, clock.now
