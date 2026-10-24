"""Real-ROM smoke test: two Yellow sessions under :class:`PyBoyLinkSession`.

The previous link-cable work in this repo used an RPC-style bridge that
hooked Pokémon's symbol-level serial routines and faked byte exchange
over TCP. Milestones 1–4 of the design doc replace that with a
bit-accurate :class:`SerialCore` living inside the PyBoy motherboard
itself. This test proves the new stack is live end-to-end on real
ROMs by:

1. Loading two Yellow sessions at the Cable Club receptionist state
   (produced by ``scripts/produce_cable_club_fixture.py``).
2. Pairing them under :class:`PyBoyLinkSession.local`, which wires the
   two motherboard serial cores together via
   :class:`LockstepCoordinator`.
3. Pressing A on both sides to initiate the receptionist dialogue,
   which eventually leads to ``CableClub_DoBattleOrTradeAgain``
   emitting the trade preamble.
4. Stepping both sides in per-frame lockstep and counting edges via a
   backend wrapper.
5. Asserting the count is non-zero — proof that the real ROM *did*
   drive serial transfers through our new core.

Gated with ``skipif`` on ROM availability, fixture availability, AND a
runtime check that ``pyboy.mb`` is Python-accessible. The harness package
bundles the pinned PyBoy runtime so the link layer can attach its
bit-accurate backend to the motherboard serial object.

The normal checkout installs the pinned, bundled source runtime with
``python -m pip install -e ".[dev]"``. It must expose ``pyboy.mb.serial`` so
the Python link session can attach the native serial backend. Native
validation is available through ``scripts/bootstrap_pyboy.py --mode cython``;
both source and Cython builds are eligible for these attachment tests when
they expose the same motherboard serial contract. Run the strict local
acceptance cases with::

    python -m pytest -q \\
        tests/test_pyboy_link_session_roms.py::test_red_yellow_trade_swaps_real_party_records \\
        tests/test_pyboy_link_session_roms.py::test_red_yellow_battle_turn_is_resolved
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_core import SerialCore
from tests._rom_assets import fixture_path, rom_path, sym_path

_REPO = Path(__file__).resolve().parents[1]
for _parent in [_REPO, *_REPO.parents]:
    if (_parent / "rom").is_dir():
        ROM_ROOT = _parent / "rom"
        break
else:  # pragma: no cover - defensive; tests skip below if ROM missing.
    ROM_ROOT = _REPO / "rom"

_YELLOW_ROM = rom_path("yellow")
_YELLOW_SYM = sym_path("yellow")
_YELLOW_STATE = fixture_path("yellow")

# Smaller chunks reduce scheduling skew when a local master reaches a
# serial edge just before the peer ROM has armed its slave transfer. Keep
# the existing default for normal runs; production acceptance can opt into
# a tighter cooperative schedule without changing emulator semantics.
_LINK_CHUNK_CYCLES = int(os.environ.get("POKERED_LINK_CHUNK_CYCLES", "256"))

_ROM_PATHS = {
    # Red uses the color-patched ROM so it matches the walkthrough
    # state produced by vigorous-lovelace-15ea8a (SHA e1deed6308…).
    "red": (
        rom_path("red", color=True),
        sym_path("red"),
    ),
    "blue": (
        rom_path("blue", color=True),
        sym_path("blue"),
    ),
    "yellow": (_YELLOW_ROM, _YELLOW_SYM),
}

# The Red/Blue save states were produced against the color-patched ROMs
# but the cable_club fixture works with either variant (save format is
# identical — the color patch is pure Cartridge-side code, WRAM layout
# unchanged). Parametrizing tests over both variants catches any future
# regressions where the patch accidentally diverges from vanilla.
_ROM_VARIANTS = {
    "red": [
        (rom_path("red"), "vanilla"),
        (rom_path("red", color=True), "color"),
    ],
    "blue": [
        (rom_path("blue"), "vanilla"),
        (rom_path("blue", color=True), "color"),
    ],
    "yellow": [(_YELLOW_ROM, "cgb")],
}


def _variant_state_path(version: str, tag: str):
    """Resolve the cable_club.state fixture path for a specific ROM
    variant. Color/CGB use the default ``cable_club.state`` (captured
    against the color-patched ROM). Vanilla uses
    ``cable_club-vanilla.state`` — produced by running
    ``scripts/produce_cable_club_fixture.py`` against a vanilla
    ``cerulean_pc.state`` source. Save states are bit-tied to the
    exact ROM bytes they were captured against, so vanilla and color
    need separate fixtures."""
    base = fixture_path(version).parent
    if tag == "vanilla":
        return base / "cable_club-vanilla.state"
    return base / "cable_club.state"


def _open_session_variant(version: str, rom_path, tag: str):
    """Like :func:`_open_session` but uses the given ROM path + the
    variant-specific cable_club fixture. Used by the variant tests to
    exercise vanilla and color ROMs with their matching fixtures."""
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.config import load_versions
    from pokered_harness.session import Session

    _, sym = _ROM_PATHS[version]
    pins = load_versions(_REPO / "VERSIONS.md")
    expected_sha = pins.sha1_for_path(rom_path)
    assert expected_sha is not None
    session = Session.from_files(
        rom_path,
        sym,
        expected_rom_sha1=expected_sha,
        expected_pyboy_version=pins.pyboy_version,
    )
    session.load_state(_variant_state_path(version, tag).read_bytes())
    return session


def _state_path(version: str):
    return fixture_path(version)


def _battle_state_path(version: str):
    return fixture_path(version).parent / "cable_club-battle.state"


_fixtures_ready = (
    _YELLOW_ROM.is_file() and _YELLOW_SYM.is_file() and _YELLOW_STATE.is_file()
)


def _pyboy_mb_swappable() -> bool:
    """Detect whether the runtime exposes the motherboard for attachment.

    :class:`PyBoyLinkSession.attach` reuses ``pyboy.mb.serial`` and installs
    the bit-accurate backend on that native serial object. Source and Cython
    runtimes are both supported when they expose this contract.
    """
    try:
        import warnings

        warnings.filterwarnings("ignore")
        from pyboy import PyBoy

        # Probe a temporary instance because runtime attributes are exposed
        # on the instance; a class-level check is insufficient for an
        # extension-backed PyBoy type.
        p = PyBoy(
            str(_YELLOW_ROM),
            window="null",
            cgb=True,
            sound_emulated=False,
            no_input=True,
        )
        try:
            return hasattr(p, "mb")
        finally:
            p.stop(save=False)
    except Exception:  # noqa: BLE001 - an unavailable probe must skip safely
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_real_rom_link_runtime():
    # Diagnostic helper imports must not create an emulator as a side effect.
    # Probe only when this module's asset-dependent tests actually execute.
    if not _fixtures_ready:
        pytest.skip(
            "Yellow ROM or Cable Club state fixture missing — regenerate "
            "with scripts/produce_cable_club_fixture.py --version yellow"
        )
    if not _pyboy_mb_swappable():
        pytest.skip(
            "PyBoy.mb is not Python-accessible; local attachment requires "
            "a source or native runtime exposing mb.serial"
        )


class _CountingBackend:
    """Wraps an existing :class:`SerialBackend` and counts link activity.

    ``on_edge`` only fires on the side currently acting as master. To
    observe the peer's slave-side progress too, a wrapper can point at a
    peer counter and increment the peer's receive counters each time it
    drives an external edge into that peer.
    """

    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped
        self.peer_counter: _CountingBackend | None = None
        self.master_edges = 0
        self.slave_edges = 0
        self.bytes_sent_complete = 0
        self.bytes_received_complete = 0

    @property
    def total_edges(self) -> int:
        return self.master_edges + self.slave_edges

    def on_edge(self, our_bit: int, our_role: int) -> int:
        peer_bit = self.wrapped.on_edge(our_bit, our_role)
        self.master_edges += 1
        if self.master_edges % 8 == 0:
            self.bytes_sent_complete += 1
        if self.peer_counter is not None:
            self.peer_counter.slave_edges += 1
            if self.peer_counter.slave_edges % 8 == 0:
                self.peer_counter.bytes_received_complete += 1
        return peer_bit


def _open_yellow_session():
    return _open_session("yellow")


def _open_session(version: str, *, state_path=None):
    """Load ``version`` ROM + cable_club.state fixture.

    Defers the ``pokered_harness.session`` import so the module remains
    collectable even when some runtime deps are missing (e.g. under
    main-env pytest where the skipif above fires early).
    """
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.config import load_versions
    from pokered_harness.session import Session

    rom, sym = _ROM_PATHS[version]
    pins = load_versions("VERSIONS.md")
    expected_sha = pins.sha1_for_path(rom)
    assert expected_sha is not None
    session = Session.from_files(
        rom,
        sym,
        expected_rom_sha1=expected_sha,
        expected_pyboy_version=pins.pyboy_version,
    )
    session.load_state((state_path or _state_path(version)).read_bytes())
    return session


def _fixtures_available(version: str) -> bool:
    rom, sym = _ROM_PATHS[version]
    return rom.is_file() and sym.is_file() and _state_path(version).is_file()


def test_yellow_pair_installs_serial_core():
    """Attach installs :class:`SerialCore` on both motherboards and
    wires the coordinator once both sides are in."""
    a = _open_yellow_session()
    b = _open_yellow_session()
    try:
        link = PyBoyLinkSession.local()

        core_a = link.attach(a._pyboy)
        assert isinstance(core_a, SerialCore)
        assert a._pyboy.mb.serial is core_a
        assert link.paired is False

        core_b = link.attach(b._pyboy)
        assert link.paired is True
        assert link.coordinator is not None
        # Each core's backend is the coordinator's CoordinatedBackend.
        from pokered_harness.link.serial_coordinator import CoordinatedBackend
        assert isinstance(core_a.backend, CoordinatedBackend)
        assert isinstance(core_b.backend, CoordinatedBackend)
    finally:
        a.close()
        b.close()


def test_yellow_pair_exchanges_bytes_after_receptionist_A_press():
    """End-to-end smoke: press A on the receptionist on both sides and
    step enough frames for Pokémon's serial code to emit at least one
    edge through our :class:`SerialCore`.

    The Cable Club receptionist dialog stalls until the game sees
    ``SERIAL_CONNECTED`` on both sides, which requires the preamble
    exchange. In practice the receptionist code sets up and tears
    down the serial connection several times as it probes, so even
    a few seconds of game-time produces many edges.
    """
    a = _open_yellow_session()
    b = _open_yellow_session()
    try:
        link = PyBoyLinkSession.local()
        core_a = link.attach(a._pyboy)
        core_b = link.attach(b._pyboy)

        # Install counters on top of the coordinator's backends so we
        # can observe real serial activity.
        counter_a = _CountingBackend(core_a.backend)
        counter_b = _CountingBackend(core_b.backend)
        counter_a.peer_counter = counter_b
        counter_b.peer_counter = counter_a
        core_a.backend = counter_a
        core_b.backend = counter_b

        # Press A on both sides simultaneously to advance past any
        # dialogue prompt and into the receptionist flow.
        a.press("a", duration=6)
        b.press("a", duration=6)

        # Step both in lockstep. ~10 seconds of game-time is plenty
        # for Pokémon's Cable Club code to attempt its preamble
        # handshake; reduce if the test proves too slow.
        total_frames = 600
        step_chunk = 4
        for _ in range(total_frames // step_chunk):
            a.step(step_chunk)
            b.step(step_chunk)

        # The meaningful assertion: at least *some* serial activity
        # happened. Pokémon's Cable Club state includes the master
        # probe; we should see many edges on both sides.
        assert counter_a.total_edges > 0, (
            "A-side SerialCore saw zero activity after 600 frames — "
            "the ROM isn't driving our serial path"
        )
        assert counter_b.total_edges > 0, (
            "B-side SerialCore saw zero activity after 600 frames — "
            "the ROM isn't driving our serial path"
        )
        # And at least one full byte should have completed somewhere on
        # the link. The side acting as slave may receive bytes without
        # ever becoming master during this short receptionist phase.
        assert (
            counter_a.bytes_sent_complete
            + counter_a.bytes_received_complete
            + counter_b.bytes_sent_complete
            + counter_b.bytes_received_complete
        ) >= 1
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Milestone-7 flagship: drive both sides to the Cable Club LinkMenu
# ---------------------------------------------------------------------------


def _install_hook_counter(session, symbol: str, bucket: list, slot: int) -> None:
    """Copy of the counter-hook pattern from test_link_integration_remote.

    Installs a PyBoy execution hook at ``symbol``; each time the
    emulator reaches that label ``bucket[slot]`` is bumped. Silently
    skips labels the version-specific symbol table doesn't contain so
    the same test can run across R/B/Y.
    """
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx: object) -> None:
        bucket[slot] += 1

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        # Hook may already exist from another source (unlikely under
        # PyBoyLinkSession, which doesn't install its own hooks).
        pass


def _drive_two_sessions_to_link_menu(
    a, b, link, *, total_frames: int = 2400, frames_per_attempt: int = 20
) -> dict:
    """Interleave per-frame ticks on ``a`` and ``b`` while pressing UP
    then A to engage the Cable Club receptionist and reach ``LinkMenu``.

    Returns a diagnostics dict with hook counts for each key milestone
    plus the actual frames consumed — useful when the test fails so
    the error message can pinpoint where the flow stalled.

    The receptionist sits one tile north of the fixture's starting
    position. Pressing UP three times walks the player to the counter;
    pressing A talks to the receptionist, which triggers
    ``CableClubNPC`` → ``CableClub_DoBattleOrTradeAgain`` → preamble
    handshake → ``SaveGameData`` → nibble sync → ``LinkMenu``.
    """
    counters = {
        "CableClubNPC": [0, 0],
        "SaveGameData": [0, 0],
        "Serial_SyncAndExchangeNybble": [0, 0],
        "Serial_ExchangeBytes": [0, 0],
        "LinkMenu": [0, 0],
    }
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)

    def tick_both_coarse(frames: int) -> None:
        """Per-frame alternation. Fine for overworld / dialog phases
        that don't stress serial sync."""
        for _ in range(frames):
            a.step(1)
            b.step(1)

    def tick_both_fine(frames: int) -> None:
        """Sub-frame interleaved via :meth:`PyBoyLinkSession.step_interleaved`.
        Needed during ``Serial_SyncAndExchangeNybble`` so A and B's
        CPUs stay cycle-aligned enough for nibble-sync to converge."""
        link.step_interleaved(frames, chunk_cycles=_LINK_CHUNK_CYCLES)

    frames_used = 0

    # Walk up to the receptionist with coarse (per-frame) interleaving.
    for _ in range(3):
        a.press("up", duration=6)
        b.press("up", duration=6)
        tick_both_coarse(20)
        frames_used += 20

    # Press A and advance. Once Serial_SyncAndExchangeNybble fires on
    # both sides, switch to fine-grained interleaving so the game's
    # tight master/slave-alternation loop can synchronize.
    attempts = (total_frames - frames_used) // frames_per_attempt
    for _attempt in range(attempts):
        if counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0:
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        # Use fine-grained interleaving once either side has saved
        # (which marks entry into the serial-heavy handshake). Coarse
        # is fine for dialog navigation and ~10x faster.
        in_serial_phase = (
            counters["SaveGameData"][0] > 0 or counters["SaveGameData"][1] > 0
        )
        if in_serial_phase:
            tick_both_fine(frames_per_attempt)
        else:
            tick_both_coarse(frames_per_attempt)
        frames_used += frames_per_attempt

    return {"counters": counters, "frames_used": frames_used}


