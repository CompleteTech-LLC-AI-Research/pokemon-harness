"""Constants, error type, and node-id helper for the coverage report (#130).

Split from ``scripts/coverage_report.py`` with no behavior change."""

from __future__ import annotations

import re
import runpy
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MATRIX = runpy.run_path(str(_REPO / "scripts" / "tcp_link_matrix.py"))
CATALOG_PATH = _REPO / "release-evidence" / "battle-scenarios.json"
COVERAGE_DIMENSION = "one_turn_pairing"
EXPANDED_DIMENSION = "expanded_mechanics"
_REQUIRED_DIMENSIONS = (COVERAGE_DIMENSION, EXPANDED_DIMENSION)
_RUNTIMES = ("source", "cython")
_ROLES = ("listen", "connect")
_VERSIONS = ("red", "blue", "yellow")
_TRANSPORTS = ("local", "remote")
_SCOPES = ("tested", "planned_unverified", "deliberately_excluded")
# Tool-owned complete effect inventory for effects_version 1: the pinned pret
# move-effect constants 0..86. Never derived from the supplied family list.
_PINNED_EFFECT_IDS: tuple[int, ...] = tuple(range(87))
# Tool-owned exact move-to-effect mapping for effects_version 1, derived from the
# pinned pret `data/moves/moves.asm` and `constants/move_effect_constants.asm`
# (Red/Blue revision fbcf7d0e). Index i is move id i+1 (move ids 1..165); the
# catalog may never reassign, omit, or duplicate a move.
_PINNED_MOVE_EFFECTS: tuple[int, ...] = (
    0,
    0,
    29,
    29,
    0,
    16,
    4,
    5,
    6,
    0,
    0,
    38,
    39,
    50,
    0,
    0,
    0,
    28,
    43,
    42,
    0,
    0,
    37,
    44,
    0,
    45,
    37,
    22,
    37,
    0,
    29,
    38,
    0,
    36,
    42,
    48,
    27,
    48,
    19,
    2,
    77,
    29,
    19,
    31,
    18,
    28,
    32,
    49,
    41,
    86,
    69,
    4,
    4,
    46,
    0,
    0,
    0,
    5,
    5,
    76,
    70,
    68,
    80,
    0,
    0,
    48,
    37,
    0,
    41,
    0,
    3,
    3,
    84,
    13,
    0,
    39,
    66,
    67,
    32,
    27,
    20,
    41,
    42,
    6,
    6,
    67,
    6,
    0,
    0,
    38,
    39,
    66,
    76,
    71,
    32,
    10,
    52,
    0,
    81,
    28,
    41,
    82,
    59,
    15,
    56,
    11,
    15,
    22,
    49,
    11,
    11,
    51,
    64,
    25,
    65,
    47,
    26,
    83,
    9,
    7,
    0,
    36,
    33,
    33,
    31,
    34,
    0,
    42,
    17,
    39,
    29,
    70,
    53,
    22,
    56,
    45,
    67,
    8,
    66,
    29,
    3,
    32,
    39,
    57,
    70,
    0,
    32,
    22,
    41,
    85,
    51,
    0,
    7,
    29,
    44,
    56,
    0,
    31,
    10,
    24,
    0,
    40,
    0,
    79,
    48,
)
# Effect slots the pinned table assigns no move (const_skip padding and unused
# constants); these must be declared empty and deliberately excluded.
_PINNED_UNUSED_EFFECT_IDS: tuple[int, ...] = (
    1,
    12,
    14,
    21,
    23,
    30,
    35,
    54,
    55,
    58,
    60,
    61,
    62,
    63,
    72,
    73,
    74,
    75,
    78,
)
# Tool-owned mandatory runtimes keyed by coverage scope version. The catalog
# declares requirements, but these pins are data the tool owns: a catalog edit
# can never remove a runtime the coverage scope version requires.
_MANDATORY_RUNTIMES_BY_COVERAGE_VERSION: dict[int, tuple[str, ...]] = {
    1: ("source", "cython"),
}
# Report status for a case whose catalog declaration is structurally invalid.
# It is never a terminal pytest outcome and can never count as tested.
_CATALOG_STATUS = "catalog"
# Internal report sentinel for a fully-evidenced required case. It is never a
# terminal pytest outcome and must stay distinct from any result status.
_CASE_SENTINEL = "tested"
# Fallback only; the catalog's coverage.evidence_policy.accepted_outcomes is
# authoritative and is enforced when classifying terminal records.
_DEFAULT_ACCEPTED_OUTCOMES = ("passed",)
_INCOMPLETE_STATUSES = frozenset(
    {
        "missing",
        "duplicate",
        "mismatched",
        "unidentified",
        "unaccepted",
        "partial",
        "collection",
        "skipped",
        "xfailed",
        "xpassed",
        "failed",
        "error",
        "timed_out",
        "interrupted",
        "not_run",
        _CATALOG_STATUS,
    }
)
_GATE_STATUS = {
    "PASS": "passed",
    "FAIL": "failed",
    "TIMEOUT": "timed_out",
    "INTERRUPTED": "interrupted",
    "NOT_STARTED": "not_run",
}
# A production-gate collection entry passes only with an explicitly successful
# status; every other status (FAIL, INTERRUPTED, NOT_STARTED) fails closed.
_COLLECTION_PASS = frozenset({"pass", "passed"})
# The one_turn_pairing cases are settled link battles. A declared case fixture
# must be a link-battle fixture for the listen endpoint's game.
_LINK_BATTLE_TYPE = "link_battle"
_LINK_BATTLE_VARIANTS = {"red": "color", "blue": "color", "yellow": "cgb"}
# Enclosing status values that still mean "not a clean terminal pass". Any
# tier/block status that maps to something outside this set marks every
# contained case as partial rather than silently qualifying the dimension.
_ENCLOSING_PASS = frozenset({"passed"})
_MOVE_EFFECT_RE = re.compile(r"[\"'](?:local|enemy)_move_effect[\"']\s*:\s*(\d+)")
_EFFECT_TEXT_KEYS = ("output_tail", "system_out", "stdout")


class CoverageError(ValueError):
    """Raised when the catalog or a supplied result cannot be interpreted."""


def normalize_nodeid(nodeid: str) -> str:
    """Normalise a pytest node ID for comparison."""
    path, separator, test_name = str(nodeid).partition("::")
    normalized_path = path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    return f"{normalized_path}::{test_name}" if separator else normalized_path
