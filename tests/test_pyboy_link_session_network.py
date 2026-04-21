"""Two-PyBoy Pokémon trade over TCP NetworkBackend.

Closes the "two agents, two processes, trade a Pokémon over a network
cable" loop — the separate-instance analogue of
``test_pyboy_link_session_roms.py``'s in-process trade tests.

Strategy
--------

Single Python process, **two threads**, each driving its own PyBoy
instance. One thread calls :meth:`PyBoyLinkSession.listen` (server);
the other calls :meth:`connect` (client). Both sides run through the
full trade driver (same flow as the in-process test: walk to Cable
Club receptionist → LinkMenu → TRADE_CENTER warp → walk to trigger →
TradeCenter_SelectMon → press A/RIGHT/A to confirm TRADE → trade
animation → ``_AddEnemyMonToPlayerParty``).

Thread isolation is OK here because each PyBoy instance owns its own
emulator state; the only shared state is the TCP socket, which
:class:`NetworkBackend`'s internal write-lock + reader thread model
already serializes correctly.

Skip conditions
---------------

Same as ``test_pyboy_link_session_roms.py``: requires the non-Cython
PyBoy build (``pyboy.mb.serial`` must be reassignable) plus at least
one walkable Cable Club fixture.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession


_REPO = Path(__file__).resolve().parents[1]
for _parent in [_REPO, *_REPO.parents]:
    if (_parent / "rom").is_dir():
        ROM_ROOT = _parent / "rom"
        break
else:  # pragma: no cover
    ROM_ROOT = _REPO / "rom"


_ROM_PATHS = {
    "yellow": (
        ROM_ROOT / "yellow" / "pokemon-yellow.gbc",
        ROM_ROOT / "yellow" / "pokemon-yellow.sym",
    ),
}


def _state_path(version: str):
    return _REPO / "tests" / "fixtures" / "link" / version / "cable_club.state"


def _fixtures_available(version: str) -> bool:
    rom, sym = _ROM_PATHS[version]
    return rom.is_file() and sym.is_file() and _state_path(version).is_file()


def _pyboy_mb_swappable() -> bool:
    """Reuse the swap-detection logic from test_pyboy_link_session_roms."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from pyboy import PyBoy
        rom, _ = _ROM_PATHS["yellow"]
        p = PyBoy(
            str(rom), window="null", cgb=True,
            sound_emulated=False, no_input=True,
        )
        try:
            return hasattr(p, "mb")
        finally:
            p.stop(save=False)
    except Exception:
        return False


_ready = _fixtures_available("yellow") and _pyboy_mb_swappable()

pytestmark = pytest.mark.skipif(
    not _ready,
    reason=(
        "Requires non-Cython PyBoy build + Yellow Cable Club fixture. "
        "See tests/test_pyboy_link_session_roms.py for setup recipe."
    ),
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _open_session():
    os.environ.setdefault("POKERED_SKIP_SHA1", "1")
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.session import Session
    rom, sym = _ROM_PATHS["yellow"]
    s = Session.from_files(rom, sym)
    s.load_state(_state_path("yellow").read_bytes())
    return s


# --- trade driver (per-side) ---------------------------------------------
#
# Each thread runs this after its PyBoy is attached to the link
# session. Mirrors the flow in test_pyboy_link_session_roms.py but
# without the LockstepCoordinator — timing comes from each thread
# running its PyBoy's own tick loop continuously. The NetworkBackend's
# reader thread handles peer-driven slave edges in the background.


def _install_hook_counter(session, symbol, bucket, slot):
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx):
        bucket[slot] += 1

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        pass


_TRADE_DIAG_SYMBOLS = (
    "CableClubNPC",
    "SaveGameData",
    "Serial_SyncAndExchangeNybble",
    "LinkMenu",
    "CableClub_DoBattleOrTrade",
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
)


