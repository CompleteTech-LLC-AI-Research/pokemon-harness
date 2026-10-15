"""Bounded Linux collection ownership; never proves descendant quiescence."""

from __future__ import annotations

import codecs
import errno
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

SUPPORTED_VERSIONS = {(3, 11, 2), (3, 11, 17), (3, 12, 13), (3, 12, 15)}
OWNERSHIP_POLICY = "linux-main-thread-exclusive-wait-owner"


class UnsupportedCollection(RuntimeError):
    """Collection was refused before child creation."""


class CollectionCleanupError(OSError):
    """Supported collection cleanup could not be completed."""


class CancellationScope:
    """Recording handlers remain active through report and temporary cleanup.

    The declaration excludes native reapers/auto-reap and concurrent policy
    mutation. Python inspection alone cannot verify those embedding promises.
    """

    def __init__(self, policy):
        self.policy = policy
        self.saved = {}
        self.first_signal = None
        self.signal_count = 0
        self.primary = None
        self.traceback = None
        self.secondary = []
        self.handler = self._record
        self.installed = []

    def _record(self, signum, _frame):
        if self.first_signal is None:
            self.first_signal = signum
        self.signal_count = min(2, self.signal_count + 1)

    def select(self, exc):
        if self.primary is None:
            self.primary, self.traceback = exc, exc.__traceback__
        elif exc is not self.primary and len(self.secondary) < 16:
            self.secondary.append(f"{type(exc).__name__}: {str(exc)[:500]}")

    def checkpoint(self, *, raise_primary=True):
        if self.first_signal is not None and self.primary is None:
            self.select(
                KeyboardInterrupt() if self.first_signal == signal.SIGINT else SystemExit(143)
            )
        if raise_primary and self.primary is not None:
            raise self.primary.with_traceback(self.traceback)

    def _admit(self):
        if self.policy != OWNERSHIP_POLICY:
            raise UnsupportedCollection(
                "Collection requires an explicit exclusive-wait ownership declaration."
            )
        if sys.platform != "linux" or sys.implementation.name != "cpython":
            raise UnsupportedCollection(
                "Standalone collection supports Linux CPython only; no child was started."
            )
        if tuple(sys.version_info[:3]) not in SUPPORTED_VERSIONS:
            raise UnsupportedCollection(
                "This supervisor version is not admitted for standalone collection."
            )
        if (
            threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            raise UnsupportedCollection(
                "Collection requires the main thread without other Python threads."
            )
        if not hasattr(selectors, "DefaultSelector"):
            raise UnsupportedCollection("Nonblocking collection I/O is unavailable.")
        if any(
            not hasattr(os, name)
            for name in (
                "waitid",
                "P_PID",
                "WEXITED",
                "WNOHANG",
                "WNOWAIT",
                "set_blocking",
                "waitpid",
                "waitstatus_to_exitcode",
            )
        ):
            raise UnsupportedCollection(
                "Non-reaping collection ownership is unavailable on this platform."
            )
        required = {
            signal.SIGINT: signal.default_int_handler,
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGCHLD: signal.SIG_DFL,
        }
        if any(signal.getsignal(sig) != handler for sig, handler in required.items()):
            raise UnsupportedCollection(
                "Collection requires default INT/TERM/CHLD policy and no external child reaper."
            )

    def __enter__(self):
        self._admit()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                self.saved[sig] = signal.getsignal(sig)
                signal.signal(sig, self.handler)
                self.installed.append(sig)
        except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
            self.select(exc)
            self._restore()
            raise self.primary.with_traceback(self.traceback)
        return self

    def policy_intact(self):
        return (
            threading.active_count() == 1
            and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
            and all(signal.getsignal(sig) == self.handler for sig in self.installed)
            and len(self.installed) == 2
        )

    def _restore(self):
        for sig in self.installed:
            try:
                signal.signal(sig, self.saved[sig])
            except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
                self.select(exc)
        self.installed.clear()

    def __exit__(self, _kind, exc, _tb):
        if exc is not None:
            self.select(exc)
        self.checkpoint(raise_primary=False)
        self._restore()
        # Account for a recording-handler cancellation at the restore boundary.
        self.checkpoint(raise_primary=False)
        if self.primary is not None:
            try:
                self.primary.add_note(
                    "Collection cancellation/cleanup: "
                    + repr(
                        {
                            "signal": self.first_signal,
                            "repeated": self.signal_count > 1,
                            "secondary": self.secondary,
                        }
                    )
                )
            except BaseException:  # noqa: BLE001 - evidence must not replace the primary
                self.note_failed = True
            raise self.primary.with_traceback(self.traceback)
        return False


class OwnedCollection:
    """Own one successful new-session spawn, its pipe and all wait operations.

    Five seconds bounds cleanup-monitor scheduling, not constructor, report,
    filesystem or uninterruptible kernel operations. No arbitrary Popen/PID can
    be converted to group-signalling authority through this class's API.
    """

    def __init__(self, scope):
        self.scope = scope
        self.process = None
        self.selector = None
        self.pipe_ready = False
        self.pipe_failed = False
        self.tail = bytearray()
        self.signals_enabled = False
        self.cleanup_started = False
        self.result = {
            "ownership": "NONE_PROVEN",
            "leader_terminal": False,
            "leader_reaped": False,
            "pipe_eof": False,
            "pipe_closed": False,
            "bytes_seen": 0,
            "truncated": False,
            "group_quiescence": "UNPROVEN",
            "actions": [],
            "errors": [],
            "cleanup_status": "NOT_NEEDED",
            "recovery_required": False,
        }

    def error(self, exc):
        if len(self.result["errors"]) < 16:
            self.result["errors"].append(f"{type(exc).__name__}: {str(exc)[:500]}")
        self.scope.select(exc)

    def _identity(self):
        if not self.signals_enabled or self.result["ownership"] not in (
            "OWNED_UNREAPED",
            "TERMINAL_UNREAPED",
        ):
            return False
        try:
            if self.process.returncode is not None or not self.scope.policy_intact():
                raise CollectionCleanupError("Collection child ownership policy changed.")
            observed = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if observed is not None:
                if observed.si_pid != self.process.pid:
                    raise CollectionCleanupError("Unexpected collection child identity.")
                self.result["leader_terminal"] = True
                self.result["ownership"] = "TERMINAL_UNREAPED"
                if observed.si_code != os.CLD_EXITED or observed.si_status != 0:
                    self.scope.select(
                        CollectionCleanupError(
                            f"Collection child exited unsuccessfully: code={observed.si_code}, status={observed.si_status}."
                        )
                    )
            return True
        except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
            self.signals_enabled = False
            self.result["ownership"] = "OWNERSHIP_UNKNOWN"
            self.error(exc)
            return False

    def _send(self, sig):
        if not self._identity():
            self.error(
                CollectionCleanupError("Required collection group action lacks owned identity.")
            )
            return
        action = {"signal": sig.name, "pgid": self.process.pid}
        try:
            os.killpg(self.process.pid, sig)
            action["result"] = "action_delivered"
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                action["result"] = "absent_at_observation"
            else:
                action["result"] = "failed"
                self.error(exc)
        except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
            action["result"] = "failed"
            self.error(exc)
        self.result["actions"].append(action)

    def _read_once(self, endpoint):
        remaining = max(0.0, endpoint - time.monotonic())
        if self.selector is None or not self.pipe_ready or self.result["pipe_eof"]:
            time.sleep(min(0.05, remaining))
            return
        for key, _events in self.selector.select(min(0.05, remaining)):
            try:
                chunk = os.read(key.fd, 32768)
            except BlockingIOError:
                return
            if not chunk:
                self.result["pipe_eof"] = True
                self.selector.unregister(key.fd)
                return
            self.result["bytes_seen"] += len(chunk)
            self.tail.extend(chunk)
            if len(self.tail) > 16384:
                del self.tail[:-16384]
                self.result["truncated"] = True
            return  # One bounded read, then clock/cancellation/identity checkpoint.

    def _reap(self):
        if (
            self.result["leader_reaped"]
            or not self.result["leader_terminal"]
            or self.result["ownership"] not in ("OWNED_UNREAPED", "TERMINAL_UNREAPED")
        ):
            return
        try:
            pid, status = os.waitpid(self.process.pid, os.WNOHANG)
            if pid == self.process.pid:
                self.process.returncode = os.waitstatus_to_exitcode(status)
                self.result.update(
                    leader_reaped=True, ownership="REAPED", returncode=self.process.returncode
                )
            elif pid != 0:
                raise CollectionCleanupError("Unattributed collection child reap.")
        except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
            self.result["ownership"] = "OWNERSHIP_UNKNOWN"
            self.error(exc)

    def _close(self):
        for item in (self.selector, getattr(self.process, "stdout", None)):
            if item is not None:
                try:
                    item.close()
                except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
                    self.error(exc)
        self.result["pipe_closed"] = getattr(getattr(self.process, "stdout", None), "closed", False)

    def _cleanup(self):
        if self.cleanup_started:
            return
        self.cleanup_started = True
        c = time.monotonic()
        g, k, d = c + 1.0, c + 2.0, c + 5.0
        self.result["deadlines"] = {"start": c, "grace": g, "escalation": k, "final": d}
        try:
            self._send(signal.SIGTERM)
            while time.monotonic() < g:
                self.scope.checkpoint(raise_primary=False)
                self._identity()
                try:
                    self._read_once(g)
                except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
                    self.error(exc)
                    self.pipe_ready = False
                    self.pipe_failed = True
            if time.monotonic() < k:
                self._send(signal.SIGKILL)
            else:
                self.error(CollectionCleanupError("Collection escalation deadline exhausted."))
        except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
            self.error(exc)
        finally:
            # No action, including report failure, can signal after this point.
            self.signals_enabled = False
        while time.monotonic() < d:
            self.scope.checkpoint(raise_primary=False)
            # A pipe failure cannot revoke identity or consume the reap chance.
            if self.result["ownership"] in ("OWNED_UNREAPED", "TERMINAL_UNREAPED"):
                try:
                    if not self.scope.policy_intact():
                        raise CollectionCleanupError("Collection wait ownership policy changed.")
                    seen = os.waitid(
                        os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    )
                    if seen is not None:
                        if seen.si_pid != self.process.pid:
                            raise CollectionCleanupError("Unattributed final child observation.")
                        self.result["leader_terminal"] = True
                    self._reap()
                except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
                    self.error(exc)
                    self.result["ownership"] = "OWNERSHIP_UNKNOWN"
            leader_done = (
                self.result["leader_reaped"] or self.result["ownership"] == "OWNERSHIP_UNKNOWN"
            )
            pipe_done = self.result["pipe_eof"] or self.pipe_failed
            if leader_done and pipe_done:
                break
            try:
                self._read_once(d)
            except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
                self.error(exc)
                self.pipe_ready = False
                self.pipe_failed = True
        if not self.result["leader_reaped"]:
            self.error(
                CollectionCleanupError("Collection leader remains unresolved at cleanup deadline.")
            )
        if not self.result["pipe_eof"]:
            self.error(
                CollectionCleanupError("Collection output did not reach EOF during cleanup.")
            )
        self._close()
        self.result["cleanup_status"] = (
            "FAILED" if self.result["errors"] else "SUPPORTED_ACTIONS_COMPLETE"
        )
        self.result["recovery_required"] = (
            bool(self.result["errors"]) or self.scope.signal_count > 1
        )

    def run(self, command, *, cwd, env, timeout):
        self.scope.checkpoint()
        if not 0 < timeout < float("inf"):
            raise UnsupportedCollection("Collection timeout must be finite and positive.")
        if self.process is not None:
            raise UnsupportedCollection("A collection lifetime cannot launch another child.")
        self.result["ownership"] = "LAUNCHING"
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
            )
        except BaseException as exc:
            self.result.update(
                ownership="CREATION_UNKNOWN", cleanup_status="UNPROVEN", recovery_required=True
            )
            self.scope.select(exc)
            raise
        self.result.update(
            ownership="OWNED_UNREAPED", pid=self.process.pid, original_pgid=self.process.pid
        )
        self.signals_enabled = True
        try:
            self.scope.checkpoint()
            end = time.monotonic() + timeout
            os.set_blocking(self.process.stdout.fileno(), False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.pipe_ready = True
            while True:
                self.scope.checkpoint()
                if not self._identity():
                    self.scope.checkpoint()
                if self.result["leader_terminal"]:
                    break
                if time.monotonic() >= end:
                    raise subprocess.TimeoutExpired(command, timeout)
                self._read_once(end)
        except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
            self.scope.select(exc)
        finally:
            try:
                self._cleanup()
            except BaseException as exc:  # noqa: BLE001 - preserve primary through owned cleanup
                self.error(exc)
                self.signals_enabled = False
                self._close()
                self.result.update(cleanup_status="FAILED", recovery_required=True)
        self.scope.checkpoint()
        return self.process.returncode

    def evidence(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(bytes(self.tail), final=True)
        self.result["output_tail"] = text[-4000:]
        self.result["decoding_replaced"] = "\ufffd" in text
        self.result["signal"] = self.scope.first_signal
        self.result["repeated_cancellation"] = self.scope.signal_count > 1
        if self.scope.signal_count > 1:
            self.result["recovery_required"] = True
        return self.result


def reap_leader_only(process, deadline=None):
    """Deprecated bool adapter: attributed leader reap only, NEVER signals."""
    end = time.monotonic() + 5.0 if deadline is None else deadline
    if not isinstance(end, (int, float)) or not math.isfinite(end):
        return False
    if not isinstance(getattr(process, "pid", None), int) or process.pid <= 0:
        return False
    try:
        while time.monotonic() < end:
            pid, status = os.waitpid(process.pid, os.WNOHANG)
            if pid == process.pid:
                process.returncode = os.waitstatus_to_exitcode(status)
                return True
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
    except (OSError, AttributeError, ValueError):
        return False
    finally:
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    return False
