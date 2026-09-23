"""Shared fixtures for the test suite."""

from __future__ import annotations

import importlib

import pytest

from pokered_harness.symbols.loader import SymbolTable, load_sym_text
from scripts import coverage_report as coverage
from tests._mcp_timed_remote_support import failure_snapshots  # noqa: F401
from tests._timed_link_session_support import (  # noqa: F401
    game,
    session_type,
    source_lifecycle_game,
)
from tests._timed_remote_support import remote  # noqa: F401
from tests._timed_wire_support import kind  # noqa: F401

try:
    from tests._battle_coverage_support import CATALOG_PATH
    from tests._tier_config import MARKERS, classify_test
except ModuleNotFoundError:  # pragma: no cover - direct conftest loading
    from _battle_coverage_support import CATALOG_PATH
    from _tier_config import MARKERS, classify_test


def pytest_configure(config: pytest.Config) -> None:
    """Register the production-gate markers for direct pytest users."""

    descriptions = {
        "unit": "ROM-free deterministic tests",
        "real_rom": "requires a BYO ROM and symbol file",
        "local_link": "real-ROM in-process link tests",
        "remote_link": "real-ROM TCP or subprocess link tests",
        "mcp_stdio": "real-ROM MCP stdio integration tests",
        "acceptance": "optional real-ROM trade or battle acceptance",
        "trade": "optional real-ROM trade coverage",
        "trade_acceptance": "strict real-ROM party-swap acceptance",
        "battle": "optional real-ROM battle coverage",
        "battle_acceptance": "strict real-ROM battle-turn acceptance",
        "timing_sensitive": "repeatable scheduling-sensitive regression",
    }
    for marker in MARKERS:
        config.addinivalue_line("markers", f"{marker}: {descriptions[marker]}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Assign every collected test to an explicit ROM-free or ROM tier."""

    del config
    for item in items:
        filename = str(item.fspath)
        test_name = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        for marker in classify_test(filename, test_name):
            item.add_marker(getattr(pytest.mark, marker))


class DictMemory:
    """Sparse dict-backed MemoryLike — defaults to 0 for unset addresses."""

    def __init__(self, initial: dict[int, int] | None = None) -> None:
        self._m: dict[int, int] = dict(initial or {})

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.start, key.stop, key.step or 1
            return [self._m.get(a, 0) for a in range(start, stop, step)]
        return self._m.get(int(key), 0)

    def __setitem__(self, key: int, value: int) -> None:
        self._m[int(key)] = int(value) & 0xFF

    def write_word_le(self, addr: int, value: int) -> None:
        self[addr] = value & 0xFF
        self[addr + 1] = (value >> 8) & 0xFF


# Addresses chosen to match real pokered conventions where known, but the
# parsers only care about the *names*, so the test suite remains valid
# even if real-build addresses shift.
CANONICAL_SYM = """\
00:D35E wCurMap
00:D361 wYCoord
00:D362 wXCoord
00:D46A wWalkCounter
00:C109 wSpritePlayerStateData1FacingDirection
00:D5AB wCurrentMapScriptFlags
00:D5A6 wCurMapScript
00:D7D4 wStatusFlags5
00:CC26 wCurrentMenuItem
00:CC28 wMaxMenuItem
00:CC29 wMenuWatchedKeys
00:CC36 wListScrollOffset
00:CC2B wPartyAndBillsPCSavedMenuItem
00:CC2C wBagSavedMenuItem
00:CC2D wBattleAndStartSavedMenuItem
00:CC3A wTextDest
00:CC3C wDoNotWaitForButtonPressAfterDisplayingText
00:D057 wIsInBattle
00:D05A wBattleType
00:D058 wEngagedTrainerClass
00:D059 wEngagedTrainerSet
00:CC2F wMoveMenuType
00:CCDC wPlayerSelectedMove
00:CCDD wEnemySelectedMove
00:CCD5 wPlayerMonNumber
00:D05E wActionResultOrTookBattleTurn
00:D163 wPartyCount
00:D16B wPartyMons
00:D356 wObtainedBadges
00:D359 wPlayerID
00:D347 wPlayerMoney
00:DA40 wPlayTimeHours
00:DA41 wPlayTimeMaxed
00:DA42 wPlayTimeMinutes
00:DA43 wPlayTimeSeconds
00:D747 wEventFlags
00:D31D wNumBagItems
00:D31E wBagItems
"""


@pytest.fixture
def symbols() -> SymbolTable:
    return load_sym_text(CANONICAL_SYM)


@pytest.fixture
def mem() -> DictMemory:
    return DictMemory()


@pytest.fixture()
def catalog() -> dict:
    return coverage.load_catalog(CATALOG_PATH)


@pytest.fixture
def probe():
    return importlib.import_module("scripts._timed_battle_probe")
