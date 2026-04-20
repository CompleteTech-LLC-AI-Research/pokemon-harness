"""Offline ASCII renderer + connectivity analyzer for pokered/pokeyellow maps.

Reads a map's .blk (block ids), its tileset blockset .bst (16-byte block
tile patterns), and the tileset's collision list from
``data/tilesets/collision_tile_ids.asm``. Outputs an ASCII grid of step
cells (each block is 4x4 tiles; player step is 2x2 tiles) using the
bottom-left single-tile collision model — the same model the live
pathfinder uses by default (PATH_MODEL=single).

Doesn't need a live emulator state. Use for offline investigation of
stuck connectivity (e.g. "from B2F (15,27), can we reach (5,7)?").

Usage::

    python scripts/render_map.py --map MtMoonB2F --warps 15,27=S 5,7=E \\
        --flood 15,27 --pret G:/project/pokemon/_vendor/pokeyellow

Each --warps entry paints a labeled cell on the rendered grid. --flood
starts a BFS from that step cell and marks every reachable cell with
``+``; unreachable cells show as ``#`` (blocked) or ``.`` (walkable
but unvisited).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Tileset id -> (blockset filename, collision label)
TILESETS = {
    "OVERWORLD": ("overworld.bst", "Overworld_Coll"),
    "REDSHOUSE1": ("reds_house.bst", "RedsHouse1_Coll"),
    "MART": ("pokecenter.bst", "Mart_Coll"),
    "FOREST": ("forest.bst", "Forest_Coll"),
    "REDSHOUSE2": ("reds_house.bst", "RedsHouse2_Coll"),
    "DOJO": ("gym.bst", "Dojo_Coll"),
    "POKECENTER": ("pokecenter.bst", "Pokecenter_Coll"),
    "GYM": ("gym.bst", "Gym_Coll"),
    "HOUSE": ("house.bst", "House_Coll"),
    "FORESTGATE": ("gate.bst", "ForestGate_Coll"),
    "MUSEUM": ("gate.bst", "Museum_Coll"),
    "UNDERGROUND": ("underground.bst", "Underground_Coll"),
    "GATE": ("gate.bst", "Gate_Coll"),
    "SHIP": ("ship.bst", "Ship_Coll"),
    "SHIPPORT": ("ship_port.bst", "ShipPort_Coll"),
    "CEMETERY": ("cemetery.bst", "Cemetery_Coll"),
    "INTERIOR": ("interior.bst", "Interior_Coll"),
    "CAVERN": ("cavern.bst", "Cavern_Coll"),
    "LOBBY": ("lobby.bst", "Lobby_Coll"),
    "MANSION": ("mansion.bst", "Mansion_Coll"),
    "LAB": ("lab.bst", "Lab_Coll"),
    "CLUB": ("club.bst", "Club_Coll"),
    "FACILITY": ("facility.bst", "Facility_Coll"),
    "PLATEAU": ("plateau.bst", "Plateau_Coll"),
    "BEACHHOUSE": ("beach_house.bst", "BeachHouse_Coll"),
}


def load_collision_list(pret: Path, label: str) -> set[int]:
    text = (pret / "data" / "tilesets" / "collision_tile_ids.asm").read_text()
    # Find "<label>::" then read subsequent coll_tiles lines until blank
    # or another label. Labels like "Cavern_Coll" may share their
    # coll_tiles line with the next block only if aliased (e.g.
    # "ForestGate_Coll::\nMuseum_Coll::\nGate_Coll::\n\tcoll_tiles ..."
    # — they all point to the same list).
    lines = text.splitlines()
    out: set[int] = set()
    found = False
    # Gather all sibling labels that share the same coll_tiles block.
    target_idx = None
    for i, line in enumerate(lines):
        if re.match(rf"\s*{re.escape(label)}::", line):
            target_idx = i
            break
    if target_idx is None:
        raise RuntimeError(f"collision label {label} not found")
    # Scan forward for the first coll_tiles line (may be after alias
    # labels on subsequent lines).
    i = target_idx
    while i < len(lines):
        m = re.match(r"\s*coll_tiles\s+(.*)", lines[i])
        if m:
            args = m.group(1).strip()
            if args and not args.startswith(";"):
                for tok in re.split(r"[,\s]+", args):
                    tok = tok.strip().rstrip(",")
                    if not tok or tok.startswith(";"):
                        continue
                    if tok.startswith("$"):
                        out.add(int(tok[1:], 16))
                    elif tok.startswith("0x"):
                        out.add(int(tok, 16))
                    elif re.match(r"^-?\d+$", tok):
                        val = int(tok)
                        if val >= 0:
                            out.add(val)
            found = True
            break
        # Only traverse through immediate alias labels + blank lines.
        if re.match(r"\s*\w+_Coll::", lines[i]) or not lines[i].strip():
            i += 1
            continue
        break
    if not found:
        raise RuntimeError(f"no coll_tiles after {label}")
    return out


def load_map(pret: Path, map_name: str) -> tuple[list[list[int]], int, int]:
    """Return (blocks[y][x], width_blocks, height_blocks)."""
    # Find map_const MAP_NAME, W, H in map_constants.asm.
    text = (pret / "constants" / "map_constants.asm").read_text()
    # map_const MT_MOON_B2F, 20, 18 — but we pass e.g. "MtMoonB2F".
    # The .blk file lives at maps/<MtMoonB2F>.blk; the map_const uses
    # SNAKE_CASE of the same name (e.g. MT_MOON_B2F). Convert.
    # Insert underscore ONLY on lowercase -> (uppercase OR digit) boundary.
    # That handles:
    #   MtMoonB2F   -> MT_MOON_B2F   (B2F stays glued after the "Moon" break)
    #   MtMoon1F    -> MT_MOON_1F    ("n1" is lower->digit, breaks)
    #   MtMoonB1F   -> MT_MOON_B1F   ("B1" is upper->digit, stays glued)
    s = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", "_", map_name)
    snake = s.upper()
    m = re.search(rf"map_const\s+{snake}\s*,\s*(\d+)\s*,\s*(\d+)", text)
    if not m:
        raise RuntimeError(f"no map_const for {snake}")
    w = int(m.group(1))
    h = int(m.group(2))
    blk = (pret / "maps" / f"{map_name}.blk").read_bytes()
    if len(blk) != w * h:
        raise RuntimeError(f"{map_name}.blk size {len(blk)} != {w}*{h}")
    blocks = [
        [blk[y * w + x] for x in range(w)]
        for y in range(h)
    ]
    return blocks, w, h


def load_blockset(pret: Path, fname: str) -> list[bytes]:
    data = (pret / "gfx" / "blocksets" / fname).read_bytes()
    if len(data) % 16:
        raise RuntimeError(f"blockset {fname} length not /16")
    return [data[i:i + 16] for i in range(0, len(data), 16)]


def expand_tiles(
    blocks: list[list[int]], blockset: list[bytes]
) -> list[list[int]]:
    h = len(blocks)
    w = len(blocks[0]) if h else 0
    tiles = [[0] * (w * 4) for _ in range(h * 4)]
    for by in range(h):
        for bx in range(w):
            bid = blocks[by][bx]
            if bid >= len(blockset):
                pat = b"\xff" * 16
            else:
                pat = blockset[bid]
            for ty in range(4):
                for tx in range(4):
                    tiles[by * 4 + ty][bx * 4 + tx] = pat[ty * 4 + tx]
    return tiles


def step_passable(tiles: list[list[int]], sx: int, sy: int,
                   passable_ids: set[int]) -> bool:
    """Bottom-left single-tile model: step cell (sx, sy) is walkable iff
    the tile at (sx*2, sy*2+1) is in the passable list."""
    h = len(tiles)
    w = len(tiles[0]) if h else 0
    tx, ty = sx * 2, sy * 2 + 1
    if not (0 <= tx < w and 0 <= ty < h):
        return False
    return tiles[ty][tx] in passable_ids


def feet_tile(tiles: list[list[int]], sx: int, sy: int) -> int | None:
    """Return the bottom-left 'feet' tile id for step cell (sx, sy),
    or None if out of bounds."""
    h = len(tiles)
    w = len(tiles[0]) if h else 0
    tx, ty = sx * 2, sy * 2 + 1
    if not (0 <= tx < w and 0 <= ty < h):
        return None
    return tiles[ty][tx]


# Same pair-collision rules as scripts/path_from_tiles.py:
# CAVERN(17): $20<->$05, $41<->$05, $2A<->$05, $05<->$21
_CAVERN_PAIRS: set[frozenset[int]] = {
    frozenset({0x20, 0x05}),
    frozenset({0x41, 0x05}),
    frozenset({0x2A, 0x05}),
    frozenset({0x05, 0x21}),
}


def bfs_flood(passable: list[list[bool]],
              start: tuple[int, int],
              tiles: list[list[int]] | None = None,
              pair_collisions: set[frozenset[int]] | None = None
              ) -> set[tuple[int, int]]:
    h = len(passable)
    w = len(passable[0]) if h else 0
    sx, sy = start
    if not (0 <= sx < w and 0 <= sy < h):
        return set()
    if not passable[sy][sx]:
        return set()
    seen = {start}
    stack = [start]
    while stack:
        x, y = stack.pop()
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and passable[ny][nx]:
                # Apply pair-collision: reject if crossing a
                # blocked feet-tile pair.
                if pair_collisions and tiles is not None:
                    cur_feet = feet_tile(tiles, x, y)
                    nb_feet = feet_tile(tiles, nx, ny)
                    if (cur_feet is not None and nb_feet is not None
                            and frozenset({cur_feet, nb_feet})
                                in pair_collisions):
                        continue
                if (nx, ny) not in seen:
                    seen.add((nx, ny))
                    stack.append((nx, ny))
    return seen


def render(passable: list[list[bool]],
           labels: dict[tuple[int, int], str],
           reachable: set[tuple[int, int]] | None,
           show_x_ruler: bool = True) -> str:
    h = len(passable)
    w = len(passable[0]) if h else 0
    lines: list[str] = []
    if show_x_ruler:
        # Two-row ruler for up to 2-digit x indices.
        tens = "    " + "".join(f"{x // 10 if x >= 10 else ' '}" for x in range(w))
        ones = "    " + "".join(f"{x % 10}" for x in range(w))
        lines.append(tens)
        lines.append(ones)
    for y in range(h):
        row = f"{y:3d} "
        for x in range(w):
            if (x, y) in labels:
                row += labels[(x, y)]
            elif not passable[y][x]:
                row += "#"
            elif reachable is not None and (x, y) not in reachable:
                row += "."
            elif reachable is not None:
                row += "+"
            else:
                row += "."
        lines.append(row)
    return "\n".join(lines)


def parse_warps(entries: list[str]) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    for e in entries:
        # "x,y=L" where L is a single char
        if "=" not in e:
            raise ValueError(f"bad --warps entry {e!r}; expect x,y=L")
        xy, label = e.split("=", 1)
        x, y = [int(v) for v in xy.split(",")]
        out[(x, y)] = label[:1]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True,
                    help="Map name (CamelCase, matches .blk filename), "
                         "e.g. MtMoonB2F")
    ap.add_argument("--tileset", default=None,
                    help="Tileset name (from map_headers.asm). "
                         "Autodetected from data/maps/headers/<map>.asm "
                         "if omitted.")
    ap.add_argument("--warps", nargs="*", default=[],
                    help='"x,y=L" entries to label on the grid')
    ap.add_argument("--flood", default=None,
                    help='"x,y" start cell; flood-fills reachable cells')
    ap.add_argument("--pret", default=None,
                    help="Path to pret/pokeyellow clone")
    ap.add_argument("--dump-tiles", default=None,
                    help='"x1,y1-x2,y2" step-cell rect; prints feet-tile '
                         "IDs at each step cell. Useful for debugging "
                         "collision-model gaps.")
    ap.add_argument("--blockers", default="",
                    help='"x,y;x,y;..." step cells treated as blocked '
                         "(e.g. static NPC sprites).")
    ap.add_argument("--auto-sprite-blockers", action="store_true",
                    help="Parse data/maps/objects/<map>.asm and auto-add "
                         "all object_event step cells as blockers. Useful "
                         "for modelling NPC/item/fossil obstacles.")
    ap.add_argument("--pair-collisions", action="store_true",
                    help="Apply CAVERN pair-collision rules during flood "
                         "($20<->$05, $41<->$05, $2A<->$05, $05<->$21). "
                         "Only affects CAVERN tileset maps.")
    args = ap.parse_args()

    pret_candidates = [
        args.pret,
        "G:/project/pokemon/_vendor/pokeyellow",
        "G:/project/pokemon/_vendor/pokered",
    ]
    pret = None
    for c in pret_candidates:
        if c and Path(c).is_dir():
            pret = Path(c)
            break
    if pret is None:
        print("error: pret clone not found; pass --pret", file=sys.stderr)
        return 2
    print(f"pret: {pret}", flush=True)

    tileset = args.tileset
    if tileset is None:
        hdr = (pret / "data" / "maps" / "headers" / f"{args.map}.asm"
               ).read_text()
        m = re.search(rf"map_header\s+{args.map}\s*,\s*\w+\s*,\s*(\w+)\s*,",
                       hdr)
        if not m:
            raise RuntimeError(f"can't parse tileset from {args.map}.asm")
        tileset = m.group(1).upper()
    print(f"tileset: {tileset}", flush=True)

    bst_fname, coll_label = TILESETS[tileset]
    blockset = load_blockset(pret, bst_fname)
    passable_ids = load_collision_list(pret, coll_label)
    print(f"passable tile ids ({coll_label}): "
          + " ".join(f"0x{v:02x}" for v in sorted(passable_ids)), flush=True)

    blocks, w_blk, h_blk = load_map(pret, args.map)
    print(f"map: {w_blk}x{h_blk} blocks "
          f"-> {w_blk*4}x{h_blk*4} tiles "
          f"-> {w_blk*2}x{h_blk*2} step cells", flush=True)
    tiles = expand_tiles(blocks, blockset)

    w_step = w_blk * 2
    h_step = h_blk * 2

    blockers: set[tuple[int, int]] = set()
    if args.blockers:
        for tok in args.blockers.split(";"):
            tok = tok.strip()
            if not tok:
                continue
            bx, by = [int(v) for v in tok.split(",")]
            blockers.add((bx, by))
    if args.auto_sprite_blockers:
        obj_path = pret / "data" / "maps" / "objects" / f"{args.map}.asm"
        if obj_path.exists():
            for line in obj_path.read_text().splitlines():
                m = re.match(r"\s*object_event\s+(\d+)\s*,\s*(\d+)", line)
                if m:
                    blockers.add((int(m.group(1)), int(m.group(2))))
        print(f"auto-sprite blockers: {sorted(blockers)}", flush=True)

    passable = [
        [step_passable(tiles, x, y, passable_ids)
         and (x, y) not in blockers
         for x in range(w_step)]
        for y in range(h_step)
    ]

    labels = parse_warps(args.warps)
    reachable: set[tuple[int, int]] | None = None
    pair = None
    if args.pair_collisions and tileset == "CAVERN":
        pair = _CAVERN_PAIRS
        print(f"pair-collisions active: {len(pair)} rules", flush=True)
    if args.flood:
        fx, fy = [int(v) for v in args.flood.split(",")]
        reachable = bfs_flood(passable, (fx, fy),
                               tiles=tiles, pair_collisions=pair)
        print(f"flood-fill from ({fx},{fy}): {len(reachable)} reachable "
              f"step cells", flush=True)

    print()
    print(render(passable, labels, reachable))
    print()

    # If warps have labels and reachable is computed, report which are
    # in the flood.
    if reachable is not None and labels:
        for (x, y), lbl in labels.items():
            status = "IN-FLOOD" if (x, y) in reachable else "OUT"
            print(f"  {lbl} at ({x},{y}): {status}", flush=True)

    if args.dump_tiles:
        x1y1, x2y2 = args.dump_tiles.split("-")
        x1, y1 = [int(v) for v in x1y1.split(",")]
        x2, y2 = [int(v) for v in x2y2.split(",")]
        print(f"\nfeet-tile IDs for step cells "
              f"({x1},{y1})..({x2},{y2}) (4 tiles per cell: "
              f"TL TR / BL BR):", flush=True)
        header = "     " + " ".join(f"x={x:2d}  " for x in range(x1, x2 + 1))
        print(header, flush=True)
        for y in range(y1, y2 + 1):
            row1 = f"y={y:2d} "
            row2 = "     "
            for x in range(x1, x2 + 1):
                tl = tiles[y * 2][x * 2]
                tr = tiles[y * 2][x * 2 + 1]
                bl = tiles[y * 2 + 1][x * 2]
                br = tiles[y * 2 + 1][x * 2 + 1]
                bl_mark = "*" if bl in passable_ids else " "
                row1 += f"{tl:02x} {tr:02x}"
                row2 += f"{bl:02x}{bl_mark}{br:02x} "
                row1 += " "
            print(row1, flush=True)
            print(row2, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
