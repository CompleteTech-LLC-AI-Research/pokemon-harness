"""Actual B2-L local custody controls, source authored only: ALL NOTRUN."""

import errno
import inspect
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_arbiter as arbiter
from scripts import production_gate_parallel_handler_custody as generic
from scripts import production_gate_parallel_handoff_custody as custody
from scripts import production_gate_parallel_job_lifecycle as life
from scripts import production_gate_parallel_ports as ports
from tests import test_parallel_runtime_job_publication as publication_controls
from tests import test_parallel_runtime_owner_service as owner_controls

publication = publication_controls.publication


class GuardedLock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held
        self.held = True

    def __exit__(self, *args):
        self.held = False


@pytest.fixture
def controlled(monkeypatch):
    c = SimpleNamespace(
        now=10.0,
        events=[],
        brokers=[],
        servers=[],
        roots=[],
        conns=[],
        threads=[],
        queue=[],
        messages={},
        errors=[],
        wrap_error=None,
        ctor_error=None,
        config_error=None,
        config_exception=OSError,
        bind_attempts=[],
        close_error=None,
        bind_errors=[],
        start_mode="fast",
        join_error=None,
        accept_join_error=None,
        send_error=None,
        short=False,
        send_before=False,
        start_hook=None,
        construct_hook=None,
        bind_hook=None,
        in_accept=False,
        join_hook=None,
        next_port=9000,
    )

    def effect(name, *args):
        assert not any(b._book.lock.held for b in c.brokers), (name, "effect under lock")
        c.events.append((name, *args))

    def clock():
        effect("clock")
        return c.now

    class Socket:
        family, type, proto = 1, 5, 0

        def __init__(self, kind, fd=None):
            self.kind, self.fd, self.closes, self.accepts, self.port = kind, fd, 0, 0, None
            {"server": c.servers, "root": c.roots, "conn": c.conns}[kind].append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def setsockopt(self, *args):
            effect("setsockopt", self.kind)
            if c.config_error == "setsockopt":
                raise c.config_exception("controlled configuration")

        def bind(self, address):
            effect("bind", self.kind)
            if self.kind == "root":
                self.port = address[1] or c.next_port
                c.bind_attempts.append(self.port)
                c.next_port += 1
                assert any(x.sock is self for b in c.brokers for x in b._book.leases.values())
                if c.bind_hook:
                    c.bind_hook()
                if c.bind_errors:
                    raise OSError(c.bind_errors.pop(0), "controlled bind")
            if c.config_error == "bind":
                raise c.config_exception("controlled configuration")

        def listen(self, count):
            effect("listen", self.kind)
            if self.kind == "root" and c.config_error == "listen":
                raise c.config_exception("controlled configuration")

        def getsockname(self):
            effect("getsockname", self.kind)
            if c.config_error == "getsockname":
                raise c.config_exception("controlled configuration")
            return ("127.0.0.1", self.port)

        def settimeout(self, seconds):
            effect("timeout", self.kind, seconds)

        def getsockopt(self, *args):
            effect("peer", self.kind)
            return b"controlled peer"

        def fileno(self):
            effect("fileno", self.kind)
            assert self.closes == 0
            return 800 + c.roots.index(self)

        def _accept(self):
            effect("accept")
            self.accepts += 1
            if not c.queue:
                next(b for b in c.brokers if b._server is self)._closing.set()
                raise TimeoutError
            value = c.queue.pop(0)
            value = value() if callable(value) else value
            if isinstance(value, BaseException):
                raise value
            return value

        def recv(self, limit):
            effect("recv", self.fd)
            values = c.messages.get(self.fd, [])
            value = values.pop(0) if values else b""
            value = value() if callable(value) else value
            if isinstance(value, BaseException):
                raise value
            return value

        def send(self, data):
            effect("send", self.fd, data)
            return len(data)

        def sendmsg(self, buffers, ancillary):
            effect("send_effect", self.fd, ancillary)
            if c.send_error:
                raise c.send_error("after rights-send effect")
            return 1 if c.short else len(buffers[0])

        def close(self):
            effect("close", self.kind)
            self.closes += 1
            if c.close_error == self.kind:
                raise OSError("after controlled close effect")

    def factory(*args, fileno=None):
        effect("construct", fileno)
        if fileno is not None:
            if c.wrap_error:
                raise c.wrap_error("wrapper did not return")
            return Socket("conn", fileno)
        if args[0] == ports.socket.AF_UNIX:
            return Socket("server")
        if c.ctor_error:
            raise c.ctor_error("root constructor did not return")
        return Socket("root")

    class Thread:
        def __init__(self, *, target, name, daemon):
            effect("thread_construct", name)
            if name.startswith("arbiter-handler") and c.construct_hook:
                c.construct_hook()
            self.target, self.name = target, name
            self.started = self.alive = self.ran = self.keep_alive = False
            self.ident = None
            c.threads.append(self)

        def start(self):
            effect("start", self.name)
            handler = self.name.startswith("arbiter-handler")
            if handler and c.start_hook:
                c.start_hook()
            if handler and c.start_mode == "before":
                raise RuntimeError("before start effect")
            self.started = self.alive = True
            self.ident = 1
            effect("start_effect", self.name)
            if handler and c.start_mode != "defer":
                self.run()
            if handler and c.start_mode == "after":
                raise RuntimeError("after start effect")

        def run(self, *, propagate=False):
            assert self.started and not self.ran
            self.ran = True
            old, c.in_accept = c.in_accept, self.name == "listener-broker"
            try:
                self.target()
            except BaseException as error:
                c.errors.append(error)
                if propagate:
                    raise
            finally:
                c.in_accept, self.alive = old, False

        def join(self, *, timeout):
            effect("join", self.name, timeout)
            assert self.started, "never-started Thread must not be joined"
            if c.join_hook:
                c.join_hook()
            error = c.accept_join_error if self.name == "listener-broker" else c.join_error
            if error:
                raise error("controlled join uncertainty")
            if self.name == "listener-broker" and not self.ran and not c.in_accept:
                owner = next(b for b in c.brokers if b._thread is self)
                assert owner._server.closes == 0
                self.run()

        def is_alive(self):
            effect("alive", self.name)
            return self.alive or self.keep_alive

    def send_fds(conn, buffers, descriptors):
        effect("send_fds", tuple(descriptors))
        if c.send_before:
            raise OSError("before send effect")
        return conn.sendmsg(buffers, [("controlled_rights", descriptors)])

    monkeypatch.setattr(ports, "_SOCKET", factory)
    monkeypatch.setattr(ports, "threading", SimpleNamespace(Thread=Thread, Event=threading.Event))
    monkeypatch.setattr(ports.socket, "send_fds", send_fds)
    monkeypatch.setattr(generic, "os", SimpleNamespace(close=lambda fd: effect("raw_close", fd)))
    monkeypatch.setattr(ports.ListenerBroker, "_same_user", staticmethod(lambda conn: True))

    def make(**kwargs):
        broker = ports.ListenerBroker(clock=clock, lease_limit=2, handler_limit=2, **kwargs)
        guard = GuardedLock()
        broker._connections.lock = broker._book.lock = broker._lock = guard
        broker._startup_deadline, broker._startup_clock = 20.0, clock
        c.brokers.append(broker)
        broker.start()
        return broker

    c.broker = make()
    c.pool, c.book = c.broker._connections, c.broker._book

    def accept(*values):
        c.queue[:] = values
        c.broker._thread.run(propagate=True)

    def request(port, answer=b'{"op":"ack"}', fd=71):
        c.messages[fd] = [
            json.dumps({"op": "take", "host": "127.0.0.1", "port": port}).encode(),
            answer,
        ]
        accept((fd, None))

    c.accept, c.request, c.make, c.effect, c.Socket = accept, request, make, effect, Socket
    return c


