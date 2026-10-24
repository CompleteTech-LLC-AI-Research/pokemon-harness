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


class RawBridge(FakeBridge):
    """Bridge double that installs the same raw hook as ``SerialBridge``."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._resolved_a = {"exchange_bytes": (0x00, 0x216F)}
        self._resolved_b = {"exchange_bytes": (0x00, 0x216F)}
        self.callback_calls = [0, 0]

    def install(self) -> None:
        super().install()
        for index, session in enumerate(self.args[:2]):

            def _callback(_ctx: object, *, _index=index) -> None:
                self.callback_calls[_index] += 1

            session.serial_hook("Serial_ExchangeBytes", _callback)


class PartiallyFailingRawBridge(RawBridge):
    """Install one bridge callback and then fail to exercise rollback."""

    def install(self) -> None:
        FakeBridge.install(self)

        def _callback(_ctx: object) -> None:
            return None

        self.args[0].serial_hook("Serial_ExchangeBytes", _callback)
        raise RuntimeError("injected bridge installation failure")


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


class RawFactory(FakeFactory):
    """Factory that returns a bridge with raw Session.serial_hook callbacks."""

    def __call__(self, *args, **kwargs) -> RawBridge:
        self.calls.append((args, kwargs))
        bridge = RawBridge(*args, **kwargs)
        self.bridges.append(bridge)
        return bridge


class PartiallyFailingRawFactory(FakeFactory):
    def __call__(self, *args, **kwargs) -> PartiallyFailingRawBridge:
        self.calls.append((args, kwargs))
        bridge = PartiallyFailingRawBridge(*args, **kwargs)
        self.bridges.append(bridge)
        return bridge


class StrictFakePyBoy(FakePyBoy):
    """Fake PyBoy with the real address-level duplicate-hook contract."""

    def hook_register(self, bank: int, addr: int, callback, context) -> None:
        if (bank, addr) in self._hooks:
            raise ValueError("Hook already registered for this bank and address")
        super().hook_register(bank, addr, callback, context)


class FailingHookPyBoy(FakePyBoy):
    """Inject a registration failure after earlier hooks have succeeded."""

    def __init__(self, memory) -> None:
        super().__init__(memory)
        self.fail_register = False

    def hook_register(self, bank: int, addr: int, callback, context) -> None:
        if self.fail_register:
            raise RuntimeError("injected hook registration failure")
        super().hook_register(bank, addr, callback, context)


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
00:216F Serial_ExchangeBytes
00:2200 Serial_ExchangeNybble
00:2210 wSerialExchangeNybbleSendData
00:2211 wSerialExchangeNybbleReceiveData
00:2220 Serial_ExchangeLinkMenuSelection
00:2230 wLinkMenuSelectionSendBuffer
00:2232 wLinkMenuSelectionReceiveBuffer
00:2300 LinkMenu.exchangeMenuSelectionLoop
"""

_CROSS_VERSION_SYM_A = """\
00:216F Serial_ExchangeBytes
00:D100 wSerialPlayerDataBlock
00:D200 wSerialRandomNumberListBlock
"""

_CROSS_VERSION_SYM_B = """\
00:1FCB Serial_ExchangeBytes
00:D110 wSerialPlayerDataBlock
00:D210 wSerialRandomNumberListBlock
"""


def _make_session(pyboy_type=FakePyBoy) -> tuple[Session, FakePyBoy]:
    pb = pyboy_type(DictMemory())
    sym = load_sym_text(_SYM)
    return Session(pyboy=pb, symbols=sym, event_bus=EventBus()), pb


def _make_pair(
    factory: FakeFactory | None = None,
    *,
    pyboy_type=FakePyBoy,
) -> tuple[LinkPair, Session, Session, FakePyBoy, FakePyBoy, FakeFactory]:
    s_a, pb_a = _make_session(pyboy_type)
    s_b, pb_b = _make_session(pyboy_type)
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


def test_pair_rejects_unknown_rom_version_before_factory_use():
    s_a, _ = _make_session()
    s_b, _ = _make_session()
    factory = FakeFactory()

    with pytest.raises(ValueError, match="unsupported ROM version"):
        LinkPair(
            s_a,
            s_b,
            version_primary="pokemon",
            version_peer="red",
            bridge_factory=factory,
        )

    assert factory.calls == []


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


def test_pair_tolerates_missing_exchange_bytes_hook():
    pair, _, _, _, pb_b, _ = _make_pair()

    def _missing_hook(_bank: int, _addr: int) -> None:
        raise ValueError("Breakpoint not found for bank and addr")

    pb_b.hook_deregister = _missing_hook
    pair.pair()

    assert (0x00, 0x216F) in pb_b._hooks


