"""Source-authored B2-H actual-boundary controls; ALL NOTRUN, no kernel qualification."""

import inspect
import sys
from types import SimpleNamespace

import pytest

from scripts import production_gate_parallel_arbiter as arbiter
from scripts import production_gate_parallel_handler_custody as custody
from scripts import production_gate_parallel_job_lifecycle as life
from scripts import production_gate_parallel_job_retirement as retirement
from tests import test_parallel_runtime_job_publication as publication_controls

publication = publication_controls.publication


class GuardedLock:
    def __init__(self):
        self.held = False

    def __enter__(self):
        assert not self.held, "custody lock reentry"
        self.held = True

    def __exit__(self, *args):
        self.held = False


@pytest.fixture
def controlled(monkeypatch):
    c = SimpleNamespace(
        now=10.0,
        events=[],
        connections=[],
        threads=[],
        queue=[],
        escapes=[],
        start_mode="fast",
        close_error=None,
        join_error=None,
        wrap_error=None,
        constructor_error=None,
        timeout_error=False,
        start_hook=None,
        constructor_hook=None,
        in_accept=False,
        join_hook=None,
        accept_join_error=None,
    )
    notice = retirement.HubLossNotice()
    c.hub = arbiter.ArbiterHub(2, handler_limit=2, clock=lambda: c.now, on_loss=notice.notify)
    c.pool, c.notice = c.hub._connections, notice
    c.pool.lock = GuardedLock()

    def effect(kind, *args):
        assert not c.pool.lock.held, (kind, "effect under custody lock")
        c.events.append((kind, *args))

    def clock():
        effect("clock")
        return c.now

    c.hub._clock = clock
    c.hub._startup_clock = clock
    c.hub._startup_deadline = 11.0

    class Connection:
        def __init__(self, fd):
            self.fd, self.closes = fd, 0
            c.connections.append(self)

        def settimeout(self, value):
            effect("connection_timeout", self.fd, value)
            if c.timeout_error:
                raise OSError("controlled settimeout")

        def getsockopt(self, *args):
            pytest.fail("kernel credentials must be controlled")

        def recv(self, limit):
            effect("recv", self.fd, limit)
            return b""

        def send(self, data):
            effect("send", self.fd, data)
            return len(data)

        def close(self):
            effect("connection_close", self.fd)
            self.closes += 1
            if c.close_error is not None:
                raise c.close_error("close effect then escape")

    class Server:
        family, type, proto = 1, 5, 0

        def __init__(self):
            self.closes = self.accepts = 0

        def bind(self, address):
            effect("bind")

        def listen(self, count):
            effect("listen", count)

        def settimeout(self, seconds):
            effect("accept_timeout", seconds)

        def _accept(self):
            effect("accept")
            self.accepts += 1
            if not c.queue:
                c.hub._stopping.set()
                raise TimeoutError
            result = c.queue.pop(0)
            if callable(result):
                result = result()
            if isinstance(result, BaseException):
                raise result
            return result

        def close(self):
            effect("server_close")
            self.closes += 1

    server = Server()

    def socket(*args, fileno=None):
        if fileno is None:
            return server
        effect("wrap", fileno)
        if c.wrap_error is not None:
            raise c.wrap_error("wrapper uncertainty")
        return Connection(fileno)

    class Thread:
        def __init__(self, *, target, name, daemon):
            effect("thread_construct", name)
            if name.startswith("arbiter-handler"):
                if c.constructor_hook is not None:
                    c.constructor_hook()
                if c.constructor_error is not None:
                    raise c.constructor_error("constructor before start")
            self.target, self.name = target, name
            self.alive, self.ident, self.started, self.ran = False, None, False, False
            self.keep_alive = False
            c.threads.append(self)

        def start(self):
            effect("start_call", self.name)
            handler = self.name.startswith("arbiter-handler")
            if handler and c.start_hook is not None:
                c.start_hook()
            if handler and c.start_mode == "before_error":
                raise RuntimeError("before controlled start effect")
            self.started, self.alive, self.ident = True, True, 1
            effect("start_effect", self.name)
            if handler and c.start_mode != "defer":
                self.run_target()
            if handler and c.start_mode == "after_error":
                raise RuntimeError("after controlled start effect")

        def run_target(self, *, propagate=False):
            assert self.started and not self.ran
            self.ran = True
            old = c.in_accept
            c.in_accept = self.name == "arbiter-hub"
            try:
                self.target()
            except BaseException as error:
                c.escapes.append(error)
                if propagate:
                    raise
            finally:
                c.in_accept = old
                self.alive = False

        def join(self, *, timeout):
            effect("join", self.name, timeout)
            assert self.started, "illegal join of a never-started Thread"
            if c.join_hook is not None:
                c.join_hook()
            if self.name == "arbiter-hub" and c.accept_join_error is not None:
                raise c.accept_join_error("controlled accept join uncertainty")
            if self.name.startswith("arbiter-handler") and c.join_error is not None:
                raise c.join_error("controlled join uncertainty")
            if self.name == "arbiter-hub" and not self.ran and not c.in_accept:
                self.run_target()

        def is_alive(self):
            effect("is_alive", self.name)
            return self.alive or self.keep_alive

    monkeypatch.setattr(arbiter, "_SOCKET", socket)
    monkeypatch.setattr(arbiter, "threading", SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(
        arbiter, "os", SimpleNamespace(getuid=lambda: 7, urandom=lambda n: b"x" * n)
    )
    monkeypatch.setattr(custody, "os", SimpleNamespace(close=lambda fd: effect("raw_close", fd)))
    monkeypatch.setattr(c.hub, "_peer", lambda conn: (12, 7))
    c.hub.start()  # real start_listener publication, controlled effects

    def accept(*results):
        c.queue[:] = results
        c.hub._thread.run_target(propagate=True)  # actual _accept, no real thread

    c.accept, c.effect, c.server, c.Thread = accept, effect, server, Thread
    c.Connection = Connection
    return c


def handlers(c):
    return [thread for thread in c.threads if thread.name.startswith("arbiter-handler")]


def test_actual_before_accept_quota_and_bounded_owner_recycling(controlled):
    c = controlled
    c.accept((71, None), (72, None), (73, None))
    assert c.server.accepts == 2 and len(c.pool.records) == c.pool.limit
    assert "quota" in c.hub.closed_reason and c.notice.event.is_set()
    assert all(
        record.done.is_set() and record.close_state == "closed"
        for record in c.pool.records.values()
    )
    assert all(connection.closes == 1 for connection in c.connections)  # prompt EOF before stop
    c.hub.check_leases()  # actual finite owner hook joins exact completed handlers
    assert not c.pool.records and c.pool.retired == 2
    assert len(c.pool.history) <= c.pool.limit
    assert all(e[2] == 0.0 for e in c.events if e[0] == "join")
    first = c.hub.closed_reason
    c.hub.lose("later reason")
    assert c.hub.closed_reason == first


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_accept_return_published_before_wrapper_failure(controlled, error):
    c = controlled
    c.wrap_error = error
    with pytest.raises(error):
        c.accept((71, "address"))
    record = next(iter(c.pool.records.values()))
    assert record.raw_result == (71, "address") and record.descriptor == 71
    assert record.transfer == "wrapping" and record.unknown and not record.start_attempted
    c.hub.stop()
    c.hub.stop()
    assert not c.hub.startup_disposed and c.server.closes == 1
    assert not any(e[0] in ("raw_close", "connection_close") for e in c.events)
    assert not handlers(c) and len(c.pool.records) == 1


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_no_return_accept_stays_unknown_without_join_or_close(controlled, error):
    c = controlled
    if error is KeyboardInterrupt:
        with pytest.raises(error):
            c.accept(error("no returned fd"))
    else:
        c.accept(error("no returned fd"))
    record = next(iter(c.pool.records.values()))
    assert record.raw_result is None and record.descriptor is None and c.pool.accept_unknown
    c.hub.stop()
    c.hub.stop()
    assert not c.hub.startup_disposed and c.server.closes == 0
    assert not any(e[0] in ("join", "raw_close", "connection_close") for e in c.events)
    assert len(c.pool.records) == 1 and c.hub.closed_reason


@pytest.mark.parametrize("mode", ["before_error", "after_error"])
def test_actual_handler_start_ambiguity_cannot_upgrade_from_fast_completion(controlled, mode):
    c = controlled
    c.start_mode = mode
    with pytest.raises(RuntimeError):
        c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    assert record.thread is handlers(c)[0] and record.start_attempted
    assert record.start_state == "unknown" and not record.start_committed
    assert len([e for e in c.events if e[0] == "start_call" and "handler" in e[1]]) == 1
    assert len([e for e in c.events if e[0] == "start_effect" and "handler" in e[1]]) == (
        mode == "after_error"
    )
    assert record.body_complete is (mode == "after_error")
    c.pool.housekeep()
    c.hub.stop()
    assert not c.hub.startup_disposed and len(c.pool.records) == 1
    assert not any(e[0] == "join" and "handler" in e[1] for e in c.events)
    assert c.connections[0].closes == (mode == "after_error")


def test_actual_fast_body_completion_survives_parent_start_commit(controlled):
    c = controlled
    c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    assert record.body_complete and record.done.is_set() and record.start_committed
    assert record.start_state == "started" and record.close_state == "closed"
    assert c.connections[0].closes == 1 and c.server.closes == 0
    c.hub.stop()
    assert c.hub.startup_disposed and c.server.closes == 1 and not c.pool.records
    joins = [e for e in c.events if e[0] == "join"]
    assert len(joins) == 2 and joins[0][1] == "arbiter-hub"
    assert c.events.index(joins[0]) < next(
        i for i, e in enumerate(c.events) if e[0] == "server_close"
    )


@pytest.mark.parametrize("stop_first", [True, False])
def test_actual_stop_and_start_claim_have_one_winner_without_effect_under_lock(
    controlled, stop_first
):
    c = controlled
    if stop_first:
        c.constructor_hook = c.hub.stop
    else:
        c.start_hook = c.hub.stop
    c.accept((71, None))
    thread = handlers(c)[0]
    assert thread.started is (not stop_first)
    assert c.connections[0].closes == 1
    assert not any(e[0] == "join" and "handler" in e[1] for e in c.events)
    assert c.server.closes == 0  # in-flight accept join was pending, not a close permit
    assert c.pool.closing and c.pool.stop_cap == 11.0


def test_actual_constructor_failure_closes_known_neverstarted_without_join(controlled):
    c = controlled
    c.constructor_error = ValueError
    with pytest.raises(ValueError):
        c.accept((71, None))
    assert c.connections[0].closes == 1 and not handlers(c)
    assert not c.pool.records and c.pool.retired == 1
    c.hub.stop()
    assert c.hub.startup_disposed
    assert not any(e[0] == "join" and "handler" in e[1] for e in c.events)


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_actual_checked_handler_close_is_single_use_even_after_effect(controlled, error):
    c = controlled
    c.close_error = error
    c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    assert record.body_complete and record.close_state == "unknown"
    assert c.connections[0].closes == 1 and record.unknown and c.escapes
    c.pool.close(record, handler=True)
    c.pool.close(record, handler=False)
    c.hub.stop()
    c.hub.stop()
    assert c.connections[0].closes == 1 and not c.hub.startup_disposed
    assert not any(e[0] == "join" and "handler" in e[1] for e in c.events)
    with pytest.raises(RuntimeError):
        custody._AcceptedView(c.pool, record).recv(1)


def test_actual_close_success_interrupted_before_commit_never_recloses(controlled):
    c = controlled
    source, line = inspect.getsourcelines(custody.ConnectionCustody.close)
    target = line + next(
        i for i, text in enumerate(source) if 'record.close_state = "closed"' in text
    )
    fired = []

    def trace(frame, event, arg):
        if (
            event == "line"
            and frame.f_code is custody.ConnectionCustody.close.__code__
            and frame.f_lineno == target
        ):
            fired.append(True)
            sys.settrace(None)
            raise KeyboardInterrupt("after close before commit")
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        c.accept((71, None))
    finally:
        sys.settrace(previous)
    record = next(iter(c.pool.records.values()))
    assert fired and c.connections[0].closes == 1 and record.close_state == "closing"
    c.hub.stop()
    c.pool.close(record, handler=True)
    assert not c.hub.startup_disposed and c.connections[0].closes == 1
    assert len(c.pool.records) == 1


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_actual_join_exception_remains_unknown_despite_dead_thread(controlled, error):
    c = controlled
    c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    c.join_error = error
    with pytest.raises(error):
        c.hub.check_leases()
    before = [e for e in c.events if e[0] == "join"]
    c.join_error = None
    assert not record.thread.is_alive() and record.done.is_set()
    c.pool.housekeep()
    c.hub.stop()
    assert record.join_state == "unknown" and record.unknown
    assert [e for e in c.events if e[0] == "join" and "handler" in e[1]] == before
    assert not c.hub.startup_disposed and len(c.pool.records) == 1


def test_normal_join0_alive_is_pending_and_can_later_retire(controlled):
    c = controlled
    c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    record.thread.keep_alive = True  # done body, controlled thread still finishing
    c.hub.check_leases()
    assert record.join_state == "pending" and not record.unknown
    record.thread.keep_alive = False
    c.hub.check_leases()
    assert not c.pool.records and c.pool.retired == 1
    assert [e[2] for e in c.events if e[0] == "join"] == [0.0, 0.0]


def test_actual_stop_cap_is_retained_tightened_and_shared(controlled):
    c = controlled
    c.accept((71, None))
    c.hub.stop()
    first = c.pool.stop_cap
    assert first == 11.0
    assert all(e[2] <= first - 10.0 for e in c.events if e[0] == "join")
    before = [e for e in c.events if e[0] == "join"]
    c.now, c.hub._startup_deadline = 20.0, 25.0
    c.hub.stop()
    assert c.pool.stop_cap == first
    c.hub._startup_deadline = 10.5
    c.hub.stop()
    assert c.pool.stop_cap == 10.5 and [e for e in c.events if e[0] == "join"] == before


def test_settimeout_failure_uses_real_context_close_without_new_protocol_classification(controlled):
    c = controlled
    c.timeout_error = True
    c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    assert record.body_error == "OSError" and not record.unknown
    assert isinstance(c.escapes[0], OSError) and not c.hub.closed_reason
    assert record.close_state == "closed" and c.connections[0].closes == 1
    c.pool.housekeep()
    assert not c.pool.records


def test_direct_existing_serve_context_oracle_and_retained_production_path(controlled):
    c = controlled
    contexts = []

    class Connection:
        def __enter__(self):
            contexts.append("enter")
            return self

        def __exit__(self, *args):
            contexts.append("exit")

        def settimeout(self, value):
            contexts.append("timeout")

        def recv(self, limit):
            return b""

    c.hub._serve(Connection())  # unchanged real private protocol oracle
    assert contexts == ["enter", "timeout", "exit"]
    c.accept((71, None))
    assert c.pool.records and c.connections[0].closes == 1
    assert next(iter(c.pool.records.values())).thread is handlers(c)[0]


@pytest.mark.parametrize("negative", [False, True])
def test_real_scheduler_finalization_observes_actual_hub_custody(
    publication, monkeypatch, negative
):
    p, hubs = publication, []
    original = arbiter.ArbiterHub.__init__
    original_start = arbiter.ArbiterHub.start
    original_finalize = life.JobLaunchLedger.finalize
    accept_calls = []
    finalizations = []

    def init(hub, *args, **kwargs):
        original(hub, *args, **kwargs)
        hubs.append(hub)

    def start(hub):
        result = original_start(hub)
        if negative:

            def failed_accept():
                accept_calls.append("actual_accept_effect")
                raise OSError("controlled no-return accept")

            hub._server._accept = failed_accept
            hub._accept()
        return result

    def finalize(ledger, *args, **kwargs):
        result = original_finalize(ledger, *args, **kwargs)
        finalizations.append((result, ledger.retained, ledger.adoption.restoration_pending))
        return result

    monkeypatch.setattr(arbiter.ArbiterHub, "__init__", init)
    monkeypatch.setattr(arbiter.ArbiterHub, "start", start)
    monkeypatch.setattr(life.JobLaunchLedger, "finalize", finalize)
    report = p.run()
    hub = hubs[0]
    assert hub.on_loss.__func__ is retirement.HubLossNotice.notify
    assert hub._connections.limit == max(
        4, 2 * p.budget.total_pairs + 2 * len(p.plan.as_dict()["jobs"])
    )
    if negative:
        assert accept_calls == ["actual_accept_effect"]
        assert hub._connections.accept_unknown
        assert report["overall"] == "FAIL" and report["allocation_publication"]["pending"]
        assert all(
            not result and retained and pending for result, retained, pending in finalizations
        )
        assert life._RETAINED_RUNS == [p.ledgers[0]] and p.exited == 0
        assert hub._server.closes == 0 and not hub._thread_dispose_attempted
    else:
        assert all(
            result and not retained and not pending for result, retained, pending in finalizations
        )
        assert hub.startup_disposed and hub._connections.accept_done and hub._server.closes == 1
        assert not life._RETAINED_RUNS and p.exited == 1


def test_actual_delayed_dispatch_after_unregister_remains_explicit_gap(controlled):
    c = controlled
    c.hub.register("job", 1)
    job = c.hub._jobs["job"]
    c.hub.unregister("job", cleanup_proven=True)
    result, reason = c.hub._dispatch(
        job, {"op": "event", "kind": "capacity_loss", "reason": "late"}, 12
    )
    assert result == "closed" and "retired" in reason and not c.hub.closed_reason
    # Custody cannot restore semantics discarded by early unregister: B3/B2 terminal cutoff.
    c.hub.stop()
    assert c.hub.startup_disposed


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_actual_accept_join_interruption_is_permanent_no_listener_close(controlled, error):
    c = controlled
    c.accept_join_error = error
    with pytest.raises(error):
        c.hub.stop()
    first = c.hub.closed_reason
    assert "accept-thread join" in first and c.notice.event.is_set()
    c.accept_join_error = None
    c.hub._thread.alive = False
    c.hub.stop()
    assert c.hub.closed_reason == first and c.server.closes == 0
    assert len([e for e in c.events if e[0] == "join"]) == 1
    assert not c.hub.startup_disposed


def test_actual_multiple_handler_joins_share_one_decreasing_stop_cap(controlled):
    c = controlled
    c.accept((71, None), (72, None))
    c.join_hook = lambda: setattr(c, "now", c.now + 0.25)
    c.hub.stop()
    joins = [e for e in c.events if e[0] == "join"]
    assert len(joins) == 3
    assert [e[2] for e in joins] == [1.0, 0.75, 0.5]
    assert c.pool.stop_cap == 11.0 and c.hub.startup_disposed


def test_actual_owner_recycling_keeps_record_and_history_bounds(controlled):
    c = controlled

    def next_result(fd):
        c.hub.check_leases()
        return (fd, None)

    c.accept((71, None), *(lambda fd=fd: next_result(fd) for fd in range(72, 82)))
    c.hub.check_leases()
    assert not c.hub.closed_reason and not c.pool.records
    assert c.pool.retired == 11 and len(c.pool.history) == c.pool.limit
    assert len(c.connections) == 11 and all(conn.closes == 1 for conn in c.connections)


def test_actual_handler_finally_commits_first_loss_before_delayed_notification(
    controlled, monkeypatch
):
    c = controlled
    installed, delayed = c.hub.on_loss, []
    c.hub.on_loss = delayed.append
    c.hub.register("job", 1)
    job = c.hub._jobs["job"]
    job.active.add("controlled")
    messages = iter([b"request", b""])
    monkeypatch.setattr(c.Connection, "recv", lambda conn, limit: next(messages))
    monkeypatch.setattr(c.hub, "_handle", lambda conn, packet, previous, sequence, pid: (job, 1))
    c.accept((71, None))
    assert c.hub.closed_reason and delayed == [c.hub.closed_reason]
    assert not c.notice.event.is_set() and c.connections[0].closes == 1
    first = c.hub.closed_reason
    c.hub.lose("later loss")
    assert c.hub.closed_reason == first and delayed == [first]
    installed(delayed[0])
    assert c.notice.event.is_set()
    c.hub.stop()
    assert c.hub.startup_disposed and c.hub.closed_reason == first


def test_actual_stop_during_accept_retains_normal_return_then_neverstarts(controlled):
    c = controlled

    def returned_after_stop():
        assert len(c.pool.records) == 1
        assert next(iter(c.pool.records.values())).accept_state == "attempted"
        c.hub.stop()
        assert c.server.closes == 0
        return (71, "normally returned after stop")

    c.accept(returned_after_stop)
    assert c.connections[0].closes == 1 and c.pool.retired == 1
    assert not handlers(c)[0].started and not c.pool.records
    assert len([e for e in c.events if e[0] == "wrap"]) == 1
    assert not any(
        e[0] in ("raw_close", "start_call", "join") and len(e) > 1 and "handler" in str(e[1])
        for e in c.events
    )
    assert c.server.closes == 0 and not c.hub.startup_disposed


def test_actual_stop_never_closes_active_handler_and_later_exact_join_retires(controlled):
    c = controlled
    c.start_mode = "defer"
    c.accept((71, None))
    record = next(iter(c.pool.records.values()))
    c.hub.stop()
    assert c.connections[0].closes == 0 and not record.body_complete
    assert not c.hub.startup_disposed and len(c.pool.records) == 1
    assert not any(e[0] == "join" and "handler" in e[1] for e in c.events)
    record.thread.run_target()
    assert c.connections[0].closes == 1 and record.body_complete
    c.hub.check_leases()
    assert c.hub.startup_disposed and not c.pool.records
    assert c.pool.stop_cap == 11.0
    assert len([e for e in c.events if e[0] == "join" and e[1] == "arbiter-hub"]) == 1