def test_actual_before_accept_quota_and_prompt_close(controlled):
    c = controlled
    c.accept((71, None), (72, None), (73, None))
    assert c.servers[0].accepts == 2 and len(c.pool.records) == 2
    assert all(conn.closes == 1 for conn in c.conns) and c.servers[0].closes == 0
    c.broker.service()
    assert not c.pool.records and len(c.pool.history) <= c.pool.limit
    assert "quota" in c.book.first_reason


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_return_before_wrap_and_no_return_unknown(controlled, error):
    c = controlled
    c.wrap_error = error
    with pytest.raises(error):
        c.accept((71, "returned"))
    record = next(iter(c.pool.records.values()))
    assert record.raw_result == (71, "returned") and record.descriptor == 71
    c.broker.close()
    assert not c.broker.startup_disposed and not c.conns
    assert not any(e[0] == "raw_close" for e in c.events)


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_no_return_accept_has_no_join_or_close(controlled, error):
    c = controlled
    if error is KeyboardInterrupt:
        with pytest.raises(error):
            c.accept(error("no return"))
    else:
        c.accept(error("no return"))
    c.broker.close()
    assert c.pool.accept_unknown and c.servers[0].closes == 0
    assert not any(e[0] == "join" for e in c.events)
    assert next(iter(c.pool.records.values())).raw_result is None


