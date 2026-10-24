"""Manual link-cable demo: pair two sessions and drive a trade.

Expects Cable Club save states at
``tests/fixtures/link/{version}/cable_club.state`` for both versions.
Produce those states manually — see the README 'Link cable' section.

Usage:

    python scripts/link_trade_demo.py --primary blue --peer yellow [--view]

With ``--view`` both PyBoy windows open so you can watch the trade.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for parent in [REPO_ROOT, *REPO_ROOT.parents]:
    if (parent / "rom").is_dir():
        ROM_ROOT = parent / "rom"
        break
else:
    ROM_ROOT = REPO_ROOT / "rom"

sys.path.insert(0, str(REPO_ROOT / "src"))

from pokered_harness.link import LinkPair  # noqa: E402
from pokered_harness.session import Session  # noqa: E402


ROM_PATHS = {
    "red": (ROM_ROOT / "red" / "pokemon-red.gb", ROM_ROOT / "red" / "pokemon-red.sym"),
    "blue": (ROM_ROOT / "blue" / "pokemon-blue.gb", ROM_ROOT / "blue" / "pokemon-blue.sym"),
    "yellow": (
        ROM_ROOT / "yellow" / "pokemon-yellow.gbc",
        ROM_ROOT / "yellow" / "pokemon-yellow.sym",
    ),
}

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "link"


def _open(version: str, *, view: bool) -> Session:
    rom, sym = ROM_PATHS[version]
    if not rom.exists():
        sys.exit(f"ROM missing: {rom}")
    if not sym.exists():
        sys.exit(f"SYM missing: {sym}")
    return Session.from_files(rom, sym, view=view)


def _load_cable_state(session: Session, version: str) -> None:
    path = FIXTURE_ROOT / version / "cable_club.state"
    if not path.exists():
        sys.exit(
            f"Save state missing: {path}\n"
            "Produce it by playing {version} to the Cerulean PC Cable Club "
            "attendant, with a 2+ mon party, then call session.save_state()."
            .format(version=version)
        )
    session.load_state(path.read_bytes())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--primary", choices=["red", "blue", "yellow"], required=True)
    ap.add_argument("--peer", choices=["red", "blue", "yellow"], required=True)
    ap.add_argument("--view", action="store_true", help="Open SDL2 windows on both sides.")
    ap.add_argument(
        "--max-ticks",
        type=int,
        default=60 * 60 * 30,  # ~30 sec at 60fps
        help="Budget for the trade walk.",
    )
    args = ap.parse_args()

    primary = _open(args.primary, view=args.view)
    peer = _open(args.peer, view=args.view)

    try:
        _load_cable_state(primary, args.primary)
        _load_cable_state(peer, args.peer)

        pair = LinkPair(
            primary,
            peer,
            version_primary=args.primary,
            version_peer=args.peer,
        )
        pair.pair()
        print(f"paired {args.primary} <-> {args.peer}; transport ready")

        # Step both sides a bit to let any idle-screen dialogue advance.
        # The save states should put both players in front of the Cable
        # Club attendant, mid-dialogue or on the menu. From here a real
        # demo would script A-presses through the trade UI, select mons,
        # confirm, and wait for Trade_ShowPlayerMon / Trade_ShowEnemyMon
        # events to fire via the pair's EventBus.
        pair.step(args.max_ticks // 4)

        print("transport snapshot:", pair.transport.snapshot())
        print(
            "events seen on primary:",
            [e.name for e in list(primary.events)[-10:]],
        )
        print(
            "events seen on peer:   ",
            [e.name for e in list(peer.events)[-10:]],
        )
        return 0
    finally:
        primary.close()
        peer.close()


if __name__ == "__main__":
    raise SystemExit(main())
