from __future__ import annotations

"""Shared helpers and fixtures for the pyboy-link real-ROM tests.

Split from ``tests/test_pyboy_link_session_roms.py`` (#132) with no behavior
change: the session/variant construction helpers, the module autouse runtime
guard, the hook-counter installer, and the party/battle-fixture legality
checks moved here verbatim so every real-ROM module shares one surface.
"""

import os
import sys
from pathlib import Path

import pytest

from tests._battle_turn_evidence import BattleTurnObserver, choose_supported_battle_move
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


_LINK_CHUNK_CYCLES = int(os.environ.get("POKERED_LINK_CHUNK_CYCLES", "256"))


_LINK_MENU_TARGET_ITEM = 1  # COLOSSEUM; TRADE_CENTER is item 0.


_LINK_MENU_SETTLE_FRAMES = 60


_LINK_MENU_CURSOR_BUDGET_FRAMES = 40


_LINK_MENU_MAX_ITEM = 3  # Yellow has four entries; Red/Blue have fewer.


_LINK_MENU_REQUIRED_KEYS = 0x01  # A is one of the ROM's watched keys.


_RECEPTIONIST_A_STAGGER_FRAMES = 4


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


def _open_session_pair(open_a, open_b):
    """Open two sessions transactionally, preserving a second-open error.

    The real-ROM tests perform their pair setup inside a later ``try`` block,
    so a failure while constructing the second endpoint would otherwise leave
    the first emulator unclosed. Factories keep this helper usable for the
    normal and variant fixtures without changing their construction paths.
    """
    first = open_a()
    try:
        second = open_b()
    except BaseException as error:
        try:
            first.close()
        except BaseException as cleanup_error:  # noqa: BLE001 - retain both failures
            error.add_note(f"first session cleanup failed: {cleanup_error!r}")
        raise
    return first, second


def _fixtures_available(version: str) -> bool:
    rom, sym = _ROM_PATHS[version]
    return rom.is_file() and sym.is_file() and _state_path(version).is_file()


def _close_linked_pair(link, *sessions) -> None:
    """Detach the pair owner before closing its two emulator sessions.

    A locally paired :class:`PyBoyLinkSession` owns both native serial cores;
    closing a Session first would leave a stopped emulator retained by the
    coordinator. Teardown attempts every cleanup step and re-raises the first
    failure after all sessions have had a close attempt.
    """
    errors: list[BaseException] = []
    if link is not None:
        try:
            link.detach_all()
            if link.attached:
                raise RuntimeError("local pair remained attached after detach_all")
        except BaseException as exc:  # noqa: BLE001 - preserve cleanup failures
            errors.append(exc)
    for session in sessions:
        try:
            session.close()
        except BaseException as exc:  # noqa: BLE001 - attempt every session cleanup
            errors.append(exc)
    if errors:
        first = errors[0]
        for extra in errors[1:]:
            first.add_note(f"additional linked-session cleanup failure: {extra!r}")
        raise first


def _install_hook_counter(
    session,
    symbol: str,
    bucket: list,
    slot: int,
    observer: BattleTurnObserver | None = None,
) -> None:
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
        if observer is not None:
            observer.observe(symbol)

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        # Hook may already exist from another source (unlikely under
        # PyBoyLinkSession, which doesn't install its own hooks).
        pass


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
    return choose_supported_battle_move(session, active_moves)


def _read_link_menu_cursor(session) -> int | None:
    """Read a ready LinkMenu cursor without changing emulator state.

    The ``LinkMenu`` symbol hook runs before the ROM has initialized its menu
    fields and before its input loop starts polling the joypad.  Treat an
    incomplete menu snapshot as delayed readiness instead of sending an
    input event into a transition.  All values come from ROM-owned bytes;
    this helper never writes RAM, registers, or execution state.
    """
    try:
        memory = session._pyboy.memory
        addr_of = session.symbols.addr_of
        current = int(memory[addr_of("wCurrentMenuItem")])
        maximum = int(memory[addr_of("wMaxMenuItem")])
        watched_keys = int(memory[addr_of("wMenuWatchedKeys")])
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None
    if not 0 <= current <= maximum <= _LINK_MENU_MAX_ITEM:
        return None
    if maximum not in (2, _LINK_MENU_MAX_ITEM):
        return None
    if watched_keys & _LINK_MENU_REQUIRED_KEYS != _LINK_MENU_REQUIRED_KEYS:
        return None
    return current


def _validate_non_negative_frame_budget(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _move_link_menu_cursors_to_colosseum(
    a,
    b,
    link,
    *,
    budget_frames: int = _LINK_MENU_CURSOR_BUDGET_FRAMES,
    frames_per_attempt: int = 20,
) -> dict[str, int | None]:
    """Move each ROM-owned LinkMenu cursor to COLOSSEUM with public input.

    Each loop observes both endpoints before issuing any directional event.
    A side already at item 1 receives no input, so a delayed peer cannot
    cause that side to wrap past COLOSSEUM.  ``link`` remains the sole frame
    owner; the last advance is clipped to the remaining budget so a caller
    never spends more than ``budget_frames`` while waiting for readiness.
    ``None`` is a read-only indication that the ROM menu fields are not ready
    yet, not a reason to guess at a button press.
    """
    budget_frames = _validate_non_negative_frame_budget(budget_frames, "budget_frames")
    if isinstance(frames_per_attempt, bool) or not isinstance(frames_per_attempt, int):
        raise TypeError("frames_per_attempt must be a positive integer")
    if frames_per_attempt <= 0:
        raise ValueError("frames_per_attempt must be a positive integer")

    frames_used = 0
    while frames_used < budget_frames:
        cursors = [
            _read_link_menu_cursor(a),
            _read_link_menu_cursor(b),
        ]
        if all(cursor == _LINK_MENU_TARGET_ITEM for cursor in cursors):
            break

        for session, cursor in zip((a, b), cursors, strict=True):
            if cursor is None or cursor == _LINK_MENU_TARGET_ITEM:
                continue
            # Use the shortest ordinary directional path from the observed
            # cursor.  In particular, a stale item > 1 is corrected with UP
            # rather than another DOWN that could wrap the menu.
            session.press(
                "down" if cursor < _LINK_MENU_TARGET_ITEM else "up",
                duration=12,
            )

        advance = min(frames_per_attempt, budget_frames - frames_used)
        link.step_interleaved(advance)
        frames_used += advance

    final_cursors = [
        _read_link_menu_cursor(a),
        _read_link_menu_cursor(b),
    ]
    return {
        "frames_used": frames_used,
        "cursor_a": final_cursors[0],
        "cursor_b": final_cursors[1],
    }


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
