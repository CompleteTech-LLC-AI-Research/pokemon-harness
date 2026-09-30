"""Authored owner-driver machinery for the split trade-probe modules (#160).

These builders and the ``create_authored_goal_driver`` factory are used by
the driver split module and resolved by ``tests._probe_timed_trade_pair_drivers:
create_authored_goal_driver`` from the supervisor's import-path contract.
"""

import json
import socket
import threading
import time

from scripts import probe_timed_rom_pair as ordinary


class _AuthoredGoalDriver:
    """A local checkpoint after one frame must leave the owner CPU running."""

    def __init__(self, session):
        self.session = session
        self.observations = []
        self.offsets = []

    def before_step(self, *, frame_offset):
        self.offsets.append(frame_offset)
        return None if self.objective_complete() else ("a", 1)

    def after_step(self, *, call):
        self.observations.append((call["requested_frames"], call["actual_completed_frames"]))

    def objective_complete(self):
        return bool(self.observations)

    def snapshot(self):
        return {
            "authored": True,
            "observations": list(self.observations),
            "objective_complete": self.objective_complete(),
        }


def create_authored_goal_driver(*, session, side, options, peer_state):
    assert side == "listen"
    assert options == {"version": "blue_color", "outgoing_slot": 0, "checkpoint": "select-mon"}
    driver_type = (
        _AuthoredClosingDriver
        if getattr(session, "authored_close_driver", False)
        else _AuthoredGoalDriver
    )
    driver = driver_type(session)
    fault = getattr(session, "authored_driver_fault", None)
    if fault:
        original = getattr(driver, fault)

        def fail_after_executed_frame(*args, **kwargs):
            if session.steps:
                raise RuntimeError(f"authored {fault} failure")
            return original(*args, **kwargs)

        setattr(driver, fault, fail_after_executed_frame)
    session.authored_driver = driver
    return driver


class _AuthoredClosingDriver(_AuthoredGoalDriver):
    def close(self):
        self.session.record("driver_close")
        assert self.session.endpoint is None
        if self.session.authored_close_failure:
            raise RuntimeError("authored driver close failure")


def run_authored_goal_owner(
    *,
    exception=None,
    goal_stop_requested=True,
    evidence_path=None,
    close_driver=False,
    close_failure=False,
    milestones=False,
    driver_fault=None,
    clock_step=0.0,
    frame_limit=4,
):
    # Reuse existing authored session/endpoint models without changing old tests.
    from tests._probe_timed_rom_pair_support import Harness, _MenuSession, arguments

    # Exact step counts, goal-driver offsets and milestone counts are asserted by
    # the trade-pair rows, so the owner must reach its frame bound rather than be
    # cut off by a wall clock (#252). `#255` added the `clock` seam for this; the
    # milestone and menu rows use it and this helper still did not. A step of 0
    # models an arbitrarily fast host, so the counts hold on any machine.
    #
    # On its own the seam above is not pinned by these rows: at the default
    # `--frame-limit=4` the owner retires in about a millisecond, so a 3 s
    # deadline can never fire and every row passes with or without the seam
    # (#274). `clock_step` and `frame_limit` exist so a sentinel row can make
    # the fake clock consume enough of the 3 s budget for the deadline branch to
    # genuinely compete with the frame bound, which is the only way to prove the
    # helper reads the injected clock. The trade-pair rows keep the defaults, so
    # their counts are unchanged.
    # `FakeClock` lives in the shared frame-bound support module (#262), not in
    # either test module, so importing it here cannot re-introduce a cycle
    # between this helper and the two timed-menu test modules.
    from tests._timed_menu_frame_bound_support import FakeClock

    clock = FakeClock(step=clock_step)
    clock_deadline_base = clock()
    args = arguments(
        "--input-profile=menu",
        "--listener-chunk=1",
        "--connector-chunk=1",
        f"--frame-limit={frame_limit}",
    )
    harness = Harness()
    session = _MenuSession(harness, "blue", "complete")
    session.authored_close_driver = close_driver
    session.authored_close_failure = close_failure
    session.authored_driver_fault = driver_fault
    args.rom_milestones = milestones
    cancelled, goal = threading.Event(), threading.Event()
    goal_stop = threading.Event()
    done_at = []
    if evidence_path is not None:
        args.call_retention = "stream"
    if exception is not None:
        whole_step = session.step

        def interrupted_step(count, *, render):
            if session.steps:
                session.steps.append(count)
                if goal_stop_requested:
                    goal_stop.set()
                cancelled.set()
                raise exception("authored cancellation boundary")
            whole_step(count, render=render)

        session.step = interrupted_step

    class Done:
        def set(self):
            done_at.append(len(session.steps))
            cancelled.set()

    records = [{"side": "listener", "calls": [], "cleanup": [], "errors": []}, {}]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as connector:
            accepted, _ = listener.accept()
            try:
                ordinary._run_owner(
                    0,
                    args,
                    records,
                    [accepted, connector],
                    cancelled,
                    Done(),
                    threading.Barrier(1),
                    [None, None],
                    threading.Lock(),
                    clock_deadline_base + 3,
                    clock_deadline_base + 5,
                    lambda *args, **kwargs: session,
                    harness.endpoint,
                    harness.assets,
                    evidence_path=evidence_path,
                    owner_driver="tests._probe_timed_trade_pair_drivers:create_authored_goal_driver",
                    peer_state=object(),
                    goal_flag=goal,
                    goal_stop=goal_stop,
                    driver_options={
                        "version": "blue_color",
                        "outgoing_slot": 0,
                        "checkpoint": "select-mon",
                    },
                    clock=clock,
                )
            finally:
                accepted.close()
    return session, records[0], goal, done_at


def authored_spawn_owner(
    options,
    index,
    sock,
    cancel,
    done,
    barrier,
    deadline,
    overall,
    sender,
    stderr_path,
    *driver_args,
):
    """Spawned control-only peer: no Session, PyBoy construction, or ROM inputs."""
    record = {
        "side": ("listener", "connector")[index],
        "calls": [],
        "cleanup": [],
        "errors": [],
        "final": {"authored": True},
        "continuations": 0,
    }
    try:
        barrier.wait(timeout=max(0.01, deadline - time.monotonic()))
        if driver_args:
            path, readiness, goal, goal_stop, driver_options = driver_args
            assert ordinary._driver_factory(path) is create_authored_goal_driver
            assert driver_options == options["owner_driver_options"][index]
            if index == 0:
                goal.set()
                readiness.publish_ready("party_qualified")
                while not cancel.wait(0.001) and time.monotonic() < deadline:
                    record["continuations"] += 1
            else:
                while not readiness.peer_ready("party_qualified"):
                    assert not cancel.wait(0.001)
                    assert time.monotonic() < deadline
                # First local goal cannot stop the pair during this delay.
                assert not cancel.wait(0.05)
                goal.set()
                assert cancel.wait(max(0, deadline - time.monotonic()))
            assert goal_stop.is_set()
            record["termination"] = "goal_cancelled"
            record["local_goal"] = goal.is_set()
        else:
            if index == 0:
                done.set()
            assert cancel.wait(max(0, deadline - time.monotonic()))
            record["termination"] = "frame_bound" if index == 0 else "cancelled_or_deadline"
    except (AssertionError, RuntimeError, ValueError, TimeoutError) as exc:
        record["errors"].append(f"{type(exc).__name__}: {exc}")
        done.set()
    finally:
        sender.send_bytes(json.dumps(record).encode())
        sender.close()
        sock.close()