@pytest.mark.parametrize("stage", ["setsockopt", "bind", "listen", "getsockname"])
def test_actual_lease_return_retained_before_fallible_configuration(controlled, stage):
    c = controlled
    c.config_error = stage
    with pytest.raises(OSError):
        c.broker.lease()
    lease = next(iter(c.book.leases.values()))
    assert lease.sock is c.roots[0] and lease.close_state == "open"
    c.broker.close()
    assert c.roots[0].closes == 0 and c.book.operations and not c.broker.startup_disposed


def test_actual_constructor_no_return_never_fabricates_socket(controlled):
    c = controlled
    c.ctor_error = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        c.broker.lease()
    lease = next(iter(c.book.leases.values()))
    assert lease.sock is None and not c.roots and c.book.operations
    c.broker.close()
    assert not c.broker.startup_disposed


@pytest.mark.parametrize("close_failure", [False, True, 20.0, 20.1])
def test_actual_bind_conflict_advances_only_after_checked_close(controlled, close_failure):
    c = controlled
    c.broker.first_port, c.broker.count = 9000, 2
    c.bind_errors = [errno.EADDRINUSE]
    if type(close_failure) is float:
        c.bind_hook = lambda: setattr(c, "now", close_failure)
        with pytest.raises(TimeoutError):
            c.broker.lease()
        assert len(c.roots) == 1 and c.roots[0].closes == 0
        assert c.book.operations and c.book.leases and c.book.first_reason
        return
    c.close_error = "root" if close_failure else None
    if close_failure:
        with pytest.raises(OSError):
            c.broker.lease()
        assert len(c.roots) == 1 and c.roots[0].closes == 1
        c.close_error = None
        c.broker.close()
        assert c.roots[0].closes == 1 and not c.broker.startup_disposed
    else:
        assert c.broker.lease() == 9001
        assert len(c.roots) == 2 and c.roots[0].closes == 1
        assert c.events.index(("close", "root")) < c.events.index(("listen", "root"))


def test_actual_direct_lease_stop_barrier_closes_only_after_last_use(controlled):
    c = controlled
    c.bind_hook = c.broker.close
    with pytest.raises(custody.Refused):
        c.broker.lease()
    assert c.roots[0].closes == 1 and not c.book.operations
    c.broker.close()
    assert c.broker.startup_disposed and c.roots[0].closes == 1


@pytest.mark.parametrize("mode", ["before", "after"])
def test_actual_start_uncertainty_keeps_original_and_never_rejoins(controlled, mode):
    c = controlled
    c.start_mode = mode
    with pytest.raises(RuntimeError):
        c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    assert record.start_state == "unknown" and record.thread in c.threads
    c.broker.service()
    c.broker.close()
    assert not c.broker.startup_disposed
    assert not any(e[0] == "join" and "handler" in e[1] for e in c.events)
    assert len([e for e in c.events if e[0] == "start_effect" and "handler" in e[1]]) == (
        mode == "after"
    )


@pytest.mark.parametrize("stop_first", [True, False])
def test_actual_stop_start_claim_has_one_winner(controlled, stop_first):
    c = controlled
    if stop_first:
        c.construct_hook = c.broker.close
    else:
        c.start_hook = c.broker.close
    c.accept((71, None))
    handler = next(t for t in c.threads if "handler" in t.name)
    assert handler.started is (not stop_first)
    assert c.conns[0].closes == 1 and c.servers[0].closes == 0


@pytest.mark.parametrize("failure", ["before", "after", "short"])
def test_actual_send_effect_unknown_never_reoffers_or_closes_under_use(controlled, failure):
    c = controlled
    port = c.broker.lease()
    c.send_before, c.short = failure == "before", failure == "short"
    c.send_error = KeyboardInterrupt if failure == "after" else None
    c.request(port)
    lease = next(iter(c.book.leases.values()))
    assert lease.unknown and c.book.operations and c.roots[0].closes == 0
    assert len([e for e in c.events if e[0] == "send_fds"]) == 1
    with pytest.raises(custody.Refused):
        c.broker._take(c.Socket("conn", 91), {"host": "127.0.0.1", "port": port})
    c.broker.close()
    assert c.roots[0].closes == 0 and not c.broker.startup_disposed
    assert len([e for e in c.events if e[0] == "send_fds"]) == 1