def test_pair_progress_hook_emits_events():
    pair, s_a, _s_b, pb_a, _, _ = _make_pair()
    pair.pair()
    # Fire the hook manually and verify the event landed on the bus.
    pb_a.fire(0x00, 0x6AB1)
    evt = s_a.events.latest("link.trade.show_player_mon")
    assert evt is not None
    assert evt.name == "link.trade.show_player_mon"


def test_pair_translates_cross_version_exchange_buffer_addresses():
    """Known trade buffers are mapped by symbol, not by raw WRAM address."""
    pb_a = FakePyBoy(DictMemory())
    pb_b = FakePyBoy(DictMemory())
    sa = Session(
        pyboy=pb_a,
        symbols=load_sym_text(_CROSS_VERSION_SYM_A),
        event_bus=EventBus(),
    )
    sb = Session(
        pyboy=pb_b,
        symbols=load_sym_text(_CROSS_VERSION_SYM_B),
        event_bus=EventBus(),
    )
    pair = LinkPair(
        sa,
        sb,
        version_primary="blue",
        version_peer="yellow",
        bridge_factory=FakeFactory(),
    )

    pair.pair()
    try:
        blue_send = 0xD100
        blue_receive = 0xD200
        yellow_send = 0xD110
        expected = b"xyz"
        for offset, value in enumerate(expected):
            pb_b.memory[yellow_send + offset] = value

        rf = pb_a.register_file
        rf.HL = blue_send
        rf.D = blue_receive >> 8
        rf.E = blue_receive & 0xFF
        rf.B = 0
        rf.C = len(expected)
        rf.SP = 0x1000
        pb_a.memory[0x1000] = 0x34
        pb_a.memory[0x1001] = 0x12
        rf.A = 0x99
        rf.F = 0x00

        assert pb_a.fire(0x00, 0x216F) == 1
        assert bytes(pb_a.memory[blue_receive + i] for i in range(3)) == expected
        assert rf.PC == 0x1234
        assert rf.A == 0
        assert rf.F == 0x80
    finally:
        pair.unpair()


def test_unpair_clears_paired_flag_and_pre_existing_hooks_survive():
    pair, s_a, _, pb_a, _, _ = _make_pair()
    # Pre-register a session hook that has nothing to do with the bridge.
    s_a.register_hook("Trade_ShowPlayerMon", "unrelated_event")
    assert pair.paired is False

    pair.pair()
    assert pair.paired is True
    # The EventBus owns one physical dispatcher and keeps both callbacks as
    # independent logical registrations at this address.
    assert len(pb_a._hooks[(0x00, 0x6AB1)]) == 1

    pair.unpair()
    assert pair.paired is False
    # The unrelated logical callback keeps the shared dispatcher alive and
    # must still fire after the pair-owned callback is removed.
    assert len(pb_a._hooks[(0x00, 0x6AB1)]) == 1
    before = s_a.events.count("unrelated_event")
    pb_a.fire(0x00, 0x6AB1)
    after = s_a.events.count("unrelated_event")
    assert after == before + 1


def test_pair_unpair_pair_removes_owned_hooks_and_stale_callbacks():
    pair, s_a, _, pb_a, pb_b, _ = _make_pair(pyboy_type=StrictFakePyBoy)

    pair.pair()
    first_hook_keys_a = set(pb_a._hooks)
    first_hook_keys_b = set(pb_b._hooks)
    assert first_hook_keys_a
    assert first_hook_keys_a == first_hook_keys_b
    assert s_a.events.count("link.trade.show_player_mon") == 0

    # The first generation fires exactly once.
    assert pb_a.fire(0x00, 0x6AB1) == 1
    assert s_a.events.count("link.trade.show_player_mon") == 1

    pair.unpair()
    assert pair.paired is False
    assert pb_a._hooks == {}
    assert pb_b._hooks == {}

    # No callback from the first generation can fire after teardown.
    assert pb_a.fire(0x00, 0x6AB1) == 0
    assert s_a.events.count("link.trade.show_player_mon") == 1

    # StrictFakePyBoy rejects duplicate physical registrations. A second pair
    # therefore proves that every pair-owned address was released.
    pair.pair()
    assert set(pb_a._hooks) == first_hook_keys_a
    assert set(pb_b._hooks) == first_hook_keys_b
    assert pb_a.fire(0x00, 0x6AB1) == 1
    assert s_a.events.count("link.trade.show_player_mon") == 2


