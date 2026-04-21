"""Real-ROM smoke test: two Yellow sessions under :class:`PyBoyLinkSession`.

The previous link-cable work in this repo used an RPC-style bridge that
hooked Pokémon's symbol-level serial routines and faked byte exchange
over TCP. Milestones 1–4 of the design doc replace that with a
bit-accurate :class:`SerialCore` living inside the PyBoy motherboard
itself. This test proves the new stack is live end-to-end on real
ROMs by:

1. Loading two Yellow sessions at the Cable Club receptionist state
   (produced by ``scripts/produce_cable_club_fixture.py``).
2. Pairing them under :class:`PyBoyLinkSession.local`, which swaps
   ``pyboy.mb.serial`` for a :class:`SerialCore` and wires the two
   cores via :class:`LockstepCoordinator`.
3. Pressing A on both sides to initiate the receptionist dialogue,
   which eventually leads to ``CableClub_DoBattleOrTradeAgain``
   emitting the trade preamble.
4. Stepping both sides in per-frame lockstep and counting edges via a
   backend wrapper.
5. Asserting the count is non-zero — proof that the real ROM *did*
   drive serial transfers through our new core.

Gated with ``skipif`` on ROM availability, fixture availability, AND a
runtime check that ``pyboy.mb`` is Python-accessible. The installed
wheel build of PyBoy is Cython-compiled and ``mb`` is a ``cdef``
attribute — inaccessible from Python — so this test only runs under a
non-Cython PyBoy build.

Running under a non-Cython venv
-------------------------------

One-shot setup (from the worktree root)::

    python -m venv .venv-noncython
    .venv-noncython/Scripts/pip install numpy
    git clone --depth 1 https://github.com/Baekalfen/PyBoy.git vendor/pyboy-src
    # Patch vendor/pyboy-src/setup.py line 10 so CYTHON respects
    # PYBOY_NO_CYTHON (already done in this worktree's vendor/ copy):
    #   CYTHON = platform.python_implementation() == "CPython" \\
    #       and not os.getenv("PYBOY_NO_CYTHON")
    PYBOY_NO_CYTHON=1 .venv-noncython/Scripts/pip install \\
        --no-build-isolation -e vendor/pyboy-src
    .venv-noncython/Scripts/pip install -e . pytest pytest-asyncio mcp

Then::

    .venv-noncython/Scripts/python.exe -m pytest \\
        tests/test_pyboy_link_session_roms.py -v

Expect ~50s for both tests (non-Cython PyBoy is markedly slower than
the Cython wheel). A pair of 600-frame Yellow emulations at realtime
is enough headroom for the Cable Club receptionist preamble handshake
to fire many edges through :class:`SerialCore`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_core import SerialCore


_REPO = Path(__file__).resolve().parents[1]
for _parent in [_REPO, *_REPO.parents]:
    if (_parent / "rom").is_dir():
        ROM_ROOT = _parent / "rom"
        break
else:  # pragma: no cover - defensive; tests skip below if ROM missing.
    ROM_ROOT = _REPO / "rom"

_YELLOW_ROM = ROM_ROOT / "yellow" / "pokemon-yellow.gbc"
_YELLOW_SYM = ROM_ROOT / "yellow" / "pokemon-yellow.sym"
_YELLOW_STATE = _REPO / "tests" / "fixtures" / "link" / "yellow" / "cable_club.state"

_ROM_PATHS = {
    "red": (
        ROM_ROOT / "red" / "pokemon-red.gb",
        ROM_ROOT / "red" / "pokemon-red.sym",
    ),
    "blue": (
        ROM_ROOT / "blue" / "pokemon-blue-color.gb",
        ROM_ROOT / "blue" / "pokemon-blue.sym",
    ),
    "yellow": (_YELLOW_ROM, _YELLOW_SYM),
}


def _state_path(version: str):
    return _REPO / "tests" / "fixtures" / "link" / version / "cable_club.state"


_fixtures_ready = (
    _YELLOW_ROM.is_file() and _YELLOW_SYM.is_file() and _YELLOW_STATE.is_file()
)


def _pyboy_mb_swappable() -> bool:
    """Detect whether ``pyboy.mb.serial`` is reassignable from Python.

    Wheel-installed PyBoy is Cython-compiled (``cdef Motherboard mb``,
    ``cdef Serial serial``); ``mb`` is not exposed to Python at all,
    so :class:`PyBoyLinkSession.attach` cannot swap the serial device.
    A source-install ``pip install -e .`` of PyBoy with the Cython
    extension disabled (``PYBOY_NO_CYTHON=1`` or building without
    Cython) makes both attributes regular Python attributes and the
    session works end-to-end.
    """
    try:
        import warnings

        warnings.filterwarnings("ignore")
        from pyboy import PyBoy

        # Probe a temporary instance for the ``mb`` attribute. We can't
        # rely on ``hasattr(PyBoy, "mb")`` at the class level because
        # cdef attributes aren't visible there either.
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
    except Exception:
        return False


_pyboy_swappable = _fixtures_ready and _pyboy_mb_swappable()


pytestmark = pytest.mark.skipif(
    not _pyboy_swappable,
    reason=(
        "PyBoy is Cython-compiled (mb / mb.serial are cdef attributes "
        "not exposed to Python) so PyBoyLinkSession.attach can't swap "
        "in SerialCore from outside the C extension. Install PyBoy "
        "from source without Cython, or use a fork that bakes "
        "SerialCore into the motherboard, then this test will run."
        if _fixtures_ready
        else (
            "Yellow ROM or Cable Club state fixture missing — regenerate "
            "with scripts/produce_cable_club_fixture.py --version yellow"
        )
    ),
)


class _CountingBackend:
    """Wraps an existing :class:`SerialBackend` and counts edges.

    Installed on a :class:`SerialCore` after its coordinator backend is
    wired, so we can observe real-ROM serial activity without changing
    the coordinator API.
    """

    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped
        self.edges = 0
        self.bytes_complete = 0
        self._bit_index = 0

    def on_edge(self, our_bit: int, our_role: int) -> int:
        peer_bit = self.wrapped.on_edge(our_bit, our_role)
        self.edges += 1
        self._bit_index += 1
        if self._bit_index % 8 == 0:
            self.bytes_complete += 1
        return peer_bit


def _open_yellow_session():
    return _open_session("yellow")


def _open_session(version: str):
    """Load ``version`` ROM + cable_club.state fixture.

    Defers the ``pokered_harness.session`` import so the module remains
    collectable even when some runtime deps are missing (e.g. under
    main-env pytest where the skipif above fires early).
    """
    os.environ.setdefault("POKERED_SKIP_SHA1", "1")
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.session import Session  # noqa: E402

    rom, sym = _ROM_PATHS[version]
    session = Session.from_files(rom, sym)
    session.load_state(_state_path(version).read_bytes())
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
        assert counter_a.edges > 0, (
            "A-side SerialCore saw zero edges after 600 frames — "
            "the ROM isn't driving our serial path"
        )
        assert counter_b.edges > 0, (
            "B-side SerialCore saw zero edges after 600 frames — "
            "the ROM isn't driving our serial path"
        )
        # And at least one full byte should have completed.
        assert counter_a.bytes_complete >= 1
        assert counter_b.bytes_complete >= 1
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
        link.step_interleaved(frames)

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
    nybble_sym = "Serial_SyncAndExchangeNybble"
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