@pytest.mark.parametrize("answer", [b'{"op":"nack"}', b"{}", b"bad", b"", TimeoutError("ACK")])
def test_actual_bad_ack_refuses_second_offer_and_retains_root(controlled, answer):
    c = controlled
    port = c.broker.lease()
    c.request(port, answer)
    assert c.broker.state(port) == "unacknowledged"
    assert c.roots[0].closes == 0 and c.book.operations
    c.broker.close()
    assert not c.broker.startup_disposed and len(c.book.leases) == 1
    assert len([e for e in c.events if e[0] == "send_effect"]) == 1


@pytest.mark.parametrize("now", [20.0, 21.0])
def test_actual_ack_after_original_bound_cannot_upgrade(controlled, now):
    c = controlled
    port = c.broker.lease()

    def late_ack():
        c.now = now
        return b'{"op":"ack"}'

    c.request(port, late_ack)
    lease = next(iter(c.book.leases.values()))
    assert lease.unknown and lease.state == "sent_await_ack" and c.roots[0].closes == 0
    assert not c.broker.startup_disposed


def test_actual_positive_ack_same_record_sendmsg_singlecloser_and_final_retirement(controlled):
    c = controlled
    port = c.broker.lease()
    c.request(port)
    record = next(iter(c.pool.records.values()))
    assert record.connection is c.conns[0] and record.close_state == "closed"
    assert c.roots[0].closes == c.conns[0].closes == 1
    assert not c.book.leases and c.broker.state(port) == "acknowledged"
    c.broker.close()
    assert c.broker.startup_disposed and c.servers[0].closes == 1
    joins = [e for e in c.events if e[0] == "join"]
    assert joins[0][1] == "listener-broker" and len(joins) == 2
    assert c.events.index(joins[0]) < c.events.index(("close", "server"))
    c.broker.close()
    assert all(s.closes == 1 for s in c.roots + c.conns + c.servers)


@pytest.mark.parametrize("operation", ["ack", "release"])
def test_actual_root_close_after_effect_is_singleuse(controlled, operation):
    c = controlled
    port = c.broker.lease()
    c.close_error = "root"
    if operation == "ack":
        c.request(port)
    else:
        with pytest.raises(OSError):
            c.broker._release(port)
    lease = next(iter(c.book.leases.values()))
    assert lease.close_state == "unknown" and c.roots[0].closes == 1
    c.close_error = None
    c.broker.close()
    with pytest.raises(custody.Refused):
        c.broker._release(port)
    assert c.roots[0].closes == 1 and not c.broker.startup_disposed


def test_actual_root_close_success_before_commit_interruption_has_no_second_effect(controlled):
    c = controlled
    port = c.broker.lease()
    source, line = inspect.getsourcelines(custody.HandoffBook.close_lease)
    target = line + next(
        i for i, text in enumerate(source) if 'lease.close_state = "closed"' in text
    )
    fired = []

    def trace(frame, event, arg):
        if (
            event == "line"
            and frame.f_code is custody.HandoffBook.close_lease.__code__
            and frame.f_lineno == target
        ):
            fired.append(True)
            sys.settrace(None)
            raise KeyboardInterrupt("after close before commit")
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(KeyboardInterrupt):
            c.broker._release(port)
    finally:
        sys.settrace(previous)
    lease = next(iter(c.book.leases.values()))
    assert fired and lease.close_state == "closing" and c.roots[0].closes == 1
    c.broker.close()
    assert not c.broker.startup_disposed and c.roots[0].closes == 1


def test_actual_release_positive_and_countzero_history_bounded(controlled):
    c = controlled
    for _ in range(7):
        port = c.broker.lease()
        c.broker._release(port)
    assert not c.book.leases and not c.book.operations and len(c.book.history) == c.book.limit
    assert all(sock.closes == 1 for sock in c.roots)
    c.broker.close()
    assert c.broker.startup_disposed


