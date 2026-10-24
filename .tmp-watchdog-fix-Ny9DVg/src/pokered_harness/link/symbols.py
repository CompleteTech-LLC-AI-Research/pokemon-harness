"""Curated registry of pret/pokered + pret/pokeyellow symbols the link-cable
bridge hooks.

Label names validated against `rom/blue/pokemon-blue.sym` (pret/pokered) and
`rom/yellow/pokemon-yellow.sym` (pret/pokeyellow) at author time. Red shares
the pokered source tree so labels are the same as Blue.

Two labels seen in community writeups do not exist in the current pret
symbols (may have been inlined or never existed under those names):
``PrintTrainerInfo`` and ``LoadTradingData``. They are omitted. If future
work needs trade-card progress events, hook ``Trade_ShowPlayerMon`` /
``Trade_ShowEnemyMon`` instead — those do resolve.

Each :class:`LinkSymbol` has a :class:`LinkRole` that tells the bridge what
to do when the hooked code runs:

- :attr:`LinkRole.BRIDGE` — intercept the call and mutate memory so the
  local side sees the remote peer's byte.
- :attr:`LinkRole.HANDSHAKE` — short-circuit the handshake to "linked"
  without going through the real serial sync.
- :attr:`LinkRole.PROGRESS` — fire an :attr:`LinkSymbol.event_name` event
  on the :class:`EventBus`; no memory mutation.
- :attr:`LinkRole.BATTLE` — reserved for the link-battle milestone; not
  wired up yet.

Only ``exchange_bytes`` and ``handshake`` are :attr:`LinkSymbol.required`
— anything else that fails to resolve is simply skipped so the bridge can
keep running on a version where a diagnostic label was renamed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pokered_harness.symbols.loader import SymbolTable


# Version string convention matches VERSIONS.md / config.py: lowercase.
_VERSIONS: tuple[str, ...] = ("red", "blue", "yellow")


class LinkRole(str, Enum):
    """What the bridge does when a hooked label executes."""

    BRIDGE = "bridge"        # bridge MUST intercept and mutate memory
    HANDSHAKE = "handshake"  # bridge short-circuits to "linked"
    PROGRESS = "progress"    # fire as event, no mutation
    BATTLE = "battle"        # reserved for link-battle milestone


@dataclass(frozen=True, slots=True)
class LinkSymbol:
    """A single hook-target label in the link-cable registry."""

    key: str
    """Stable identifier the bridge uses to refer to this hook."""

    role: LinkRole
    """What the bridge does when the hooked label executes."""

    event_name: str | None
    """EventBus event name — required for PROGRESS, None otherwise."""

    per_version: dict[str, str]
    """Candidate label names per ROM, e.g. ``{"red": "Serial_ExchangeBytes", ...}``.

    Red and Blue share source so labels usually match exactly; Yellow
    diverges slightly and is the main reason this mapping exists.
    """

    required: bool
    """If True, :func:`resolve_link_symbols` raises when it can't resolve."""


def _same(label: str) -> dict[str, str]:
    """Shorthand for the common case: same label across all versions."""
    return {v: label for v in _VERSIONS}


LINK_SYMBOLS: tuple[LinkSymbol, ...] = (
    # ---- BRIDGE ---------------------------------------------------------
    LinkSymbol(
        key="exchange_bytes",
        role=LinkRole.BRIDGE,
        event_name=None,
        per_version=_same("Serial_ExchangeBytes"),
        required=True,
    ),
    LinkSymbol(
        key="send_zero_byte",
        role=LinkRole.BRIDGE,
        event_name=None,
        per_version=_same("Serial_SendZeroByte"),
        required=False,
    ),
    LinkSymbol(
        key="exchange_nybble",
        role=LinkRole.BRIDGE,
        event_name=None,
        per_version=_same("Serial_SyncAndExchangeNybble"),
        required=False,
    ),
    # ---- HANDSHAKE ------------------------------------------------------
    LinkSymbol(
        key="handshake",
        role=LinkRole.HANDSHAKE,
        event_name=None,
        per_version=_same("Serial_TryEstablishingExternallyClockedConnection"),
        required=True,
    ),
    LinkSymbol(
        key="cable_club_return",
        role=LinkRole.HANDSHAKE,
        event_name=None,
        per_version=_same("CableClub_DoBattleOrTradeAgain"),
        required=False,
    ),
    # ---- PROGRESS (events) ---------------------------------------------
    LinkSymbol(
        key="trade_select_mon",
        role=LinkRole.PROGRESS,
        event_name="link.trade.select_mon",
        per_version=_same("TradeCenter_SelectMon"),
        required=False,
    ),
    LinkSymbol(
        key="trade_show_player_mon",
        role=LinkRole.PROGRESS,
        event_name="link.trade.show_player_mon",
        per_version=_same("Trade_ShowPlayerMon"),
        required=False,
    ),
    LinkSymbol(
        key="trade_show_enemy_mon",
        role=LinkRole.PROGRESS,
        event_name="link.trade.show_enemy_mon",
        per_version=_same("Trade_ShowEnemyMon"),
        required=False,
    ),
    # ---- BATTLE (reserved, milestone 2) --------------------------------
    LinkSymbol(
        key="link_battle_versus",
        role=LinkRole.BATTLE,
        event_name=None,
        per_version=_same("DisplayLinkBattleVersusTextBox"),
        required=False,
    ),
)


def resolve_link_symbols(
    symbols: SymbolTable, version: str
) -> dict[str, tuple[int, int]]:
    """Resolve each :data:`LINK_SYMBOLS` entry's label for ``version``.

    Returns a ``{link_symbol.key: (bank, addr)}`` dict. Non-required
    entries that can't be resolved on ``version`` are silently skipped;
    required entries that can't be resolved raise :class:`LookupError`
    with a message naming the missing label so the operator can patch
    the per-version map.
    """
    if version not in _VERSIONS:
        raise ValueError(
            f"unknown version {version!r}; expected one of {_VERSIONS}"
        )

    resolved: dict[str, tuple[int, int]] = {}
    missing_required: list[tuple[str, str]] = []

    for link_sym in LINK_SYMBOLS:
        label = link_sym.per_version.get(version)
        if label is None:
            if link_sym.required:
                missing_required.append((link_sym.key, f"<no label for {version}>"))
            continue

        sym = symbols.get(label)
        if sym is None:
            if link_sym.required:
                missing_required.append((link_sym.key, label))
            continue

        resolved[link_sym.key] = (sym.bank, sym.addr)

    if missing_required:
        details = ", ".join(f"{k}={lbl!r}" for k, lbl in missing_required)
        raise LookupError(
            f"required link symbols missing on {version}: {details}"
        )

    return resolved


def symbols_for_role(role: LinkRole) -> tuple[LinkSymbol, ...]:
    """Return the subset of :data:`LINK_SYMBOLS` whose role matches ``role``."""
    return tuple(s for s in LINK_SYMBOLS if s.role is role)


__all__ = [
    "LINK_SYMBOLS",
    "LinkRole",
    "LinkSymbol",
    "resolve_link_symbols",
    "symbols_for_role",
]