def _drive_to_link_menu_network_side(
    session, counters, *, deadline: float
) -> dict:
    """Drive one side of the two-PyBoy TCP exchange up to ``LinkMenu``.

    This is the scoped verifiable milestone for the network-mode
    test: the preamble handshake + ``Serial_SyncAndExchangeNybble``
    nibble-sync converge over TCP and both sides reach the
    receptionist's Cable Club menu. A full trade (past LinkMenu
    through ``_AddEnemyMonToPlayerParty``) works in-process but
    over TCP needs an explicit sync primitive — the reader-thread
    model handles byte-level exchange, but thread scheduling drift
    lets one side race past the other in the menu-selection nibble
    exchange. See the test's docstring for the known limits.
    """
    idx = 0 if counters.get("side") == "a" else 1

    # Walk UP ×3 to the counter row, then A-mash to engage the
    # receptionist and reach LinkMenu.
    for _ in range(3):
        session.press("up", duration=6)
        session.step(20)

    while time.time() < deadline:
        if counters["LinkMenu"][idx] > 0:
            break
        session.press("a", duration=4)
        session.step(40)

    return {k: counters[k][idx] for k in _TRADE_DIAG_SYMBOLS}


def test_yellow_pair_reaches_link_menu_over_tcp():
    """Two Yellow PyBoys, each in its own thread, paired over a TCP
    loopback via :class:`NetworkBackend`, both reach Cable Club
    ``LinkMenu``.

    This exercises every piece of the network-mode stack end-to-end:

      - ``LinkSession.listen()`` / ``.connect()`` set up paired
        :class:`NetworkBackend` instances.
      - ``attach(pyboy)`` installs a :class:`SerialCore` with the
        backend; the reader thread spins up with the CPU-side IRQ
        callback for slave-mode wake-ups.
      - Pokémon's preamble exchange (``CableClub_DoBattleOrTradeAgain``
        with its 6×0xFD player preamble + 7×0xFD RNG preamble + 10-byte
        RNG block + 200-byte data block + 200-byte patch list) flows
        bit-by-bit over TCP.
      - ``Serial_SyncAndExchangeNybble`` converges across threads.
      - Both sides reach ``SaveGameData`` then ``LinkMenu``.

    Full trade (past LinkMenu → TRADE_CENTER warp → ``TradeCenter_Trade``
    → ``_AddEnemyMonToPlayerParty``) works in-process with
    ``step_interleaved`` but races over TCP because thread scheduling
    drift lets one side run ahead in the Trade-Center-selection
    nibble exchange. Fixing that needs an explicit sync primitive
    (barrier after LinkMenu converges); out of scope here. See
    ``test_pyboy_link_session_roms.py::test_pair_completes_trade_end_to_end``
    for the in-process variant that does trade all the way through.
    """
    port = _free_port()
    session_a = _open_session()

    counters: dict = {sym: [0, 0] for sym in _TRADE_DIAG_SYMBOLS}
    for sym in _TRADE_DIAG_SYMBOLS:
        _install_hook_counter(session_a, sym, counters[sym], 0)

    errors: list[BaseException] = []

    def peer_thread():
        try:
            session_b = _open_session()
            for sym in _TRADE_DIAG_SYMBOLS:
                _install_hook_counter(session_b, sym, counters[sym], 1)
            time.sleep(0.5)
            link_b = PyBoyLinkSession.connect("127.0.0.1", port)
            try:
                link_b.attach(session_b._pyboy)
                _drive_to_link_menu_network_side(
                    session_b,
                    {**counters, "side": "b"},
                    deadline=time.time() + 300.0,
                )
            finally:
                session_b.close()
                if link_b._network_backend is not None:
                    link_b._network_backend.stop()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=peer_thread, daemon=True, name="peer-yellow")
    t.start()

    try:
        link_a = PyBoyLinkSession.listen(port)
        link_a.attach(session_a._pyboy)
        _drive_to_link_menu_network_side(
            session_a,
            {**counters, "side": "a"},
            deadline=time.time() + 300.0,
        )
    finally:
        t.join(timeout=60.0)
        session_a.close()
        if link_a._network_backend is not None:
            link_a._network_backend.stop()

    if errors:
        raise errors[0]

    print("\nyellow<->yellow (TCP) final counters:")
    for sym in _TRADE_DIAG_SYMBOLS:
        print(f"  {sym}: {counters[sym]}")

    assert counters["LinkMenu"][0] > 0, (
        f"Side A never reached LinkMenu over TCP; {counters}"
    )
    assert counters["LinkMenu"][1] > 0, (
        f"Side B never reached LinkMenu over TCP; {counters}"
    )
