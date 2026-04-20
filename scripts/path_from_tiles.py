"""Tile-data-aware A* pathfinder for the Pokemon Red overworld.

Reads the current map's block layout from ``wOverworldMap``, expands it
into a per-tile walkability grid using the tileset blockset (read from the
pret/pokered ``.bst`` file on disk), and runs A* from the player's
position to the requested goal. Produces a direction string (u/d/l/r).

This avoids the save/restore BFS in ``bfs_route.py`` (which pays the full
emulator cost per edge). After the one-time state load, the search is
pure Python over ~a few thousand tiles.

Usage:
  PYTHONPATH=src POKERED_ROM_PATH=... POKERED_SYM_PATH=... \
    POKERED_ROM_SHA1=... python -u scripts/path_from_tiles.py \
    --state walkthrough_brock_color/milestones/viridian_to_route2.state \
    --goal-xy 3,43

  # or save to a file
  python -u scripts/path_from_tiles.py \
    --state .../forest_entry.state --goal-xy 1,0 \
    --save-path-to walkthrough_brock_color/forest_north_path.txt
"""

from __future__ import annotations

import argparse
import heapq
import os
import sys
from pathlib import Path

from pokered_harness.session import Session


# wOverworldMap holds BLOCK ids with a 3-block border of the map's
# background tile. Each block expands to a 4x4 block of tile ids via the
# tileset's blockset (16 bytes per block).
MAP_BORDER = 3
BLOCK_WIDTH = 4
BLOCK_HEIGHT = 4

# Tileset id (wCurMapTileset) -> blockset filename inside
# pret/pokered/gfx/blocksets/. Order and IDs come from
# constants/tileset_constants.asm.
TILESET_BLOCKSET = {
    0: "overworld.bst",
    1: "reds_house.bst",   # RedsHouse1
    2: "pokecenter.bst",   # Mart shares pokecenter.bst (same block data)
    3: "forest.bst",
    4: "reds_house.bst",   # RedsHouse2
    5: "gym.bst",          # Dojo shares gym.bst
    6: "pokecenter.bst",
    7: "gym.bst",
    8: "house.bst",
    9: "gate.bst",         # ForestGate shares gate.bst
    10: "gate.bst",        # Museum shares gate.bst
    11: "underground.bst",
    12: "gate.bst",
    13: "ship.bst",
    14: "ship_port.bst",
    15: "cemetery.bst",
    16: "interior.bst",
    17: "cavern.bst",
    18: "lobby.bst",
    19: "mansion.bst",
    20: "lab.bst",
    21: "club.bst",
    22: "facility.bst",
    23: "plateau.bst",
}


DIRS = [
    ("u", 0, -1),
    ("d", 0, 1),
    ("l", -1, 0),
    ("r", 1, 0),
]