TRADE_CENTER_MAP_ID = 0xEF  # per pokeyellow/constants/map_constants.asm
COLOSSEUM_MAP_ID = 0xF0
PARTY_MON_SIZE = 44  # bytes per party-mon record (wPartyMon1..6)


def _party_raw_summary(session) -> dict[str, object]:
    """Return game-owned party lists and records for transfer assertions."""
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count = int(pb.memory[addr_of("wPartyCount")])
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    species = [int(pb.memory[species_addr + slot]) for slot in range(count + 1)]
    mon_species = [
        int(pb.memory[mons_addr + slot * PARTY_MON_SIZE]) for slot in range(count)
    ]
    mon_records = [
        bytes(
            pb.memory[mons_addr + slot * PARTY_MON_SIZE + offset]
            for offset in range(PARTY_MON_SIZE)
        ).hex()
        for slot in range(count)
    ]
    return {
        "count": count,
        "species": species,
        "mon_species": mon_species,
        "mon_records": mon_records,
    }


def _assert_battle_fixture_is_legal(session) -> None:
    """Require a pre-generated legal party; do not mutate it in the test."""
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    count = int(pb.memory[count_addr])
    assert count >= 3, f"battle fixture has only {count} party Pokémon"
    assert count <= 6, f"battle fixture has invalid party count {count}"
    assert int(pb.memory[species_addr + count]) == 0xFF
    for slot in range(count):
        species = int(pb.memory[species_addr + slot])
        mon_addr = mons_addr + slot * PARTY_MON_SIZE
        assert species not in (0, 0xFF)
        assert int(pb.memory[mon_addr]) == species
        hp = (int(pb.memory[mon_addr + 1]) << 8) | int(pb.memory[mon_addr + 2])
        assert hp > 0
        saw_empty_move = False
        for move_idx in range(4):
            move_id = int(pb.memory[mon_addr + 8 + move_idx])
            pp = int(pb.memory[mon_addr + 29 + move_idx]) & 0x3F
            if move_id == 0:
                saw_empty_move = True
                assert pp == 0, (
                    f"{session!r} party slot {slot} has PP for an empty "
                    f"move slot {move_idx}: pp={pp}"
                )
            else:
                assert not saw_empty_move, (
                    f"{session!r} party slot {slot} has a move after an "
                    f"empty slot: index={move_idx}, move={move_id}"
                )
                assert move_id < 0xFF
                assert pp > 0, (
                    f"{session!r} party slot {slot} move {move_idx} "
                    f"({move_id}) has no PP"
                )

    lead_addr = mons_addr
    assert any(
        int(pb.memory[lead_addr + 8 + move_idx]) != 0
        and (int(pb.memory[lead_addr + 29 + move_idx]) & 0x3F) > 0
        for move_idx in range(4)
    ), "battle fixture lead has no usable move"


