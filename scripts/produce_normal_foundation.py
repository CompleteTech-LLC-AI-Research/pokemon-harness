"""Capture a bounded Red foundation from fresh ROM boot and ordinary input.

This early foundation stops at a declared phase; it is not a full gameplay gate.
No inherited state or battery RAM is loaded, and no emulated memory is written
by this controller. All checkpoints and the full input journal stay external.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

from pokered_harness.config import load_versions
from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import Session
from scripts import walkthrough

ROOT = Path(__file__).resolve().parents[1]
ROM_LABEL = "rom/red/pokemon-red-color.gb"
SYM_LABEL = "rom/red/pokemon-red.sym"


class CaptureRefused(RuntimeError):
    """The attempted capture lacks its required input or observed boundary."""


def require_fresh_inputs(rom: Path, symbols: Path, output: Path) -> None:
    """Reject inherited battery/state inputs and output reuse before launch."""
    if output.exists():
        raise CaptureRefused("output already exists; preserve the earlier attempt")
    if output.resolve().is_relative_to(ROOT):
        raise CaptureRefused("capture output must be outside the source checkout")
    if not rom.is_file() or not symbols.is_file():
        raise CaptureRefused("matching ROM and symbol files are required")
    inherited = [
        path.name
        for path in rom.parent.iterdir()
        if path.is_file() and path.suffix.lower() in {".sav", ".ram", ".state"}
    ]
    if inherited:
        raise CaptureRefused("fresh input directory contains inherited save/RAM/state files")


def validate_phase(name: str, state) -> None:
    """Require the observed phase, rather than treating driver exit as success."""
    expected_map = {
        "intro": walkthrough.MAP_REDS_HOUSE_2F,
        "exit_house": walkthrough.MAP_PALLET_TOWN,
        "oak_intercept": walkthrough.MAP_OAKS_LAB,
        "pick_starter": walkthrough.MAP_OAKS_LAB,
        "rival_battle": walkthrough.MAP_PALLET_TOWN,
        "pallet_to_route1": walkthrough.MAP_ROUTE_1,
        "route1_to_viridian": walkthrough.MAP_VIRIDIAN_CITY,
    }[name]
    if state.overworld.map_id != expected_map:
        raise CaptureRefused(f"{name}: observed map does not match the required boundary")
    if name in {"pick_starter", "rival_battle", "pallet_to_route1", "route1_to_viridian"} and (
        state.party.count != 1 or state.party.mons[0].species != 0x99
    ):
        raise CaptureRefused(f"{name}: the ordinary Bulbasaur starter was not observed")
    if name in {"rival_battle", "pallet_to_route1", "route1_to_viridian"} and state.battle.active:
        raise CaptureRefused(f"{name}: the battle has not returned to overworld control")


class JournalDriver(walkthrough.WalkthroughDriver):
    """Use the existing input phases, with a durable complete action receipt."""

    def __init__(self, session: Session, outdir: Path, *, deadline: float):
        super().__init__(session=session, outdir=outdir)
        self.deadline = deadline
        self.journal = outdir / "inputs.jsonl"
        self.phase = "boot"
        self.actions = 0

    def _check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise CaptureRefused("capture wall deadline exceeded")

    def _append(self, row: dict) -> None:
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _action(self, button: str | None, *, duration: int, settle: int, render: bool, note: str):
        self._check_deadline()
        self.actions += 1
        began = self.session.current_tick()
        row = {
            "action": self.actions,
            "phase": self.phase,
            "button": button,
            "duration_frames": duration,
            "settle_frames": settle,
            "render": render,
            "tick_before": began,
            "note": note,
        }
        # Intent precedes input. A stopped process never loses an input's
        # attribution, and an incomplete intent is not reported as completed.
        self._append({**row, "status": "intent"})
        if button is not None:
            self.session.press(button, duration=duration)
        self.session.step(settle, render=render)
        state = self.session.read_game_state()
        completed = {
            **row,
            "status": "completed",
            "tick_after": self.session.current_tick(),
            "map_id": state.overworld.map_id,
            "xy": [state.overworld.x, state.overworld.y],
            "party_count": state.party.count,
            "in_battle": state.battle.active,
        }
        self._append(completed)
        return completed

    def press(self, button: str, *, note: str = "", step_ticks: int = 24, duration: int = 6):
        self.press_index += 1
        row = self._action(button, duration=duration, settle=step_ticks, render=True, note=note)
        print(
            f"action={self.actions} phase={self.phase} map={row['map_id']} xy={row['xy']}",
            flush=True,
        )
        return row

    def idle(self, ticks: int, *, render: bool = False) -> None:
        self._action(None, duration=0, settle=ticks, render=render, note="ordinary idle frames")


def run_route1_to_viridian(driver: JournalDriver) -> None:
    """Retain each path step across dialog/encounters and leave wild battles."""
    path = (
        ["up"] * 7
        + ["left"] * 3
        + ["up"] * 4
        + ["right"] * 5
        + ["up"] * 4
        + ["left"] * 3
        + ["up"] * 6
        + ["right"] * 5
        + ["up"] * 11
        + ["left"] * 3
        + ["up"] * 5
    )

    def restore_overworld() -> None:
        for _ in range(160):
            state = driver.session.read_game_state()
            if state.overworld.map_id not in {
                walkthrough.MAP_ROUTE_1,
                walkthrough.MAP_VIRIDIAN_CITY,
            }:
                raise CaptureRefused(
                    "Route1 continuation left the admitted route (possible blackout)"
                )
            if state.battle.active:
                if state.battle.kind.name != "WILD":
                    raise CaptureRefused("Route1 continuation encountered a non-wild battle")
                if state.battle.menu_open is True:
                    # Cancel a move submenu, select the command menu's RUN,
                    # then observe escape; no success is assumed from a press.
                    driver.press("b", note="wild: command menu", step_ticks=30)
                    driver.press("up", note="wild: top row", step_ticks=30)
                    driver.press("left", note="wild: left column", step_ticks=30)
                    driver.press("right", note="wild: RUN column", step_ticks=30)
                    driver.press("down", note="wild: RUN row", step_ticks=30)
                    driver.press("a", note="wild: attempt RUN", step_ticks=60)
                else:
                    driver.press("a", note="wild: advance encounter/escape text", step_ticks=30)
                continue
            if driver.input_locked():
                driver.press("a", note="route: advance locked dialog", step_ticks=30)
                continue
            return
        raise CaptureRefused("Route1 interruption did not settle within its input bound")

    for direction in path:
        restore_overworld()
        before = driver.session.read_game_state()
        if before.overworld.map_id == walkthrough.MAP_VIRIDIAN_CITY:
            return
        driver.press(direction, note=f"Route1 retained path: {direction}")
        restore_overworld()
        after = driver.session.read_game_state()
        if (after.overworld.x, after.overworld.y) == (before.overworld.x, before.overworld.y):
            # A direction change can consume the first press just turning.
            driver.press(direction, note=f"Route1 retained path after turn: {direction}")
    restore_overworld()
    if driver.session.read_game_state().overworld.map_id != walkthrough.MAP_VIRIDIAN_CITY:
        raise CaptureRefused("Route1 path exhausted before reaching Viridian")


def capture(rom: Path, symbols: Path, output: Path, *, stop_after: str, seconds: float) -> dict:
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise CaptureRefused("wall budget must be finite and positive")
    require_fresh_inputs(rom, symbols, output)
    pins = load_versions(ROOT / "VERSIONS.md")
    rom_sha1 = pins.sha1_for_path(ROM_LABEL)
    sym_sha1 = pins.symbol_sha1_for_path(SYM_LABEL)
    if not rom_sha1 or not sym_sha1:
        raise CaptureRefused("canonical Red-color input pins are unavailable")
    phases = dict(walkthrough.PHASES)
    if stop_after not in phases:
        raise CaptureRefused("unknown stop phase")
    output.mkdir(parents=True, exist_ok=False)
    began = time.monotonic()
    receipt = {
        "status": "starting",
        "scope": "Fresh Red boot through the declared early phase; not a full gameplay gate",
        "source_state": None,
        "inherited_battery_ram": False,
        "controller_ram_writes": False,
        "rom_sha1": rom_sha1,
        "symbols_sha1": sym_sha1,
        "rom_sha256": hashlib.sha256(rom.read_bytes()).hexdigest(),
        "symbols_sha256": hashlib.sha256(symbols.read_bytes()).hexdigest(),
        "pyboy_revision": pins.pyboy_revision,
        "python": sys.version,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "stop_after": stop_after,
        "wall_budget_seconds": seconds,
        "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "phase_driver_sha256": hashlib.sha256(Path(walkthrough.__file__).read_bytes()).hexdigest(),
        "checkpoints": [],
    }
    receipt_path = output / "receipt.json"
    session = None
    driver = None
    try:
        session = Session.from_files(
            rom,
            symbols,
            expected_rom_sha1=rom_sha1,
            expected_symbol_sha1=sym_sha1,
            expected_pyboy_version=pins.pyboy_version,
            expected_pyboy_revision=pins.pyboy_revision,
        )
        runtime = sys.modules.get("pyboy.pyboy")
        receipt["pyboy_runtime_origin"] = getattr(runtime, "__file__", None)
        register_default_hooks(session)
        driver = JournalDriver(session, output, deadline=began + seconds)
        receipt["status"] = "running"
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        for name, phase in walkthrough.PHASES:
            driver.phase = name
            if name == "route1_to_viridian":
                phase = run_route1_to_viridian
            phase(driver)
            state = session.read_game_state()
            validate_phase(name, state)
            raw = session.save_state()
            (output / f"{name}.state").write_bytes(raw)
            (output / f"{name}.json").write_text(
                json.dumps(to_jsonable(state), indent=2), encoding="utf-8"
            )
            receipt["checkpoints"].append(
                {"phase": name, "sha1": hashlib.sha1(raw).hexdigest(), "size_bytes": len(raw)}
            )
            receipt["actions_completed"] = driver.actions
            receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            if name == stop_after:
                break
        receipt["status"] = "captured"
        return receipt
    except BaseException as exc:
        receipt["status"] = "failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - began
        if driver is not None:
            receipt["actions_attempted"] = driver.actions
        try:
            if session is not None:
                try:
                    session.close(save=False)
                    receipt["session_closed_without_save"] = True
                except BaseException as close_error:
                    receipt["session_closed_without_save"] = False
                    receipt["teardown_error"] = f"{type(close_error).__name__}: {close_error}"
                    if receipt["status"] != "failed":
                        receipt["status"] = "failed"
                        raise
        finally:
            receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--sym", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--stop-after", choices=[name for name, _ in walkthrough.PHASES], default="pick_starter"
    )
    parser.add_argument("--wall-seconds", type=float, default=300)
    args = parser.parse_args()
    capture(args.rom, args.sym, args.out, stop_after=args.stop_after, seconds=args.wall_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
