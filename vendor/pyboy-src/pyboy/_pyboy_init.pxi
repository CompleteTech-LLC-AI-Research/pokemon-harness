#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""
The core module of the emulator
"""

import heapq
import os
import re
import time
from pathlib import Path

import numpy as np
from itertools import cycle

from pyboy.api.constants import TILES, TILES_CGB
from pyboy.api.gameshark import GameShark
from pyboy.api.memory_scanner import MemoryScanner
from pyboy.api.screen import Screen
from pyboy.api.sound import Sound
from pyboy.api.tilemap import TileMap
from pyboy.logging import get_logger
from pyboy.logging import log_level as _log_level
from pyboy.plugins.manager import PluginManager, parser_arguments
from pyboy.utils import (
    IntIOWrapper,
    PyBoyException,
    PyBoyInvalidInputException,
    PyBoyInvalidOperationException,
    PyBoyOutOfBoundsException,
    WindowEvent,
    cython_compiled,
    OPCODE_BRK,
)

try:
    import cython
except ImportError:

    class _mock:
        def __enter__(self):
            pass

        def __exit__(self, *args):
            pass

    exec(
        """
class cython:
    gil = _mock()
    nogil = _mock()
""",
        globals(),
        locals(),
    )

from .api import Sprite, Tile, constants
from .core.mb import Motherboard

logger = get_logger(__name__)

SPF = 1 / 60.0  # inverse FPS (frame-per-second)

DMG_PALETTES = {
    "SameBoy DMG": (0xC6DE8C, 0x84A563, 0x396139, 0x081810),
    "Classic Green": (0x9BBC0F, 0x8BAC0F, 0x306230, 0x0F380F),
    "Parchment": (0xE0DBCD, 0xA89F94, 0x706B64, 0x2B2B26),
    "Mossy": (0xC4CFA1, 0x8B956D, 0x4D533C, 0x1F1F1C),
    "Grey": (0xFFFFFF, 0x999999, 0x555555, 0x000000),
}

defaults = {
    "color_palette": DMG_PALETTES["Grey"],
    "cgb_color_palette": (
        (0xFFFFFF, 0x7BFF31, 0x0063C5, 0x000000),
        (0xFFFFFF, 0xFF8484, 0x943A3A, 0x000000),
        (0xFFFFFF, 0xFF8484, 0x943A3A, 0x000000),
    ),
    "scale": 3,
    "window": "SDL2",
    "log_level": "WARNING",
}


class PyBoy:
    def __init__(
        self,
        gamerom,
        *,
        ram_file=None,
        rtc_file=None,
        window=defaults["window"],
        scale=defaults["scale"],
        symbols=None,
        bootrom=None,
        sound_volume=100,
        sound_emulated=True,
        sound_sample_rate=None,
        cgb=None,
        gameshark=None,
        no_input=False,
        log_level=defaults["log_level"],
        color_palette=defaults["color_palette"],
        cgb_color_palette=defaults["cgb_color_palette"],
        title_status=False,
        **kwargs,
    ):
        """
        PyBoy is loadable as an object in Python. This means, it can be initialized from another script, and be
        controlled and probed by the script. It is supported to spawn multiple emulators, just instantiate the class
        multiple times.

        A range of methods are exposed, which should allow for complete control of the emulator. Please open an issue on
        GitHub, if other methods are needed for your projects. Take a look at the files in `examples/` for a crude
        "bots", which interact with the game.

        Only the `gamerom` argument is required.

        If `gamerom` is a filepath and `ram_file` and `rtc_file` are not provided, filepaths will be determined
        automatically next to the game ROM. If this is not wanted, provide a file-like object as argument.

        Example:
        ```python
        >>> pyboy = PyBoy('game_rom.gb')
        >>> for _ in range(60): # Use 'while True:' for infinite
        ...     pyboy.tick()
        True...
        >>> pyboy.stop()

        ```

        Args:
            gamerom (str or file-like object): Filepath to a game-ROM for Game Boy or Game Boy Color.

        Kwargs:
            * ram_file (file-like object):
            * rtc_file (file-like object):
            * window (str): "SDL2", "OpenGL", "GLFW", or "null"
            * scale (int): Window scale factor. Doesn't apply to API.
            * symbols (str): Filepath to a .sym file to use. If unsure, specify `None`.
            * bootrom (str): Filepath to a boot-ROM to use. If unsure, specify `None`.
            * sound_volume (int): Set sound volume in percent (0-100).
            * sound_emulated (bool): Disables sound emulation (not just muted!).
            * sound_sample_rate (int): Set sound sample rate. Has to be divisible in 60.
            * cgb (bool): Forcing Game Boy Color mode.
            * gameshark (str): GameShark codes to apply.
            * no_input (bool): Disable all user-input (mostly for autonomous testing)
            * log_level (str): "CRITICAL", "ERROR", "WARNING", "INFO" or "DEBUG"
            * color_palette (tuple): Specify the color palette to use for rendering.
            * cgb_color_palette (list of tuple): Specify the color palette to use for rendering in CGB-mode for non-color games.
            * title_status (bool): Enable performance status in window title

        ## Plugin kwargs:
        * autopause (bool): Enable auto-pausing when window looses focus [plugin: AutoPause]
        * breakpoints (str): Add breakpoints on start-up (internal use) [plugin: DebugPrompt]
        * record_input (bool): Record user input and save to a file (internal use) [plugin: RecordReplay]
        * rewind (bool): Enable rewind function [plugin: Rewind]

        Other keyword arguments may exist for plugins that are not listed here. They can be viewed by running `pyboy --help` in the terminal.
        """

        self.initialized = False
        self.no_input = no_input

        _log_level(log_level)

        logger.debug("Cython compilation status: %s", cython_compiled)

        if "bootrom_file" in kwargs:
            logger.error(
                "Deprecated use of 'bootrom_file'. Use 'bootrom' keyword argument instead. https://github.com/Baekalfen/PyBoy/wiki/Migrating-from-v1.x.x-to-v2.0.0"
            )
            bootrom = kwargs.pop("bootrom_file")

        if "window_type" in kwargs:
            logger.error(
                "Deprecated use of 'window_type'. Use 'window' keyword argument instead. https://github.com/Baekalfen/PyBoy/wiki/Migrating-from-v1.x.x-to-v2.0.0"
            )
            window = kwargs.pop("window_type")

        if window not in ["SDL2", "OpenGL", "GLFW", "null", "headless", "dummy"]:
            raise KeyError(f'Unknown window type: {window}. Use "SDL2", "OpenGL", "GLFW", or "null"')

        kwargs["window"] = window
        kwargs["scale"] = scale
        randomize = kwargs.pop("randomize", False)  # Undocumented feature

        for k, v in defaults.items():
            if k not in kwargs:
                kwargs[k] = v

        if gamerom is None:
            raise FileNotFoundError("No game ROM provided!")

        gamerom_file = None
        self.gamerom = None
        gamerom_file_handled = False
        ram_file_handled = False
        rtc_file_handled = False
        if isinstance(gamerom, (str, Path)):
            self.gamerom = str(gamerom)
            try:
                gamerom_file = open(self.gamerom, "rb")
                gamerom_file_handled = True

                if ram_file is None:
                    try:
                        ram_file = open(self.gamerom + ".ram", "rb")
                        ram_file_handled = True
                    except FileNotFoundError:
                        pass

                if rtc_file is None:
                    try:
                        rtc_file = open(self.gamerom + ".rtc", "rb")
                        rtc_file_handled = True
                    except FileNotFoundError:
                        pass

            except FileNotFoundError:
                raise FileNotFoundError(f"ROM file {gamerom} was not found!")
            except Exception:
                raise
        elif hasattr(gamerom, "read"):
            gamerom_file = gamerom
        else:
            raise PyBoyInvalidInputException("Provided game ROM cannot be used. Expected str, Path or file-like object")

        self.rom_symbols = {}
        self.rom_symbols_inverse = {}
        if symbols is not None:
            if not os.path.isfile(symbols):
                raise FileNotFoundError(f"Symbols file {symbols} was not found!")
        self.symbols_file = symbols
        self._load_symbols()

        # Backwards compatibility
        # Setting volume if True, but we don't disable emulation if False/None.
        if kwargs.pop("sound", None):
            sound_volume = 100
            logger.error(
                'Deprecated use of "sound" on PyBoy constructor. Use "sound_volume" or "sound_emulated" instead.'
            )

        if not (0 <= sound_volume <= 100):
            raise PyBoyInvalidInputException("Sound volume has to be between 0 and 100.")

        try:
            self.mb = Motherboard(
                gamerom_file,
                ram_file,
                rtc_file,
                bootrom,
                color_palette,
                cgb_color_palette,
                sound_volume,
                sound_emulated,
                sound_sample_rate,
                cgb,
                randomize=randomize,
            )
        finally:
            # Close the files we opened -- i.e. not passed from args
            if gamerom_file_handled:
                gamerom_file.close()

            if ram_file_handled:
                ram_file.close()

            if rtc_file_handled:
                rtc_file.close()

        # Active palette detection
        self._palette_cycle = cycle(DMG_PALETTES.items())

        # Validate all kwargs
        plugin_manager_keywords = []
        for x in parser_arguments():
            if not x:
                continue
            plugin_manager_keywords.extend(z.strip("-").replace("-", "_") for y in x for z in y[:-1])

        for k, v in kwargs.items():
            if k not in defaults and k not in plugin_manager_keywords:
                logger.error("Unknown keyword argument: %s", k)
                raise KeyError(f"Unknown keyword argument: {k}")

        # Performance measures
        self.avg_tick = 0
        self.avg_emu = 0

        # Absolute frame count of the emulation
        self.frame_count = 0

        self.set_emulation_speed(1)
        self.paused = False
        self.events = []
        self.queued_input = []
        self.quitting = False
        self.stopped = False
        self.window_title = ""
        self.title_status = title_status

        ###################
        # API attributes
        self.screen = Screen(self.mb)
        """
        Use this method to get a `pyboy.api.screen.Screen` object. This can be used to get the screen buffer in
        a variety of formats.

        It's also here you can find the screen position (SCX, SCY, WX, WY) for each scan line in the screen buffer. See
        `pyboy.api.screen.Screen.tilemap_position_list` for more information.

        Example:
        ```python
        >>> pyboy.screen.image.show()
        >>> pyboy.screen.ndarray.shape
        (144, 160, 4)
        >>> pyboy.screen.raw_buffer_format
        'RGBA'

        ```

        NOTE: See `PyBoy.sound` to get the sound buffer.

        Returns
        -------
        `pyboy.api.screen.Screen`:
            A Screen object with helper functions for reading the screen buffer.
        """
        self.sound = Sound(self.mb)
        """
        Use this method to get a `pyboy.api.sound.Sound` object. This can be used to get the sound buffer of the
        latest screen frame (see `PyBoy.screen`).

        Example:
        ```python
        >>> pyboy.sound.ndarray.shape # 801 samples, 2 channels (stereo)
        (801, 2)
        >>> pyboy.sound.ndarray
        array([[0, 0],
               [0, 0],
               ...
               [0, 0],
               [0, 0]], dtype=int8)
        ```

        Returns
        -------
        `pyboy.api.sound.Sound`:
            A Sound object with helper functions for accessing the sound buffer.
        """
        self.memory = PyBoyMemoryView(self.mb)
        """
        Provides a `pyboy.PyBoyMemoryView` object for reading and writing the memory space of the Game Boy.

        For a more comprehensive description, see the `pyboy.PyBoyMemoryView` class.

        Example:
        ```python
        >>> pyboy.memory[0x0000:0x0010] # Read 16 bytes from ROM bank 0
        [49, 254, 255, 33, 0, 128, 175, 34, 124, 254, 160, 32, 249, 6, 48, 33]
        >>> pyboy.memory[1, 0x2000] = 12 # Override address 0x2000 from ROM bank 1 with the value 12
        >>> pyboy.memory[0xC000] = 1 # Write to address 0xC000 with value 1
        ```

        """

        self.register_file = PyBoyRegisterFile(self.mb.cpu)
        """
        Provides a `pyboy.PyBoyRegisterFile` object for reading and writing the CPU registers of the Game Boy.

        The register file is best used inside the callback of a hook, as `PyBoy.tick` doesn't return at a specific point.

        For a more comprehensive description, see the `pyboy.PyBoyRegisterFile` class.

        Example:
        ```python
        >>> def my_callback(register_file):
        ...     print("Register A:", register_file.A)
        >>> pyboy.hook_register(0, 0x100, my_callback, pyboy.register_file)
        >>> pyboy.tick(70)
        Register A: 1
        True
        ```
        """

        self.memory_scanner = MemoryScanner(self)
        """
        Provides a `pyboy.api.memory_scanner.MemoryScanner` object for locating addresses of interest in the memory space
        of the Game Boy. This might require some trial and error. Values can be represented in memory in surprising ways.

        _Open an issue on GitHub if you need finer control, and we will take a look at it._

        Example:
        ```python
        >>> current_score = 4 # You write current score in game
        >>> pyboy.memory_scanner.scan_memory(current_score, start_addr=0xC000, end_addr=0xDFFF)
        []
        >>> for _ in range(175):
        ...     pyboy.tick(1, True) # Progress the game to change score
        True...
        >>> current_score = 8 # You write the new score in game
        >>> from pyboy.api.memory_scanner import DynamicComparisonType
        >>> addresses = pyboy.memory_scanner.rescan_memory(current_score, DynamicComparisonType.MATCH)
        >>> print(addresses) # If repeated enough, only one address will remain
        []

        ```
        """

        self.tilemap_background = TileMap(self, self.mb, "BACKGROUND")
        """
        The Game Boy uses two tile maps at the same time to draw graphics on the screen. This method will provide one
        for the _background_ tiles. The game chooses whether it wants to use the low or the high tilemap.

        Read more details about it, in the [Pan Docs](https://gbdev.io/pandocs/Tile_Maps.html).

        Example:
        ```
        >>> pyboy.tilemap_background[8,8]
        1
        >>> pyboy.tilemap_background[7:12,8]
        [0, 1, 0, 1, 0]
        >>> pyboy.tilemap_background[7:12,8:11]
        [[0, 1, 0, 1, 0], [0, 2, 3, 4, 5], [0, 0, 6, 0, 0]]

        ```

        Returns
        -------
        `pyboy.api.tilemap.TileMap`:
            A TileMap object for the tile map.
        """

        self.tilemap_window = TileMap(self, self.mb, "WINDOW")
        """
        The Game Boy uses two tile maps at the same time to draw graphics on the screen. This method will provide one
        for the _window_ tiles. The game chooses whether it wants to use the low or the high tilemap.

        Read more details about it, in the [Pan Docs](https://gbdev.io/pandocs/Tile_Maps.html).

        Example:
        ```
        >>> pyboy.tilemap_window[8,8]
        1
        >>> pyboy.tilemap_window[7:12,8]
        [0, 1, 0, 1, 0]
        >>> pyboy.tilemap_window[7:12,8:11]
        [[0, 1, 0, 1, 0], [0, 2, 3, 4, 5], [0, 0, 6, 0, 0]]

        ```

        Returns
        -------
        `pyboy.api.tilemap.TileMap`:
            A TileMap object for the tile map.
        """

        self.cartridge_title = self.mb.cartridge.gamename
        """
        The title stored on the currently loaded cartridge ROM. The title is all upper-case ASCII and may
        have been truncated to 11 characters.

        Example:
        ```python
        >>> pyboy.cartridge_title # Title of PyBoy's default ROM
        'DEFAULT-ROM'

        ```

        Returns
        -------
        str :
            Game title
        """

        self._hooks = {}

        self._plugin_manager = PluginManager(self, self.mb, kwargs)
        """
        Returns
        -------
        `pyboy.plugins.manager.PluginManager`:
            Object for handling plugins in PyBoy
        """

        self.game_wrapper = self._plugin_manager.gamewrapper()
        """
        Provides an instance of a game-specific or generic wrapper. The game is detected by the cartridge's hard-coded
        game title (see `pyboy.PyBoy.cartridge_title`).

        If a game-specific wrapper is not found, a generic wrapper will be returned.

        To get more information, find the wrapper for your game in `pyboy.plugins`.

        Example:
        ```python
        >>> pyboy.game_wrapper.start_game()
        >>> pyboy.game_wrapper.reset_game()

        ```

        Returns
        -------
        `pyboy.plugins.base_plugin.PyBoyGameWrapper`:
            A game-specific wrapper object.
        """

        self.gameshark = GameShark(self.memory)
        """
        Provides an instance of the `pyboy.api.gameshark.GameShark` handler. This allows you to inject GameShark-based cheat codes.

        Example:
        ```python
        >>> pyboy.gameshark.add("010138CD")
        >>> pyboy.gameshark.remove("010138CD")
        >>> pyboy.gameshark.clear_all()
        ```
        """
        if gameshark:
            for code in gameshark.split(","):
                self.gameshark.add(code.strip())

        self.initialized = True