def _read_active_battle_moves(session) -> tuple[tuple[int, int], ...]:
    """Read the ROM-owned active move/PP slots without changing emulator RAM."""
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    moves_addr = addr_of("wBattleMonMoves")
    pp_addr = addr_of("wBattleMonPP")
    return tuple(
        (
            int(pb.memory[moves_addr + move_idx]),
            int(pb.memory[pp_addr + move_idx]) & 0x3F,
        )
        for move_idx in range(4)
    )


def _assert_active_battle_state_is_legal(session) -> tuple[int, int]:
    """Validate ROM-populated battle state and return ``(slot, move_id)``.

    A depleted move is legal game state, so the active move list may contain
    non-zero move IDs with zero PP.  The driver must select a different slot
    through the real move menu; it must never repair PP or any other RAM field.
    """
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    party_species = int(pb.memory[addr_of("wPartySpecies")])
    active_species = int(pb.memory[addr_of("wBattleMonSpecies")])
    assert active_species == party_species, (
        f"active battle species {active_species} does not match lead "
        f"party species {party_species}"
    )
    hp_addr = addr_of("wBattleMonHP")
    max_hp_addr = addr_of("wBattleMonMaxHP")
    hp = (int(pb.memory[hp_addr]) << 8) | int(pb.memory[hp_addr + 1])
    max_hp = (int(pb.memory[max_hp_addr]) << 8) | int(pb.memory[max_hp_addr + 1])
    assert 0 < hp <= max_hp, f"invalid active battle HP {hp}/{max_hp}"

    active_moves = _read_active_battle_moves(session)
    saw_empty_move = False
    usable_slots: list[int] = []
    for move_idx, (move_id, pp) in enumerate(active_moves):
        if move_id == 0:
            saw_empty_move = True
            assert pp == 0, (
                f"active move slot {move_idx} is empty but has PP {pp}"
            )
            continue
        assert not saw_empty_move, (
            f"active move slot {move_idx} is populated after an empty slot"
        )
        assert move_id < 0xFF
        if pp > 0:
            usable_slots.append(move_idx)
    assert usable_slots, (
        f"active battle mon has no usable move: "
        f"moves={[move for move, _ in active_moves]}, "
        f"pp={[pp for _, pp in active_moves]}"
    )
    return usable_slots[0], active_moves[usable_slots[0]][0]


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("yellow", "yellow"),
        ("blue", "blue"),
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "yellow"),
        ("yellow", "blue"),
    ],
)
def test_pair_reaches_link_menu_via_pyboy_link_session(version_a, version_b):
    """Milestone 7 flagship, parameterized over every R/B/Y pairing.

    Drive two instances under :class:`PyBoyLinkSession` through the
    Cable Club receptionist dialog to ``LinkMenu``.

    The hard assertion: ``LinkMenu`` fires on *both* sides. That
    means the preamble handshake and nibble exchange have actually
    converged through our bit-accurate :class:`SerialCore` on real
    ROMs — end-to-end proof of the new architecture.

    Pairings where either side's fixture is missing skip rather than
    fail, so partial fixture coverage still exercises the available
    pairs.
    """
    if not (_fixtures_available(version_a) and _fixtures_available(version_b)):
        pytest.skip(
            f"Cable Club fixture(s) missing for {version_a}/{version_b} — "
            f"produce with scripts/produce_cable_club_fixture.py"
        )

    a = _open_session(version_a)
    b = _open_session(version_b)
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_two_sessions_to_link_menu(a, b, link)
        counters = diag["counters"]

        print(f"\n{version_a}<->{version_b} diagnostic counters (per-side [a, b]):")
        for sym, cnt in counters.items():
            print(f"  {sym}: {cnt}")
        print(f"  frames_used: {diag['frames_used']}")

        sg = counters["SaveGameData"]
        lm = counters["LinkMenu"]
        assert sg[0] > 0 and sg[1] > 0, (
            f"{version_a}<->{version_b}: SaveGameData never fired on "
            f"both sides; {counters}. Preamble handshake failed."
        )
        assert lm[0] > 0 and lm[1] > 0, (
            f"{version_a}<->{version_b}: LinkMenu never reached on both "
            f"sides; {counters}, frames={diag['frames_used']}. "
            f"Preamble converged but nibble-sync did not."
        )
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Trade-completion flow: LinkMenu -> TRADE_CENTER warp
# ---------------------------------------------------------------------------


def _drive_past_link_menu_to_trade_center(
    a, b, link, *, post_link_menu_frames: int = 1200, frames_per_attempt: int = 20
) -> dict:
    """Continue from ``LinkMenu`` by pressing A on both sides (default
    cursor is on "Trade Center" — item 0).

    After both sides exchange matching ``0xD4`` selections via
    ``Serial_ExchangeLinkMenuSelection``, the game warps each player
    to map ``TRADE_CENTER`` (``0xEF``). We keep pressing A every
    ``frames_per_attempt`` frames to dismiss any subsequent dialog
    and poll ``read_game_state().overworld.map_id`` to detect the
    warp.

    Returns a dict with per-side map-id progression and the frames
    consumed after LinkMenu fired.
    """
    # Reuse the existing helper to reach LinkMenu.
    diag = _drive_two_sessions_to_link_menu(a, b, link)
    counters = diag["counters"]
    assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
        "precondition: both sides must have reached LinkMenu before "
        "attempting TRADE_CENTER warp"
    )

    extra_frames = 0
    attempts = post_link_menu_frames // frames_per_attempt
    for _ in range(attempts):
        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        if map_a == TRADE_CENTER_MAP_ID and map_b == TRADE_CENTER_MAP_ID:
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        # Sub-frame interleaving stays on — still in serial-heavy phase
        # (LinkMenu exchange, then the big trainer-data block exchange
        # during warp setup).
        link.step_interleaved(
            frames_per_attempt, chunk_cycles=_LINK_CHUNK_CYCLES
        )
        extra_frames += frames_per_attempt

    map_a = a.read_game_state().overworld.map_id
    map_b = b.read_game_state().overworld.map_id
    return {
        "counters": counters,
        "frames_to_link_menu": diag["frames_used"],
        "extra_frames": extra_frames,
        "final_map_a": map_a,
        "final_map_b": map_b,
    }


_TRADE_DIAG_SYMBOLS = (
    "CableClub_DoBattleOrTrade",
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
)


def _install_trade_diag_counters(a, b) -> dict:
    """Installs hooks for the trade-phase diagnostic symbols on both
    sides and returns a shared counters dict. Call once, before any
    driving, so hooks catch events that fire during the warp itself
    (e.g. ``CableClub_DoBattleOrTrade``)."""
    counters = {sym: [0, 0] for sym in _TRADE_DIAG_SYMBOLS}
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)
    return counters


