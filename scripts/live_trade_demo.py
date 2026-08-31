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

import argparse
import os
import sys
import time
from dataclasses import dataclass
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
# venv re-exec
# ---------------------------------------------------------------------------


def _maybe_reexec_under_venv(venv_tag: str | None) -> None:
    """If ``--venv`` points at an existing ``.venv-<tag>/Scripts/python.exe``,
    re-exec this script under it. Otherwise print a warning and fall
    back to the ambient interpreter. No-op when ``venv_tag`` is None or
    we're already running under that venv.
    """
    if not venv_tag:
        return
    target = _REPO / f".venv-{venv_tag}" / "Scripts" / "python.exe"
    if not target.is_file():
        print(
            f"[warn] --venv {venv_tag} requested but {target} not found; "
            f"falling back to ambient interpreter {sys.executable}",
            flush=True,
        )
        return
    # Already under that interp?
    try:
        if Path(sys.executable).resolve() == target.resolve():
            return
    except OSError:
        pass

    # Preserve all CLI args except drop --venv (we've already resolved it).
    argv = [str(target)]
    skip = False
    for arg in sys.argv:
        if skip:
            skip = False
            continue
        if arg == "--venv":
            skip = True
            continue
        if arg.startswith("--venv="):
            continue
        argv.append(arg)
    print(f"[info] re-exec under {target}", flush=True)
    os.execv(str(target), argv)


# ---------------------------------------------------------------------------
# ROM + fixture resolution
# ---------------------------------------------------------------------------


_ROM_PATHS: dict[str, tuple[Path, Path]] = {
    "red": (
        _REPO / "rom" / "red" / "pokemon-red-color.gb",
        _REPO / "rom" / "red" / "pokemon-red.sym",
    ),
    "blue": (
        _REPO / "rom" / "blue" / "pokemon-blue-color.gb",
        _REPO / "rom" / "blue" / "pokemon-blue.sym",
    ),
    "yellow": (
        _REPO / "rom" / "yellow" / "pokemon-yellow.gbc",
        _REPO / "rom" / "yellow" / "pokemon-yellow.sym",
    ),
}


def _state_path(version: str) -> Path:
    return _REPO / "tests" / "fixtures" / "link" / version / "cable_club.state"


def _assert_fixtures_available(version: str) -> None:
    rom, sym = _ROM_PATHS[version]
    state = _state_path(version)
    missing: list[Path] = []
    for p in (rom, sym, state):
        if not p.is_file():
            missing.append(p)
    if missing:
        print(
            "[error] required BYO-ROM / fixture files are missing:",
            flush=True,
        )
        for p in missing:
            print(f"   - {p}", flush=True)
        print(
            "    Generate the Cable Club fixture with "
            "'scripts/produce_cable_club_fixture.py' and ensure the "
            "Full Color Hack v1.2 ROMs are dropped under rom/.",
            flush=True,
        )
        sys.exit(2)


def _find_pyboy_hwnds() -> list[int]:
    """Return the handles of all visible top-level windows titled
    ``PyBoy``. Returned in creation order (same as EnumWindows — which
    matches open order for these sessions)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    found: list[int] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value and "PyBoy" in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found


def _win32_grab_window(hwnd: int, out_path: Path) -> bool:
    """Capture the client area of ``hwnd`` via Win32 ``PrintWindow`` +
    BitBlt and save as a PNG at ``out_path``. Returns True on success.

    This bypasses PyBoy's ``screen.image`` framebuffer readback entirely
    — it grabs exactly what the user sees on the SDL2 window, including
    dialog overlays that the Cython CGB renderer's internal backbuffer
    doesn't reflect in ``screen.ndarray``.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    from PIL import Image

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return False
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False

    hdc_window = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbm = gdi32.CreateCompatibleBitmap(hdc_window, w, h)
    gdi32.SelectObject(hdc_mem, hbm)

    # PW_CLIENTONLY (1) | PW_RENDERFULLCONTENT (2) — the latter forces
    # the window to render fully even if DWM-composited / off-screen.
    PW_CLIENTONLY = 1
    PW_RENDERFULLCONTENT = 2
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # negative → top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0  # BI_RGB
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)

    if not ok:
        return False
    img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
    img.convert("RGB").save(out_path)
    return True