def test_actual_lease_quota_refuses_before_next_socket(controlled):
    c = controlled
    c.broker.lease()
    c.broker.lease()
    with pytest.raises(custody.Refused):
        c.broker.lease()
    assert len(c.roots) == 2 and len(c.book.leases) == 2 and "quota" in c.book.first_reason


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_join_interruption_never_retries_dead_thread(controlled, error):
    c = controlled
    c.accept((71, None))
    c.join_error = error
    with pytest.raises(error):
        c.broker.service()
    before = len([e for e in c.events if e[0] == "join" and "handler" in e[1]])
    c.join_error = None
    c.broker.close()
    assert not c.broker.startup_disposed
    assert len([e for e in c.events if e[0] == "join" and "handler" in e[1]]) == before


def test_actual_shared_cap_and_pending_join0_normal_reobservation(controlled):
    c = controlled
    c.accept((71, None), (72, None))
    record = next(iter(c.pool.records.values()))
    record.thread.keep_alive = True
    c.broker.service()
    assert record.join_state == "pending"
    record.thread.keep_alive = False
    c.join_hook = lambda: setattr(c, "now", c.now + 0.25)
    c.broker.close()
    cap = c.pool.stop_cap
    assert cap == 12.0 and c.broker.startup_disposed
    c.now, c.broker._startup_deadline = 99.0, 200.0
    c.broker.close()
    assert c.pool.stop_cap == cap
    c.broker._startup_deadline = 11.0
    c.broker.close()
    assert c.pool.stop_cap == 11.0
    assert all(e[2] <= 2.0 for e in c.events if e[0] == "join")


def test_actual_registry_bounds_and_normal_open_caller_publication(controlled):
    c = controlled
    registry = ports.HandoffRegistry(job_ids=[f"job-{n}" for n in range(70)])
    assert registry._maximum == 70
    registry.bind_loss(lambda reason: c.events.append(("loss", reason)))
    slot = SimpleNamespace(broker=None, cleanup_cap=20.0, clock=lambda: c.now)
    registry.open("job-69", 0, 0, launch=slot)
    assert registry._brokers["job-69"] is slot.broker and slot.broker._server is c.servers[-1]
    with pytest.raises(RuntimeError):
        registry.open("job-69", 0, 0)
    with pytest.raises(custody.Refused):
        registry.open("not-in-plan", 0, 0)
    small = ports.HandoffRegistry(max_brokers=1)
    small.open("a", 0, 0)
    with pytest.raises(custody.Refused):
        small.open("b", 0, 0)
    assert ports.HandoffRegistry()._maximum == 64


@pytest.mark.parametrize("mode", ["preflight", "pulse", "pulse_no_preflight"])
def test_actual_registry_service_once_preserves_firsterror_and_sibling_stops(controlled, mode):
    c = controlled
    c.accept((71, None))
    registry = ports.HandoffRegistry(job_ids=["a", "b"])
    other = ports.ListenerBroker()
    registry._brokers.update(a=c.broker, b=other)
    calls, first = [], OSError("first exact join failure")
    original_join = next(t for t in c.threads if "handler" in t.name).join

    def failing_join(**kwargs):
        calls.append("first")
        raise first

    next(t for t in c.threads if "handler" in t.name).join = failing_join
    real_service = other.service

    def second():
        calls.append("second")
        real_service()

    other.service = second
    service, jobs, now, events = owner_controls.fixture()
    service.handoffs = registry
    now[0] = 12.0
    with pytest.raises(OSError) as caught:
        if mode == "preflight":
            service.preflight()
        else:
            service.pulse(preflight=mode == "pulse")
    assert caught.value is first and calls == ["first", "second"]
    if mode != "preflight":
        assert all(job.stop_requested for job in jobs)
        assert "observe" in events and service.deadlines.expired
    assert original_join is not None and not service._active


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_actual_owner_service_does_not_swallow_baseexception(controlled, error):
    c = controlled
    c.accept((71, None))
    c.join_error = error
    registry = ports.HandoffRegistry(job_ids=["a"])
    registry._brokers["a"] = c.broker
    service, _, _, _ = owner_controls.fixture()
    service.handoffs = registry
    with pytest.raises(error):
        service.pulse()
    assert not service._active