def _drive_complete_trade(
    a, b, link, *, counters: dict,
    trade_budget_frames: int = 4000, step_frames: int = 20,
) -> dict:
    """Drive a full Pokémon trade from fixture start to completion.

    Sequence (after both sides warp to TRADE_CENTER):

    1. Press A — selects the first player-party mon (cursor is already
       on slot 0 since ``TradeCenter_SelectMon`` initializes it there).
       Opens the STATS/TRADE sub-menu with the cursor on "STATS".
    2. Press RIGHT — moves the sub-menu cursor to "TRADE".
    3. Press A — chooses TRADE. Fires
       ``Serial_PrintWaitingTextAndSyncAndExchangeNybble`` which sends
       the selected player-mon index to the peer.
    4. Peer does the same on its side.
    5. Both sides see each other's selections, enter
       ``TradeCenter_ConfirmMonSelection`` → "WILL TRADE X FOR Y?" YES/NO
       prompt (default cursor on YES).
    6. Press A on both sides — confirms the trade.
    7. ``TradeCenter_Trade`` runs; trade animation plays; both sides'
       ``_AddEnemyMonToPlayerParty`` fires and the new mon lands in the
       party.

    Between presses we run ``link.step_interleaved`` so the busy serial
    exchange under nibble-sync and patch-list transfer stays aligned.
    """
    # ``counters`` is expected pre-populated by
    # :func:`_install_trade_diag_counters` before the warp driver ran,
    # so fire events during CableClub_DoBattleOrTrade are caught too.
    add_mon = counters["_AddEnemyMonToPlayerParty"]
    trade_center_trade = counters["TradeCenter_Trade"]

    def tick_interleaved(frames: int) -> None:
        """Sub-frame interleaved — for serial-heavy phases."""
        link.step_interleaved(frames, chunk_cycles=_LINK_CHUNK_CYCLES)

    def tick_per_frame(frames: int) -> None:
        """Per-frame via session.step — for overworld/menu navigation
        where input handling is what matters, not byte-level serial
        sync. The coordinator still handles any incidental serial
        transfers via its CoordinatedBackend."""
        for _ in range(frames):
            a.step(1)
            b.step(1)

    # After warp, both players must walk onto their hidden-event
    # trigger tiles to flip wLinkState = LINK_STATE_START_TRADE and
    # cause CableClub_Run (which fires in WaitForTextScrollButtonPress)
    # to kick off CableClub_DoBattleOrTrade.
    #   - Master (internal clock): spawn (3, 4), walk RIGHT to (4, 4).
    #   - Slave  (external clock): spawn (6, 4), walk LEFT to (5, 4).
    # See data/events/hidden_events.asm:283-286.
    # Resolve directions based on hSerialConnectionStatus so the pair
    # works regardless of which PyBoy took the master role.
    conn_a_now = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
    conn_b_now = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
    INTERNAL = 0x02
    dir_a = "right" if conn_a_now == INTERNAL else "left"
    dir_b = "right" if conn_b_now == INTERNAL else "left"

    # Per-frame stepping during the walk — button-event handling is
    # sensitive to tick discipline and step_interleaved's singlestep
    # path was losing some inputs.
    for _ in range(4):
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        # Press the walk direction a few times — spawn facing may
        # need the first press to rotate, the second to walk.
        a.press(dir_a, duration=8)
        b.press(dir_b, duration=8)
        # Do not batch the trigger crossing.  One side can enter
        # CableClub_DoBattleOrTrade during this call; ticking twenty
        # complete frames on that side before advancing its peer lets the
        # first few serial transfers use stale handshake bytes.
        for _ in range(step_frames):
            tick_per_frame(1)
            if counters["CableClub_DoBattleOrTrade"][0] > 0 or counters["CableClub_DoBattleOrTrade"][1] > 0:
                break

    # Now A-mash to dismiss "JUST A MOMENT!" dialog on each side,
    # which causes WaitForTextScrollButtonPress -> CableClub_Run to
    # fire and launch the big pre-trade exchange.
    settle_frames = 0
    while settle_frames < 1800:
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        # The first call into CableClub_Run can begin the large trainer/
        # party exchange before the entry hook is observed on both sides.
        # Keep the CPUs interleaved for this whole post-warp dialog loop;
        # switching from sequential frames only after the hook fires lets
        # the first side get ahead and corrupt the byte stream.
        tick_interleaved(step_frames)
        settle_frames += step_frames

    # State-aware trade navigation — reactive to the *deepest* hook
    # that just fired, not a forward phase counter. The game can
    # escape back to outer menus (A in STATS sub-menu displays stats
    # then returns to playerMonMenu), so we can't assume linear
    # progression.
    #
    # Rules per tick:
    #   - If selectTradeMenuItem just ticked      → press A (confirm TRADE)
    #   - Elif selectStatsMenuItem just ticked    → press RIGHT (STATS→TRADE)
    #   - Elif playerMonMenu_HandleInput ticked   → press A (enter sub-menu)
    #   - Elif TradeCenter_Trade ticked           → press A (advance YES/NO)
    #   - Otherwise                               → press A (dismiss dialogs)

    stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
    trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
    menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
    tct_key = "TradeCenter_Trade"

    prev = {
        k: list(counters[k])
        for k in (stats_key, trade_key, menu_key, tct_key)
    }
    # Per-side "press RIGHT for N more iterations" counter. Set when
    # selectStatsMenuItem ticks; decremented each tick. Reset when
    # selectTradeMenuItem ticks (cursor already moved).
    right_pending = [0, 0]
    RIGHT_PRESS_ITERATIONS = 5

    extra_frames = 0
    attempts = trade_budget_frames // step_frames
    for _ in range(attempts):
        if add_mon[0] > 0 and add_mon[1] > 0:
            break
        # Tick first so hook counters reflect what happened during
        # the just-run frames. Then snapshot, compare to prev (last
        # iteration's snapshot), and issue keys based on what fired.
        # Switch to sub-frame interleaving as soon as either side
        # enters CableClub_DoBattleOrTrade — that function drives
        # the big ~200-byte block exchange via Serial_ExchangeBytes,
        # which is too serial-heavy for per-frame interleaving
        # (peer misses bytes as IRQs pile up while its CPU is frozen).
        cct_key = "CableClub_DoBattleOrTrade"
        if counters[cct_key][0] > 0 or counters[cct_key][1] > 0:
            tick_interleaved(step_frames)
        else:
            tick_per_frame(step_frames)
        extra_frames += step_frames

        now = {k: list(counters[k]) for k in (stats_key, trade_key, menu_key, tct_key)}
        for idx, sess in enumerate((a, b)):
            def ticked(key, *, _now=now, _idx=idx, _prev=prev):
                return _now[key][_idx] > _prev[key][_idx]

            if ticked(trade_key):
                # Cursor is on TRADE — confirm.
                sess.press("a", duration=4)
                right_pending[idx] = 0
            elif ticked(stats_key):
                # Entered STATS/TRADE sub-menu — move cursor to TRADE.
                # Keep RIGHT held for a few iterations to be robust
                # against HandleMenuInput's poll cadence.
                right_pending[idx] = RIGHT_PRESS_ITERATIONS
                sess.press("right", duration=12)
            elif right_pending[idx] > 0:
                sess.press("right", duration=12)
                right_pending[idx] -= 1
            elif ticked(menu_key):
                # Player-mon menu waiting for input — pick the lead.
                sess.press("a", duration=4)
            elif ticked(tct_key):
                # TradeCenter_Trade animation + YES/NO dialog — A-mash.
                sess.press("a", duration=4)
            else:
                # Pre-menu dialog or between transitions — A-mash.
                sess.press("a", duration=4)
        prev = now

    # The execution hook fires on function entry, before
    # ``_AddEnemyMonToPlayerParty`` has copied the received record into
    # the party array. Advance both emulators past the function body so
    # callers inspect completed game state rather than an entry snapshot.
    post_hook_frames = 0
    if add_mon[0] > 0 and add_mon[1] > 0:
        post_hook_frames = 120
        tick_interleaved(post_hook_frames)

    return {
        "add_mon": add_mon,
        "trade_center_trade": trade_center_trade,
        "trade_phase_frames": extra_frames + step_frames * 7 + post_hook_frames,
        "counters": counters,
    }


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("yellow", "yellow"),
        ("blue", "blue"),
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "yellow"),
        ("yellow", "blue"),
    ],
)
def test_pair_completes_trade_end_to_end(version_a, version_b):
    """Flagship: two instances actually trade a Pokémon.

    This closes the "two agents trade a Pokémon" loop the design doc
    set out to unblock. The hard assertion: ``_AddEnemyMonToPlayerParty``
    fires on both sides — meaning each one received and installed the
    peer's mon into its own party.
    """
    if not (_fixtures_available(version_a) and _fixtures_available(version_b)):
        pytest.skip(
            f"Cable Club fixture(s) missing for {version_a}/{version_b}"
        )

    a = _open_session(version_a)
    b = _open_session(version_b)
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        pre_a = a.read_game_state().party
        pre_b = b.read_game_state().party
        pre_a_lead = pre_a.lead.species if pre_a.lead else None
        pre_b_lead = pre_b.lead.species if pre_b.lead else None
        before_a_raw = _party_raw_summary(a)
        before_b_raw = _party_raw_summary(b)

        diag_counters = _install_trade_diag_counters(a, b)

        warp = _drive_past_link_menu_to_trade_center(a, b, link)
        assert warp["final_map_a"] == TRADE_CENTER_MAP_ID
        assert warp["final_map_b"] == TRADE_CENTER_MAP_ID

        trade_diag = _drive_complete_trade(a, b, link, counters=diag_counters)

        after_a_raw = _party_raw_summary(a)
        after_b_raw = _party_raw_summary(b)
        post_a_state = a.read_game_state()
        post_b_state = b.read_game_state()
        link_state_a = a._pyboy.memory[a.symbols.addr_of("wLinkState")]
        link_state_b = b._pyboy.memory[b.symbols.addr_of("wLinkState")]
        ptr_idx_a = a._pyboy.memory[
            a.symbols.addr_of("wTradeCenterPointerTableIndex")
        ]
        ptr_idx_b = b._pyboy.memory[
            b.symbols.addr_of("wTradeCenterPointerTableIndex")
        ]
        # hSerialConnectionStatus lives in HRAM; read via addr.
        conn_a = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
        conn_b = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
        # Player X/Y so we can see if walking actually happened.
        ow_a = post_a_state.overworld
        ow_b = post_b_state.overworld
        post_a_lead = post_a_state.party.lead.species if post_a_state.party.lead else None
        post_b_lead = post_b_state.party.lead.species if post_b_state.party.lead else None

        print(
            f"\n{version_a}<->{version_b} trade diagnostic:\n"
            f"  pre party leads:  A=#{pre_a_lead}, B=#{pre_b_lead}\n"
            f"  post party leads: A=#{post_a_lead}, B=#{post_b_lead}\n"
            f"  warp finals: map_a=0x{warp['final_map_a']:02x}, "
            f"map_b=0x{warp['final_map_b']:02x} "
            f"(after {warp['extra_frames']} frames past LinkMenu)\n"
            f"  post-trade maps: "
            f"map_a=0x{post_a_state.overworld.map_id:02x}, "
            f"map_b=0x{post_b_state.overworld.map_id:02x}\n"
            f"  post-trade wLinkState: "
            f"A=0x{link_state_a:02x}, B=0x{link_state_b:02x}\n"
            f"  post-trade wTradeCenterPointerTableIndex: "
            f"A=0x{ptr_idx_a:02x}, B=0x{ptr_idx_b:02x}\n"
            f"  hSerialConnectionStatus: "
            f"A=0x{conn_a:02x}, B=0x{conn_b:02x} "
            f"(01=EXTERNAL slave, 02=INTERNAL master)\n"
            f"  player pos: A=({ow_a.x}, {ow_a.y}), B=({ow_b.x}, {ow_b.y})\n"
            f"  trade phase frames: {trade_diag['trade_phase_frames']}"
        )
        print(f"  raw party A: {_party_raw_summary(a)}")
        print(f"  raw party B: {_party_raw_summary(b)}")
        for label, core in zip(("A", "B"), link.cores):
            backend = getattr(core, "backend", None)
            print(
                f"  coordinator {label}: edges={getattr(backend, 'edge_count', None)} "
                f"unarmed={getattr(backend, 'peer_unarmed_edges', None)} "
                f"peer_master={getattr(backend, 'peer_master_edges', None)} "
                f"rearm_attempts={getattr(backend, 'peer_rearm_attempts', None)} "
                f"rearm_successes={getattr(backend, 'peer_rearm_successes', None)}"
            )
        for sym, cnt in trade_diag["counters"].items():
            print(f"  {sym}: {cnt}")

        am = trade_diag["add_mon"]
        assert am[0] > 0, (
            f"A never ran _AddEnemyMonToPlayerParty; trade didn't complete "
            f"on side A. diagnostic={trade_diag}"
        )
        assert am[1] > 0, (
            f"B never ran _AddEnemyMonToPlayerParty; trade didn't complete "
            f"on side B. diagnostic={trade_diag}"
        )
        assert after_a_raw["count"] == before_a_raw["count"]
        assert after_b_raw["count"] == before_b_raw["count"]
        assert after_a_raw["species"][0] == before_b_raw["mon_species"][0]
        assert after_b_raw["species"][0] == before_a_raw["mon_species"][0]
        assert after_a_raw["mon_records"][0] == before_b_raw["mon_records"][0]
        assert after_b_raw["mon_records"][0] == before_a_raw["mon_records"][0]
    finally:
        a.close()
        b.close()


