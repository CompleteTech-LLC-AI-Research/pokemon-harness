"""Parse pokered/pokeyellow ``data/maps/objects/*.asm`` files and
compute sight-cone tiles for each STAY trainer on a given map.

Usage as a library::

    from trainer_sight_cones import sight_cone_tiles
    blockers = sight_cone_tiles("MtMoon1F", pret_root)
    # returns set[(x, y)] of tiles to treat as impassable

Sight cones are the tiles a trainer watches when standing still.
Stepping into a sight cone triggers trainer engagement. Computing
them + adding to A*'s blocker set lets the pathfinder avoid
engagement entirely on maps where we just want to cross (like
Mt. Moon 1F where most trainers don't need to be fought).

Sight range: Gen 1 trainers watch up to ``TRAINER_SIGHT_RANGE`` tiles
in their facing direction (blocked by walls). pokered defines 4 in
the constants but we use a conservative default of 5 here —
empirically matches what the game treats as "in range".
"""
from __future__ import annotations

import re
from pathlib import Path

_OBJECT_RE = re.compile(
    r"object_event\s+"
    r"(\d+)\s*,\s*(\d+)\s*,\s*"
    r"SPRITE_\w+\s*,\s*"
    r"(STAY|WALK)\s*,\s*"
    r"(UP|DOWN|LEFT|RIGHT|BOULDER_MOVEMENT_BYTE_\d|NONE|ANY_DIR)\s*,\s*"
    r"TEXT_\w+"
    r"(?:\s*,\s*(OPP_\w+)\s*,\s*(\d+))?"
)


def parse_object_events(path: Path) -> list[dict]:
    """Parse ``object_event`` macros in a map's objects .asm file.

    Returns a list of dicts with keys: x, y, movement (STAY/WALK),
    facing (UP/DOWN/LEFT/RIGHT/NONE), trainer_class (str|None),
    trainer_set (int|None).
    """
    events: list[dict] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _OBJECT_RE.search(line)
        if not m:
            continue
        events.append({
            "x": int(m.group(1)),
            "y": int(m.group(2)),
            "movement": m.group(3),
            "facing": m.group(4),
            "trainer_class": m.group(5),
            "trainer_set": int(m.group(6)) if m.group(6) else None,
        })
    return events


_FACING_DELTA = {
    "UP": (0, -1),
    "DOWN": (0, 1),
    "LEFT": (-1, 0),
    "RIGHT": (1, 0),
}


def sight_cone_tiles(map_name: str, pret_root: Path,
                     max_sight: int = 5) -> set[tuple[int, int]]:
    """Return the set of (x, y) step cells in every STAY-trainer's
    sight cone on the given map. ``pret_root`` points at the cloned
    pret/pokeyellow repo.

    Only trainers (events with a trainer_class OPP_*) contribute.
    Static NPCs (no trainer) don't engage on sight-line."""
    obj_path = pret_root / "data" / "maps" / "objects" / f"{map_name}.asm"
    events = parse_object_events(obj_path)
    cone: set[tuple[int, int]] = set()
    for ev in events:
        if ev["movement"] != "STAY":
            continue
        if ev["trainer_class"] is None:
            continue
        dx, dy = _FACING_DELTA.get(ev["facing"], (0, 0))
        if (dx, dy) == (0, 0):
            continue
        x, y = ev["x"], ev["y"]
        for i in range(1, max_sight + 1):
            cone.add((x + dx * i, y + dy * i))
    return cone


def trainer_sight_cones_cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("map", help="Map name (e.g. MtMoon1F, Route3)")
    p.add_argument("--pret-root", required=True,
                   help="Path to pret/pokeyellow clone")
    p.add_argument("--max-sight", type=int, default=5)
    p.add_argument("--format", choices=("semi", "lines"), default="semi",
                   help="semi: output as 'x,y;x,y;...' (for --extra-blockers)")
    args = p.parse_args()
    tiles = sight_cone_tiles(args.map, Path(args.pret_root),
                              max_sight=args.max_sight)
    if args.format == "semi":
        print(";".join(f"{x},{y}" for x, y in sorted(tiles)))
    else:
        for x, y in sorted(tiles):
            print(f"{x},{y}")
    return 0


if __name__ == "__main__":
    raise SystemExit(trainer_sight_cones_cli())