@pytest.mark.parametrize("negative", [False, True])
def test_actual_scheduler_registry_finalizer_and_installed_loss(publication, monkeypatch, negative):
    p, brokers, calls = publication, [], []
    original_start = ports.ListenerBroker.start

    def start(broker):
        result = original_start(broker)
        brokers.append(broker)
        if negative:

            def failed_accept():
                calls.append("actual_no_return_accept")
                raise OSError("no return")

            broker._server._accept = failed_accept
            broker._serve()
        return result

    monkeypatch.setattr(ports.ListenerBroker, "start", start)
    report = p.run()
    ledger, broker = p.ledgers[0], brokers[0]
    assert broker.on_loss.__self__ is ledger.hub
    assert ledger.hub.on_loss.__self__ is ledger.loss_notice
    assert ledger.handoffs._keys == frozenset(j["job_id"] for j in p.plan.as_dict()["jobs"])
    if negative:
        assert calls == ["actual_no_return_accept"]
        assert report["overall"] == "FAIL" and not broker.startup_disposed
        assert ledger.handoffs._brokers and life._RETAINED_RUNS == [ledger]
        assert ledger.adoption.restoration_pending and p.exited == 0
        assert ledger.hub.closed_reason and ledger.loss_notice.event.is_set()
    else:
        assert not ledger.handoffs._brokers and broker.startup_disposed
        assert not life._RETAINED_RUNS and p.exited == 1
    # Capacity-before-close and separate outer adoption are explicitly not fixed here.


def test_actual_handler_constructor_failure_is_known_neverstarted(controlled):
    c = controlled

    def fail():
        raise ValueError("constructor before returned Thread")

    c.construct_hook = fail
    with pytest.raises(ValueError):
        c.accept((71, None))
    assert c.conns[0].closes == 1 and not c.pool.records
    assert not any(e[0] in ("start", "join") and "handler" in e[1] for e in c.events)
    c.broker.close()
    assert c.broker.startup_disposed


def test_actual_close_during_ack_wait_never_owner_closes_live_root(controlled):
    c = controlled
    port, observed = c.broker.lease(), []

    def during_ack():
        c.broker.close()
        observed.append(c.roots[0].closes)
        assert c.book.operations and c.pool.records
        return b'{"op":"ack"}'

    c.request(port, during_ack)
    assert observed == [0] and c.roots[0].closes == 1
    assert c.conns[0].closes == 1 and c.servers[0].closes == 0
    assert not c.broker.startup_disposed  # one accept join was pending, never renewed


def test_actual_release_cannot_claim_socket_during_take_fileno(controlled):
    c = controlled
    port = c.broker.lease()
    original, observed = c.roots[0].fileno, []

    def fileno():
        c.broker._release(port)
        observed.append(c.roots[0].closes)
        return original()

    c.roots[0].fileno = fileno
    c.request(port)
    assert observed == [0] and c.roots[0].closes == 1
    assert len([e for e in c.events if e[0] == "send_fds"]) == 1
    assert not c.book.operations and not c.book.leases


def test_actual_installed_hub_sink_commits_firstreason_before_delayed_event(
    publication, monkeypatch
):
    p, delayed, installed, brokers = publication, [], [], []
    original = ports.ListenerBroker.start

    def start(broker):
        result = original(broker)
        brokers.append(broker)
        hub = broker.on_loss.__self__
        installed.append(hub.on_loss)
        hub.on_loss = delayed.append

        def failed_accept():
            raise OSError("controlled original accepted return unknown")

        broker._server._accept = failed_accept
        broker._serve()
        assert hub.closed_reason == delayed[0]
        assert not p.ledgers[0].loss_notice.event.is_set()
        return result

    monkeypatch.setattr(ports.ListenerBroker, "start", start)
    report = p.run()
    ledger = p.ledgers[0]
    assert report["overall"] == "FAIL" and ledger.hub.closed_reason == delayed[0]
    assert not ledger.loss_notice.event.is_set() and len(delayed) == 1
    state = (len(p.polls), list(p.closed), p.exited)
    installed[0](delayed[0])  # actual captured HubLossNotice.notify, Event only
    assert ledger.loss_notice.event.is_set()
    assert (len(p.polls), p.closed, p.exited) == state
    assert ledger.adoption.restoration_pending and not brokers[0].startup_disposed


