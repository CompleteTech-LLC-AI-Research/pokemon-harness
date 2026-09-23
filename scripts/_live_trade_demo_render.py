"""Rendering, screenshot, and Win32 window helpers for
``scripts/live_trade_demo.py``.

Split out of the demo entrypoint (issue #140). Every definition below is
relocated byte-for-byte from the original script so runtime behavior and
the demo's contract tests are unchanged.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


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
    except Exception:  # noqa: BLE001 - optional image dependencies must fail soft
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
    except Exception:  # noqa: BLE001 - optional image dependencies must fail soft
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