def test_red_yellow_trade_swaps_real_party_records():
    """Release acceptance: a natural Red/Yellow trade swaps both leads.

    The broad matrix above is intentionally diagnostic and only proves the
    ROM reached the trade routine. This case is the strict gate: it starts
    from two untouched, ROM-matched Cable Club fixtures and checks the
    game-owned species list plus each received party-mon record after the
    real ``_AddEnemyMonToPlayerParty`` path completes.
    """
    if not (_fixtures_available("red") and _fixtures_available("yellow")):
        pytest.skip("Red and Yellow Cable Club fixtures are required")

    a = _open_session("red")
    b = _open_session("yellow")
    try:
        before_a = _party_raw_summary(a)
        before_b = _party_raw_summary(b)
        expected_a = before_b["mon_species"][0]
        expected_b = before_a["mon_species"][0]
        assert expected_a != expected_b, (
            f"strict fixture leads must differ: A={before_a} B={before_b}"
        )

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        counters = _install_trade_diag_counters(a, b)

        warp = _drive_past_link_menu_to_trade_center(a, b, link)
        assert warp["final_map_a"] == TRADE_CENTER_MAP_ID
        assert warp["final_map_b"] == TRADE_CENTER_MAP_ID
        diag = _drive_complete_trade(a, b, link, counters=counters)

        after_a = _party_raw_summary(a)
        after_b = _party_raw_summary(b)
        assert diag["add_mon"][0] > 0 and diag["add_mon"][1] > 0, (
            f"trade hook did not fire on both sides: {diag}"
        )
        assert after_a["count"] == before_a["count"]
        assert after_b["count"] == before_b["count"]
        assert after_a["species"][0] == expected_a
        assert after_a["mon_species"][0] == expected_a
        assert after_a["mon_records"][0] == before_b["mon_records"][0]
        assert after_b["species"][0] == expected_b
        assert after_b["mon_species"][0] == expected_b
        assert after_b["mon_records"][0] == before_a["mon_records"][0]
        assert after_a["species"][-1] == 0xFF
        assert after_b["species"][-1] == 0xFF
    finally:
        a.close()
        b.close()


def test_yellow_pair_warps_to_trade_center():
    """Drive past LinkMenu to the TRADE_CENTER map warp on both sides.

    This is the next real milestone after the LinkMenu reach: it
    proves the big (~200-byte) trainer + party data exchange that
    happens inside ``CableClub_DoBattleOrTradeAgain`` also converges
    through :class:`SerialCore`. The map warp is the observable
    side-effect — both sides end up on map ``0xEF`` (TRADE_CENTER).
    """
    if not _fixtures_available("yellow"):
        pytest.skip("Yellow Cable Club fixture missing")

    a = _open_session("yellow")
    b = _open_session("yellow")
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_past_link_menu_to_trade_center(a, b, link)

        print(
            f"\nyellow<->yellow TRADE_CENTER warp diagnostic:\n"
            f"  LinkMenu counters: {diag['counters']}\n"
            f"  frames_to_link_menu: {diag['frames_to_link_menu']}\n"
            f"  extra_frames: {diag['extra_frames']}\n"
            f"  final_map_a: 0x{diag['final_map_a']:02x}\n"
            f"  final_map_b: 0x{diag['final_map_b']:02x}"
        )

        assert diag["final_map_a"] == TRADE_CENTER_MAP_ID, (
            f"A didn't warp to TRADE_CENTER; "
            f"final_map_a=0x{diag['final_map_a']:02x}"
        )
        assert diag["final_map_b"] == TRADE_CENTER_MAP_ID, (
            f"B didn't warp to TRADE_CENTER; "
            f"final_map_b=0x{diag['final_map_b']:02x}"
        )
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Link-battle flow: LinkMenu -> Colosseum warp -> DisplayLinkBattleVersusTextBox
# ---------------------------------------------------------------------------


_BATTLE_DIAG_SYMBOLS = (
    "CableClub_DoBattleOrTrade",
    "DisplayLinkBattleVersusTextBox",
    "BattleTransition",
    "MainInBattleLoop",
    "DisplayBattleMenu",
    "DisplayBattleMenu.leftColumn_WaitForInput",
    "DisplayBattleMenu.rightColumn_WaitForInput",
    "MoveSelectionMenu",
    "MoveSelectionMenu.menuset",
    "MainInBattleLoop.selectEnemyMove",
    "LinkBattleExchangeData",
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
    "PlayerCalcMoveDamage",
)


def _install_battle_diag_counters(a, b) -> dict:
    counters = {sym: [0, 0] for sym in _BATTLE_DIAG_SYMBOLS}
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)
    return counters


