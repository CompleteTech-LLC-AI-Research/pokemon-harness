"""Source-only #623 controls; controlled endpoints do not qualify native PyBoy.

The actual-main visible mode discloses a navigation-only forwarding overlay;
its final screenshot restores Session.step before the genuine hold branch.
Use the SAME tests on unchanged baseline and candidate under separate EVI.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from pokered_harness.config import VersionsConfig, load_versions
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link._pyboy_link_session_support import PairedSessionOperationError
from pokered_harness.ownership import owner_for
from pokered_harness.session import Session, VersionMismatch
from pokered_harness.symbols.loader import load_sym_text
from tests.fakes import FakePyBoy
from tests.test_pyboy_link_session import _FakePyBoy

ROOT = Path(__file__).resolve().parents[1]


def _module(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helpers(monkeypatch):
    return _module("battle_pair_controls", ROOT / "scripts/_live_battle_demo_pair.py", monkeypatch)


@pytest.fixture
def demo(monkeypatch):
    return _module("battle_demo_controls", ROOT / "scripts/live_battle_demo.py", monkeypatch)


class Endpoint(_FakePyBoy):
    """Actual provider scheduler, tiny instruction clock and explicit input model.

    Queue rules mirror inspected bundled boundaries, not a native execution.
    """

    def __init__(self):
        super().__init__()
        self.paused = self.quitting = self.stopped = False
        self.memory = defaultdict(int)
        self.pending = []
        self.consumed = []
        self.hooks = {}
        self.boundary = []
        self.presentations = []
        self.on_events = lambda: None

    def _handle_events(self, events):
        super()._handle_events(events)
        self.boundary.append(("pre", self.frame_count))
        for event in events:
            self.consumed.append(event)
            if event == "PAUSE":
                self.paused = True
            elif event == "QUIT":
                self.quitting = True
        self.on_events()
        for callback, context in list(self.hooks.values()):
            callback(context)

    def _post_handle_events(self):
        self.boundary.append(("post", self.frame_count))
        self.events.clear()
        remaining = []
        for frame, event in self.pending:
            if frame == self.frame_count:
                self.events.append(event)
            else:
                remaining.append((frame, event))
        self.pending = remaining

    def button(self, name, duration=1):
        self.events.append(("press", name))
        self.pending.append((self.frame_count + duration, ("release", name)))

    def button_press(self, name):
        self.events.append(("press", name))

    def button_release(self, name):
        self.events.append(("release", name))

    def hook_register(self, bank, addr, callback, context):
        self.hooks[bank, addr] = callback, context

    def hook_deregister(self, bank, addr):
        self.hooks.pop((bank, addr), None)

    def tick(self, count=1, render=False, sound=False):
        self.presentations.append((count, render, sound))
        self.frame_count += count
        return super().tick(count, render, sound)


@pytest.fixture
def pair(monkeypatch):
    # Deliberately tiny physical quantum for controlled shells, not an ABI claim.
    monkeypatch.setattr(PyBoyLinkSession, "PHYSICAL_QUANTUM", 64)
    endpoints = (Endpoint(), Endpoint())
    symbols = load_sym_text("00:1234 Probe\n00:0000 hSerialConnectionStatus\n")
    sessions = tuple(Session(pyboy=p, symbols=symbols) for p in endpoints)
    provider = PyBoyLinkSession.local()
    originals = tuple(p.mb.serial for p in endpoints)
    for endpoint in endpoints:
        provider.attach(endpoint)
    yield provider, sessions, endpoints, originals
    provider.detach_all()


def test_positive_pair_progress_subsequent_render_and_guard(pair, helpers):
    provider, sessions, endpoints, _ = pair
    progress = helpers.PairProgress(provider, *sessions, view=True)
    for session in sessions:
        with pytest.raises(PairedSessionOperationError):
            session.step(1)
    progress.advance(3)
    progress.advance(2)
    assert progress.requested == 5 and progress.completed == [5, 5]
    assert tuple(s.current_tick() for s in sessions) == (5, 5)
    assert tuple(p.frame_count for p in endpoints) == (5, 5)
    assert provider._scheduler_quantum_count == 5
    assert all(p.presentations == [(0, True, False)] * 5 for p in endpoints)


def test_hook_start_tick_history_and_queued_button_boundaries(pair, helpers):
    provider, sessions, endpoints, _ = pair
    progress = helpers.PairProgress(provider, *sessions)
    seen = []
    a = sessions[0]
    generations = (a._load_generation, a._reset_generation)
    a.register_hook_at("Probe", lambda _: seen.append(a.current_tick()))
    a.register_hook("Probe", "probe")
    initial = a.events.emit(tick=0, name="prior", bank=0, addr=0)
    a.press("a", duration=2)
    a.hold("up")
    a.release("up")
    progress.advance(2)
    assert seen == [0, 1] and a.current_tick() == 2
    assert [(e.name, e.tick) for e in a.events] == [("prior", 0), ("probe", 0), ("probe", 1)]
    assert list(a.events)[0] is initial
    assert (a._load_generation, a._reset_generation) == generations
    assert ("press", "a") in endpoints[0].consumed
    assert ("press", "up") in endpoints[0].consumed
    assert ("release", "up") in endpoints[0].consumed
    assert ("release", "a") not in endpoints[0].consumed
    assert endpoints[0].events == [("release", "a")]
    progress.advance(1)
    assert seen == [0, 1, 2]
    assert endpoints[0].consumed.count(("release", "a")) == 1


@pytest.mark.parametrize("peer", [0, 1], ids=['case00-0', 'case01-1'])
@pytest.mark.parametrize("flag", ["paused", "quitting"], ids=['case00-paused', 'case01-quitting'])
def test_preexisting_pause_quit_refuses_without_consuming(pair, helpers, peer, flag):
    provider, sessions, endpoints, _ = pair
    setattr(endpoints[peer], flag, True)
    endpoints[peer].events.append("pending")
    progress = helpers.PairProgress(provider, *sessions)
    with pytest.raises(RuntimeError, match="before progression"):
        progress.advance(4)
    assert provider._scheduler_quantum_count == 0
    assert endpoints[peer].events == ["pending"]
    assert [s.current_tick() for s in sessions] == [0, 0]


@pytest.mark.parametrize("peer", [0, 1], ids=['case00-0', 'case01-1'])
@pytest.mark.parametrize("event,flag", [("PAUSE", "paused"), ("QUIT", "quitting")], ids=['case00-PAUSE-paused', 'case01-QUIT-quitting'])
def test_actual_provider_event_transition_one_one_stops(pair, helpers, peer, event, flag):
    provider, sessions, endpoints, _ = pair
    endpoints[peer].events.append(event)
    sessions[peer].press("a", duration=2)
    progress = helpers.PairProgress(provider, *sessions)
    with pytest.raises(RuntimeError, match="during progression"):
        progress.advance(4)
    assert getattr(endpoints[peer], flag) is True
    assert event in endpoints[peer].consumed
    assert progress.last_evidence["deltas"] == [1, 1]
    assert progress.last_evidence["accounted"] == [True, True]
    assert [s.current_tick() for s in sessions] == [1, 1]
    assert progress.completed == [1, 1] and progress.requested == 4
    assert provider._scheduler_quantum_count == 1
    assert endpoints[peer].pending == [(2, ("release", "a"))]
    with pytest.raises(RuntimeError, match="no replay"):
        progress.advance(1)
    assert provider._scheduler_quantum_count == 1


@pytest.mark.parametrize("state", [None, 0, "false"], ids=['case00-None', 'case01-0', 'case02-false'])
def test_unsupported_flag_refuses(pair, helpers, state):
    provider, sessions, endpoints, _ = pair
    endpoints[0].paused = state
    with pytest.raises(RuntimeError, match="unsupported"):
        helpers.PairProgress(provider, *sessions).advance(2)
    assert provider._scheduler_quantum_count == 0


def test_flag_read_failure_refuses_before_step(pair, helpers, monkeypatch):
    provider, sessions, _, _ = pair
    error = RuntimeError("state read")
    def broken(_):
        raise error
    monkeypatch.setattr(Endpoint, "quitting", property(broken), raising=False)
    with pytest.raises(RuntimeError) as caught:
        helpers.PairProgress(provider, *sessions).advance(2)
    assert caught.value is error
    assert provider._scheduler_quantum_count == 0


@pytest.mark.parametrize("deltas", [(0, 0), (1, 0), (0, 1), (-1, 1), (2, 1)], ids=['case00-0-0', 'case01-1-0', 'case02-0-1', 'case03-_1-1', 'case04-2-1'])
def test_partial_or_invalid_deltas_no_replay(pair, helpers, monkeypatch, deltas):
    provider, sessions, endpoints, _ = pair
    for p in endpoints:
        p.frame_count = 5
    calls = []
    def step(frames, *, render):
        calls.append((frames, render))
        for p, delta in zip(endpoints, deltas):
            p.frame_count += delta
    monkeypatch.setattr(provider, "step_interleaved", step)
    progress = helpers.PairProgress(provider, *sessions)
    with pytest.raises(RuntimeError):
        progress.advance(4)
    assert calls == [(1, False)]
    assert progress.last_evidence["deltas"] == list(deltas)
    assert [s.current_tick() for s in sessions] == [d if d in (0, 1) else 0 for d in deltas]
    with pytest.raises(RuntimeError, match="no replay"):
        progress.advance(1)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit], ids=['case00-RuntimeError', 'case01-KeyboardInterrupt', 'case02-SystemExit'])
def test_actual_post_first_increment_error_preserves_primary(pair, helpers, monkeypatch, failure):
    provider, sessions, endpoints, _ = pair
    error = failure("first post")
    def fail():
        raise error
    monkeypatch.setattr(endpoints[0], "_post_handle_events", fail)
    progress = helpers.PairProgress(provider, *sessions)
    with pytest.raises(failure) as caught:
        progress.advance(3)
    assert caught.value is error
    assert progress.last_evidence["deltas"] == [1, 0]
    assert [s.current_tick() for s in sessions] == [1, 0]
    assert provider._scheduler_quantum_count == 1
    assert any("partial" in str(e) for e in error.pair_secondary_errors)


@pytest.mark.parametrize("failure", ["counter", "bookkeeping", "state"], ids=['case00-counter', 'case01-bookkeeping', 'case02-state'])
def test_secondary_observation_failure_still_accounts_other_peer(pair, helpers, monkeypatch, failure):
    provider, sessions, endpoints, _ = pair
    primary = RuntimeError("provider primary")
    secondary = RuntimeError("secondary")
    def step(*args, **kwargs):
        for p in endpoints:
            p.frame_count += 1
        if failure == "counter":
            endpoints[0].frame_count = "unknown"
        elif failure == "state":
            endpoints[0].paused = "unknown"
        raise primary
    if failure == "bookkeeping":
        def advance(_):
            raise secondary
        monkeypatch.setattr(sessions[0], "_advance_tick", advance)
    monkeypatch.setattr(provider, "step_interleaved", step)
    progress = helpers.PairProgress(provider, *sessions)
    with pytest.raises(RuntimeError) as caught:
        progress.advance(3)
    assert caught.value is primary
    assert primary.pair_secondary_errors
    assert sessions[1].current_tick() == 1
    assert progress.last_evidence["accounted"][1] is True
    assert progress.failed


@pytest.mark.parametrize("frames", [0, -1, True, 1.0, "1"], ids=['case00-0', 'case01-_1', 'case02-True', 'case03-1.0', 'case04-1'])
def test_invalid_requested_frames_refuses(pair, helpers, frames):
    provider, sessions, _, _ = pair
    with pytest.raises(ValueError):
        helpers.PairProgress(provider, *sessions).advance(frames)
    assert provider._scheduler_quantum_count == 0


@pytest.mark.parametrize("mode", ["closed", "timed", "mismatch", "badcounter"], ids=['case00-closed', 'case01-timed', 'case02-mismatch', 'case03-badcounter'])
def test_admission_refuses_without_provider_step(pair, helpers, mode):
    provider, sessions, endpoints, _ = pair
    if mode == "closed":
        sessions[0]._closed = True
    elif mode == "timed":
        sessions[0]._timed_endpoint = object()
    elif mode == "mismatch":
        provider.detach(endpoints[1])
    else:
        endpoints[0].frame_count = True
    try:
        with pytest.raises(Exception):
            helpers.PairProgress(provider, *sessions).advance(1)
        assert provider._scheduler_quantum_count == 0
    finally:
        sessions[0]._closed = False
        sessions[0]._timed_endpoint = None


class CloseOwner:
    def __init__(self, label, calls, error=None):
        self.label, self.calls, self.error = label, calls, error
        self._stop_attempt = object()
        self.closed = False

    def close(self):
        self.calls.append(self.label)
        self.closed = True
        if self.error is not None:
            raise self.error


@pytest.mark.parametrize("where", ["detach", "A", "B"], ids=['case00-detach', 'case01-A', 'case02-B'])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit], ids=['case00-RuntimeError', 'case01-KeyboardInterrupt', 'case02-SystemExit'])
def test_independent_cleanup_primary_and_exact_unresolved_identity(helpers, where, failure):
    calls = []
    error = failure("cleanup")
    primary = RuntimeError("operation")
    a = CloseOwner("A", calls, error if where == "A" else None)
    b = CloseOwner("B", calls, error if where == "B" else None)
    def detach():
        calls.append("detach")
        if where == "detach":
            error.add_note("additional detach cleanup failure: retained provider note")
            raise error
    provider = SimpleNamespace(detach_all=detach)
    owner = helpers.DemoResources(("A", a), ("B", b))
    owner.provider = provider
    with pytest.raises(BaseExceptionGroup) as caught:
        owner.cleanup(active_error=primary)
    assert calls == ["detach", "A", "B"]
    assert caught.value.__cause__ is primary
    assert caught.value.exceptions == (error,)
    assert isinstance(caught.value, ExceptionGroup) == issubclass(failure, Exception)
    assert caught.value.cleanup_owner is owner
    assert owner.provider is provider and owner.sessions == [("A", a), ("B", b)]
    record = owner.unresolved[0]
    expected = {"detach": provider, "A": a, "B": b}[where]
    assert record["owner"] is expected and record["error"] is error
    if where != "detach":
        assert record["stop_attempt"] is expected._stop_attempt
    else:
        assert any("retained provider note" in note for note in error.__notes__)


def test_multiple_cleanup_members_and_explicit_same_owner_retry(helpers):
    calls = []
    errors = [RuntimeError("A"), KeyboardInterrupt("B")]
    a, b = (CloseOwner(label, calls, error) for label, error in zip(("A", "B"), errors))
    owner = helpers.DemoResources(("A", a), ("B", b))
    with pytest.raises(BaseExceptionGroup) as caught:
        owner.cleanup()
    assert caught.value.exceptions == tuple(errors)
    attempts = tuple(record["stop_attempt"] for record in owner.unresolved)
    a.error = b.error = None
    owner.cleanup()  # explicitly requested retry, no replacement handles
    assert calls == ["A", "B", "A", "B"]
    assert tuple(record["stop_attempt"] for record in owner.unresolved) == attempts
    assert owner.sessions[0][1] is a and owner.sessions[1][1] is b


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit], ids=['case00-RuntimeError', 'case01-KeyboardInterrupt', 'case02-SystemExit'])
def test_clean_cleanup_preserves_original_operation(demo, failure):
    error = failure("original")
    calls = []
    owner = demo._pair_helpers().DemoResources(("A", CloseOwner("A", calls)))
    with pytest.raises(failure) as caught:
        try:
            raise error
        finally:
            owner.cleanup(active_error=error)
    assert caught.value is error and calls == ["A"]


@pytest.mark.parametrize("attached", [1, 2], ids=['case00-1', 'case01-2'])
def test_actual_detach_restores_serial_backend_and_membership(pair, helpers, monkeypatch, attached):
    provider, sessions, endpoints, originals = pair
    if attached == 1:
        provider.detach(endpoints[1])
    calls = []
    owner = helpers.DemoResources(*[(str(i), s) for i, s in enumerate(sessions)])
    owner.provider = provider
    for i, session in enumerate(sessions):
        def close(index=i):
            assert provider.attached == ()
            assert all(p.mb.serial is serial for p, serial in zip(endpoints, originals))
            assert all(owner_for(p)._provider is None for p in endpoints)
            calls.append(index)
        monkeypatch.setattr(session, "close", close)
    owner.cleanup()
    assert calls == [0, 1]
    assert provider.attached == ()
    assert tuple(p.mb.serial for p in endpoints) == originals


def _trade_trap(monkeypatch):
    trade = ModuleType("live_trade_demo")
    def forbidden(*args, **kwargs):
        pytest.fail("forbidden trade opener / SHA bypass")
    trade._open_session = forbidden
    trade._find_pyboy_hwnds = lambda: []
    trade._force_move_pyboy_windows = lambda positions: False
    monkeypatch.setitem(sys.modules, "live_trade_demo", trade)
    monkeypatch.setenv("POKERED_SKIP_SHA1", "1")  # must not bypass strict opener
    return trade


@pytest.mark.parametrize("phase", ["constructor", "first_attach", "before_second_attach", "second_attach", "progress"], ids=['case00-constructor', 'case01-first_attach', 'case02-before_second_attach', 'case03-second_attach', 'case04-progress'])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit], ids=['case00-RuntimeError', 'case01-KeyboardInterrupt', 'case02-SystemExit'])
def test_actual_main_partial_setup_custody(demo, pair, helpers, monkeypatch, tmp_path, phase, failure):
    provider, sessions, endpoints, _ = pair
    provider.detach_all()
    calls = []
    primary = failure(phase)
    cleanup = RuntimeError("detach cleanup")
    trade = _trade_trap(monkeypatch)
    trade._ROM_PATHS = {"red": (), "blue": ()}
    trade._assert_fixtures_available = lambda version: None
    trade.Shooter = lambda outdir: SimpleNamespace(shoot_pair=lambda *args: None)
    monkeypatch.setattr(demo, "_open_battle_sessions", lambda *args, **kwargs: sessions)
    def local(**kwargs):
        calls.append("constructor")
        if phase == "constructor":
            raise primary
        return provider
    monkeypatch.setattr(PyBoyLinkSession, "local", local)
    actual_attach = provider.attach
    def attach(endpoint):
        calls.append(endpoint)
        if phase == "before_second_attach" and len(calls) == 3:
            raise primary
        actual_attach(endpoint)
        if len(calls) == (2 if phase == "first_attach" else 3) and phase.endswith("attach"):
            raise primary
    monkeypatch.setattr(provider, "attach", attach)
    trade._install_hook_counter = lambda *args: None
    monkeypatch.setattr(Session, "read_game_state", lambda session: SimpleNamespace(
        overworld=SimpleNamespace(x=0, y=0, map_id=0)))
    if phase == "progress":
        def progress(*args, **kwargs):
            raise primary
        monkeypatch.setattr(provider, "step_interleaved", progress)
    def detach():
        calls.append("detach")
        raise cleanup
    actual_detach = provider.detach_all
    monkeypatch.setattr(provider, "detach_all", detach)
    for i, s in enumerate(sessions):
        monkeypatch.setattr(s, "close", lambda index=i: calls.append(index))
    monkeypatch.setattr(sys, "argv", ["demo", "--outdir", str(tmp_path)])
    try:
        if phase == "constructor":
            with pytest.raises(failure) as caught:
                demo.main()
            assert caught.value is primary
            assert "detach" not in calls
        else:
            with pytest.raises(ExceptionGroup) as caught:
                demo.main()
            assert caught.value.__cause__ is primary
            assert caught.value.cleanup_owner.provider is provider
            assert caught.value.cleanup_owner.sessions == [("A", sessions[0]), ("B", sessions[1])]
            assert "detach" in calls
        assert calls[-2:] == [0, 1]
    finally:
        monkeypatch.setattr(provider, "detach_all", actual_detach)
        actual_detach()


def test_partial_opener_nested_holder_retains_failed_second_session(demo, monkeypatch):
    _trade_trap(monkeypatch)
    calls = []
    first = CloseOwner("first", calls, RuntimeError("first close"))
    second = CloseOwner("second", calls, RuntimeError("second close"))
    operation = KeyboardInterrupt("state load")
    def opener(version, **kwargs):
        if version == "red":
            return first
        demo._close_sessions(("blue", second), active_error=operation)
    monkeypatch.setattr(demo, "_open_validated_session", opener)
    with pytest.raises(ExceptionGroup) as caught:
        demo._open_battle_sessions("red", "blue", view=False)
    assert calls == ["second", "first"]
    assert caught.value.cleanup_owner.sessions == [("A", first), ("B", None)]
    nested = caught.value.__cause__
    assert nested.cleanup_owner.sessions == [("blue", second)]
    assert nested.__cause__ is operation


def _route(demo, pair, monkeypatch, tmp_path, *, visible):
    provider, sessions, endpoints, _ = pair
    provider.detach_all()
    trade = _trade_trap(monkeypatch)
    record = {"attach": [], "routes": [], "owner_calls": [], "shots": [], "restored": False}
    trade._ROM_PATHS = {"red": (), "blue": ()}
    trade._assert_fixtures_available = lambda version: None
    monkeypatch.setattr(demo, "_open_battle_sessions", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(PyBoyLinkSession, "local", lambda **kwargs: provider)
    actual_attach = provider.attach
    def attach(endpoint):
        result = actual_attach(endpoint)
        record["attach"].append(endpoint)
        print(f"[623 route witness] actual attach {len(record['attach'])}")
        return result
    monkeypatch.setattr(provider, "attach", attach)
    hooks = [[], []]
    def install(session, symbol, bucket, index):
        hooks[index].append((symbol, bucket, index))
    trade._install_hook_counter = install
    for index, endpoint in enumerate(endpoints):
        def events(i=index, p=endpoint):
            thresholds = {"SaveGameData": 61, "LinkMenu": 101, "CableClub_DoBattleOrTrade": 170}
            for symbol, bucket, peer in hooks[i]:
                if symbol in thresholds and p.frame_count >= thresholds[symbol]:
                    bucket[peer] = 1
        endpoint.on_events = events
    def state(session):
        frame = session._pyboy.frame_count
        return SimpleNamespace(overworld=SimpleNamespace(x=0, y=0, map_id=0xF0 if frame >= 150 else 0))
    monkeypatch.setattr(Session, "read_game_state", state)
    for session in sessions:
        monkeypatch.setattr(session, "close", lambda: None)
    original_step = Session.step
    if visible:
        def forward(session, count=1, *, render=None):
            # Fixture-only prior navigation; real original guard restored at witness.
            assert not record["restored"]
            if session is sessions[0]:
                provider.step_interleaved(count, render=False)
                for peer in sessions:
                    peer._advance_tick(count)
        monkeypatch.setattr(Session, "step", forward)
    actual_step = provider.step_interleaved
    def step(frames, **kwargs):
        record["routes"].append(tuple(frame.function for frame in inspect.stack(context=0)))
        record["owner_calls"].append((frames, kwargs.get("render")))
        return actual_step(frames, **kwargs)
    monkeypatch.setattr(provider, "step_interleaved", step)
    def shoot(a, b, phase):
        record["shots"].append((phase, endpoints[0].frame_count))
        if phase == "bphase_08_battle_final" and visible:
            monkeypatch.setattr(Session, "step", original_step)
            record["restored"] = True
            print("[623 route witness] final screenshot reached; original Session.step restored before hold")
    trade.Shooter = lambda outdir: SimpleNamespace(shoot_pair=shoot)
    args = ["demo", "--outdir", str(tmp_path), "--battle-budget-frames", "20", "--hold-after-s", "1"]
    if visible:
        args.append("--view")
    monkeypatch.setattr(sys, "argv", args)
    return record, original_step


def test_actual_main_coarse_and_fine_same_overlay_unmet_goal(demo, pair, monkeypatch, tmp_path):
    record, _ = _route(demo, pair, monkeypatch, tmp_path, visible=False)
    assert demo.main() == 1  # Baseline must FAIL here through actual paired guard, not be xfailed.
    provider, sessions, endpoints, _ = pair
    assert record["attach"] == list(endpoints)
    assert any("tick_both_coarse" in route for route in record["routes"])
    assert any("tick_both_fine" in route for route in record["routes"])
    assert sum("tick_both_coarse" in route for route in record["routes"]) == 100
    assert sum("tick_both_fine" in route for route in record["routes"]) == 105
    assert record["owner_calls"] == [(1, False)] * 205
    assert [p.frame_count for p in endpoints] == [205, 205]
    assert record["shots"][-1][0] == "bphase_08_battle_final"
    assert [s.current_tick() for s in sessions] == [p.frame_count for p in endpoints]
    assert provider.attached == ()
    assert all(not p.presentations for p in endpoints)


def test_actual_main_visible_isolated_hold_same_overlay(demo, pair, monkeypatch, tmp_path):
    record, original_step = _route(demo, pair, monkeypatch, tmp_path, visible=True)
    assert demo.main() == 1  # Unchanged baseline reaches original guard only AFTER witness.
    provider, sessions, endpoints, _ = pair
    assert record["attach"] == list(endpoints)
    assert record["restored"] and Session.step is original_step
    assert record["owner_calls"] == [(1, True)] * 265
    hold_start = record["shots"][-1][1]
    assert [p.frame_count - hold_start for p in endpoints] == [60, 60]
    assert all(all(call == (0, True, False) for call in p.presentations) for p in endpoints)
    assert [s.current_tick() for s in sessions] == [p.frame_count for p in endpoints]
    assert provider.attached == ()


@pytest.fixture
def opener_inputs(demo, monkeypatch, tmp_path):
    trade = _trade_trap(monkeypatch)
    rom, sym, state = (tmp_path / name for name in ("rom.gb", "rom.sym", "state.bin"))
    rom.write_bytes(b"authored diagnostic control bytes, not a legal ROM")
    sym.write_text("00:1234 Probe\n", encoding="utf-8")
    state.write_bytes(b"authored state model")
    trade._ROM_PATHS = {"red": (rom, sym)}
    trade._state_path = lambda version: state
    calls = []
    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return FakePyBoy(defaultdict(int))
    pyboy = ModuleType("pyboy")
    pyboy.PyBoy = factory
    pyboy.__version__ = "2.7.0"
    pyboy.__pokered_harness_revision__ = "a" * 40
    monkeypatch.setitem(sys.modules, "pyboy", pyboy)
    import pokered_harness.session as session_module
    monkeypatch.setattr(session_module, "_default_pyboy_factory", trade._open_session)
    pins = SimpleNamespace(
        sha1_for_path=lambda path: hashlib.sha1(rom.read_bytes()).hexdigest(),
        symbol_sha1_for_path=lambda path: hashlib.sha1(sym.read_bytes()).hexdigest(),
        pyboy_version="2.7.0", pyboy_revision="a" * 40,
    )
    import pokered_harness.config as config
    monkeypatch.setattr(config, "load_versions", lambda path: pins)
    # Observe real load_state/step without Session.close spawning a stop worker.
    monkeypatch.setattr(Session, "close", lambda session: session._pyboy.stop())
    return SimpleNamespace(pins=pins, runtime=pyboy, calls=calls, rom=rom, sym=sym,
                           state=state, config=config, trade=trade)


@pytest.mark.parametrize("category", [
    "rom_pin_missing", "sym_pin_missing", "rom_pin_malformed", "sym_pin_malformed",
    "version_missing", "version_empty", "version_nonstring", "revision_missing",
    "revision_empty", "revision_nonstring", "revision_malformed", "parsed_no_revision",
    "version_row_missing", "version_row_malformed", "revision_row_malformed",
    "rom_file_missing", "sym_file_missing", "rom_mismatch", "sym_mismatch",
    "actual_version_missing", "actual_revision_missing", "actual_version_mismatch", "actual_revision_mismatch",
], ids=['case00-rom_pin_missing', 'case01-sym_pin_missing', 'case02-rom_pin_malformed', 'case03-sym_pin_malformed', 'case04-version_missing', 'case05-version_empty', 'case06-version_nonstring', 'case07-revision_missing', 'case08-revision_empty', 'case09-revision_nonstring', 'case10-revision_malformed', 'case11-parsed_no_revision', 'case12-version_row_missing', 'case13-version_row_malformed', 'case14-revision_row_malformed', 'case15-rom_file_missing', 'case16-sym_file_missing', 'case17-rom_mismatch', 'case18-sym_mismatch', 'case19-actual_version_missing', 'case20-actual_revision_missing', 'case21-actual_version_mismatch', 'case22-actual_revision_mismatch'])
def test_actual_opener_strict_validation_no_factory(demo, opener_inputs, monkeypatch, tmp_path, category):
    f = opener_inputs
    expected = ""
    if category.startswith("rom_pin") or category.startswith("sym_pin"):
        attr = "sha1_for_path" if category.startswith("rom") else "symbol_sha1_for_path"
        setattr(f.pins, attr, lambda path: None if category.endswith("missing") else "not-a-hash")
        expected = "pin must be exactly"
    elif category in {"version_missing", "version_empty", "version_nonstring"}:
        f.pins.pyboy_version = {"version_missing": None, "version_empty": " ", "version_nonstring": 7}[category]
        expected = "version pin"
    elif category in {"revision_missing", "revision_empty", "revision_nonstring", "revision_malformed"}:
        f.pins.pyboy_revision = {"revision_missing": None, "revision_empty": "", "revision_nonstring": 7,
                                  "revision_malformed": "g" * 40}[category]
        expected = "revision pin"
    elif category in {"parsed_no_revision", "version_row_missing", "version_row_malformed", "revision_row_malformed"}:
        row = {"parsed_no_revision": "| PyBoy | `2.7.0` |",
               "version_row_missing": "",
               "version_row_malformed": "| PyBoy | 2.7.0 |",
               "revision_row_malformed": "| PyBoy | `2.7.0` + fork `bad` |"}[category]
        path = tmp_path / "VERSIONS.md"
        path.write_text(f"| SHA-1 | `{'0' * 40}` |\n{row}\n")
        def parsed(_):
            value = load_versions(path)
            return VersionsConfig(value.rom_sha1, value.pyboy_version, value.pyboy_revision,
                                  ((str(f.rom).lstrip("./").lower(), hashlib.sha1(f.rom.read_bytes()).hexdigest()),),
                                  ((str(f.sym).lstrip("./").lower(), hashlib.sha1(f.sym.read_bytes()).hexdigest()),))
        monkeypatch.setattr(f.config, "load_versions", parsed)
        expected = "revision pin" if category == "parsed_no_revision" else "no PyBoy version row"
    elif category.endswith("file_missing"):
        target = f.rom if category.startswith("rom") else f.sym
        # Keep selected expected pins valid when the actual file vanishes.
        rom_hash = hashlib.sha1(f.rom.read_bytes()).hexdigest()
        sym_hash = hashlib.sha1(f.sym.read_bytes()).hexdigest()
        f.pins.sha1_for_path = lambda path: rom_hash
        f.pins.symbol_sha1_for_path = lambda path: sym_hash
        target.unlink()
        expected = "file not found"
    elif category in {"rom_mismatch", "sym_mismatch"}:
        attr = "sha1_for_path" if category == "rom_mismatch" else "symbol_sha1_for_path"
        setattr(f.pins, attr, lambda path: "0" * 40)
        expected = "SHA-1 mismatch"
    else:
        attr = "__version__" if "version" in category else "__pokered_harness_revision__"
        if category.endswith("missing"):
            delattr(f.runtime, attr)
        else:
            setattr(f.runtime, attr, "wrong" if attr == "__version__" else "b" * 40)
        expected = "not the pinned" if category == "actual_revision_missing" else "mismatch"
    with pytest.raises((ValueError, VersionMismatch), match=expected):
        demo._open_validated_session("red")
    assert f.calls == []


def test_actual_opener_positive_normalizes_and_passes_all_pins(demo, opener_inputs, monkeypatch):
    f = opener_inputs
    f.pins.pyboy_revision = "A" * 40
    f.pins.pyboy_version = " 2.7.0 "
    actual = Session.from_files.__func__
    seen = []
    def from_files(cls, *args, **kwargs):
        seen.append(kwargs.copy())
        return actual(cls, *args, **kwargs)
    monkeypatch.setattr(Session, "from_files", classmethod(from_files))
    session = demo._open_validated_session("red")
    try:
        assert len(f.calls) == 1
        assert seen[0]["expected_pyboy_revision"] == "a" * 40
        assert seen[0]["expected_pyboy_version"] == "2.7.0"
        assert seen[0]["expected_rom_sha1"] == hashlib.sha1(f.rom.read_bytes()).hexdigest()
        assert seen[0]["expected_symbol_sha1"] == hashlib.sha1(f.sym.read_bytes()).hexdigest()
        assert session.current_tick() == 12
        assert session._pyboy.tick_calls == [(12, True)]
        assert session._pyboy._saved_state == f.state.read_bytes()
    finally:
        session.close()
    assert session._pyboy.stopped


def test_actual_opener_load_failure_retains_cleanup_owner(demo, opener_inputs, monkeypatch):
    f = opener_inputs
    primary = SystemExit("load")
    cleanup = RuntimeError("close")
    def load(session, data):
        raise primary
    def close(session):
        raise cleanup
    monkeypatch.setattr(Session, "load_state", load)
    monkeypatch.setattr(Session, "close", close)
    with pytest.raises(ExceptionGroup) as caught:
        demo._open_validated_session("red")
    assert len(f.calls) == 1
    assert caught.value.__cause__ is primary
    assert caught.value.exceptions == (cleanup,)
    assert isinstance(caught.value.cleanup_owner.sessions[0][1], Session)


def test_exact_adjacent_helper_loader_without_scripts_path(demo, monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p).resolve() != ROOT / "scripts"])
    poison = ModuleType("_live_battle_demo_pair")
    monkeypatch.setitem(sys.modules, "_live_battle_demo_pair", poison)
    helper = demo._pair_helpers()
    assert Path(helper.__file__).resolve() == ROOT / "scripts/_live_battle_demo_pair.py"
    assert helper is not poison and demo._pair_helpers() is helper


@pytest.mark.parametrize("name", ["battle_as_script", "scripts.live_battle_demo"], ids=['case00-battle_as_script', 'case01-scripts.live_battle_demo'])
def test_loader_script_and_package_names_without_ambient_scripts(monkeypatch, name):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p).resolve() != ROOT / "scripts"])
    module = _module(name, ROOT / "scripts/live_battle_demo.py", monkeypatch)
    assert Path(module._pair_helpers().__file__).resolve() == ROOT / "scripts/_live_battle_demo_pair.py"


@pytest.mark.parametrize("value", [None, "", 7, "a" * 39, "a" * 41, "g" * 40, " " + "a" * 39], ids=['case00-None', 'case01-', 'case02-7', 'case03-_a_39', 'case04-_a_41', 'case05-_g_40', 'case06-_a_39'])
def test_required_hash_refuses_missing_malformed_before_factory(helpers, value):
    with pytest.raises(ValueError, match="exactly 40 hexadecimal"):
        helpers.required_hash(value, "expected revision")


def test_post_state_read_failure_reconciles_both_no_next_quantum(pair, helpers, monkeypatch):
    provider, sessions, endpoints, _ = pair
    def read(endpoint):
        if endpoint.frame_count:
            raise RuntimeError("post state read")
        return False
    monkeypatch.setattr(Endpoint, "paused", property(read), raising=False)
    progress = helpers.PairProgress(provider, *sessions)
    with pytest.raises(RuntimeError, match="post state read"):
        progress.advance(4)
    assert progress.last_evidence["deltas"] == [1, 1]
    assert [s.current_tick() for s in sessions] == [1, 1]
    assert provider._scheduler_quantum_count == 1


def test_cleanup_attribute_failure_cannot_suppress_second_owner(helpers):
    calls = []
    class Broken:
        @property
        def close(self):
            raise KeyboardInterrupt("close lookup")
    broken = Broken()
    second = CloseOwner("second", calls)
    owner = helpers.DemoResources(("first", broken), ("second", second))
    with pytest.raises(BaseExceptionGroup) as caught:
        owner.cleanup()
    assert calls == ["second"]
    assert caught.value.cleanup_owner is owner
    assert owner.unresolved[0]["owner"] is broken


def test_cleanup_output_failure_is_secondary_after_all_owned_operations(helpers, monkeypatch):
    calls = []
    original = RuntimeError("first close")
    output = OSError("stderr")
    first = CloseOwner("first", calls, original)
    second = CloseOwner("second", calls)
    def fail_print(*args, **kwargs):
        raise output
    monkeypatch.setattr(helpers, "print", fail_print, raising=False)
    owner = helpers.DemoResources(("first", first), ("second", second))
    with pytest.raises(ExceptionGroup) as caught:
        owner.cleanup()
    assert calls == ["first", "second"]
    assert caught.value.exceptions == (original, output)


def test_actual_main_normal_goal_result_preserved(demo, pair, monkeypatch, tmp_path):
    record, _ = _route(demo, pair, monkeypatch, tmp_path, visible=False)
    trade = sys.modules["live_trade_demo"]
    install = trade._install_hook_counter
    def target(session, symbol, bucket, index):
        install(session, symbol, bucket, index)
        if symbol == "PlayerCalcMoveDamage":
            bucket[index] = 1
    trade._install_hook_counter = target
    assert demo.main() == 0
    assert record["shots"][-1][0] == "bphase_08_battle_final"
    assert pair[0].attached == ()


def test_independent_session_guard_preserved_on_both_versions(pair):
    provider, sessions, endpoints, _ = pair
    assert provider.attached == endpoints
    for session in sessions:
        with pytest.raises(PairedSessionOperationError, match="pair owner"):
            session.step(1)
    assert [s.current_tick() for s in sessions] == [0, 0]
    assert [p.frame_count for p in endpoints] == [0, 0]
    assert provider._scheduler_quantum_count == 0


def test_explicit_retained_provider_detach_retry_keeps_failure_custody(pair, helpers, monkeypatch):
    """Actual local detach; close/stop-attempt markers are counted source models."""
    provider, sessions, endpoints, originals = pair
    owner = helpers.DemoResources(("A", sessions[0]), ("B", sessions[1]))
    owner.provider = provider
    primary = SystemExit("operation")
    detach_error, close_error = RuntimeError("detach"), RuntimeError("close A")
    marker = object()  # synthetic pending-stop identity; no worker is launched
    monkeypatch.setattr(sessions[0], "_stop_attempt", marker)
    actual_detach = provider.detach_all
    calls = []
    detach_calls = []
    close_calls = []
    def detach():
        calls.append(("detach", provider))
        detach_calls.append(provider)
        if len(detach_calls) == 1:
            raise detach_error
        actual_detach()
    monkeypatch.setattr(provider, "detach_all", detach)
    for session in sessions:
        def close(current=session):
            calls.append(("close", current))
            close_calls.append(current)
            if len(detach_calls) == 1 and current is sessions[0]:
                raise close_error
            if len(detach_calls) == 2:
                assert provider.attached == ()
                assert all(p.mb.serial is old for p, old in zip(endpoints, originals))
                assert all(owner_for(p)._provider is None for p in endpoints)
        monkeypatch.setattr(session, "close", close)
    with pytest.raises(ExceptionGroup) as caught:
        owner.cleanup(active_error=primary)
    assert caught.value.__cause__ is primary
    assert caught.value.exceptions == (detach_error, close_error)
    assert caught.value.cleanup_owner is owner
    assert calls == [("detach", provider), ("close", sessions[0]), ("close", sessions[1])]
    assert detach_calls == [provider] and close_calls == list(sessions)
    assert provider.attached == endpoints  # first controlled detach did not run
    assert owner.provider is provider and owner.sessions == [("A", sessions[0]), ("B", sessions[1])]
    history = tuple(owner.attempts)
    pending = tuple(owner.unresolved)
    assert [record["status"] for record in history] == ["unresolved", "unresolved", "returned"]
    assert pending[0]["owner"] is provider and pending[0]["error"] is detach_error
    assert pending[1]["owner"] is sessions[0] and pending[1]["error"] is close_error
    assert pending[1]["stop_attempt"] is marker
    owner.cleanup()  # the sole explicit second attempt; same retained identities
    assert calls == [("detach", provider), ("close", sessions[0]), ("close", sessions[1])] * 2
    assert detach_calls == [provider, provider] and close_calls == list(sessions) * 2
    assert owner.provider is provider and owner.sessions == [("A", sessions[0]), ("B", sessions[1])]
    assert all(owner.attempts[i] is old for i, old in enumerate(history))
    assert [record["status"] for record in owner.attempts[3:]] == ["returned"] * 3
    assert len(owner.attempts) == 6 and len(owner.unresolved) == 2
    assert all(owner.unresolved[i] is old for i, old in enumerate(pending))
    assert pending[0]["error"] is detach_error and pending[1]["error"] is close_error
    assert pending[1]["stop_attempt"] is marker and sessions[0]._stop_attempt is marker
    assert caught.value.exceptions == (detach_error, close_error) and caught.value.__cause__ is primary
    assert provider.attached == ()
    monkeypatch.setattr(provider, "detach_all", actual_detach)  # fixture teardown adds no modeled retry