def _read_vram_range(pyboy, start: int, length: int) -> bytes:
    """Bulk-read ``length`` bytes starting at ``start`` from pyboy memory."""
    return bytes(pyboy.memory[start + i] for i in range(length))


def _read_vram_bank(pyboy, bank: int, start: int, length: int) -> bytes:
    """Read VRAM at ``start..start+length`` from bank ``bank`` (0 or 1)
    in CGB mode. Temporarily swaps VBK (0xFF4F) and restores it."""
    old_vbk = pyboy.memory[0xFF4F]
    try:
        pyboy.memory[0xFF4F] = bank
        return bytes(pyboy.memory[start + i] for i in range(length))
    finally:
        pyboy.memory[0xFF4F] = old_vbk


def _read_cgb_bg_palettes(pyboy) -> list:
    """Return the 8 CGB BG palettes as a list of 8 entries; each entry
    is 4 RGB tuples. Read via BCPS/BCPD (0xFF68/0xFF69) — setting the
    auto-increment bit in BCPS lets us stream 64 bytes out of BCPD.
    Each color is 15-bit BGR555: lo + (hi << 8) → rrrrr gggggbbbbb.
    """
    old_bcps = pyboy.memory[0xFF68]
    try:
        pyboy.memory[0xFF68] = 0x80  # index 0, auto-increment on
        raw = bytes(pyboy.memory[0xFF69] for _ in range(64))
    finally:
        pyboy.memory[0xFF68] = old_bcps
    palettes = []
    for pal_i in range(8):
        colors = []
        for col_i in range(4):
            lo = raw[pal_i * 8 + col_i * 2]
            hi = raw[pal_i * 8 + col_i * 2 + 1]
            val = lo | (hi << 8)
            r5 = val & 0x1F
            g5 = (val >> 5) & 0x1F
            b5 = (val >> 10) & 0x1F
            # 5-bit → 8-bit scale (× 255 / 31 ≈ × 8.23)
            colors.append((r5 * 255 // 31, g5 * 255 // 31, b5 * 255 // 31))
        palettes.append(colors)
    return palettes


def _render_tilemap_from_vram(pyboy, layer: str, out_path: Path, *, color: bool = True) -> bool:
    """Vectorized renderer for the BG or Window tile map directly from
    VRAM. ``layer`` must be ``"bg"`` or ``"win"``. Produces a 160x144
    PNG showing the *logical* tilemap content — bypasses the CGB
    compositor entirely, so menu/dialog tiles that PyBoy's CGB renderer
    doesn't draw on screen still appear here.

    When ``color=True`` (default) the image is rendered in full CGB
    color by reading per-tile attributes from VRAM bank 1 and per-index
    RGB palettes from BCPD (0xFF68/69). When ``color=False`` a
    monochrome 2-bit DMG image is produced.
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return False
    lcdc = pyboy.memory[0xFF40]
    unsigned = bool(lcdc & 0x10)
    if layer == "bg":
        map_base = 0x9C00 if (lcdc & 0x08) else 0x9800
        scy = pyboy.memory[0xFF42]
        scx = pyboy.memory[0xFF43]
        start_y = scy
        start_x = scx
    else:  # "win"
        map_base = 0x9C00 if (lcdc & 0x40) else 0x9800
        wy = pyboy.memory[0xFF4A]
        wx = pyboy.memory[0xFF4B] - 7
        start_y = -wy
        start_x = -wx
    # Bank 0: tile IDs + DMG tile data.
    tile_map = np.frombuffer(
        _read_vram_bank(pyboy, 0, map_base, 0x400), dtype=np.uint8
    ).reshape(32, 32)
    if unsigned:
        tile_data_b0 = np.frombuffer(
            _read_vram_bank(pyboy, 0, 0x8000, 0x1800), dtype=np.uint8
        )
        tile_data_b1 = np.frombuffer(
            _read_vram_bank(pyboy, 1, 0x8000, 0x1800), dtype=np.uint8
        ) if color else None
    else:
        tile_data_b0 = np.frombuffer(
            _read_vram_bank(pyboy, 0, 0x8800, 0x1000), dtype=np.uint8
        )
        tile_data_b1 = np.frombuffer(
            _read_vram_bank(pyboy, 1, 0x8800, 0x1000), dtype=np.uint8
        ) if color else None
    if color:
        # Bank 1: tile attribute bytes (palette index, tile bank, flip, etc).
        tile_attrs = np.frombuffer(
            _read_vram_bank(pyboy, 1, map_base, 0x400), dtype=np.uint8
        ).reshape(32, 32)
        cgb_palettes = _read_cgb_bg_palettes(pyboy)
        # Flatten into a 32-entry RGB LUT: 8 palettes x 4 colors.
        rgb_lut = np.array(
            [c for pal in cgb_palettes for c in pal], dtype=np.uint8
        ).reshape(8, 4, 3)
    # Build a 160x144 pixel coordinate grid.
    ys = np.arange(144)
    xs = np.arange(160)
    gx, gy = np.meshgrid(xs, ys)
    if layer == "bg":
        src_x = (gx + start_x) & 0xFF
        src_y = (gy + start_y) & 0xFF
        in_range = np.ones_like(gx, dtype=bool)
    else:
        src_x = gx - start_x
        src_y = gy - start_y
        in_range = (src_x >= 0) & (src_y >= 0) & (src_x < 256) & (src_y < 256)
    tile_col = np.clip(src_x >> 3, 0, 31)
    tile_row = np.clip(src_y >> 3, 0, 31)
    tile_ids = tile_map[tile_row, tile_col]
    if unsigned:
        tile_addrs = tile_ids.astype(np.int32) * 16
    else:
        signed = tile_ids.astype(np.int8).astype(np.int32)
        tile_addrs = (signed + 128) * 16
    row_in_tile = (src_y & 0x07).astype(np.int32)
    col_in_tile = 7 - (src_x & 0x07)
    bit = (1 << col_in_tile).astype(np.uint8)
    if color:
        attrs = tile_attrs[tile_row, tile_col]
        pal_idx = attrs & 0x07
        tile_bank = (attrs >> 3) & 0x01
        flip_x = (attrs >> 5) & 0x01
        flip_y = (attrs >> 6) & 0x01
        # Account for Y flip.
        eff_row = np.where(flip_y == 1, 7 - row_in_tile, row_in_tile)
        eff_col_bit = np.where(flip_x == 1, src_x & 0x07, col_in_tile)
        bit_eff = (1 << eff_col_bit).astype(np.uint8)
        shift_eff = eff_col_bit
        # Pick tile data from bank 0 or bank 1 per tile.
        lo0 = tile_data_b0[tile_addrs + eff_row * 2]
        hi0 = tile_data_b0[tile_addrs + eff_row * 2 + 1]
        lo1 = tile_data_b1[tile_addrs + eff_row * 2]
        hi1 = tile_data_b1[tile_addrs + eff_row * 2 + 1]
        lo = np.where(tile_bank == 1, lo1, lo0)
        hi = np.where(tile_bank == 1, hi1, hi0)
        color_idx = (((hi & bit_eff) >> shift_eff) << 1) | ((lo & bit_eff) >> shift_eff)
        # Look up RGB from per-tile palette.
        rgb = rgb_lut[pal_idx, color_idx]  # (144, 160, 3)
        if layer == "win":
            # Keep out-of-window pixels as white.
            mask = np.stack([in_range] * 3, axis=-1)
            rgb = np.where(mask, rgb, 0xFF).astype(np.uint8)
        Image.fromarray(rgb, mode="RGB").save(out_path)
        return True
    # Monochrome fallback.
    lo = tile_data_b0[tile_addrs + row_in_tile * 2]
    hi = tile_data_b0[tile_addrs + row_in_tile * 2 + 1]
    color_idx = (((hi & bit) >> col_in_tile) << 1) | ((lo & bit) >> col_in_tile)
    palette = np.array([0xFF, 0xAA, 0x55, 0x00], dtype=np.uint8)
    img_arr = palette[color_idx]
    if layer == "win":
        img_arr = np.where(in_range, img_arr, 0xFF).astype(np.uint8)
    Image.fromarray(img_arr, mode="L").save(out_path)
    return True


def _render_bg_from_vram(pyboy, out_path: Path) -> bool:
    # Monochrome by default — the CGB attribute/palette read via
    # pyboy.memory doesn't honor VBK bank swaps reliably.
    return _render_tilemap_from_vram(pyboy, "bg", out_path, color=False)


def _render_window_from_vram(pyboy, out_path: Path) -> bool:
    return _render_tilemap_from_vram(pyboy, "win", out_path, color=False)


def _make_side_by_side(left_path: Path, right_path: Path, out_path: Path) -> bool:
    """Emit ``out_path`` as ``left_path`` + ``right_path`` side-by-side
    with matched heights (monochrome image scaled up to the color
    image's height)."""
    try:
        from PIL import Image
    except Exception:
        return False
    if not left_path.exists() or not right_path.exists():
        return False
    left = Image.open(left_path).convert("RGB")
    right = Image.open(right_path).convert("RGB")
    # Scale right to match left's height using nearest-neighbor (preserve pixel art look).
    target_h = left.height
    scale = target_h / right.height
    target_w = round(right.width * scale)
    right = right.resize((target_w, target_h), Image.Resampling.NEAREST)
    out = Image.new("RGB", (left.width + right.width, target_h), (30, 30, 30))
    out.paste(left, (0, 0))
    out.paste(right, (left.width, 0))
    out.save(out_path)
    return True


def _force_move_pyboy_windows(positions: list[tuple[int, int]]) -> bool:
    """Win32-move every visible window titled ``PyBoy`` to the given
    ``(x, y)`` positions in order. ``SDL_VIDEO_WINDOW_POS`` is unreliable
    on Windows when multiple monitors are configured (SDL often restores
    a cached position on the wrong monitor), so we enumerate top-level
    windows via Win32 and call ``SetWindowPos`` directly.

    Returns True iff at least ``len(positions)`` PyBoy windows were moved.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    GetWindowTextW = user32.GetWindowTextW
    IsWindowVisible = user32.IsWindowVisible
    SetWindowPos = user32.SetWindowPos
    SWP_NOZORDER = 0x0004
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    found: list[int] = []

    def _cb(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, buf, 256)
        if buf.value and "PyBoy" in buf.value:
            found.append(hwnd)
        return True

    EnumWindows(EnumWindowsProc(_cb), 0)
    for hwnd, (x, y) in zip(found, positions):
        # SWP_NOSIZE keeps PyBoy's current size; we only reposition.
        SetWindowPos(
            hwnd, 0, x, y, 0, 0,
            SWP_NOZORDER | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
    return len(found) >= len(positions)


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

    session = Session.from_files(rom, sym, view=view, pyboy_factory=_factory)
    session.load_state(_state_path(version).read_bytes())
    # ``load_state`` restores RAM + registers but the LCD framebuffer it
    # repaints can lag the restored map by one or two frames (when the
    # state was saved mid-transition). Tick a dozen frames with rendering
    # on so the first screenshot reflects the *current* map, not a stale
    # pre-save one.
    session.step(12, render=True)
    return session


# ---------------------------------------------------------------------------
# Copied trade helpers (from tests/test_pyboy_link_session_roms.py)
# ---------------------------------------------------------------------------


def _install_hook_counter(session, symbol: str, bucket: list, slot: int) -> None:
    if symbol not in session.symbols:
        return
    bank, addr = session.symbols.bank_addr(symbol)

    def _cb(_ctx: object) -> None:
        bucket[slot] += 1

    try:
        session._pyboy.hook_register(bank, addr, _cb, None)
    except ValueError:
        pass


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
        for _ in range(frames):
            a.step(1)
            b.step(1)
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
    prev_link = [counters["LinkMenu"][0], counters["LinkMenu"][1]]
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


_TRADE_DIAG_SYMBOLS = (
    "CableClub_DoBattleOrTrade",
    "CallCurrentTradeCenterFunction",
    "TradeCenter_SelectMon",
    "TradeCenter_SelectMon.playerMonMenu",
    "TradeCenter_SelectMon.playerMonMenu_HandleInput",
    "TradeCenter_SelectMon.chosePlayerMon",
    "TradeCenter_SelectMon.selectStatsMenuItem",
    "TradeCenter_SelectMon.selectTradeMenuItem",
    "TradeCenter_Trade",
    "_AddEnemyMonToPlayerParty",
)


def _install_trade_diag_counters(a, b) -> dict:
    counters = {sym: [0, 0] for sym in _TRADE_DIAG_SYMBOLS}
    for idx, sess in enumerate((a, b)):
        for sym, bucket in counters.items():
            _install_hook_counter(sess, sym, bucket, idx)
    return counters


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
        for _ in range(frames):
            a.step(1)
            b.step(1)
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
            def ticked(key, _idx=idx):
                return now[key][_idx] > prev[key][_idx]

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
# Helpers: screenshot + species naming + party state
# ---------------------------------------------------------------------------


# Gen-I *internal* species IDs as used in wPartyMons.Species — this is
# NOT the pokedex number; it's the hardware-ordering internal ID from
# pokered/constants/pokemon_constants.asm. Table below covers every
# canonical ID; unknown IDs render as a hex fallback.
_GEN1_SPECIES: dict[int, str] = {
    0x01: "Rhydon", 0x02: "Kangaskhan", 0x03: "Nidoran-M", 0x04: "Clefairy",
    0x05: "Spearow", 0x06: "Voltorb", 0x07: "Nidoking", 0x08: "Slowbro",
    0x09: "Ivysaur", 0x0A: "Exeggutor", 0x0B: "Lickitung", 0x0C: "Exeggcute",
    0x0D: "Grimer", 0x0E: "Gengar", 0x0F: "Nidoran-F", 0x10: "Nidoqueen",
    0x11: "Cubone", 0x12: "Rhyhorn", 0x13: "Lapras", 0x14: "Arcanine",
    0x15: "Mew", 0x16: "Gyarados", 0x17: "Shellder", 0x18: "Tentacool",
    0x19: "Gastly", 0x1A: "Scyther", 0x1B: "Staryu", 0x1C: "Blastoise",
    0x1D: "Pinsir", 0x1E: "Tangela",
    0x21: "Growlithe", 0x22: "Onix", 0x23: "Fearow", 0x24: "Pidgey",
    0x25: "Slowpoke", 0x26: "Kadabra", 0x27: "Graveler", 0x28: "Chansey",
    0x29: "Machoke", 0x2A: "Mr.Mime", 0x2B: "Hitmonlee", 0x2C: "Hitmonchan",
    0x2D: "Arbok", 0x2E: "Parasect", 0x2F: "Psyduck", 0x30: "Drowzee",
    0x31: "Golem",
    0x33: "Magmar",
    0x35: "Electabuzz", 0x36: "Magneton", 0x37: "Koffing",
    0x39: "Mankey", 0x3A: "Seel", 0x3B: "Diglett", 0x3C: "Tauros",
    0x40: "Farfetch'd", 0x41: "Venonat", 0x42: "Dragonite",
    0x46: "Doduo", 0x47: "Poliwag", 0x48: "Jynx", 0x49: "Moltres",
    0x4A: "Articuno", 0x4B: "Zapdos", 0x4C: "Ditto", 0x4D: "Meowth",
    0x4E: "Krabby",
    0x52: "Vulpix", 0x53: "Ninetales", 0x54: "Pikachu", 0x55: "Raichu",
    0x58: "Dratini", 0x59: "Dragonair", 0x5A: "Kabuto", 0x5B: "Kabutops",
    0x5C: "Horsea", 0x5D: "Seadra",
    0x60: "Sandshrew", 0x61: "Sandslash", 0x62: "Omanyte", 0x63: "Omastar",
    0x64: "Jigglypuff", 0x65: "Wigglytuff", 0x66: "Eevee", 0x67: "Flareon",
    0x68: "Jolteon", 0x69: "Vaporeon", 0x6A: "Machop", 0x6B: "Zubat",
    0x6C: "Ekans", 0x6D: "Paras", 0x6E: "Poliwhirl", 0x6F: "Poliwrath",
    0x70: "Weedle", 0x71: "Kakuna", 0x72: "Beedrill",
    0x74: "Dodrio", 0x75: "Primeape", 0x76: "Dugtrio", 0x77: "Venomoth",
    0x78: "Dewgong",
    0x7B: "Caterpie", 0x7C: "Metapod", 0x7D: "Butterfree", 0x7E: "Machamp",
    0x80: "Golduck", 0x81: "Hypno", 0x82: "Golbat", 0x83: "Mewtwo",
    0x84: "Snorlax", 0x85: "Magikarp",
    0x88: "Muk",
    0x8A: "Kingler", 0x8B: "Cloyster",
    0x8D: "Electrode", 0x8E: "Clefable", 0x8F: "Weezing",
    0x90: "Persian", 0x91: "Marowak",
    0x93: "Haunter", 0x94: "Abra", 0x95: "Alakazam", 0x96: "Pidgeotto",
    0x97: "Pidgeot", 0x98: "Starmie", 0x99: "Bulbasaur", 0x9A: "Venusaur",
    0x9B: "Tentacruel",
    0x9D: "Goldeen", 0x9E: "Seaking",
    0xA3: "Ponyta", 0xA4: "Rapidash", 0xA5: "Rattata", 0xA6: "Raticate",
    0xA7: "Nidorino", 0xA8: "Nidorina", 0xA9: "Geodude", 0xAA: "Porygon",
    0xAB: "Aerodactyl",
    0xAD: "Magnemite",
    0xB0: "Charmander", 0xB1: "Squirtle", 0xB2: "Charmeleon",
    0xB3: "Wartortle", 0xB4: "Charizard",
    0xB9: "Oddish", 0xBA: "Gloom", 0xBB: "Vileplume", 0xBC: "Bellsprout",
    0xBD: "Weepinbell", 0xBE: "Victreebel",
}


def _species_name(species_id: int) -> str:
    name = _GEN1_SPECIES.get(species_id)
    if name is not None:
        return f"{name}({species_id:03d})"
    return f"UNKNOWN(0x{species_id:02x})"


def _lead_species(session) -> int | None:
    party = session.read_game_state().party
    return party.lead.species if party.lead else None


def _lead_ot_fingerprint(session) -> bytes:
    """Read 11 bytes of the lead's wPartyMonOT slot 0 — the Original
    Trainer name, which changes after a successful trade even when the
    species is identical. Used as an additional "the trade actually
    happened" signal when both peers start with the same species.
    """
    pb = session._pyboy
    try:
        addr = session.symbols.addr_of("wPartyMonOT")
    except Exception:
        return b""
    return bytes(pb.memory[addr + i] for i in range(11))


@dataclass
class Shooter:
    outdir: Path

    def shoot(self, session_label: str, session, stem: str) -> Path:
        """Save ``session``'s current framebuffer to ``stem__<label>.png``.
        Returns the absolute path. Uses PyBoy's built-in
        ``pyboy.screen.image`` which returns a PIL image (color).
        """
        path = self.outdir / f"{stem}__{session_label}.png"
        session._pyboy.screen.image.save(path)
        return path

    def shoot_pair(self, a, b, stem: str) -> list[Path]:
        return [self.shoot("red", a, stem), self.shoot("blue", b, stem)]


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live end-to-end trade demo: pair two Pokemon ROMs, "
        "drive a complete Cable Club trade, save per-phase color PNGs."
    )
    p.add_argument(
        "--versions", default="red,blue",
        help="Comma-separated 'a,b' versions. Default: red,blue.",
    )
    p.add_argument(
        "--outdir", default="walkthrough_link_demo",
        help="Output directory for PNGs. Created if missing. "
             "Default: walkthrough_link_demo.",
    )
    p.add_argument(
        "--venv", choices=["cython", "noncython"], default=None,
        help="Re-exec under .venv-<tag>/Scripts/python.exe if it exists; "
             "otherwise warn and use ambient interp.",
    )
    p.add_argument(
        "--view", action="store_true",
        help="Open SDL2 windows for both peers so the trade is visible live.",
    )
    p.add_argument(
        "--sample-every", type=int, default=0, metavar="N",
        help="If > 0, capture a framebuffer snapshot from both peers every "
             "N driver iterations during all phases into "
             "'<outdir>/timeline/<NNNN>__{red,blue}.png'. Used for visually "
             "verifying navigation when the SDL2 window isn't being watched.",
    )
    p.add_argument(
        "--speed", type=float, default=1.0, metavar="FACTOR",
        help="Emulation speed multiplier; 1.0 = real-time, 0.5 = half speed, "
             "2.0 = double speed. Default 1.0. Use <1 with --view to make "
             "menu navigation (LinkMenu BATTLE/TRADE, mon-select, TRADE "
             "confirmation) easier to follow visually.",
    )
    p.add_argument(
        "--hold-after-s", type=int, default=90, metavar="N",
        help="Seconds to idle both emulators after the trade completes "
             "so you can watch the final state on the live windows. "
             "Default 90. Only applies with --view.",
    )
    p.add_argument(
        "--no-cgb", action="store_true",
        help="Force DMG mode (cgb=False). Loses the Full Color Hack palette "
             "but makes Gen 1 menu/dialog overlays render correctly. Use for "
             "diagnosing whether the CGB+ColorHack combo is masking menus.",
    )
    p.add_argument(
        "--natural", action="store_true",
        help="Drive the trade the way a human would: pause on each key "
             "menu (save prompt, LinkMenu BATTLE/TRADE/CANCEL, mon-select "
             "STATS/TRADE, YES/NO confirmation, 'Take good care!'), and "
             "briefly move the LinkMenu cursor down to BATTLE and back up "
             "to TRADE so you can see the choice. Off by default so "
             "tests/matrix runs stay fast.",
    )
    return p.parse_args()


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

    # Side-by-side on the primary monitor: Red on the left, Blue on the right.
    # Game Boy screen is 160x144; SDL2 default 3x scale is ~480x432 plus
    # window chrome. Spacing ~520px apart keeps both fully visible.
    # Primary monitor is queried at runtime — we can't assume (0,0) spans
    # it on multi-monitor setups (secondary monitors with negative X exist).
    # SDL_VIDEO_WINDOW_POS is unreliable on Windows under those setups too,
    # so we open the windows, then force-move them via Win32 below.
    a = _open_session(version_a, view=args.view, window_pos=(80, 120))
    b = _open_session(version_b, view=args.view, window_pos=(640, 120))

    if args.view:
        # Force both windows onto the primary monitor, side-by-side. The
        # primary monitor on this machine is 2560x1440 at (0,0) — these
        # coords leave the windows comfortably centered and visible.
        if _force_move_pyboy_windows([(200, 300), (1200, 300)]):
            print("[info] moved PyBoy windows to (200,300) and (1200,300) "
                  "on primary monitor", flush=True)
        else:
            print("[warn] could not locate both PyBoy windows to move them; "
                  "they may be on a secondary monitor", flush=True)
        _pyboy_hwnds = _find_pyboy_hwnds()
        if len(_pyboy_hwnds) >= 2:
            print(f"[info] PyBoy window handles: red={_pyboy_hwnds[0]}, "
                  f"blue={_pyboy_hwnds[1]} — natural-mode captures will use "
                  f"Win32 screen-grab so dialogs are included", flush=True)
        else:
            _pyboy_hwnds = []
            print("[warn] could not resolve both PyBoy window handles; "
                  "natural-mode captures will fall back to framebuffer reads",
                  flush=True)
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
    try:
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
            for _ in range(60):   # ~1s — let the dialog render
                a.step(1)
                b.step(1)
            _nat_shot("nat_07_take_good_care")
            for _ in range(120):  # ~2s — remainder of the dwell
                a.step(1)
                b.step(1)
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
                for _ in range(180):  # 3 seconds
                    a.step(1)
                    b.step(1)
                try:
                    paths = shoot.shoot_pair(a, b, tag)
                    for p in paths:
                        print(f"  wrote {p}", flush=True)
                except Exception:
                    pass
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
            for _ in range(hold_s * 60):
                a.step(1)
                b.step(1)

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

        if not trade_happened:
            print(
                "[error] trade did not complete end-to-end. See diag "
                "counters above and the PNGs under the outdir.",
                flush=True,
            )
            return 1
        return 0
    finally:
        try:
            a.close()
        except Exception:
            pass
        try:
            b.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
