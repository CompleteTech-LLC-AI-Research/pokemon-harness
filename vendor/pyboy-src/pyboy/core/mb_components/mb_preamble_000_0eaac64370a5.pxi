#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

from array import array

import pyboy
import cython
from pyboy.utils import (
    STATE_VERSION,
    PyBoyException,
    PyBoyOutOfBoundsException,
    PyBoyInvalidOperationException,
    INTR_TIMER,
    INTR_SERIAL,
    INTR_HIGHTOLOW,
    OPCODE_BRK,
    MAX_CYCLES,
)

from . import bootrom, cartridge, cpu, interaction, lcd, ram, serial, sound, timer

logger = pyboy.logging.get_logger(__name__)
PHYSICAL_CLOCK_MAX = 0xFFFFFFFFFFFFFFFF


def _serial_check_error(serial_device):
    callback = getattr(serial_device, "check_error", None)
    if callback is not None:
        callback()


def _serial_check_execution_allowed(serial_device):
    callback = getattr(serial_device, "check_execution_allowed", None)
    if callback is not None:
        callback()