def test_pair_unpair_pair_releases_raw_bridge_hooks():
    factory = RawFactory()
    pair, s_a, s_b, pb_a, pb_b, _ = _make_pair(
        factory=factory,
        pyboy_type=StrictFakePyBoy,
    )

    pair.pair()
    assert pair.paired is True
    assert len(pb_a._hooks[(0x00, 0x216F)]) == 1
    assert len(pb_b._hooks[(0x00, 0x216F)]) == 1

    pair.unpair()
    assert pair.paired is False
    assert pb_a._hooks == {}
    assert pb_b._hooks == {}
    assert s_a._serial_hooks == []
    assert s_b._serial_hooks == []

    # StrictFakePyBoy raises on an occupied address, so a second successful
    # pair proves that the raw SerialBridge callback and its replacement were
    # both removed rather than merely made unreachable.
    pair.pair()
    assert pair.paired is True
    assert len(factory.bridges) == 2
    assert len(pb_a._hooks[(0x00, 0x216F)]) == 1
    assert len(pb_b._hooks[(0x00, 0x216F)]) == 1


def test_pair_replacement_preserves_unrelated_raw_callback():
    factory = RawFactory()
    pair, _, _, pb_a, pb_b, _ = _make_pair(factory=factory)
    unrelated_calls: list[str] = []

    def unrelated_a(_ctx: object) -> None:
        unrelated_calls.append("a")

    def unrelated_b(_ctx: object) -> None:
        unrelated_calls.append("b")

    pb_a.hook_register(0x00, 0x216F, unrelated_a, None)
    pb_b.hook_register(0x00, 0x216F, unrelated_b, None)

    pair.pair()
    # The fake permits multiple callbacks at an address; the bridge callback
    # is replaced exactly and the unrelated callback remains beside the
    # EventBus dispatcher.
    assert len(pb_a._hooks[(0x00, 0x216F)]) == 2
    assert len(pb_b._hooks[(0x00, 0x216F)]) == 2

    pair.unpair()
    assert len(pb_a._hooks[(0x00, 0x216F)]) == 1
    assert len(pb_b._hooks[(0x00, 0x216F)]) == 1
    assert pb_a.fire(0x00, 0x216F) == 1
    assert pb_b.fire(0x00, 0x216F) == 1
    assert unrelated_calls == ["a", "b"]


def test_pair_rolls_back_partial_raw_bridge_installation():
    pair, s_a, s_b, pb_a, pb_b, _ = _make_pair(
        factory=PartiallyFailingRawFactory(),
        pyboy_type=StrictFakePyBoy,
    )

    with pytest.raises(RuntimeError, match="injected bridge installation failure"):
        pair.pair()

    assert pair.paired is False
    assert pb_a._hooks == {}
    assert pb_b._hooks == {}
    assert s_a._serial_hooks == []
    assert s_b._serial_hooks == []
    assert pair.transport.pending_a_to_b == 0
    assert pair.transport.pending_b_to_a == 0


def test_pair_rolls_back_owned_hooks_when_installation_fails():
    pair, _, _, pb_a, pb_b, _ = _make_pair(pyboy_type=FailingHookPyBoy)
    pb_b.fail_register = True

    with pytest.raises(RuntimeError, match="injected hook registration failure"):
        pair.pair()

    assert pair.paired is False
    assert pb_a._hooks == {}
    assert pb_b._hooks == {}
    assert pair.transport.pending_a_to_b == 0
    assert pair.transport.pending_b_to_a == 0

    # A failed transaction must not poison the pair for a later retry.
    pb_b.fail_register = False
    pair.pair()
    assert pair.paired is True


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


@pytest.mark.parametrize("count", [True, "1"])
def test_step_rejects_non_integer_count(count):
    pair, _, _, _, _, _ = _make_pair()
    with pytest.raises(TypeError, match="positive integer"):
        pair.step(count)  # type: ignore[arg-type]


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
        "link.trade.show_player_mon",
        max_ticks=16,
        chunk=4,
    )
    assert result.reached is True
    assert result.event is not None
    assert result.event.name == "link.trade.show_player_mon"


def test_run_until_event_pair_returns_when_event_present_on_peer():
    pair, _, s_b, _, _, _ = _make_pair()
    s_b.events.emit(tick=50, name="link.trade.load_data", bank=0, addr=0)
    result = pair.run_until_event_pair(
        "link.trade.load_data",
        side="peer",
        max_ticks=16,
        chunk=4,
    )
    assert result.reached is True
    assert result.event is not None


def test_run_until_event_pair_times_out():
    pair, _, _, _, _, _ = _make_pair()
    result = pair.run_until_event_pair(
        "link.trade.show_player_mon",
        max_ticks=12,
        chunk=4,
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