def find_pret_root() -> Path:
    """Locate the pret/pokered disassembly to read .bst files from."""
    candidates = [
        os.environ.get("POKERED_PRET_ROOT"),
        "G:/project/pokemon/_vendor/pokered",
        "G:\\project\\pokemon\\_vendor\\pokered",
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    raise FileNotFoundError(
        "can't find pret/pokered; set POKERED_PRET_ROOT env var"
    )


def load_blockset(tileset_id: int, pret_root: Path) -> list[bytes]:
    """Return a list of 16-byte block tile-id patterns, indexed by block id."""
    fname = TILESET_BLOCKSET.get(tileset_id)
    if fname is None:
        raise ValueError(f"unknown tileset id 0x{tileset_id:02x}")
    data = (pret_root / "gfx" / "blocksets" / fname).read_bytes()
    if len(data) % 16 != 0:
        raise ValueError(f"blockset {fname} size {len(data)} is not a multiple of 16")
    return [data[i : i + 16] for i in range(0, len(data), 16)]


def read_sprite_blockers(session: Session,
                          expand_npc_neighbors: bool = False) -> set[tuple[int, int]]:
    """Return a set of (x, y) step cells occupied by static sprites.

    Map-object sprites (NPCs, item balls, etc.) are stored in
    ``wSpriteStateData2`` slots 1..15 at +4 (map Y) / +5 (map X). The
    ``object_event`` macro stores raw coords with **+4 added** to both
    axes (see ``macros/scripts/maps.asm``), but the player's
    ``wXCoord`` / ``wYCoord`` have no such offset. To compare against
    the player grid, subtract 4 from the stored RAM value.

    Slots with ``(MAPX,MAPY) == (0,0)`` are unused; they're either
    literal (0,0) objects (extremely rare and wouldn't be in the step
    range anyway) or unallocated slots. Slot 0 is the player.

    When ``expand_npc_neighbors`` is True, wandering NPCs'
    4-directional neighbors are ALSO marked as blockers. NPCs with
    movement-status ``WALK`` (wSpriteStateData1[i][1] bit 0 set, or
    by checking movement bytes) can step ±1 into any cardinal
    neighbor on the next game tick — if we plan a path that merely
    avoids their CURRENT tile, they may walk into the player's
    next-tile between frames and block the press. Over-approximating
    to "any tile adjacent to an NPC is blocked" guarantees no
    collision but loses some walkable space.
    """
    mem = session._pyboy.memory
    data2 = session.symbols.addr_of("wSpriteStateData2")
    data1 = session.symbols.addr_of("wSpriteStateData1")
    blockers: set[tuple[int, int]] = set()
    for i in range(1, 16):
        base2 = data2 + i * 0x10
        base1 = data1 + i * 0x10
        my_raw = int(mem[base2 + 4]) & 0xFF
        mx_raw = int(mem[base2 + 5]) & 0xFF
        if (mx_raw, my_raw) == (0, 0):
            continue
        sx, sy = mx_raw - 4, my_raw - 4
        blockers.add((sx, sy))
        if expand_npc_neighbors:
            # Movement status byte at wSpriteStateData1+1. Non-zero
            # picture_id sprites (real NPCs) may wander; we don't
            # distinguish STAY vs WALK here (would require reading
            # the object_event .movement field from ROM), so be
            # conservative: expand neighbor tiles for all NPCs.
            picture_id = int(mem[base1 + 0]) & 0xFF
            if picture_id != 0:
                for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    blockers.add((sx + dx, sy + dy))
    return blockers


def read_passable_tiles(session: Session) -> set[int]:
    """Follow wTilesetCollisionPtr and gather passable tile ids until 0xff."""
    mem = session._pyboy.memory
    symbols = session.symbols
    ptr_addr = symbols.addr_of("wTilesetCollisionPtr")
    lo = int(mem[ptr_addr]) & 0xFF
    hi = int(mem[ptr_addr + 1]) & 0xFF
    list_addr = lo | (hi << 8)
    passable: set[int] = set()
    # Guard against runaway reads with a generous cap (the tables in
    # data/tilesets/collision_tile_ids.asm are all well under 32 entries).
    for i in range(128):
        b = int(mem[list_addr + i]) & 0xFF
        if b == 0xFF:
            break
        passable.add(b)
    else:
        raise RuntimeError("collision list terminator not found in 128 bytes")
    return passable


def read_overworld_map(
    session: Session, width_blocks: int, height_blocks: int
) -> list[list[int]]:
    """Return the block grid for the current map (no border), indexed [y][x]."""
    mem = session._pyboy.memory
    base = session.symbols.addr_of("wOverworldMap")
    stride = width_blocks + MAP_BORDER * 2
    # First MAP_BORDER rows are the north border, then per row: MAP_BORDER
    # border columns, then width_blocks map columns, then MAP_BORDER border.
    blocks: list[list[int]] = []
    for by in range(height_blocks):
        row_start = base + (MAP_BORDER + by) * stride + MAP_BORDER
        row = [int(mem[row_start + bx]) & 0xFF for bx in range(width_blocks)]
        blocks.append(row)
    return blocks


def expand_to_tile_grid(
    blocks: list[list[int]], blockset: list[bytes]
) -> list[list[int]]:
    """Return a (height_blocks*4) x (width_blocks*4) grid of tile ids."""
    h_blocks = len(blocks)
    w_blocks = len(blocks[0]) if h_blocks else 0
    h_tiles = h_blocks * BLOCK_HEIGHT
    w_tiles = w_blocks * BLOCK_WIDTH
    tiles = [[0] * w_tiles for _ in range(h_tiles)]
    for by in range(h_blocks):
        for bx in range(w_blocks):
            block_id = blocks[by][bx]
            if block_id >= len(blockset):
                # Out-of-range block id: fill with an obviously-impassable
                # marker (0xFF is never in any collision list).
                pattern = b"\xff" * 16
            else:
                pattern = blockset[block_id]
            # Block layout inside a .bst entry is row-major 4x4.
            for ty in range(BLOCK_HEIGHT):
                for tx in range(BLOCK_WIDTH):
                    tiles[by * BLOCK_HEIGHT + ty][bx * BLOCK_WIDTH + tx] = (
                        pattern[ty * BLOCK_WIDTH + tx]
                    )
    return tiles


# Gen 1 ledge tile IDs in the overworld tileset (pokered/pokeyellow
# data/tilesets/ledge_tiles.asm):
#   tile 0x27 — west-facing ledge (jump LEFT)
#   tile 0x36, 0x37 — south-facing ledge (jump DOWN)
#   tile 0x0D, 0x1D — east-facing ledge (jump RIGHT)
# When the player presses a direction matching the ledge type while on
# a walkable tile (e.g. grass 0x2C), they hop OVER the ledge tile,
# landing two tiles away in that direction. A* models this as a
# special ledge-hop neighbor: from (x, y) in direction d, if the
# immediate neighbor is a matching ledge tile, we can reach the tile
# TWO steps away in direction d with cost 1 (single press).
_LEDGE_TILES_BY_DIR: dict[str, set[int]] = {
    "d": {0x36, 0x37},
    "l": {0x27},
    "r": {0x0D, 0x1D},
}


def astar(
    passable: list[list[bool]],
    start: tuple[int, int],
    goal: tuple[int, int],
    tile_grid: list[list[int]] | None = None,
) -> str | None:
    """Standard 4-connected A* with Manhattan heuristic. Returns direction
    string or None.

    When ``tile_grid`` is provided, A* also considers Gen 1 ledge hops:
    from cell (x, y) pressing direction d, if cell (x+dx, y+dy) sits on
    a ledge tile matching direction d, the player jumps to (x+2dx, y+2dy)
    in a single press. This lets the pathfinder use south-ledge drops
    (Route 3, Route 4, Route 24 etc.) that otherwise read as walls.
    """
    h = len(passable)
    w = len(passable[0]) if h else 0
    sx, sy = start
    gx, gy = goal
    if not (0 <= sx < w and 0 <= sy < h and 0 <= gx < w and 0 <= gy < h):
        return None
    if not passable[gy][gx]:
        return None

    def heur(x: int, y: int) -> int:
        return abs(x - gx) + abs(y - gy)

    # Step-cell → tile (feet) mapping: cell (sx, sy) has feet tile at
    # (sx*2, sy*2 + 1) in the tile_grid. Used to check ledge tiles.
    def cell_feet_tile(sx: int, sy: int) -> int | None:
        if tile_grid is None:
            return None
        tx, ty = sx * 2, sy * 2 + 1
        if not (0 <= ty < len(tile_grid) and 0 <= tx < len(tile_grid[0])):
            return None
        return tile_grid[ty][tx]

    # Priority queue entries: (f, g, x, y). Parent tracked in came_from.
    pq: list[tuple[int, int, int, int]] = [(heur(sx, sy), 0, sx, sy)]
    came_from: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    best_g: dict[tuple[int, int], int] = {(sx, sy): 0}

    while pq:
        f, g, x, y = heapq.heappop(pq)
        if (x, y) == (gx, gy):
            # Reconstruct direction string.
            path_chars: list[str] = []
            cur = (x, y)
            while cur != (sx, sy):
                parent, ch = came_from[cur]
                path_chars.append(ch)
                cur = parent
            path_chars.reverse()
            return "".join(path_chars)
        if g > best_g.get((x, y), g):
            continue
        for ch, dx, dy in DIRS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            ng = g + 1
            if passable[ny][nx]:
                key = (nx, ny)
                if ng < best_g.get(key, 1 << 30):
                    best_g[key] = ng
                    came_from[key] = ((x, y), ch)
                    heapq.heappush(pq, (ng + heur(nx, ny), ng, nx, ny))
                continue
            # Ledge-hop: neighbor is impassable — but if its feet tile is
            # a ledge of the right direction, we can jump over it to the
            # cell beyond.
            if tile_grid is None:
                continue
            ledge_tiles = _LEDGE_TILES_BY_DIR.get(ch, set())
            if not ledge_tiles:
                continue
            feet = cell_feet_tile(nx, ny)
            if feet is None or feet not in ledge_tiles:
                continue
            jx, jy = nx + dx, ny + dy
            if not (0 <= jx < w and 0 <= jy < h):
                continue
            if not passable[jy][jx]:
                continue
            key = (jx, jy)
            if ng < best_g.get(key, 1 << 30):
                best_g[key] = ng
                came_from[key] = ((x, y), ch)
                heapq.heappush(pq, (ng + heur(jx, jy), ng, jx, jy))
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", required=True)
    p.add_argument("--goal-xy", type=str, default=None,
                   help='target tile coord within current map (e.g. "3,43")')
    p.add_argument("--save-path-to", type=str, default=None)
    p.add_argument("--dump-grid", action="store_true",
                   help="also print the walkability grid for debugging")
    p.add_argument("--expand-npc-neighbors", action="store_true",
                   help="Conservatively mark tiles adjacent to every "
                        "NPC as impassable. Avoids plans that pass "
                        "next to a wandering NPC who could walk into "
                        "the player's next tile. Over-approximates — "
                        "loses walkable space in NPC-dense areas.")
    p.add_argument("--extra-blockers", type=str, default=None,
                   help='semicolon-separated list of x,y tiles to treat '
                        'as impassable in addition to the game\'s sprite '
                        'blockers (e.g. "15,8;16,9;17,8"). Useful for '
                        'known trainer-pen tiles the pathfinder would '
                        'otherwise route us into.')
    args = p.parse_args()

    if not args.goal_xy:
        print("--goal-xy X,Y is required", file=sys.stderr)
        return 2
    gx_s, gy_s = args.goal_xy.split(",")
    goal = (int(gx_s), int(gy_s))

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    print(f"loading rom {rom}", flush=True)
    s = Session.from_files(
        rom, sym,
        expected_rom_sha1=os.environ.get("POKERED_ROM_SHA1"),
    )
    print(f"loading state {args.state}", flush=True)
    s.load_state(Path(args.state).read_bytes())
    # Step enough that any pending map-transition scripts finish loading
    # wOverworldMap and the map header. 60 ticks is comfortably over the
    # "fade in" timing. Render is on so the transition actually runs.
    s.step(60, render=True)

    gs = s.read_game_state()
    mem = s._pyboy.memory
    symbols = s.symbols
    width_blocks = int(mem[symbols.addr_of("wCurMapWidth")]) & 0xFF
    height_blocks = int(mem[symbols.addr_of("wCurMapHeight")]) & 0xFF
    tileset_id = int(mem[symbols.addr_of("wCurMapTileset")]) & 0xFF
    px, py = gs.overworld.x, gs.overworld.y
    print(
        f"map=0x{gs.overworld.map_id:02x} tileset={tileset_id} "
        f"dims(blocks)={width_blocks}x{height_blocks} "
        f"dims(tiles)={width_blocks * 4}x{height_blocks * 4} "
        f"player=({px},{py})",
        flush=True,
    )

    # Collision list.
    passable_tiles = read_passable_tiles(s)
    print(
        "passable tile ids: "
        + ", ".join(f"0x{t:02x}" for t in sorted(passable_tiles)),
        flush=True,
    )

    # NPC / object-event sprite positions in step-grid coords. These
    # block movement into their cell regardless of tile walkability
    # (trainer NPCs, trainers with sight lines, Poke Ball pickups, etc).
    sprite_blockers = read_sprite_blockers(
        s, expand_npc_neighbors=args.expand_npc_neighbors,
    )
    # Caller-injected blockers (route-specific trainer-pen avoidance).
    if args.extra_blockers:
        for part in args.extra_blockers.split(";"):
            part = part.strip()
            if not part:
                continue
            try:
                ex, ey = part.split(",")
                sprite_blockers.add((int(ex), int(ey)))
            except ValueError:
                print(f"  WARN bad --extra-blockers entry: {part!r}",
                      flush=True)
    if sprite_blockers:
        print(
            "sprite blockers: "
            + ", ".join(f"({x},{y})" for x, y in sorted(sprite_blockers)),
            flush=True,
        )

    # Blockset and expansion.
    pret_root = find_pret_root()
    blockset = load_blockset(tileset_id, pret_root)
    print(f"blockset: {TILESET_BLOCKSET[tileset_id]} ({len(blockset)} blocks)", flush=True)

    blocks = read_overworld_map(s, width_blocks, height_blocks)
    tile_grid = expand_to_tile_grid(blocks, blockset)

    h_tiles = len(tile_grid)
    w_tiles = len(tile_grid[0])

    # Player coords (wXCoord/wYCoord) step in half-blocks: each +1 moves
    # the sprite by 2 visual tiles. So the pathfinding grid is at
    # *step* granularity, with dims (width_blocks*2, height_blocks*2).
    # A step cell (sx, sy) corresponds to the visual-tile region
    # (sx*2, sy*2) .. (sx*2+1, sy*2+1). The game's collision check for
    # moving into that cell looks at the tile at its top-left, which is
    # what lda_coord(8, 9/7/11/...) resolves to after the step.
    w_steps = width_blocks * 2
    h_steps = height_blocks * 2

    # Tileset-specific feet-tile collision model:
    # - Overworld (0): BOTTOM-LEFT (sx*2, sy*2+1). Calibrated for
    #   Route 2/3/4 walkable paths and verified against real game.
    # - Cave (CAVERN=17, etc.): AND-BOTH bottom tiles. Mt. Moon has
    #   asymmetric cells (one feet tile walkable, other wall) that
    #   require both to pass. Empirically: cell (8, 20) has
    #   bottom-left 0x31 BLOCKED, bottom-right 0x05 walkable —
    #   game blocks RIGHT into it.
    _CAVE_TILESETS = {3, 14, 16, 17, 21, 22}
    use_and_both = tileset_id in _CAVE_TILESETS
    if os.environ.get("PATH_CAVE_MODEL") == "single":
        use_and_both = False  # debug: use overworld single-offset for caves

    def step_passable(sx: int, sy: int) -> bool:
        if (sx, sy) in sprite_blockers:
            return False
        ty = sy * 2 + 1
        if not (0 <= ty < h_tiles):
            return False
        if use_and_both:
            for dx in (0, 1):
                tx = sx * 2 + dx
                if not (0 <= tx < w_tiles):
                    return False
                if tile_grid[ty][tx] not in passable_tiles:
                    return False
            return True
        else:
            tx = sx * 2
            if not (0 <= tx < w_tiles):
                return False
            return tile_grid[ty][tx] in passable_tiles

    passable_grid = [
        [step_passable(x, y) for x in range(w_steps)]
        for y in range(h_steps)
    ]
    walkable = sum(1 for row in passable_grid for v in row if v)
    total = w_steps * h_steps
    print(f"walkable step cells: {walkable}/{total}", flush=True)

    # Sanity: the player's own step cell should be walkable.
    if 0 <= px < w_steps and 0 <= py < h_steps:
        if not passable_grid[py][px]:
            tile_at_player = tile_grid[py * 2 + 1][px * 2]
            print(
                f"WARNING: player step cell ({px},{py}) feet tile "
                f"0x{tile_at_player:02x} not in passable list; forcing walkable",
                flush=True,
            )
            passable_grid[py][px] = True

    if args.dump_grid:
        for y in range(h_steps):
            row = ""
            for x in range(w_steps):
                if (x, y) == (px, py):
                    row += "P"
                elif (x, y) == goal:
                    row += "G"
                elif passable_grid[y][x]:
                    row += "."
                else:
                    row += "#"
            print(row, flush=True)

    # Warp tiles (doors/stairs) often fail the collision check but must be
    # reachable. Force the goal cell walkable so A* will route onto it;
    # the game scripts handle the warp trigger.
    gx, gy = goal
    if 0 <= gx < w_steps and 0 <= gy < h_steps:
        if not passable_grid[gy][gx]:
            t_id = tile_grid[gy * 2 + 1][gx * 2]
            print(
                f"note: goal step cell ({gx},{gy}) feet tile 0x{t_id:02x} "
                f"not in passable list; forcing walkable (warps are common)",
                flush=True,
            )
            passable_grid[gy][gx] = True

    print(f"running A* from ({px},{py}) to {goal}", flush=True)
    path = astar(passable_grid, (px, py), goal, tile_grid=tile_grid)
    if path is None:
        print("NO PATH FOUND", flush=True)
        return 1
    print(f"PATH FOUND ({len(path)} steps): {path}", flush=True)
    if args.save_path_to:
        Path(args.save_path_to).write_text(path)
        print(f"saved to {args.save_path_to}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