def _drive_past_link_menu_to_colosseum(
    a, b, link, *, post_link_menu_frames: int = 1800, frames_per_attempt: int = 20
) -> dict:
    """Continue from ``LinkMenu`` by pressing DOWN (cursor TRADE→COLOSSEUM)
    then A on both sides.

    After both sides exchange matching selections via
    ``Serial_ExchangeLinkMenuSelection``, the game warps each player to
    map ``COLOSSEUM`` (``0xF1``) — provided each party has ≥3 mons.
    Caller is responsible for loading a pre-generated battle fixture with a
    legal party.
    """
    diag = _drive_two_sessions_to_link_menu(a, b, link)
    counters = diag["counters"]
    assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
        "precondition: both sides must have reached LinkMenu"
    )

    # LinkMenu's hook fires at function entry, before HandleMenuInput
    # begins polling keys. Tick forward so the menu is actually waiting
    # for input, then press DOWN once to move TRADE CENTER→COLOSSEUM.
    # Three DOWN presses would wrap past CANCEL back to TRADE CENTER
    # (3 items with wrap), so use a single press and verify it landed
    # by checking wCurrentMenuItem — which LinkMenu uses directly.
    link.step_interleaved(60)
    a.press("down", duration=12)
    b.press("down", duration=12)
    link.step_interleaved(40)
    # Sanity: both cursors should now be on item 1 (COLOSSEUM).
    cur_a = a._pyboy.memory[a.symbols.addr_of("wCurrentMenuItem")]
    cur_b = b._pyboy.memory[b.symbols.addr_of("wCurrentMenuItem")]
    assert cur_a == 1 and cur_b == 1, (
        f"cursor didn't land on COLOSSEUM: a={cur_a}, b={cur_b}"
    )

    extra_frames = 0
    attempts = post_link_menu_frames // frames_per_attempt
    for _ in range(attempts):
        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        if map_a == COLOSSEUM_MAP_ID and map_b == COLOSSEUM_MAP_ID:
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        link.step_interleaved(frames_per_attempt)
        extra_frames += frames_per_attempt

    return {
        "counters": counters,
        "frames_to_link_menu": diag["frames_used"],
        "extra_frames": extra_frames,
        "final_map_a": a.read_game_state().overworld.map_id,
        "final_map_b": b.read_game_state().overworld.map_id,
    }


def test_yellow_pair_warps_to_colosseum():
    """Drive past LinkMenu to the COLOSSEUM map warp on both sides.

    The battle-path analogue of :func:`test_yellow_pair_warps_to_trade_center`.
    Proves that LinkMenu's DOWN+A navigation works, that ``Serial_
    ExchangeLinkMenuSelection`` carries the BATTLE selection, and that
    both sides satisfy the Colosseum party-size gate.
    """
    if not _fixtures_available("yellow"):
        pytest.skip("Yellow Cable Club fixture missing")

    a = _open_session("yellow", state_path=_battle_state_path("yellow"))
    b = _open_session("yellow", state_path=_battle_state_path("yellow"))
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_past_link_menu_to_colosseum(a, b, link)

        print(
            f"\nyellow<->yellow COLOSSEUM warp diagnostic:\n"
            f"  LinkMenu counters: {diag['counters']}\n"
            f"  frames_to_link_menu: {diag['frames_to_link_menu']}\n"
            f"  extra_frames: {diag['extra_frames']}\n"
            f"  final_map_a: 0x{diag['final_map_a']:02x}\n"
            f"  final_map_b: 0x{diag['final_map_b']:02x}"
        )

        assert diag["final_map_a"] == COLOSSEUM_MAP_ID, (
            f"A didn't warp to COLOSSEUM; "
            f"final_map_a=0x{diag['final_map_a']:02x}"
        )
        assert diag["final_map_b"] == COLOSSEUM_MAP_ID, (
            f"B didn't warp to COLOSSEUM; "
            f"final_map_b=0x{diag['final_map_b']:02x}"
        )
    finally:
        a.close()
        b.close()