def test_actual_preflight_first_error_wins_but_registry_still_services_sibling(controlled):
    c = controlled
    service, _, _, _ = owner_controls.fixture()
    first, calls = ValueError("original lease check"), []

    def lease_failure():
        raise first

    service.hub.check_leases = lease_failure
    registry = ports.HandoffRegistry(job_ids=["a", "b"])
    other = ports.ListenerBroker()
    registry._brokers.update(a=c.broker, b=other)
    real = other.service

    def second():
        calls.append("second")
        real()

    other.service = second

    def first_service():
        calls.append("first")
        raise OSError("later housekeeping failure")

    c.broker.service = first_service  # controlled leaf failure; registry/OwnerService remain actual
    service.handoffs = registry
    with pytest.raises(ValueError) as caught:
        service.preflight()
    assert caught.value is first and calls == ["first", "second"]


def test_actual_close_all_visits_siblings_preserving_original_exception(controlled):
    c = controlled
    registry = ports.HandoffRegistry(job_ids=["a", "b"])
    other = c.make()
    registry._issued.update(("a", "b"))
    registry._brokers.update(a=c.broker, b=other)
    c.broker.lease()
    c.close_error = "root"
    with pytest.raises(OSError, match="controlled close effect"):
        registry.close_all()
    assert registry._brokers == {"a": c.broker} and "b" in registry._retired
    assert other.startup_disposed and not c.broker.startup_disposed
    before = c.roots[0].closes
    c.close_error = None
    registry.close_all()
    assert c.roots[0].closes == before and not registry.startup_disposed


def test_actual_primary_operation_failure_survives_secondary_loss_sink_escape(controlled):
    c = controlled
    c.config_error = "listen"

    def callback(reason):
        raise SystemExit("secondary notification")

    c.broker.on_loss = callback
    with pytest.raises(OSError, match="controlled configuration") as caught:
        c.broker.lease()
    assert any("SystemExit" in note for note in caught.value.__notes__)
    assert c.book.operations and c.book.leases and c.roots[0].closes == 0
    assert c.book.first_reason and not c.broker.startup_disposed


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_accept_join_unknown_is_not_repaired_by_dead_observation(controlled, error):
    c = controlled
    c.accept_join_error = error
    with pytest.raises(error):
        c.broker.close()
    c.accept_join_error = None
    c.broker._thread.alive = False
    c.broker.close()
    assert not c.broker.startup_disposed and c.servers[0].closes == 0
    assert len([e for e in c.events if e[0] == "join"]) == 1
    assert c.broker._thread_state == "uncertain"


def test_actual_accept_return_after_stop_is_retained_and_neverstarted(controlled):
    c = controlled

    def returned():
        assert next(iter(c.pool.records.values())).accept_state == "attempted"
        c.broker.close()
        assert c.servers[0].closes == 0
        return (71, "normal return after stop")

    c.accept(returned)
    assert c.conns[0].closes == 1 and not c.pool.records
    assert not any(e[0] in ("start", "join") and "handler" in e[1] for e in c.events)
    assert len([e for e in c.events if e[0] == "construct" and e[1] == 71]) == 1
    assert c.servers[0].closes == 0 and not c.broker.startup_disposed


def test_actual_many_completed_requests_recycle_only_after_exact_join(controlled):
    c = controlled

    def result(fd):
        c.broker.service()
        return (fd, None)

    c.accept((71, None), *(lambda fd=fd: result(fd) for fd in range(72, 80)))
    c.broker.service()
    assert not c.book.first_reason and not c.pool.records and not c.book.operations
    assert len(c.conns) == c.pool.retired == 9
    assert len(c.pool.history) == c.pool.limit
    assert all(conn.closes == 1 for conn in c.conns)
    assert all(e[2] == 0.0 for e in c.events if e[0] == "join")


def test_actual_unleased_take_never_sends_rights_or_falls_back_to_bind(controlled):
    c = controlled
    port = c.broker.lease()
    c.request(port + 1)
    assert not any(e[0] == "send_fds" for e in c.events)
    assert len(c.roots) == 1 and c.roots[0].closes == 0
    assert any(e[0] == "send" and b"not leased" in e[2] for e in c.events)
    record = c.broker.close()
    assert port in record["unconsumed"] and c.broker.startup_disposed
    assert c.roots[0].closes == c.conns[0].closes == 1


