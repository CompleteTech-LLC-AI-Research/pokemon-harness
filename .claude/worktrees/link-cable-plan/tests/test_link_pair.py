"""Tests for :class:`pokered_harness.link.pair.LinkPair`.

These tests inject a ``FakeBridge`` via ``bridge_factory`` so they never
depend on the real :class:`SerialBridge` implementation.
"""

from __future__ import annotations

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.pair import LinkPair
from pokered_harness.link.transport import LinkTransport
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy


# ---------------------------------------------------------------------------
# Fake bridge — replaces the real SerialBridge so tests don't need it.
# ---------------------------------------------------------------------------


class FakeBridge:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.install_calls = 0

    def install(self) -> None:
        self.install_calls += 1

    @property
    def installed(self) -> bool:
        return self.install_calls > 0


class FakeFactory:
    """Callable that records its calls and returns a fresh :class:`FakeBridge`."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.bridges: list[FakeBridge] = []

    def __call__(self, *args, **kwargs) -> FakeBridge:
        self.calls.append((args, kwargs))
        b = FakeBridge(*args, **kwargs)
        self.bridges.append(b)
        return b


# ---------------------------------------------------------------------------
# Session construction helpers.
# ---------------------------------------------------------------------------

# Just enough of the canonical pokered sym set for Session.read_game_state
# plus a PROGRESS-role label (Trade_ShowPlayerMon) so pair() has something to
# hook. TradeCenter_SelectMon is intentionally omitted to exercise the
# "silently skip missing PROGRESS label" path.
_SYM = """\
00:D35E wCurMap
00:D361 wYCoord
00:D362 wXCoord
00:D46A wWalkCounter
00:CC26 wCurrentMenuItem
00:CC28 wMaxMenuItem
00:D057 wIsInBattle
00:D163 wPartyCount
00:D16B wPartyMons
00:D356 wObtainedBadges
00:D31D wNumBagItems
00:D31E wBagItems
00:6AB1 Trade_ShowPlayerMon
"""


def _make_session() -> tuple[Session, FakePyBoy]:
    pb = FakePyBoy(DictMemory())
    sym = load_sym_text(_SYM)
    return Session(pyboy=pb, symbols=sym, event_bus=EventBus()), pb


def _make_pair(
    factory: FakeFactory | None = None,
) -> tuple[LinkPair, Session, Session, FakePyBoy, FakePyBoy, FakeFactory]:
    s_a, pb_a = _make_session()
    s_b, pb_b = _make_session()
    f = factory if factory is not None else FakeFactory()
    pair = LinkPair(
        s_a,
        s_b,
        version_primary="red",
        version_peer="red",
        bridge_factory=f,
    )
    return pair, s_a, s_b, pb_a, pb_b, f


# ---------------------------------------------------------------------------
# Construction.
# ---------------------------------------------------------------------------


def test_construction_without_pair_exposes_properties():
    pair, s_a, s_b, _, _, f = _make_pair()
    assert pair.paired is False
    assert pair.primary is s_a
    assert pair.peer is s_b
    assert isinstance(pair.transport, LinkTransport)
    # Fresh transport: no pending bytes either way.
    assert pair.transport.pending_a_to_b == 0
    assert pair.transport.pending_b_to_a == 0
    # Factory wasn't invoked yet.
    assert f.calls == []


# ---------------------------------------------------------------------------
# pair() / unpair().
# ---------------------------------------------------------------------------


def test_pair_invokes_factory_once_and_installs():
    pair, s_a, s_b, _, _, f = _make_pair()
    pair.pair()
    assert pair.paired is True
    assert len(f.calls) == 1
    args, kwargs = f.calls[0]
    assert args == (s_a, s_b, pair.transport)
    assert kwargs == {"version_a": "red", "version_b": "red"}
    assert len(f.bridges) == 1
    assert f.bridges[0].install_calls == 1
    assert f.bridges[0].installed is True


def test_pair_twice_raises_runtime_error():
    pair, _, _, _, _, _ = _make_pair()
    pair.pair()
    with pytest.raises(RuntimeError):
        pair.pair()


def test_pair_registers_progress_hook_for_present_symbol():
    pair, _, _, pb_a, pb_b, _ = _make_pair()
    pair.pair()
    # Trade_ShowPlayerMon is at 00:6AB1 in our fake sym table; pair()
    # should have installed an event-bus callback there on both sides.
    assert (0x00, 0x6AB1) in pb_a._hooks
    assert (0x00, 0x6AB1) in pb_b._hooks


def test_pair_silently_skips_missing_progress_symbol():
    # TradeCenter_SelectMon is not in _SYM, so register_hook would raise —
    # pair() must swallow that and continue.
    pair, _, _, pb_a, _, _ = _make_pair()
    pair.pair()  # must not raise
    # And the firing-able hook (Trade_ShowPlayerMon) should still be there.
    assert (0x00, 0x6AB1) in pb_a._hooks


def test_pair_progress_hook_emits_events():
    pair, s_a, s_b, pb_a, _, _ = _make_pair()
    pair.pair()
    # Fire the hook manually and verify the event landed on the bus.
    pb_a.fire(0x00, 0x6AB1)
    evt = s_a.events.latest("link.trade.show_player_mon")
    assert evt is not None
    assert evt.name == "link.trade.show_player_mon"


def test_unpair_clears_paired_flag_and_pre_existing_hooks_survive():
    pair, s_a, _, pb_a, _, _ = _make_pair()
    # Pre-register a session hook that has nothing to do with the bridge.
    s_a.register_hook("Trade_ShowPlayerMon", "unrelated_event")
    assert pair.paired is False

    pair.pair()
    assert pair.paired is True
    # Both the pre-existing hook and the pair()-installed one are present.
    assert len(pb_a._hooks[(0x00, 0x6AB1)]) >= 2

    pair.unpair()
    assert pair.paired is False
    # Hooks remain installed on the FakePyBoy (no hook_deregister) — the
    # pre-existing session hook must still fire.
    before = s_a.events.count("unrelated_event")
    pb_a.fire(0x00, 0x6AB1)
    after = s_a.events.count("unrelated_event")
    assert after == before + 1


def test_pair_after_unpair_installs_fresh_bridge():
    pair, _, _, _, _, f = _make_pair()
    pair.pair()
    pair.unpair()
    pair.pair()
    assert len(f.calls) == 2
    assert len(f.bridges) == 2
    # Each bridge got its own install() call.
    assert f.bridges[0].install_calls == 1
    assert f.bridges[1].install_calls == 1


# ---------------------------------------------------------------------------
# step().
# ---------------------------------------------------------------------------


def test_step_advances_both_sides_by_n_ticks():
    pair, s_a, s_b, _, _, _ = _make_pair()
    pair.step(5)
    assert s_a.current_tick() == 5
    assert s_b.current_tick() == 5


def test_step_with_large_n_interleaves_in_chunks():
    pair, s_a, s_b, pb_a, pb_b, _ = _make_pair()
    pair.step(10)
    # Each side ends at 10 ticks total.
    assert s_a.current_tick() == 10
    assert s_b.current_tick() == 10
    # Un-paired (no bridge): stepping uses CHUNK_SIZE=4 slice granularity,
    # giving tick_calls == [4, 4, 2] on each side. When paired, the hardware
    # serial tick runs per single emulator frame so tick_calls becomes
    # ones. `_make_pair` does not call pair.pair(), so we exercise the
    # chunk-slice path.
    assert [c for c, _ in pb_a.tick_calls] == [4, 4, 2]
    assert [c for c, _ in pb_b.tick_calls] == [4, 4, 2]


def test_step_rejects_non_positive():
    pair, _, _, _, _, _ = _make_pair()
    with pytest.raises(ValueError):
        pair.step(0)


def test_step_chunk_size_one_still_advances_correctly():
    pair, s_a, s_b, _, _, _ = _make_pair()
    pair.CHUNK_SIZE = 1  # per-instance override for this test
    pair.step(3)
    assert s_a.current_tick() == 3
    assert s_b.current_tick() == 3


# ---------------------------------------------------------------------------
# run_until_event_pair().
# ---------------------------------------------------------------------------


def test_run_until_event_pair_returns_when_event_present_on_primary():
    pair, s_a, _, _, _, _ = _make_pair()
    # Pre-seed the primary's bus with an event at tick=100 (> start_tick=0).
    s_a.events.emit(tick=100, name="link.trade.show_player_mon", bank=0, addr=0)
    result = pair.run_until_event_pair(
        "link.trade.show_player_mon", max_ticks=16, chunk=4,
    )
    assert result.reached is True
    assert result.event is not None
    assert result.event.name == "link.trade.show_player_mon"


def test_run_until_event_pair_returns_when_event_present_on_peer():
    pair, _, s_b, _, _, _ = _make_pair()
    s_b.events.emit(tick=50, name="link.trade.load_data", bank=0, addr=0)
    result = pair.run_until_event_pair(
        "link.trade.load_data", side="peer", max_ticks=16, chunk=4,
    )
    assert result.reached is True
    assert result.event is not None


def test_run_until_event_pair_times_out():
    pair, _, _, _, _, _ = _make_pair()
    result = pair.run_until_event_pair(
        "link.trade.show_player_mon", max_ticks=12, chunk=4,
    )
    assert result.reached is False
    assert result.event is None
    # ticks_spent is measured against the watched side's clock; since
    # step advances the watched side by `count` ticks per call, the
    # full budget should have been consumed.
    assert result.ticks_spent == 12


def test_run_until_event_pair_rejects_bad_side():
    pair, _, _, _, _, _ = _make_pair()
    with pytest.raises(ValueError):
        pair.run_until_event_pair("x", side="both", max_ticks=4)


def test_run_until_event_pair_rejects_empty_names():
    pair, _, _, _, _, _ = _make_pair()
    with pytest.raises(ValueError):
        pair.run_until_event_pair([], max_ticks=4)


def test_run_until_event_pair_rejects_bad_budget():
    pair, _, _, _, _, _ = _make_pair()
    with pytest.raises(ValueError):
        pair.run_until_event_pair("x", max_ticks=0)
    with pytest.raises(ValueError):
        pair.run_until_event_pair("x", max_ticks=4, chunk=0)
