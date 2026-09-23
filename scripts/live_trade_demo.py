"""Live end-to-end trade demo: pair Red + Blue (or any R/B/Y pair) and
drive a full Cable Club trade, saving per-phase color screenshots and
printing wall-clock timing.

Re-uses :mod:`pokered_harness.link.pyboy_link_session.PyBoyLinkSession`
plus the same trade-drive helpers exercised by
``tests/test_pyboy_link_session_roms.py``. Those helpers live inside
the test module and aren't a stable public surface, so the small
handful we need is copied inline here (copy-over-import, as called out
in the brief — the tests aren't meant to be imported as a library).

Usage
-----

::

    python -u scripts/live_trade_demo.py
    python -u scripts/live_trade_demo.py --versions red,blue
    python -u scripts/live_trade_demo.py --outdir walkthrough_link_demo
    python -u scripts/live_trade_demo.py --venv noncython

When ``--venv`` is passed and the target venv's ``python.exe`` exists,
the script re-execs itself under that interpreter; otherwise it warns
and continues under the ambient interpreter.

Notes on the TRADE_CENTER map id
--------------------------------

The brief mentions map ``0x36`` ("LINK_CLUB Trade Center"); the proven
working constant used by the passing trade-matrix test in
``tests/test_pyboy_link_session_roms.py`` is ``0xEF`` (from pokeyellow's
``constants/map_constants.asm`` — the same value applies to Red/Blue's
internal TRADE_CENTER map). We use ``0xEF`` and assert against it so
we match the test suite that already passes 9/9.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# TRADE_CENTER map id (verified via passing trade-matrix test;
# pokeyellow constants/map_constants.asm names this 0xEF).
TRADE_CENTER_MAP_ID = 0xEF

# Controlled by --cgb/--no-cgb; default True (CGB mode + Full Color
# Hack gives the color presentation we want). If Gen 1 menu/dialog
# overlays don't render in CGB + Color-Hack, use --no-cgb to force DMG
# rendering — loses color but makes menus visible.
_PYBOY_CGB_OVERRIDE: bool = True


# ---------------------------------------------------------------------------
# Sibling helper modules (split out of this entrypoint; see issue #140).
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _live_trade_demo_render import (
    Shooter,
    _find_pyboy_hwnds,
    _force_move_pyboy_windows,
    _make_side_by_side,
    _render_window_from_vram,
    _win32_grab_window,
)
from _live_trade_demo_support import (
    _ROM_PATHS,
    _assert_fixtures_available,
    _install_hook_counter,
    _install_trade_diag_counters,
    _lead_ot_fingerprint,
    _lead_species,
    _maybe_reexec_under_venv,
    _parse_args,
    _species_name,
    _state_path,
)

# ---------------------------------------------------------------------------
# Pair lifecycle
# ---------------------------------------------------------------------------


def _open_session(version: str, view: bool = False, window_pos: tuple[int, int] | None = None):
    """Mirror of ``_open_session`` in the test module.

    ``view=True`` opens an SDL2 window for this PyBoy so the game is
    visible while the trade runs. ``window_pos=(x, y)`` positions the
    window on the primary monitor so both peers can be shown side-by-side.
    Sound is always disabled (``sound_emulated=False``).
    """
    os.environ.setdefault("POKERED_SKIP_SHA1", "1")
    sys.path.insert(0, str(_REPO / "src"))
    from pyboy import PyBoy

    from pokered_harness.session import Session

    rom, sym = _ROM_PATHS[version]

    # Position SDL2 window via env var (SDL reads this at window creation).
    if view and window_pos is not None:
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{window_pos[0]},{window_pos[1]}"

    def _factory(path: str):
        return PyBoy(
            path,
            window="SDL2" if view else "null",
            cgb=_PYBOY_CGB_OVERRIDE,
            sound_emulated=False,
        )

    session = None
    try:
        session = Session.from_files(rom, sym, view=view, pyboy_factory=_factory)
        session.load_state(_state_path(version).read_bytes())
        # ``load_state`` restores RAM + registers but the LCD framebuffer it
        # repaints can lag the restored map by one or two frames (when the
        # state was saved mid-transition). Tick a dozen frames with rendering
        # on so the first screenshot reflects the *current* map, not a stale
        # pre-save one.
        session.step(12, render=True)
        return session
    except BaseException as exc:
        _cleanup_pair(None, session, None, active_error=exc)
        raise


def _cleanup_pair(
    link,
    a,
    b,
    *,
    active_error: BaseException | None = None,
) -> None:
    """Detach a pair owner, then close both sessions.

    Every cleanup operation is attempted, including when one raises a
    ``BaseException``. An active operation error remains primary: cleanup
    failures are attached as notes and never replace it. With no active
    error, cleanup failures are raised as a group so successful work cannot
    be reported after an incomplete teardown.
    """
    failures: list[tuple[str, BaseException]] = []
    if link is not None:
        try:
            link.detach_all()
        except BaseException as exc:  # noqa: BLE001 - teardown must continue
            failures.append(("link.detach_all", exc))
    for label, session in (("A", a), ("B", b)):
        if session is None:
            continue
        try:
            session.close()
        except BaseException as exc:  # noqa: BLE001 - teardown must continue
            failures.append((f"session {label}.close", exc))

    if not failures:
        return

    if active_error is not None:
        active_error.add_note("pair cleanup failures:")
        for operation, error in failures:
            active_error.add_note(
                f"  {operation}: {type(error).__name__}: {error}"
            )
        return

    for operation, error in failures:
        error.add_note(f"pair cleanup operation: {operation}")
    raise BaseExceptionGroup(
        "pair cleanup failed",
        [error for _operation, error in failures],
    )


def _open_pair_sessions(
    version_a: str,
    version_b: str,
    *,
    view: bool,
):
    """Open both sessions and roll back a partial startup on any failure."""
    a = b = None
    try:
        a = _open_session(version_a, view=view, window_pos=(80, 120))
        b = _open_session(version_b, view=view, window_pos=(640, 120))

        pyboy_hwnds: list[int] = []
        if view:
            # Force both windows onto the primary monitor, side-by-side. The
            # primary monitor on this machine is 2560x1440 at (0,0) — these
            # coords leave the windows comfortably centered and visible.
            if _force_move_pyboy_windows([(200, 300), (1200, 300)]):
                print(
                    "[info] moved PyBoy windows to (200,300) and (1200,300) "
                    "on primary monitor",
                    flush=True,
                )
            else:
                print(
                    "[warn] could not locate both PyBoy windows to move them; "
                    "they may be on a secondary monitor",
                    flush=True,
                )
            pyboy_hwnds = _find_pyboy_hwnds()
            if len(pyboy_hwnds) >= 2:
                print(
                    f"[info] PyBoy window handles: red={pyboy_hwnds[0]}, "
                    f"blue={pyboy_hwnds[1]} — natural-mode captures will use "
                    "Win32 screen-grab so dialogs are included",
                    flush=True,
                )
            else:
                pyboy_hwnds = []
                print(
                    "[warn] could not resolve both PyBoy window handles; "
                    "natural-mode captures will fall back to framebuffer reads",
                    flush=True,
                )
        return a, b, pyboy_hwnds
    except BaseException as exc:
        _cleanup_pair(None, a, b, active_error=exc)
        raise



# ---------------------------------------------------------------------------
# Copied trade helpers (from tests/test_pyboy_link_session_roms.py)
# ---------------------------------------------------------------------------


def _drive_two_sessions_to_link_menu(
    a, b, link, *, total_frames: int = 2400, frames_per_attempt: int = 20,
    sampler=None, dwell_s: float = 0.0, natural: bool = False,
    natural_shot=None,
) -> dict:
    counters = {
        "CableClubNPC": [0, 0],
        "SaveGameData": [0, 0],
        "Serial_SyncAndExchangeNybble": [0, 0],
        "Serial_ExchangeBytes": [0, 0],
        "LinkMenu": [0, 0],
    }
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)

    def tick_both_coarse(frames: int) -> None:
        link.step_interleaved(frames)
        if dwell_s > 0:
            time.sleep(dwell_s)

    def tick_both_fine(frames: int) -> None:
        link.step_interleaved(frames)
        if dwell_s > 0:
            time.sleep(dwell_s)

    frames_used = 0
    for _ in range(3):
        a.press("up", duration=6)
        b.press("up", duration=6)
        tick_both_coarse(20)
        frames_used += 20
        if sampler is not None:
            sampler("A_walk_up")

    prev_save = [counters["SaveGameData"][0], counters["SaveGameData"][1]]
    save_dwelt = False
    linkmenu_shown = False

    attempts = (total_frames - frames_used) // frames_per_attempt
    for _ in range(attempts):
        if counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0:
            # Natural: hold on the BATTLE/TRADE/CANCEL menu so the viewer
            # can read all three options before the game's default
            # cursor selection resolves. We intentionally don't move the
            # cursor — the fixture's saved cursor position is what the
            # trade-to-TRADE_CENTER flow depends on, and an up/down dance
            # can leave it on BATTLE (warps to the Colosseum, map 0xF0).
            if natural and not linkmenu_shown:
                linkmenu_shown = True
                tick_both_coarse(30)  # let the menu render
                if natural_shot is not None:
                    natural_shot("nat_02_link_menu")
                tick_both_coarse(60)  # rest of the 1.5s hold
            break
        # Natural: when the "Would you like to save?" prompt just
        # appeared, hold briefly so the YES/NO menu is readable.
        if natural and not save_dwelt and (
            counters["SaveGameData"][0] > prev_save[0]
            or counters["SaveGameData"][1] > prev_save[1]
        ):
            save_dwelt = True
            tick_both_coarse(15)  # tick a bit so the prompt has rendered
            if natural_shot is not None:
                natural_shot("nat_01_save_prompt")
            tick_both_coarse(30)  # remainder of the dwell
        a.press("a", duration=4)
        b.press("a", duration=4)
        in_serial_phase = (
            counters["SaveGameData"][0] > 0 or counters["SaveGameData"][1] > 0
        )
        if in_serial_phase:
            tick_both_fine(frames_per_attempt)
        else:
            tick_both_coarse(frames_per_attempt)
        frames_used += frames_per_attempt
        if sampler is not None:
            sampler("A_receptionist" if not in_serial_phase else "A_save")

    return {"counters": counters, "frames_used": frames_used}


def _drive_past_link_menu_to_trade_center(
    a, b, link, *, post_link_menu_frames: int = 1200, frames_per_attempt: int = 20,
    mid_callback=None, sampler=None, dwell_s: float = 0.0,
    natural: bool = False, natural_shot=None,
) -> dict:
    diag = _drive_two_sessions_to_link_menu(
        a, b, link, sampler=sampler, dwell_s=dwell_s, natural=natural,
        natural_shot=natural_shot,
    )
    counters = diag["counters"]
    assert counters["LinkMenu"][0] > 0 and counters["LinkMenu"][1] > 0, (
        "precondition: both sides must have reached LinkMenu before "
        "attempting TRADE_CENTER warp"
    )

    extra_frames = 0
    called_mid = False
    attempts = post_link_menu_frames // frames_per_attempt
    for _ in range(attempts):
        map_a = a.read_game_state().overworld.map_id
        map_b = b.read_game_state().overworld.map_id
        if map_a == TRADE_CENTER_MAP_ID and map_b == TRADE_CENTER_MAP_ID:
            break
        if mid_callback is not None and not called_mid and extra_frames >= 120:
            mid_callback()
            called_mid = True
        a.press("a", duration=4)
        b.press("a", duration=4)
        link.step_interleaved(frames_per_attempt)
        if dwell_s > 0:
            time.sleep(dwell_s)
        extra_frames += frames_per_attempt
        if sampler is not None:
            sampler("B_warp")

    map_a = a.read_game_state().overworld.map_id
    map_b = b.read_game_state().overworld.map_id
    return {
        "counters": counters,
        "frames_to_link_menu": diag["frames_used"],
        "extra_frames": extra_frames,
        "final_map_a": map_a,
        "final_map_b": map_b,
    }


def _drive_complete_trade(
    a, b, link, *, counters: dict,
    trade_budget_frames: int = 4000, step_frames: int = 20,
    mid_callback=None, sampler=None, dwell_s: float = 0.0,
    natural: bool = False, natural_shot=None,
) -> dict:
    add_mon = counters["_AddEnemyMonToPlayerParty"]
    trade_center_trade = counters["TradeCenter_Trade"]

    def tick_interleaved(frames: int) -> None:
        link.step_interleaved(frames)
        if dwell_s > 0:
            time.sleep(dwell_s)

    def tick_per_frame(frames: int) -> None:
        link.step_interleaved(frames)
        if dwell_s > 0:
            time.sleep(dwell_s)

    conn_a_now = a._pyboy.memory[a.symbols.addr_of("hSerialConnectionStatus")]
    conn_b_now = b._pyboy.memory[b.symbols.addr_of("hSerialConnectionStatus")]
    INTERNAL = 0x02
    dir_a = "right" if conn_a_now == INTERNAL else "left"
    dir_b = "right" if conn_b_now == INTERNAL else "left"

    for _ in range(4):
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        a.press(dir_a, duration=8)
        b.press(dir_b, duration=8)
        tick_per_frame(step_frames)
        if sampler is not None:
            sampler("C_walk_into_partner")

    settle_frames = 0
    while settle_frames < 1800:
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                and counters["CableClub_DoBattleOrTrade"][1] > 0):
            break
        a.press("a", duration=4)
        b.press("a", duration=4)
        if (counters["CableClub_DoBattleOrTrade"][0] > 0
                or counters["CableClub_DoBattleOrTrade"][1] > 0):
            tick_interleaved(step_frames)
        else:
            tick_per_frame(step_frames)
        settle_frames += step_frames
        if sampler is not None:
            sampler("C_settle")

    stats_key = "TradeCenter_SelectMon.selectStatsMenuItem"
    trade_key = "TradeCenter_SelectMon.selectTradeMenuItem"
    menu_key = "TradeCenter_SelectMon.playerMonMenu_HandleInput"
    tct_key = "TradeCenter_Trade"

    prev = {
        k: list(counters[k])
        for k in (stats_key, trade_key, menu_key, tct_key)
    }
    right_pending = [0, 0]
    RIGHT_PRESS_ITERATIONS = 5

    def _hold(frames: int) -> None:
        """Tick both peers in sync without any button press, so a menu
        or dialog stays on screen long enough for the viewer to read."""
        tick_per_frame(frames)

    # Natural: dwell once on the partner-dialog ("What would you like
    # to do?" / mon-select entry) so the viewer sees the prompt before
    # we mash A to open the party list.
    if natural:
        _hold(15)
        if natural_shot is not None:
            natural_shot("nat_03_partner_dialog")
        _hold(30)

    extra_frames = 0
    called_anim_shot = False
    attempts = trade_budget_frames // step_frames
    for _ in range(attempts):
        if add_mon[0] > 0 and add_mon[1] > 0:
            break
        cct_key = "CableClub_DoBattleOrTrade"
        if counters[cct_key][0] > 0 or counters[cct_key][1] > 0:
            tick_interleaved(step_frames)
        else:
            tick_per_frame(step_frames)
        extra_frames += step_frames
        if sampler is not None:
            sampler("C_trade_menu")

        # Capture a mid-trade-animation frame once TradeCenter_Trade has
        # actually been entered.
        if (mid_callback is not None and not called_anim_shot
                and trade_center_trade[0] > 0 and trade_center_trade[1] > 0):
            mid_callback()
            called_anim_shot = True

        now = {k: list(counters[k]) for k in (stats_key, trade_key, menu_key, tct_key)}
        for idx, sess in enumerate((a, b)):
            def ticked(key, _idx=idx, _now=now, _prev=prev):
                return _now[key][_idx] > _prev[key][_idx]

            if ticked(trade_key):
                # STATS/TRADE/CANCEL cursor landed on TRADE. Natural:
                # hold so the viewer sees TRADE highlighted before we
                # press A to confirm.
                if natural:
                    _hold(10)
                    if natural_shot is not None and idx == 0:
                        natural_shot("nat_06_trade_highlighted")
                    _hold(20)
                sess.press("a", duration=4)
                right_pending[idx] = 0
            elif ticked(stats_key):
                # STATS/TRADE/CANCEL just opened with cursor on STATS.
                # Natural: hold so STATS is readable before we move
                # right to TRADE.
                if natural:
                    _hold(10)
                    if natural_shot is not None and idx == 0:
                        natural_shot("nat_05_stats_trade_menu")
                    _hold(20)
                right_pending[idx] = RIGHT_PRESS_ITERATIONS
                sess.press("right", duration=12)
            elif right_pending[idx] > 0:
                sess.press("right", duration=12)
                right_pending[idx] -= 1
            elif ticked(menu_key):
                # Party list just opened — cursor on lead mon. Natural:
                # hold so the viewer sees the party list before A.
                if natural:
                    _hold(10)
                    if natural_shot is not None and idx == 0:
                        natural_shot("nat_04_party_list")
                    _hold(20)
                sess.press("a", duration=4)
            elif ticked(tct_key):
                sess.press("a", duration=4)
            else:
                sess.press("a", duration=4)
        prev = now

    return {
        "add_mon": add_mon,
        "trade_center_trade": trade_center_trade,
        "trade_phase_frames": extra_frames,
        "counters": counters,
    }


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    _maybe_reexec_under_venv(args.venv)
    global _PYBOY_CGB_OVERRIDE
    _PYBOY_CGB_OVERRIDE = not args.no_cgb

    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    if len(versions) != 2:
        print(f"[error] --versions must be 'a,b'; got {args.versions!r}",
              flush=True)
        return 2
    version_a, version_b = versions
    for v in (version_a, version_b):
        if v not in _ROM_PATHS:
            print(f"[error] unknown version {v!r}; choose from "
                  f"{sorted(_ROM_PATHS)}", flush=True)
            return 2

    _assert_fixtures_available(version_a)
    _assert_fixtures_available(version_b)

    outdir = (_REPO / args.outdir) if not Path(args.outdir).is_absolute() \
        else Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    shoot = Shooter(outdir)

    print(f"[info] versions: A={version_a}, B={version_b}", flush=True)
    print(f"[info] outdir:   {outdir}", flush=True)
    print(f"[info] python:   {sys.executable}", flush=True)

    # Ensure harness on sys.path before we import pokered_harness here.
    sys.path.insert(0, str(_REPO / "src"))
    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

    t_all_start = time.perf_counter()

    a = b = None
    link = None
    active_error: BaseException | None = None
    try:
        # Side-by-side on the primary monitor: Red on the left, Blue on the right.
        # Opening and window setup are transactional so a failed second session
        # cannot strand the first emulator.
        a, b, _pyboy_hwnds = _open_pair_sessions(
            version_a, version_b, view=args.view
        )

        # PyBoy's Cython ``set_emulation_speed`` is declared ``int`` in the
        # .pxd, so fractional values silently truncate to 0 (unlimited —
        # the opposite of what we want). Instead, translate ``--speed`` into
        # an extra ``time.sleep`` between driver iterations ("dwell") so
        # each menu state stays on-screen long enough to watch.
        # 20 frames/iter @ 60fps = 0.333s emulated. At speed=0.5 we want
        # each iter to take ~0.666s wall — so dwell ~= 0.333s.
        _iter_s = 20.0 / 60.0
        _dwell_s = max(0.0, (_iter_s / max(args.speed, 1e-3)) - _iter_s) \
            if args.view else 0.0
        if args.view and _dwell_s > 0:
            print(f"[info] dwell per driver iteration: {_dwell_s*1000:.0f}ms "
                  f"(target speed {args.speed}x)", flush=True)
        # Phase A: pair + start.
        t0 = time.perf_counter()
        link = PyBoyLinkSession.local(view=args.view)
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        t_pair = time.perf_counter() - t0
        print(f"[pair attach] {t_pair:.2f}s", flush=True)

        pre_a_species = _lead_species(a)
        pre_b_species = _lead_species(b)
        pre_a_ot = _lead_ot_fingerprint(a)
        pre_b_ot = _lead_ot_fingerprint(b)

        ow_a = a.read_game_state().overworld
        ow_b = b.read_game_state().overworld
        print(
            f"[start] A coord=({ow_a.x},{ow_a.y}) map=0x{ow_a.map_id:02x} | "
            f"B coord=({ow_b.x},{ow_b.y}) map=0x{ow_b.map_id:02x}",
            flush=True,
        )
        start_png = shoot.shoot_pair(a, b, "phase_00_start")
        for p in start_png:
            print(f"  wrote {p}", flush=True)

        # Install trade-phase diag counters *before* warp so events that
        # fire during CableClub_DoBattleOrTrade are caught.
        diag_counters = _install_trade_diag_counters(a, b)

        # Optional per-iteration timeline sampler. When --sample-every is
        # set, every Nth driver iteration dumps a numbered PNG from each
        # peer into outdir/timeline/ — this is a non-interactive proxy
        # for "watching the SDL2 windows" so post-run we can verify
        # navigation progressed on both sides.
        sampler = None
        if args.sample_every > 0:
            timeline_dir = outdir / "timeline"
            timeline_dir.mkdir(parents=True, exist_ok=True)
            state = {"counter": 0, "shots": 0}
            every = args.sample_every
            def sampler(phase_tag: str):
                state["counter"] += 1
                if state["counter"] % every != 0:
                    return
                idx = state["shots"]
                state["shots"] += 1
                stem = f"{idx:04d}__{phase_tag}"
                a._pyboy.screen.image.save(timeline_dir / f"{stem}__red.png")
                b._pyboy.screen.image.save(timeline_dir / f"{stem}__blue.png")
            print(f"[info] timeline sampler: every {every} iters -> "
                  f"{timeline_dir}", flush=True)

        # Phase B: drive past LinkMenu to TRADE_CENTER warp.
        t0 = time.perf_counter()

        def _mid_linkmenu_shot():
            # Captures a mid-warp frame (typically while the "Please
            # wait" / menu-exchange is in progress).
            paths = shoot.shoot_pair(a, b, "phase_01_linkmenu")
            for p in paths:
                print(f"  wrote {p}", flush=True)

        def _nat_shot(stem: str) -> None:
            """Capture each hook-fire state into:

            1. ``stem__{red,blue}.png`` — Win32 PrintWindow of the
               live SDL2 windows. Color, as the user sees, but the
               PyBoy CGB compositor drops Gen 1's window-layer menu
               dialogs so text/cursor boxes are missing.
            2. ``stem_win__{red,blue}.png`` — Monochrome 160x144
               render of the Window tile map read straight from VRAM
               (bank 0). Has the dialog text and cursor but no color.
            3. ``stem_combined__{red,blue}.png`` — Side-by-side of (1)
               and (2) scaled to equal height. One image tells you
               both what the screen shows AND what the dialog says.
            """
            if len(_pyboy_hwnds) >= 2:
                red_path = outdir / f"{stem}__red.png"
                blue_path = outdir / f"{stem}__blue.png"
                ok_r = _win32_grab_window(_pyboy_hwnds[0], red_path)
                ok_b = _win32_grab_window(_pyboy_hwnds[1], blue_path)
                if ok_r:
                    print(f"  wrote {red_path} (win32)", flush=True)
                if ok_b:
                    print(f"  wrote {blue_path} (win32)", flush=True)
            win_r = outdir / f"{stem}_win__red.png"
            win_b = outdir / f"{stem}_win__blue.png"
            if _render_window_from_vram(a._pyboy, win_r):
                print(f"  wrote {win_r} (vram/win)", flush=True)
            if _render_window_from_vram(b._pyboy, win_b):
                print(f"  wrote {win_b} (vram/win)", flush=True)
            # Side-by-side composite per peer.
            for peer, live, vram in (
                ("red", outdir / f"{stem}__red.png", win_r),
                ("blue", outdir / f"{stem}__blue.png", win_b),
            ):
                combined = outdir / f"{stem}_combined__{peer}.png"
                if _make_side_by_side(live, vram, combined):
                    print(f"  wrote {combined} (color+dialog)", flush=True)

        warp = _drive_past_link_menu_to_trade_center(
            a, b, link, mid_callback=_mid_linkmenu_shot, sampler=sampler,
            dwell_s=_dwell_s, natural=args.natural,
            natural_shot=_nat_shot if args.natural else None,
        )
        t_phase_b = time.perf_counter() - t0
        print(f"[phase B: link menu -> trade center] {t_phase_b:.2f}s",
              flush=True)

        if warp.get("extra_frames", 0) < 120:
            # Warp happened too quickly for the mid-callback to trigger;
            # grab a post-warp snapshot under the linkmenu stem so the
            # demo always emits that file.
            paths = shoot.shoot_pair(a, b, "phase_01_linkmenu")
            for p in paths:
                print(f"  wrote {p}", flush=True)

        assert warp["final_map_a"] == TRADE_CENTER_MAP_ID, (
            f"A didn't warp to TRADE_CENTER; got "
            f"0x{warp['final_map_a']:02x}, expected 0x{TRADE_CENTER_MAP_ID:02x}"
        )
        assert warp["final_map_b"] == TRADE_CENTER_MAP_ID, (
            f"B didn't warp to TRADE_CENTER; got "
            f"0x{warp['final_map_b']:02x}, expected 0x{TRADE_CENTER_MAP_ID:02x}"
        )

        trade_center_png = shoot.shoot_pair(a, b, "phase_02_trade_center")
        for p in trade_center_png:
            print(f"  wrote {p}", flush=True)

        # Phase C: drive the complete trade.
        t0 = time.perf_counter()
        trade_start_png = shoot.shoot_pair(a, b, "phase_03_trade_start")
        for p in trade_start_png:
            print(f"  wrote {p}", flush=True)

        def _mid_trade_anim_shot():
            paths = shoot.shoot_pair(a, b, "phase_04_trade_animation")
            for p in paths:
                print(f"  wrote {p}", flush=True)

        trade_diag = _drive_complete_trade(
            a, b, link, counters=diag_counters,
            mid_callback=_mid_trade_anim_shot, sampler=sampler,
            dwell_s=_dwell_s, natural=args.natural,
            natural_shot=_nat_shot if args.natural else None,
        )
        t_phase_c = time.perf_counter() - t0
        print(f"[phase C: complete trade] {t_phase_c:.2f}s", flush=True)

        add_mon = trade_diag["add_mon"]
        if add_mon[0] == 0 or add_mon[1] == 0:
            print(
                f"[warn] _AddEnemyMonToPlayerParty hooks didn't fire on "
                f"both sides: {add_mon}. Trade may have stalled; "
                f"continuing to post snapshots for diagnostics.",
                flush=True,
            )

        # Ensure we got an animation shot even if mid_callback didn't fire
        # (e.g. TradeCenter_Trade hook resolved too fast to catch between
        # our polling steps).
        anim_shot = outdir / "phase_04_trade_animation__red.png"
        if not anim_shot.exists():
            paths = shoot.shoot_pair(a, b, "phase_04_trade_animation")
            for p in paths:
                print(f"  wrote {p} (late-catch)", flush=True)

        trade_done_png = shoot.shoot_pair(a, b, "phase_05_trade_done")
        for p in trade_done_png:
            print(f"  wrote {p}", flush=True)

        # Natural: after the trade animation finishes the game shows the
        # "Take good care of <mon>!" dialog. Hold on it so the viewer
        # sees the line before we tick forward to verify the party swap.
        if args.natural:
            link.step_interleaved(60)  # ~1s — let the dialog render
            _nat_shot("nat_07_take_good_care")
            link.step_interleaved(120)  # ~2s — remainder of the dwell
        # Let a few extra frames tick so the party reflects the swap.
        link.step_interleaved(30)

        post_a_species = _lead_species(a)
        post_b_species = _lead_species(b)
        post_a_ot = _lead_ot_fingerprint(a)
        post_b_ot = _lead_ot_fingerprint(b)

        post_png = shoot.shoot_pair(a, b, "phase_06_post")
        for p in post_png:
            print(f"  wrote {p}", flush=True)

        # Post-trade hold so the viewer can see:
        #  - the received Pokémon being added to the party (Pokédex
        #    card flash + "No. 003 VENUSAUR / OT/ASH / IDNo. ####" dialog)
        #  - the post-trade auto-save ("SAVING DON'T TURN OFF THE POWER")
        #  - return to the Trade Center with the new Pokémon in party
        # Captures intermediate screenshots every few hundred frames so
        # the specific sub-states are preserved in PNGs too.
        # Seven 3s chunks = 21s total so the full tail of the sequence
        # (dex card → party-add → save → return to TC → idle) has room
        # to play out without being cut off at the window close.
        if args.view:
            print("[info] holding post-trade state for 21s so you can see "
                  "the received Pokémon's Pokédex card, the party-add, "
                  "the post-trade auto-save, and the return to the Trade "
                  "Center…", flush=True)
            for chunk_idx, tag in enumerate([
                "phase_07_party_add",
                "phase_08_pokedex_card",
                "phase_09_post_save_a",
                "phase_10_post_save_b",
                "phase_11_back_in_tc",
                "phase_12_tc_idle_a",
                "phase_13_tc_idle_b",
            ]):
                link.step_interleaved(180)  # 3 seconds
                try:
                    paths = shoot.shoot_pair(a, b, tag)
                    for p in paths:
                        print(f"  wrote {p}", flush=True)
                except Exception:  # noqa: BLE001, S112 - screenshots are best-effort diagnostics
                    continue
            # Post-trade idle hold so the viewer can watch the tail
            # end without the windows closing. Deliberately NOT
            # interactive — ``sys.stdin.isatty()`` lies under
            # some harness runners (pseudo-TTY attached but stdin
            # returns EOF immediately), and that would drop through
            # the "press Enter" branch and close the windows
            # instantly. Fixed hold is robust in every environment.
            hold_s = args.hold_after_s
            print(f"[info] post-trade idle hold for {hold_s}s — watch the "
                  f"windows, they'll close on their own.", flush=True)
            if hold_s > 0:
                link.step_interleaved(hold_s * 60)

        t_total = time.perf_counter() - t_all_start
        print(f"[total wall-clock] {t_total:.2f}s", flush=True)

        pre_a_str = _species_name(pre_a_species) if pre_a_species is not None else "<none>"
        pre_b_str = _species_name(pre_b_species) if pre_b_species is not None else "<none>"
        post_a_str = _species_name(post_a_species) if post_a_species is not None else "<none>"
        post_b_str = _species_name(post_b_species) if post_b_species is not None else "<none>"

        print(
            f"pre:  {version_a} lead = {pre_a_str}, "
            f"{version_b} lead = {pre_b_str}",
            flush=True,
        )
        # Primary check: _AddEnemyMonToPlayerParty fires on both sides
        # means the game engine actually installed a peer mon into each
        # side's party. Species-equality is a weak secondary signal —
        # if both sides start with the same species the lead species
        # stays the same after a straight swap. OT-name fingerprint
        # flips in that case, so we use it as an extra signal.
        species_changed = (
            pre_a_species != post_a_species and pre_b_species != post_b_species
        )
        ot_changed = pre_a_ot != post_a_ot and pre_b_ot != post_b_ot
        engine_trade = add_mon[0] > 0 and add_mon[1] > 0
        trade_happened = engine_trade and (species_changed or ot_changed)
        tag = "  <- TRADE SUCCEEDED" if trade_happened else "  <- TRADE DID NOT COMPLETE"
        print(
            f"post: {version_a} lead = {post_a_str}, "
            f"{version_b} lead = {post_b_str}{tag}",
            flush=True,
        )
        print(
            f"       _AddEnemyMonToPlayerParty hooks: A={add_mon[0]}, "
            f"B={add_mon[1]}; species-changed={species_changed}; "
            f"OT-fingerprint-changed={ot_changed}",
            flush=True,
        )

        result = 0
        if not trade_happened:
            print(
                "[error] trade did not complete end-to-end. See diag "
                "counters above and the PNGs under the outdir.",
                flush=True,
            )
            result = 1
        # Keep the result outside the try/finally return path.  A return from
        # inside the try block makes it too easy for a future try/except/else
        # refactor to skip the success-path bookkeeping while teardown is
        # still running.  The finally block must always see the real active
        # exception (if any), and the caller receives the status only after
        # pair cleanup has completed.
        return_code = result
    except BaseException as exc:
        active_error = exc
        raise
    else:
        # This branch runs for both the successful and unsuccessful trade
        # result.  Keep ``active_error`` explicitly clear before ``finally``
        # so cleanup failures are reported as teardown failures rather than
        # being mistaken for a failure from the trade operation itself.
        active_error = None
    finally:
        _cleanup_pair(link, a, b, active_error=active_error)

    return return_code


if __name__ == "__main__":
    sys.exit(main())