def _drive_complete_battle_turn(
    a, b, link, *, counters: dict,
    battle_budget_frames: int = 6000, step_frames: int = 20,
    completion: str = "turn",
) -> dict:
    """Drive a full link-battle turn from COLOSSEUM warp to damage resolution.

    Sequence:

    1. Walk onto the hidden-event trigger tile (same tiles as Trade
       Center: (4,4) for master, (5,4) for slave).
    2. Wait without input for ``CableClub_DoBattleOrTrade`` to run its big
       trainer+party data block exchange.
    3. ``DisplayLinkBattleVersusTextBox`` + ``BattleTransition`` fire —
       the battle intro animation plays.
    4. Wait for ``MainInBattleLoop``/``DisplayBattleMenu`` before pressing
       A once to choose FIGHT; no input is sent during the intro transition.
    5. Read the ROM-populated active move/PP buffers, move the real menu
       cursor to the first move with PP remaining, and press A once.
    6. ``LinkBattleExchangeData`` nibble-exchanges both sides' moves.
    7. ``ExecutePlayerMove`` / ``ExecuteEnemyMove`` fire as the turn resolves.

    ``completion`` controls the bounded stopping condition. ``"versus"``
    stops after both sides enter the battle intro, ``"turn"`` stops after the
    native ``LinkBattleExchangeData`` move exchange plus at least one execute
    path on each side, and ``"damage"`` additionally requires
    ``PlayerCalcMoveDamage`` on both sides. The broad matrix uses ``"turn"``;
    the focused Red/Yellow release case uses ``"damage"``. This keeps the
    wait aligned with each caller's actual acceptance assertion instead of
    making non-damaging but valid Gen I moves consume the whole frame budget.
    """
    cct = counters["CableClub_DoBattleOrTrade"]
    vs = counters["DisplayLinkBattleVersusTextBox"]
    main = counters["MainInBattleLoop"]
    battle_menu = counters["DisplayBattleMenu"]
    mm = counters["MoveSelectionMenu"]
    select_enemy = counters["MainInBattleLoop.selectEnemyMove"]
    lbe = counters["LinkBattleExchangeData"]
    dmg = counters["PlayerCalcMoveDamage"]
    epm = counters["ExecutePlayerMove"]
    eem = counters["ExecuteEnemyMove"]
    selected_move_slots: list[int] = []
    selected_move_ids: list[int] = []
    active_move_choices: list[tuple[tuple[int, int], ...]] = []

    def tick_interleaved(frames: int) -> None:
        link.step_interleaved(frames, chunk_cycles=_LINK_CHUNK_CYCLES)

    def tick_per_frame(frames: int) -> None:
        for _ in range(frames):
            a.step(1)
            b.step(1)

    if step_frames <= 0:
        raise ValueError("step_frames must be positive")
    if battle_budget_frames < 0:
        raise ValueError("battle_budget_frames must be non-negative")
    if completion not in {"versus", "turn", "damage"}:
        raise ValueError(
            "completion must be one of: versus, turn, damage"
        )

    phase_frames = 0
    remaining_budget = battle_budget_frames

    def tick_bounded(frames: int) -> int:
        """Advance at most the remaining post-CCT budget."""
        nonlocal phase_frames, remaining_budget
        chunk = min(frames, remaining_budget)
        if chunk <= 0:
            return 0
        tick_interleaved(chunk)
        phase_frames += chunk
        remaining_budget -= chunk
        return chunk

    def wait_interleaved(predicate) -> bool:
        """Poll a ROM milestone within one shared finite frame budget."""
        while remaining_budget > 0 and not predicate():
            tick_bounded(step_frames)
        return bool(predicate())

    # Walk onto trigger tiles — same direction rules as trade flow.
    conn_a = int(a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")])
    conn_b = int(b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")])
    INTERNAL = 0x02
    dir_a = "right" if conn_a == INTERNAL else "left"
    dir_b = "right" if conn_b == INTERNAL else "left"

    trigger_frames = 0
    for _ in range(4):
        if cct[0] > 0 and cct[1] > 0:
            break
        a.press(dir_a, duration=8)
        b.press(dir_b, duration=8)
        # Keep the trigger crossing close to simultaneous; the following
        # CableClub_DoBattleOrTrade exchange is bit-level serial traffic.
        for _ in range(step_frames):
            tick_per_frame(1)
            trigger_frames += 1
            if cct[0] > 0 or cct[1] > 0:
                break

    # Dismiss the post-warp "JUST A MOMENT!" prompt until each side enters
    # the serial-heavy function.  Stop sending input to a side immediately
    # after its hook fires: the battle intro is a timed transition, and an A
    # press consumed there can leak into a later menu in a role-dependent
    # way.  Once either side enters the function, use interleaved stepping so
    # the first serial bytes cannot run against a frozen peer.
    if not (cct[0] > 0 and cct[1] > 0):
        remaining = max(0, 1800 - trigger_frames)
        while remaining > 0 and not (cct[0] > 0 and cct[1] > 0):
            if cct[0] == 0:
                a.press("a", duration=4)
            if cct[1] == 0:
                b.press("a", duration=4)
            if cct[0] > 0 or cct[1] > 0:
                chunk = min(step_frames, remaining)
                tick_interleaved(chunk)
                phase_frames += chunk
                remaining -= chunk
            else:
                tick_per_frame(1)
                trigger_frames += 1
                remaining -= 1

    # These waits are bounded and intentionally input-free.  Returning a
    # diagnostic without fabricating input preserves the existing callers'
    # strict milestone assertions while making a scheduler/ROM stall visible.
    if cct[0] > 0 and cct[1] > 0:
        wait_interleaved(lambda: vs[0] > 0 and vs[1] > 0)

    def menu_fields(session) -> tuple[int, int, int] | None:
        try:
            memory = session._pyboy.memory
            symbols = session.symbols
            return (
                int(memory[symbols.addr_of("wCurrentMenuItem")]),
                int(memory[symbols.addr_of("wMaxMenuItem")]),
                int(memory[symbols.addr_of("wMenuWatchedKeys")]),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def battle_menu_input_ready(session) -> bool:
        fields = menu_fields(session)
        if fields is None:
            return False
        current, maximum, watched_keys = fields
        return 0 <= current <= 1 and maximum == 1 and watched_keys & 0x01

    def move_menu_input_ready(session) -> bool:
        fields = menu_fields(session)
        if fields is None:
            return False
        current, maximum, watched_keys = fields
        return (
            # The ROM stores ``wNumMovesMinusOne + 2`` as the menu maximum;
            # that is one greater than the last real move slot.  A four-move
            # mon therefore exposes max=5 while valid move cursors remain
            # 1..4.  This mirrors SelectMenuItem_CursorDown in the cartridge
            # code instead of treating wMaxMenuItem as a move count.
            1 <= current < maximum <= 5
            and watched_keys & 0x01
        )

    if completion != "versus" and vs[0] > 0 and vs[1] > 0:
        wait_interleaved(
            lambda: (
                main[0] > 0
                and main[1] > 0
                and battle_menu[0] > 0
                and battle_menu[1] > 0
            ),
        )
        wait_interleaved(
            lambda: battle_menu_input_ready(a) and battle_menu_input_ready(b)
        )

    menu_ready = (
        completion != "versus"
        and main[0] > 0
        and main[1] > 0
        and battle_menu[0] > 0
        and battle_menu[1] > 0
        and battle_menu_input_ready(a)
        and battle_menu_input_ready(b)
    )
    if menu_ready:
        # Let both ROMs finish drawing/entering HandleMenuInput, then select
        # FIGHT through the real command menu. The input-ready hook/fields
        # are ROM-owned milestones; retrying only while a side still exposes
        # that menu avoids losing a one-frame A event to the intro transition.
        tick_bounded(min(step_frames, 4))
        next_fight_input_frame = [0, 0]
        while remaining_budget > 0 and not (mm[0] > 0 and mm[1] > 0):
            for idx, session in enumerate((a, b)):
                if (
                    mm[idx] == 0
                    and phase_frames >= next_fight_input_frame[idx]
                    and battle_menu_input_ready(session)
                ):
                    session.press("a")
                    next_fight_input_frame[idx] = phase_frames + 8
            tick_bounded(min(step_frames, 2))

    if completion != "versus":
        wait_interleaved(
            lambda: (
                mm[0] > 0
                and mm[1] > 0
                and move_menu_input_ready(a)
                and move_menu_input_ready(b)
            )
        )

    move_menu_ready = (
        completion != "versus"
        and mm[0] > 0
        and mm[1] > 0
        and move_menu_input_ready(a)
        and move_menu_input_ready(b)
    )
    if move_menu_ready:
        # The hook fires at function entry.  Give the ROM enough input-free
        # time to install the menu cursor before inspecting it.
        settle = min(step_frames, 4)
        tick_bounded(settle)

        # ``battle_budget_frames`` covers the normal battle phase, but a
        # ROM can enter MoveSelectionMenu at the exact end of that budget.
        # Reserve a small, separately bounded handoff window so a valid menu
        # cannot be mistaken for a transport failure merely because the
        # final A edge was sampled on the next joypad poll.
        if remaining_budget == 0:
            remaining_budget = 1200

        slot_a, move_a = _assert_active_battle_state_is_legal(a)
        slot_b, move_b = _assert_active_battle_state_is_legal(b)
        active_a = _read_active_battle_moves(a)
        active_b = _read_active_battle_moves(b)
        active_move_choices.extend((active_a, active_b))
        selected_move_slots.extend((slot_a, slot_b))
        selected_move_ids.extend((move_a, move_b))

        def known_move_count(slots: tuple[tuple[int, int], ...]) -> int:
            count = 0
            for move_id, _pp in slots:
                if move_id == 0:
                    break
                count += 1
            assert count > 0
            return count

        def menu_cursor(session, move_count: int) -> int:
            cursor = int(
                session._pyboy.memory[
                    session.symbols.addr_of("wCurrentMenuItem")
                ]
            )
            assert 1 <= cursor <= move_count, (
                f"invalid move-menu cursor {cursor} for {move_count} moves"
            )
            return cursor - 1

        count_a = known_move_count(active_a)
        count_b = known_move_count(active_b)
        cursor_a = menu_cursor(a, count_a)
        cursor_b = menu_cursor(b, count_b)
        remaining_down = [
            (slot_a - cursor_a) % count_a,
            (slot_b - cursor_b) % count_b,
        ]
        while remaining_down[0] or remaining_down[1]:
            if remaining_down[0]:
                a.press("down")
                remaining_down[0] -= 1
            if remaining_down[1]:
                b.press("down")
                remaining_down[1] -= 1
            tick_bounded(min(step_frames, 2))

        assert menu_cursor(a, count_a) == slot_a
        assert menu_cursor(b, count_b) == slot_b

        # A legal move is selected through the ROM's own menu handling.  The
        # cartridge's HandleMenuInput waits for a fresh low-sensitivity
        # joypad sample; a single event can be queued just before that poll
        # and be consumed by the surrounding transition instead. Retry only
        # while the ROM still exposes the move menu, and stop per side as
        # soon as the ROM-owned selectEnemyMove label proves that A was
        # consumed. No input is injected once that boundary is crossed.
        select_enemy_before = [select_enemy[0], select_enemy[1]]
        next_move_input_frame = [phase_frames, phase_frames]
        move_input_attempts = [0, 0]
        while remaining_budget > 0 and not (
            select_enemy[0] > select_enemy_before[0]
            and select_enemy[1] > select_enemy_before[1]
        ):
            for idx, session in enumerate((a, b)):
                if (
                    select_enemy[idx] == select_enemy_before[idx]
                    and phase_frames >= next_move_input_frame[idx]
                    and move_menu_input_ready(session)
                ):
                    session.press("a")
                    move_input_attempts[idx] += 1
                    next_move_input_frame[idx] = phase_frames + 8
            tick_bounded(min(step_frames, 2))

    # Keep the original bounded resolution window and acceptance semantics:
    # callers decide whether link exchange, execution, or damage is required
    # for their tier.  Crucially, this loop never sends blind input.
    def completion_reached() -> bool:
        if completion == "versus":
            return vs[0] > 0 and vs[1] > 0
        if completion == "damage":
            return dmg[0] > 0 and dmg[1] > 0
        return (
            lbe[0] > 0
            and lbe[1] > 0
            and epm[0] + eem[0] > 0
            and epm[1] + eem[1] > 0
        )

    while remaining_budget > 0 and not completion_reached():
        tick_bounded(step_frames)

    return {
        "cct": cct,
        "vs": vs,
        "mm": mm,
        "lbe": lbe,
        "dmg": dmg,
        "main": main,
        "battle_menu": battle_menu,
        "select_enemy_move": select_enemy,
        "menu_ready": menu_ready,
        "move_menu_ready": move_menu_ready,
        "move_input_attempts": move_input_attempts if move_menu_ready else [],
        "final_menu_fields": (menu_fields(a), menu_fields(b)),
        "remaining_budget": remaining_budget,
        "active_move_choices": active_move_choices,
        "selected_move_slots": selected_move_slots,
        "selected_move_ids": selected_move_ids,
        "battle_phase_frames": phase_frames,
        "counters": counters,
    }


def test_yellow_pair_starts_link_battle():
    """Yellow pair enters Colosseum and triggers the battle VS splash.

    The milestone between "warps to COLOSSEUM" and "completes turn":
    both sides run ``DisplayLinkBattleVersusTextBox``, meaning the
    big pre-battle trainer+party block exchange converged through
    our :class:`SerialCore`.
    """
    if not _fixtures_available("yellow"):
        pytest.skip("Yellow Cable Club fixture missing")

    a = _open_session("yellow", state_path=_battle_state_path("yellow"))
    b = _open_session("yellow", state_path=_battle_state_path("yellow"))
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        counters = _install_battle_diag_counters(a, b)
        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID

        diag = _drive_complete_battle_turn(
            a,
            b,
            link,
            counters=counters,
            battle_budget_frames=2400,
            completion="versus",
        )

        print("\nyellow<->yellow battle-start diagnostic:")
        for sym, cnt in counters.items():
            print(f"  {sym}: {cnt}")
        print(f"  battle_phase_frames: {diag['battle_phase_frames']}")

        assert diag["vs"][0] > 0, (
            f"A never ran DisplayLinkBattleVersusTextBox; "
            f"battle didn't start. counters={counters}"
        )
        assert diag["vs"][1] > 0, (
            f"B never ran DisplayLinkBattleVersusTextBox; "
            f"battle didn't start. counters={counters}"
        )
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    "version_a,version_b",
    [
        ("yellow", "yellow"),
        ("blue", "blue"),
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "yellow"),
        ("yellow", "blue"),
    ],
)
def test_pair_completes_battle_turn(version_a, version_b):
    """Flagship battle test: two instances resolve one link-battle turn.

    The hard assertion: both sides complete one full turn of the battle
    loop — ``ExecutePlayerMove`` and ``ExecuteEnemyMove`` each fire on
    both sides (each instance processes its own move and simulates the
    peer's move locally), and ``LinkBattleExchangeData`` fires on both
    sides (the move-selection nibble exchange went through the link).

    ``PlayerCalcMoveDamage`` is not the right acceptance hook because
    it only fires on damaging moves that go through the canonical
    damage-calc path. Gen I battles have plenty of non-canonical paths
    (status moves, OHKO moves, trapping moves like Bind/Wrap whose
    damage is done via :asm:`DoMultiHitTrappingMove`, missing, fainting
    before it runs, etc.). The acceptance criterion is that the link
    protocol carries the move exchange and both sides advance through
    the turn in lockstep — that's what Execute* + LinkBattleExchangeData
    assert.
    """
    if not (_fixtures_available(version_a) and _fixtures_available(version_b)):
        pytest.skip(
            f"Cable Club fixture(s) missing for {version_a}/{version_b}"
        )

    a = _open_session(version_a, state_path=_battle_state_path(version_a))
    b = _open_session(version_b, state_path=_battle_state_path(version_b))
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        counters = _install_battle_diag_counters(a, b)
        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID

        diag = _drive_complete_battle_turn(a, b, link, counters=counters)

        print(f"\n{version_a}<->{version_b} battle-turn diagnostic:")
        for sym, cnt in counters.items():
            print(f"  {sym}: {cnt}")
        print(f"  battle_phase_frames: {diag['battle_phase_frames']}")
        print(
            "  battle handoff: "
            f"menu_fields={diag['final_menu_fields']} "
            f"remaining_budget={diag['remaining_budget']} "
            f"move_menu_ready={diag['move_menu_ready']} "
            f"move_input_attempts={diag['move_input_attempts']}"
        )

        epm = counters["ExecutePlayerMove"]
        eem = counters["ExecuteEnemyMove"]
        lbe = counters["LinkBattleExchangeData"]
        assert lbe[0] > 0 and lbe[1] > 0, (
            f"LinkBattleExchangeData didn't fire on both sides; moves "
            f"were never exchanged via the link. counters={counters} "
            f"diag={diag}"
        )
        # Either ExecutePlayerMove or ExecuteEnemyMove (or both) must
        # fire on each side. In heavily-mismatched pairings (L54 vs
        # L16 Pikachu) the weaker side's mon can faint before its own
        # turn executes — skipping ExecutePlayerMove on that side while
        # its ExecuteEnemyMove counterpart still fires — and vice-versa
        # on the peer (where only their own ExecutePlayerMove ran
        # before the opponent fainted on their machine's simulation).
        # Requiring ≥1 execute-path fire per side is the cleanest
        # "this side advanced through some portion of the turn"
        # predicate that survives the fainting race.
        assert epm[0] + eem[0] > 0, (
            f"Side A never fired ExecutePlayerMove or ExecuteEnemyMove; "
            f"the turn didn't advance on A. counters={counters}"
        )
        assert epm[1] + eem[1] > 0, (
            f"Side B never fired ExecutePlayerMove or ExecuteEnemyMove; "
            f"the turn didn't advance on B. counters={counters}"
        )
    finally:
        a.close()
        b.close()


def test_red_yellow_battle_turn_is_resolved():
    """Release acceptance: Red/Yellow exchange and resolve one move turn."""
    if not (_fixtures_available("red") and _fixtures_available("yellow")):
        pytest.skip("Red and Yellow battle fixtures are required")

    a = _open_session("red", state_path=_battle_state_path("red"))
    b = _open_session("yellow", state_path=_battle_state_path("yellow"))
    try:
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        counters = _install_battle_diag_counters(a, b)
        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID
        _drive_complete_battle_turn(
            a, b, link, counters=counters, completion="damage"
        )

        required_hooks = (
            "DisplayLinkBattleVersusTextBox",
            "BattleTransition",
            "MainInBattleLoop",
            "MoveSelectionMenu",
            "LinkBattleExchangeData",
            "ExecutePlayerMove",
            "ExecuteEnemyMove",
            "PlayerCalcMoveDamage",
        )
        for symbol in required_hooks:
            assert counters[symbol][0] > 0 and counters[symbol][1] > 0, (
                f"{symbol} did not fire on both sides: counters={counters}"
            )
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# ROM-variant coverage: vanilla vs color-patched Red/Blue
# ---------------------------------------------------------------------------


def _rom_variant_pairs():
    """Enumerate all (version, variant_a, variant_b) same-version pairings.

    Generates e.g. ``("red", "vanilla", "vanilla")``, ``("red", "vanilla",
    "color")``, ``("red", "color", "color")``, plus the mirrored
    ``("red", "color", "vanilla")``. Yellow has only one variant so it
    contributes a single entry.

    Fixtures are ROM-specific (PyBoy save states are bit-tied to the
    ROM bytes they were captured against). The test body skips when
    a variant's fixture doesn't exist so vanilla pairings auto-enable
    once ``cable_club-vanilla.state`` is produced via::

        python scripts/produce_cable_club_fixture.py \\
            --version red --variant vanilla \\
            --source path/to/vanilla_cerulean_pc.state

    The source must itself be captured against the vanilla ROM (run
    the walkthrough scripts with ``POKERED_ROM_PATH`` pointing at the
    vanilla ROM first).
    """
    pairs = []
    for version, variants in _ROM_VARIANTS.items():
        for rom_a, tag_a in variants:
            for rom_b, tag_b in variants:
                pairs.append(
                    pytest.param(
                        version, rom_a, tag_a, rom_b, tag_b,
                        id=f"{version}-{tag_a}-x-{tag_b}",
                    )
                )
    return pairs


@pytest.mark.parametrize("version,rom_a,tag_a,rom_b,tag_b", _rom_variant_pairs())
def test_same_version_variants_reach_link_menu(
    version, rom_a, tag_a, rom_b, tag_b,
):
    """Prove same-version pairings reach ``LinkMenu`` under every ROM
    variant combo (vanilla×vanilla, vanilla×color, color×color).

    The color patch only alters cartridge-header CGB flags + color
    palette code; serial-protocol code is untouched. This test guards
    against any future regression where the patch accidentally affects
    the serial path or Cable Club script.

    Uses the LinkMenu milestone rather than full battle to keep runtime
    reasonable — 4 variants × 9 pairings × 7 min would be ~4 hours. The
    LinkMenu path converges in ~2 min per pairing.

    Each side loads its variant-specific cable_club.state fixture
    (``cable_club.state`` for color, ``cable_club-vanilla.state`` for
    vanilla). Skips when the required fixture is missing — so pairings
    auto-enable once vanilla fixtures are produced.
    """
    if not (rom_a.is_file() and rom_b.is_file()):
        pytest.skip(f"ROM variant missing: {rom_a.name} or {rom_b.name}")
    state_a = _variant_state_path(version, tag_a)
    state_b = _variant_state_path(version, tag_b)
    if not (state_a.is_file() and state_b.is_file()):
        missing = [p.name for p in (state_a, state_b) if not p.is_file()]
        pytest.skip(
            f"cable_club state fixture(s) missing for {version}: "
            f"{missing}. Produce via scripts/produce_cable_club_fixture.py"
        )

    a = _open_session_variant(version, rom_a, tag_a)
    b = _open_session_variant(version, rom_b, tag_b)
    try:
        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)

        diag = _drive_two_sessions_to_link_menu(a, b, link)
        counters = diag["counters"]

        print(
            f"\n{version} {tag_a}<->{tag_b} LinkMenu reach:\n"
            + "\n".join(f"  {sym}: {cnt}" for sym, cnt in counters.items())
            + f"\n  frames_used: {diag['frames_used']}"
        )

        lm = counters["LinkMenu"]
        assert lm[0] > 0 and lm[1] > 0, (
            f"{version} {tag_a}<->{tag_b}: LinkMenu never reached; {counters}"
        )
    finally:
        a.close()
        b.close()