@pytest.mark.parametrize("stage", ["constructor", "setsockopt", "bind", "listen", "getsockname"])
@pytest.mark.parametrize("error", [OSError, ValueError, KeyboardInterrupt])
def test_protocol_lease_fault_commits_before_expected_error_catch(controlled, stage, error):
    c = controlled
    losses = []
    hub = arbiter.ArbiterHub(1, on_loss=losses.append)
    c.broker.on_loss = hub.lose
    if stage == "constructor":
        c.ctor_error = error
    else:
        c.config_error, c.config_exception = stage, error
    c.messages[71] = [b'{"op":"lease"}']
    c.accept((71, None))  # real _serve -> dispatch -> _handle -> _lease
    lease = next(iter(c.book.leases.values()))
    operation = next(iter(c.book.operations.values()))
    assert lease.unknown and operation.unknown and losses == [c.book.first_reason]
    assert hub.closed_reason == losses[0] and "allocation/configuration" in losses[0]
    assert len(c.errors) == (1 if error is KeyboardInterrupt else 0)
    if c.errors:
        assert isinstance(c.errors[0], error)
    c.config_error = c.ctor_error = None
    c.broker.close()
    assert not c.broker.startup_disposed and c.book.leases and c.book.operations
    assert all(sock.closes == 0 for sock in c.roots)
    assert not any(e[0] == "send" for e in c.events)


@pytest.mark.parametrize("now", [20.0, 20.1])
@pytest.mark.parametrize("handler", [False, True])
def test_release_original_cutoff_has_zero_root_close(controlled, now, handler):
    c = controlled
    port = c.broker.lease()
    c.now = now
    if handler:
        c.messages[71] = [json.dumps({"op": "release", "port": port}).encode()]
        c.accept((71, None))
    else:
        with pytest.raises(TimeoutError):
            c.broker._release(port)
    assert c.roots[0].closes == 0 and c.book.leases
    assert c.book.first_reason and c.book.operations
    c.broker.close()
    assert c.roots[0].closes == 0 and not c.broker.startup_disposed


def test_two_lease_callers_contend_for_same_reserved_candidate(controlled):
    c = controlled
    c.broker.first_port, c.broker.count = 9000, 2
    second = []

    def overlapping():
        c.bind_hook = None
        assert c.book.candidates == {9000} and len(c.book.operations) == 1
        second.append(c.broker.lease())  # actual second caller while first bind is suspended
        assert len(c.book.operations) == 1 and c.bind_attempts == [9000, 9001]

    c.bind_hook = overlapping
    first = c.broker.lease()
    assert first == 9000 and second == [9001] and set(c.book.available) == {9000, 9001}
    assert not c.book.operations and not c.book.first_reason
    c.broker.close()
    assert c.broker.startup_disposed and all(sock.closes == 1 for sock in c.roots)


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_protocol_lease_constructor_loss_reaches_actual_scheduler_finalizer(
    publication, monkeypatch, error
):
    p, brokers, failures = publication, [], []
    primary = error("controlled protocol lease constructor")
    original_start, factory = ports.ListenerBroker.start, ports._SOCKET

    class Connection:
        def settimeout(self, seconds):
            pass

        def recv(self, limit):
            return b'{"op":"lease"}'

        def close(self):
            pass

    def start(broker):
        result = original_start(broker)
        brokers.append(broker)
        accepted = []

        def accept():
            if not accepted:
                accepted.append(True)
                return (71, None)
            broker._closing.set()
            raise TimeoutError

        def socket(*args, fileno=None):
            if fileno is not None:
                return Connection()
            failures.append(primary)
            raise primary

        broker._server._accept = accept
        monkeypatch.setattr(ports, "_SOCKET", socket)
        monkeypatch.setattr(ports.ListenerBroker, "_same_user", staticmethod(lambda conn: True))
        broker._serve()
        next(iter(broker._connections.records.values())).thread.target()
        monkeypatch.setattr(ports, "_SOCKET", factory)
        return result

    monkeypatch.setattr(ports.ListenerBroker, "start", start)
    report = p.run()
    ledger, broker = p.ledgers[0], brokers[0]
    assert report["overall"] == "FAIL" and broker._book.operations and broker._book.leases
    assert ledger.hub.closed_reason == broker._book.first_reason
    assert len(failures) == 1 and failures[0] is primary
    assert ledger.hub.closed_reason == "listener allocation/configuration unproven"
    assert ledger.loss_notice.event.is_set() and ledger.adoption.restoration_pending
    assert ledger.handoffs._brokers and life._RETAINED_RUNS == [ledger]
    assert not broker.startup_disposed and p.exited == 0
