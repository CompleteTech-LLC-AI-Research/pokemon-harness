#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

from array import array
from ctypes import c_void_p
from random import getrandbits

import pyboy
from pyboy.utils import PyBoyException, INTR_VBLANK, INTR_LCDC, FRAME_CYCLES
from pyboy.api.constants import ROWS, COLS, TILES

logger = pyboy.logging.get_logger(__name__)

VIDEO_RAM = 8 * 1024  # 8KB
OBJECT_ATTRIBUTE_MEMORY = 0xA0


def rgb_to_bgr(color):
    a = 0xFF
    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF
    return (a << 24) | (b << 16) | (g << 8) | r


