
    def game_area_dimensions(self, x, y, width, height, follow_scrolling=True):
        """
        If using the generic game wrapper (see `pyboy.PyBoy.game_wrapper`), you can use this to set the section of the
        tilemaps to extract. This will default to the entire tilemap.

        Example:
        ```python
        >>> pyboy.game_wrapper.shape
        (32, 32)
        >>> pyboy.game_area_dimensions(2, 2, 10, 18, False)
        >>> pyboy.game_wrapper.shape
        (10, 18)
        ```

        Args:
            x (int): Offset from top-left corner of the screen
            y (int): Offset from top-left corner of the screen
            width (int): Width of game area
            height (int): Height of game area
            follow_scrolling (bool): Whether to follow the scrolling of [SCX and SCY](https://gbdev.io/pandocs/Scrolling.html)
        """
        self.game_wrapper._set_dimensions(x, y, width, height, follow_scrolling=True)

    def game_area_collision(self):
        """
        Some game wrappers define a collision map. Check if your game wrapper has this feature implemented: `pyboy.plugins`.

        The output will be unique for each game wrapper.

        Example:
        ```python
        >>> # This example show nothing, but a supported game will
        >>> pyboy.game_area_collision()
        array([[0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0],
               [0, 0, 0, 0, 0, 0, 0, 0, 0]], dtype=uint32)

        ```

        Returns
        -------
        memoryview:
            Simplified 2-dimensional memoryview of the collision map
        """
        return self.game_wrapper.game_area_collision()

    def game_area_mapping(self, mapping, sprite_offset=0):
        """
        Define custom mappings for tile identifiers in the game area.

        Example of custom mapping:
        ```python
        >>> from pyboy.api.constants import TILES
        >>> mapping = [x for x in range(TILES)] # 1:1 mapping of 384 tiles
        >>> mapping[0] = 0 # Map tile identifier 0 -> 0
        >>> mapping[1] = 0 # Map tile identifier 1 -> 0
        >>> mapping[2] = 0 # Map tile identifier 2 -> 0
        >>> mapping[3] = 0 # Map tile identifier 3 -> 0
        >>> pyboy.game_area_mapping(mapping, 1000)

        ```

        Some game wrappers will supply mappings as well. See the specific documentation for your game wrapper:
        `pyboy.plugins`.
        ```python
        >>> pyboy.game_area_mapping(pyboy.game_wrapper.mapping_one_to_one, 0)

        ```

        Args:
            mapping (list or ndarray): list of 384 (DMG) or 768 (CGB) tile mappings. Use `None` to reset to a 1:1 mapping.
            sprite_offest (int): Optional offset add to tile id for sprites
        """

        if mapping is None:
            mapping = [x for x in range(TILES_CGB)]

        assert isinstance(sprite_offset, int)
        assert isinstance(mapping, (np.ndarray, list))
        assert len(mapping) == TILES or len(mapping) == TILES_CGB

        self.game_wrapper.game_area_mapping(mapping, sprite_offset)

    def game_area(self):
        """
        Use this method to get a matrix of the "game area" of the screen. This view is simplified to be perfect for
        machine learning applications.

        The layout will vary from game to game. Below is an example from Tetris:

        Example:
        ```python
        >>> pyboy.game_area()
        array([[ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47, 130, 130,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47, 130, 130,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47],
               [ 47,  47,  47,  47,  47,  47,  47,  47,  47,  47]], dtype=uint32)

        ```

        If you want a "compressed", "minimal" or raw mapping of tiles, you can change the mapping using
        `pyboy.PyBoy.game_area_mapping`. Either you'll have to supply your own mapping, or you can find one
        that is built-in with the game wrapper plugin for your game. See `pyboy.PyBoy.game_area_mapping`.

        Returns
        -------
        memoryview:
            Simplified 2-dimensional memoryview of the screen
        """

        return self.game_wrapper.game_area()

    def set_color_palette(self, palette):
        """
        Set the color palette of DMG games.

        Example:
        ```python
        >>> pyboy.set_color_palette((0x9BBC0F, 0x8BAC0F, 0x306230, 0x0F380F))
        ```
        """
        if self.mb.cgb:
            raise PyBoyInvalidOperationException("Palette change is only available in DMG mode")

        for palette_reg in [self.mb.lcd.BGP, self.mb.lcd.OBP0, self.mb.lcd.OBP1]:
            palette_reg.set_palette_colors(palette)

    def _serial(self):
        """
        Provides all data that has been sent over the serial port since last call to this function.

        Returns
        -------
        str :
            Buffer data
        """
        return self.mb.getserial()

    def set_emulation_speed(self, target_speed):
        """
        Set the target emulation speed. It might loose accuracy of keeping the exact speed, when using a high
        `target_speed`.

        The speed is defined as a multiple of real-time. I.e `target_speed=2` is double speed.

        A `target_speed` of `0` means unlimited. I.e. fastest possible execution.

        Due to backwards compatibility, the null window starts at unlimited speed (i.e. `target_speed=0`), while
        others start at realtime (i.e. `target_speed=1`).

        Example:
        ```python
        >>> pyboy.tick() # Delays 16.67ms
        True
        >>> pyboy.set_emulation_speed(0) # Disable limit
        >>> pyboy.tick() # As fast as possible
        True
        ```

        Args:
            target_speed (int): Target emulation speed as multiplier of real-time.
        """
        if target_speed > 5:
            logger.warning("The emulation speed might not be accurate when speed-target is higher than 5")
        self.target_emulationspeed = target_speed

    def _load_symbols(self):
        if self.rom_symbols:
            return self.rom_symbols
        gamerom_paths = []
        if self.gamerom:
            gamerom_file_no_ext, rom_ext = os.path.splitext(self.gamerom)
            gamerom_paths = [gamerom_file_no_ext + ".sym", gamerom_file_no_ext + rom_ext + ".sym"]
        for sym_path in [self.symbols_file] + gamerom_paths:
            if sym_path and os.path.isfile(sym_path):
                logger.info("Loading symbol file: %s", sym_path)
                group = "labels"
                with open(sym_path) as f:
                    for _line in f.readlines():
                        line = _line.strip()
                        if line == "":
                            continue
                        elif line.startswith(";"):
                            continue
                        elif line.startswith("["):
                            # Start of key group
                            group = line.strip()[1:-1]
                            # [labels]
                            # [definitions]
                            continue

                        if group == "labels":
                            try:
                                bank, addr, sym_label = re.split(":| ", line.strip())
                                bank = int(bank, 16)
                                addr = int(addr, 16)
                                if bank not in self.rom_symbols:
                                    self.rom_symbols[bank] = {}

                                if addr not in self.rom_symbols[bank]:
                                    self.rom_symbols[bank][addr] = []

                                self.rom_symbols[bank][addr].append(sym_label)
                                self.rom_symbols_inverse[sym_label] = (bank, addr)
                            except ValueError:
                                logger.warning("Skipping .sym line: %s", line.strip())
                        elif group == "definitions":
                            pass
                        else:
                            logger.warning("Invalid group. Skipping .sym line: %s", line.strip())
        return self.rom_symbols

    def _lookup_symbol(self, symbol):
        bank_addr = self.rom_symbols_inverse.get(symbol)
        if bank_addr is None:
            raise ValueError("Symbol not found: %s" % symbol)
        return bank_addr

    def symbol_lookup(self, symbol):
        """
        Look up a specific symbol from provided symbols file.

        This can be useful in combination with `PyBoy.memory` or even `PyBoy.hook_register`.

        See `PyBoy.hook_register` for how to load symbol into PyBoy.

        Example:
        ```python
        >>> # Directly
        >>> pyboy.memory[pyboy.symbol_lookup("Tileset")]
        0
        >>> # By bank and address
        >>> bank, addr = pyboy.symbol_lookup("Tileset")
        >>> pyboy.memory[bank, addr]
        0
        >>> pyboy.memory[bank, addr:addr+10]
        [0, 0, 0, 0, 0, 0, 102, 102, 102, 102]

        ```
        Returns
        -------
        (int, int):
            ROM/RAM bank, address
        """
        return self._lookup_symbol(symbol)

    def hook_register(self, bank, addr, callback, context):
        """
        Adds a hook into a specific bank and memory address.
        When the Game Boy executes this address, the provided callback function will be called.

        By providing an object as `context`, you can later get access to information inside and outside of the callback.

        Example:
        ```python
        >>> context = "Hello from hook"
        >>> def my_callback(context):
        ...     print(context)
        >>> pyboy.hook_register(0, 0x100, my_callback, context)
        >>> pyboy.tick(70)
        Hello from hook
        True

        ```

        If a symbol file is loaded, this function can also automatically resolve a bank and address from a symbol. To
        enable this, you'll need to place a `.sym` file next to your ROM, or provide it using:
        `PyBoy(..., symbols="game_rom.gb.sym")`.

        Then provide `None` for `bank` and the symbol for `addr` to trigger the automatic lookup.

        Example:
        ```python
        >>> # Continued example above
        >>> pyboy.hook_register(None, "Main.move", lambda x: print(x), "Hello from hook2")
        >>> pyboy.tick(81)
        Hello from hook2
        True

        ```

        **NOTE**:

        Don't register hooks to something that isn't executable (graphics data etc.). This will cause your game to show
        weird behavior or crash. Hooks are installed by replacing the instruction at the bank and address with a special
        opcode (`0xDB`). If the address is read by the game instead of executed as code, this value will be read instead.

        Args:
            bank (int or None): ROM or RAM bank (None for symbol lookup)
            addr (int or str): Address in the Game Boy's address space (str for symbol lookup)
            callback (func): A function which takes `context` as argument
            context (object): Argument to pass to callback when hook is called
        """
        if bank is None and isinstance(addr, str):
            bank, addr = self._lookup_symbol(addr)

        opcode = self.memory[bank, addr]
        if opcode == OPCODE_BRK:
            raise ValueError("Hook already registered for this bank and address.")
        self.mb.breakpoint_add(bank, addr)
        bank_addr_opcode = (bank & 0xFF) << 24 | (addr & 0xFFFF) << 8 | (opcode & 0xFF)
        logger.debug("Adding hook for opcode %08x", bank_addr_opcode)
        self._hooks[bank_addr_opcode] = (callback, context)

    def hook_deregister(self, bank, addr):
        """
        Remove a previously registered hook from a specific bank and memory address.

        Example:
        ```python
        >>> context = "Hello from hook"
        >>> def my_callback(context):
        ...     print(context)
        >>> pyboy.hook_register(0, 0x2000, my_callback, context)
        >>> pyboy.hook_deregister(0, 0x2000)

        ```

        This function can also deregister a hook based on a symbol. See `PyBoy.hook_register` for details.

        Example:
        ```python
        >>> pyboy.hook_register(None, "Main", lambda x: print(x), "Hello from hook")
        >>> pyboy.hook_deregister(None, "Main")

        ```

        Args:
            bank (int or None): ROM or RAM bank (None for symbol lookup)
            addr (int or str): Address in the Game Boy's address space (str for symbol lookup)
        """
        if bank is None and isinstance(addr, str):
            bank, addr = self._lookup_symbol(addr)

        breakpoint_meta = self.mb.breakpoint_find(bank, addr)
        if not breakpoint_meta:
            raise ValueError("Breakpoint not found for bank and addr")
        _, _, opcode = breakpoint_meta

        self.mb.breakpoint_remove(bank, addr)
        bank_addr_opcode = (bank & 0xFF) << 24 | (addr & 0xFFFF) << 8 | (opcode & 0xFF)
        self._hooks.pop(bank_addr_opcode)

    def _handle_hooks(self):
        if _handler := self._hooks.get(self.mb.breakpoint_waiting):
            (callback, context) = _handler
            callback(context)
            return True
        return False

    def get_sprite(self, sprite_index):
        """
        Provides a `pyboy.api.sprite.Sprite` object, which makes the OAM data more presentable. The given index
        corresponds to index of the sprite in the "Object Attribute Memory" (OAM).

        The Game Boy supports 40 sprites in total. Read more details about it, in the [Pan
        Docs](http://bgb.bircd.org/pandocs.htm).

        ```python
        >>> s = pyboy.get_sprite(12)
        >>> s
        Sprite [12]: Position: (-8, -16), Shape: (8, 8), Tiles: (Tile: 0), On screen: False
        >>> s.on_screen
        False
        >>> s.tiles
        [Tile: 0]

        ```

        Args:
            index (int): Sprite index from 0 to 39.
        Returns
        -------
        `pyboy.api.sprite.Sprite`:
            Sprite corresponding to the given index.
        """
        return Sprite(self.mb, sprite_index)

    def get_sprite_by_tile_identifier(self, tile_identifiers, on_screen=True):
        """
        Provided a list of tile identifiers, this function will find all occurrences of sprites using the tile
        identifiers and return the sprite indexes where each identifier is found. Use the sprite indexes in the
        `pyboy.PyBoy.get_sprite` function to get a `pyboy.api.sprite.Sprite` object.

        Example:
        ```python
        >>> print(pyboy.get_sprite_by_tile_identifier([43, 123]))
        [[0, 2, 4], []]

        ```

        Meaning, that tile identifier `43` is found at the sprite indexes: 0, 2, and 4, while tile identifier
        `123` was not found anywhere.

        Args:
            identifiers (list): List of tile identifiers (int)
            on_screen (bool): Require that the matched sprite is on screen

        Returns
        -------
        list:
            list of sprite matches for every tile identifier in the input
        """

        matches = []
        for i in tile_identifiers:
            match = []
            for s in range(constants.SPRITES):
                sprite = Sprite(self.mb, s)
                for t in sprite.tiles:
                    if t.tile_identifier == i and (not on_screen or (on_screen and sprite.on_screen)):
                        match.append(s)
            matches.append(match)
        return matches

    def get_tile(self, identifier):
        """
        The Game Boy can have 384 tiles loaded in memory at once (768 for Game Boy Color). Use this method to get a
        `pyboy.api.tile.Tile`-object for given identifier.

        The identifier is a PyBoy construct, which unifies two different scopes of indexes in the Game Boy hardware. See
        the `pyboy.api.tile.Tile` object for more information.

        Example:
        ```python
        >>> t = pyboy.get_tile(2)
        >>> t
        Tile: 2
        >>> t.shape
        (8, 8)

        ```

        Returns
        -------
        `pyboy.api.tile.Tile`:
            A Tile object for the given identifier.
        """
        return Tile(self.mb, identifier=identifier)

    def rtc_lock_experimental(self, enable):
        """
        **WARN: This is an experimental API and is subject to change.**

        Lock the Real Time Clock (RTC) of a supporting cartridge. It might be advantageous to lock the RTC when training
        an AI in games that use it to change behavior (i.e. day and night).

        The first time the game is turned on, an `.rtc` file is created with the current time. This is the epoch for the
        RTC. When using `rtc_lock_experimental`, the RTC will always report this point in time. If you let the game
        progress first, before using `rtc_lock_experimental`, the internal clock will move backwards and might corrupt
        the game.

        Example:
        ```python
        >>> pyboy = PyBoy('game_rom.gb')
        >>> pyboy.rtc_lock_experimental(True) # RTC will not progress
        ```

        **WARN: This is an experimental API and is subject to change.**

        Args:
            enable (bool): True to lock RTC, False to operate normally
        """
        if self.mb.cartridge.rtc_enabled:
            self.mb.cartridge.rtc.timelock = enable
        else:
            raise PyBoyException("There's no RTC for this cartridge type")

    def _cycles(self):
        return self.mb.cpu.cycles
