"""BFS pathfinder over the overworld.

Loads a start-state, then explores reachable (map_id, x, y) nodes via
save/restore. Early-exits when it either reaches a target coord or
triggers a map transition. Prints the shortest path as a direction
string (u/d/l/r).

Usage:
  PYTHONPATH=src POKERED_ROM_PATH=... POKERED_SYM_PATH=... \
    POKERED_ROM_SHA1=... python scripts/bfs_route.py \
    --state path/to/start.state \
    --goal-map 0x32           # stop when we hit Forest South Gate
    # or
    --goal-xy 3,43            # stop when we reach this coord

Battle interruptions auto-resolve by mashing A.
"""

from __future__ import annotations

import argparse
import os
from collections import deque
from pathlib import Path

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session

DIRS = [("u", "up"), ("d", "down"), ("l", "left"), ("r", "right")]


def key_for(state: Session) -> tuple[int, int, int]:
    gs = state.read_game_state()
    return (gs.overworld.map_id, gs.overworld.x, gs.overworld.y)


def clear_battle(s: Session, max_a: int = 150) -> None:
    for _ in range(max_a):
        if not s.read_game_state().battle.active:
            return
        s.press("a", duration=6)
        s.step(24, render=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", required=True)
    p.add_argument("--goal-map", type=lambda x: int(x, 0), default=None,
                   help="stop if map_id changes to this value (e.g. 0x32)")
    p.add_argument("--goal-xy", type=str, default=None,
                   help='stop at x,y within the starting map (e.g. "3,43")')
    p.add_argument("--max-nodes", type=int, default=5000)
    p.add_argument("--save-path-to", type=str, default=None,
                   help="if reached, save the path directions to this file")
    args = p.parse_args()

    goal_xy = None
    if args.goal_xy:
        gx, gy = [int(v) for v in args.goal_xy.split(",")]
        goal_xy = (gx, gy)

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    s = Session.from_files(
        rom, sym,
        expected_rom_sha1=os.environ.get(
            "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
        ),
    )
    register_default_hooks(s)
    start_bytes = Path(args.state).read_bytes()
    s.load_state(start_bytes); s.step(60, render=True)
    clear_battle(s)
    start_key = key_for(s)
    start_state = s.save_state()
    start_map = start_key[0]
    print(f"start: map=0x{start_key[0]:02x} xy=({start_key[1]},{start_key[2]})", flush=True)

    # BFS frontier: list of (state_bytes, key, path_str)
    visited: dict[tuple[int, int, int], str] = {start_key: ""}
    q: deque = deque([(start_state, start_key, "")])
    nodes = 0
    found_path: str | None = None

    while q:
        state_bytes, key, path = q.popleft()
        nodes += 1
        if nodes % 50 == 0:
            print(f"  explored {nodes} nodes, frontier={len(q)}, last={key}", flush=True)
        for char, button in DIRS:
            s.load_state(state_bytes); s.step(24, render=True)
            s.press(button, duration=6); s.step(24, render=True)
            clear_battle(s)
            # settle
            s.step(16, render=True)
            new_key = key_for(s)
            if new_key == key:
                continue  # no movement
            # Map changed — potentially a goal.
            if new_key[0] != start_map:
                new_path = path + char
                print(f"  MAP CHANGE via {button}: now map=0x{new_key[0]:02x} path={new_path!r}", flush=True)
                if args.goal_map is not None and new_key[0] == args.goal_map:
                    found_path = new_path
                    break
                # Not our goal — skip this node (don't descend into the
                # new map).
                continue
            if new_key in visited:
                continue
            new_path = path + char
            visited[new_key] = new_path
            if goal_xy is not None and (new_key[1], new_key[2]) == goal_xy:
                found_path = new_path
                print(f"  REACHED goal coord! path={new_path!r}", flush=True)
                break
            q.append((s.save_state(), new_key, new_path))
        if found_path or nodes >= args.max_nodes:
            break

    if found_path:
        print(f"\nPATH FOUND ({len(found_path)} steps): {found_path}", flush=True)
        if args.save_path_to:
            Path(args.save_path_to).write_text(found_path)
            print(f"saved to {args.save_path_to}", flush=True)
        return 0
    print(f"\nno path found after {nodes} nodes", flush=True)
    # Print farthest tile (by path length) as a hint
    longest = max(visited.items(), key=lambda kv: len(kv[1]))
    print(f"farthest reached: {longest[0]} via {longest[1]!r} ({len(longest[1])} steps)", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
