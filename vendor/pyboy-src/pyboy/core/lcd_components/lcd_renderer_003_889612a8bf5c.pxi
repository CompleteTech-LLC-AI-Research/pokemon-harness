class Renderer:
    def __init__(self, lcd, cgb):
        self.lcd = lcd
        self.cgb = cgb
        self.color_format = "RGBA"

        self.buffer_dims = (ROWS, COLS)

        # Init buffers as white
        self._screenbuffer_raw = array("B", [0x00] * (ROWS * COLS * 4))
        self._screenbuffer_attributes_raw = array("B", [0x00] * (ROWS * COLS))
        self._tilecache_raw = array("B", [0x00] * (TILES * 8 * 8 * 2))
        self._spritecache_raw = array("B", [0x00] * (TILES * 8 * 8 * 2))
        self.sprites_to_render = array("i", [0] * 10)

        # Allocate both cache 0 and 1. Even on DMG, where tilecache[0] is only used.
        self._tilecache_state = array("B", [0] * TILES * 2)
        self._spritecache_state = array("B", [0] * TILES * 2)
        self.clear_cache()

        self._screenbuffer = memoryview(self._screenbuffer_raw).cast("I", shape=(ROWS, COLS))
        self._screenbuffer_attributes = memoryview(self._screenbuffer_attributes_raw).cast("B", shape=(ROWS, COLS))
        self._tilecache = memoryview(self._tilecache_raw).cast("B", shape=(2, TILES * 8, 8))
        self._tilecache_64 = memoryview(self._tilecache_raw).cast(
            "Q",
            shape=(
                2,
                TILES * 8,
            ),
        )

        # The look-up table only stored 4 bits from each byte, packed into a single byte
        self.colorcode_table = array("I", [0x00000000] * (0x100))  # Should be "L"!?
        """Convert 2 bytes into color code at a given offset.

        The colors are 2 bit and are found like this:

        Color of the first pixel is 0b10
        | Color of the second pixel is 0b01
        v v
        1 0 0 1 0 0 0 1 <- byte1
        0 1 1 1 1 1 0 0 <- byte2
        """
        for byte in range(0x100):
            byte1 = byte & 0xF
            byte2 = (byte >> 4) & 0xF
            v = 0
            for offset in range(4):
                t = (((byte2 >> (offset)) & 0b1) << 1) | ((byte1 >> (offset)) & 0b1)
                assert t < 4
                v |= t << (8 * (3 - offset))  # Store them in little-endian
            self.colorcode_table[byte] = v

        # OBP0 and OBP1 palette
        self._spritecache = memoryview(self._spritecache_raw).cast("B", shape=(2, TILES * 8, 8))
        self._spritecache_64 = memoryview(self._spritecache_raw).cast("Q", shape=(2, TILES * 8))

        self._screenbuffer_ptr = c_void_p(self._screenbuffer_raw.buffer_info()[0])

        self.ly_window = 0

        # WY has a strange behavior described at:
        # https://gbdev.io/pandocs/Scrolling.html#window
        self.wy_activated_frame = False

    def scanline(self, y):
        if self.lcd.disable_renderer:
            return

        bx, by = self.lcd.getviewport()
        wx, wy = self.lcd.getwindowpos()

        x = 0
        if self.lcd._LCDC.window_enable and self.wy_activated_frame and wy <= y and wx < COLS:
            # Window has it's own internal line counter. It's only incremented whenever the window is drawing something on the screen.
            self.ly_window += 1

            # Before window
            if wx > x:
                x += self.scanline_background(y, x, bx, by, wx, self.lcd)

            # Window hit
            self.scanline_window(y, x, wx, wy, COLS - x, self.lcd)
        elif self.lcd._LCDC.background_enable:
            # No window
            self.scanline_background(y, x, bx, by, COLS, self.lcd)
        else:
            self.scanline_blank(y, x, COLS, self.lcd)

        if y == 143:
            # Reset at the end of a frame. We set it to -1, so it will be 0 after the first increment
            self.ly_window = -1

    def _get_tile(self, y, x, offset, lcd):
        tile_addr = offset + y // 8 * 32 % 0x400 + x // 8 % 32
        tile = lcd.VRAM0[tile_addr]

        # If using signed tile indices, modify index
        if not lcd._LCDC.tiledata_select:
            # (x ^ 0x80 - 128) to convert to signed, then
            # add 256 for offset (reduces to + 128)
            tile = (tile ^ 0x80) + 128

        yy = 8 * tile + y % 8
        return tile, yy, tile_addr

    def _pixel(self, cache_index, pixel, x, y, xx, yy, bg_priority_apply):
        col0 = (self._tilecache[cache_index, yy, xx] == 0) & 1
        self._screenbuffer[y, x] = pixel
        # COL0_FLAG is 1
        self._screenbuffer_attributes[y, x] = bg_priority_apply | col0

    def scanline_window(self, y, _x, wx, wy, cols, lcd):
        for x in range(_x, _x + cols):
            xx = (x - wx) % 8
            if xx == 0 or x == _x:
                wt, yy, _ = self._get_tile(self.ly_window, x - wx, lcd._LCDC.windowmap_offset, lcd)
                self.update_tilecache(0, lcd, wt, 0)

            pixel = lcd.BGP.getcolor(self._tilecache[0, yy, xx])
            self._pixel(0, pixel, x, y, xx, yy, 0)
        return cols

    def scanline_background(self, y, _x, bx, by, cols, lcd):
        for x in range(_x, _x + cols):
            # bx mask used for the half tile at the left side when scrolling
            b_xx = (x + (bx & 0b111)) % 8
            if b_xx == 0 or x == 0:
                bt, b_yy, _ = self._get_tile(y + by, x + bx, lcd._LCDC.backgroundmap_offset, lcd)
                self.update_tilecache(0, lcd, bt, 0)

            xx = b_xx
            yy = b_yy

            pixel = lcd.BGP.getcolor(self._tilecache[0, yy, xx])
            self._pixel(0, pixel, x, y, xx, yy, 0)
        return cols

    def scanline_blank(self, y, _x, cols, lcd):
        # If background is disabled, it becomes white
        for x in range(_x, _x + cols):
            self._screenbuffer[y, x] = lcd.BGP.getcolor(0)
            self._screenbuffer_attributes[y, x] = 0
        return cols

    def sort_sprites(self, sprite_count):
        # Use insertion sort, as it has O(n) on already sorted arrays. This
        # functions is likely called multiple times with unchanged data.
        # Sort descending because of the sprite priority.

        for i in range(1, sprite_count):
            key = self.sprites_to_render[i]  # The current element to be inserted into the sorted portion
            j = i - 1  # Index of the last element in the sorted portion of the array

            # Move elements of the sorted portion greater than the key to the right
            while j >= 0 and key > self.sprites_to_render[j]:
                self.sprites_to_render[j + 1] = self.sprites_to_render[j]
                j -= 1

            # Insert the key into its correct position in the sorted portion
            self.sprites_to_render[j + 1] = key

    def scanline_sprites(self, ly, buffer, buffer_attributes, ignore_priority):
        if not self.lcd._LCDC.sprite_enable or self.lcd.disable_renderer:
            return

        # Find the first 10 sprites in OAM that appears on this scanline.
        # The lowest X-coordinate has priority, when overlapping
        spriteheight = 16 if self.lcd._LCDC.sprite_height else 8
        sprite_count = 0
        for n in range(0x00, OBJECT_ATTRIBUTE_MEMORY, 4):
            y = self.lcd.OAM[n] - 16  # Documentation states the y coordinate needs to be subtracted by 16
            x = self.lcd.OAM[n + 1] - 8  # Documentation states the x coordinate needs to be subtracted by 8

            if y <= ly < y + spriteheight:
                # x is used for sorting for priority
                if self.cgb:
                    self.sprites_to_render[sprite_count] = n
                else:
                    self.sprites_to_render[sprite_count] = x << 16 | n
                sprite_count += 1

            if sprite_count == 10:
                break

        # Pan docs:
        # When these 10 sprites overlap, the highest priority one will appear above all others, etc. (Thus, no
        # Z-fighting.) In CGB mode, the first sprite in OAM ($FE00-$FE03) has the highest priority, and so on. In
        # Non-CGB mode, the smaller the X coordinate, the higher the priority. The tie breaker (same X coordinates) is
        # the same priority as in CGB mode.
        self.sort_sprites(sprite_count)

        for _n in self.sprites_to_render[:sprite_count]:
            if self.cgb:
                n = _n
            else:
                n = _n & 0xFF
            # n = self.sprites_to_render_n[_n]
            y = self.lcd.OAM[n] - 16  # Documentation states the y coordinate needs to be subtracted by 16
            x = self.lcd.OAM[n + 1] - 8  # Documentation states the x coordinate needs to be subtracted by 8
            tileindex = self.lcd.OAM[n + 2]
            if spriteheight == 16:
                tileindex &= 0b11111110
            attributes = self.lcd.OAM[n + 3]
            xflip = attributes & 0b00100000
            yflip = attributes & 0b01000000
            spritepriority = (attributes & 0b10000000) and not ignore_priority
            sprite_cache_no = 0
            if self.cgb:
                palette = attributes & 0b111
                if attributes & 0b1000:
                    sprite_cache_no = 1
            else:
                # Fake palette index
                palette = 0
                if attributes & 0b10000:
                    sprite_cache_no = 1

            self.update_spritecache(sprite_cache_no, self.lcd, tileindex, sprite_cache_no if self.cgb else 0)
            if self.lcd._LCDC.sprite_height:
                self.update_spritecache(sprite_cache_no, self.lcd, tileindex + 1, sprite_cache_no if self.cgb else 0)

            dy = ly - y
            yy = spriteheight - dy - 1 if yflip else dy

            for dx in range(8):
                xx = 7 - dx if xflip else dx
                color_code = self._spritecache[sprite_cache_no, 8 * tileindex + yy, xx]
                if 0 <= x < COLS and not color_code == 0:  # If pixel is not transparent
                    if self.cgb:
                        pixel = self.lcd.ocpd.getcolor(palette, color_code)
                        bgmappriority = buffer_attributes[ly, x] & BG_PRIORITY_FLAG

                        if (
                            self.lcd._LCDC.cgb_master_priority
                        ):  # If 0, sprites are always on top, if 1 follow priorities
                            if bgmappriority:  # If 0, use spritepriority, if 1 take priority
                                if buffer_attributes[ly, x] & COL0_FLAG:
                                    buffer[ly, x] = pixel
                            elif (
                                spritepriority
                            ):  # If 1, sprite is behind bg/window. Color 0 of window/bg is transparent
                                if buffer_attributes[ly, x] & COL0_FLAG:
                                    buffer[ly, x] = pixel
                            else:
                                buffer[ly, x] = pixel
                        else:
                            buffer[ly, x] = pixel
                    else:
                        # TODO: Unify with CGB
                        if attributes & 0b10000:
                            pixel = self.lcd.OBP1.getcolor(color_code)
                        else:
                            pixel = self.lcd.OBP0.getcolor(color_code)

                        if spritepriority:  # If 1, sprite is behind bg/window. Color 0 of window/bg is transparent
                            if buffer_attributes[ly, x] & COL0_FLAG:  # if BG pixel is transparent
                                buffer[ly, x] = pixel
                        else:
                            buffer[ly, x] = pixel
                x += 1
            x -= 8

    def clear_cache(self):
        self.clear_tilecache(0)
        if self.cgb:
            self.clear_tilecache(1)
        self.clear_spritecache(0)
        self.clear_spritecache(1)

    def invalidate_tile(self, tile, vbank):
        # TODO: Is this right?
        if vbank and self.cgb:
            self._tilecache_state[tile] = 0
            self._tilecache_state[tile + TILES] = 0
            self._spritecache_state[tile] = 0
            self._spritecache_state[tile + TILES] = 0
        else:
            self._tilecache_state[tile] = 0
            if self.cgb:
                self._tilecache_state[tile + TILES] = 0
            self._spritecache_state[tile] = 0
            self._spritecache_state[tile + TILES] = 0

    def clear_tilecache(self, cache_no):
        for i in range(TILES):
            self._tilecache_state[i + (TILES if cache_no else 0)] = 0

    def clear_spritecache(self, cache_no):
        for i in range(TILES):
            self._spritecache_state[i + (TILES if cache_no else 0)] = 0

    def update_tilecache(self, cache_no, lcd, t, bank):
        if self._tilecache_state[t + (TILES if cache_no else 0)]:
            return
        # for t in self.tiles_changed0:
        for k in range(0, 16, 2):  # 2 bytes for each line
            if self.cgb and bank:
                byte1 = lcd.VRAM1[t * 16 + k]
                byte2 = lcd.VRAM1[t * 16 + k + 1]
            else:
                byte1 = lcd.VRAM0[t * 16 + k]
                byte2 = lcd.VRAM0[t * 16 + k + 1]
            y = (t * 16 + k) // 2

            self._tilecache_64[cache_no, y] = self.colorcode(byte1, byte2)

        self._tilecache_state[t + (TILES if cache_no else 0)] = 1

    def update_spritecache(self, cache_no, lcd, t, bank):
        if self._spritecache_state[t + (TILES if cache_no else 0)]:
            return
        # for t in self.tiles_changed0:
        for k in range(0, 16, 2):  # 2 bytes for each line
            if self.cgb and bank:
                byte1 = lcd.VRAM1[t * 16 + k]
                byte2 = lcd.VRAM1[t * 16 + k + 1]
            else:
                byte1 = lcd.VRAM0[t * 16 + k]
                byte2 = lcd.VRAM0[t * 16 + k + 1]
            y = (t * 16 + k) // 2

            self._spritecache_64[cache_no, y] = self.colorcode(byte1, byte2)

        self._spritecache_state[t + (TILES if cache_no else 0)] = 1

    def colorcode(self, byte1, byte2):
        colorcode_low = self.colorcode_table[(byte1 & 0xF) | ((byte2 & 0xF) << 4)]
        colorcode_high = self.colorcode_table[((byte1 >> 4) & 0xF) | (byte2 & 0xF0)]
        return (colorcode_low << 32) | colorcode_high

    def blank_screen(self):
        # If the screen is off, fill it with a color.
        for y in range(ROWS):
            for x in range(COLS):
                self._screenbuffer[y, x] = self.lcd.BGP.getcolor(0)
                self._screenbuffer_attributes[y, x] = 0

    def save_state(self, f):
        for y in range(ROWS):
            for x in range(COLS):
                f.write_32bit(self._screenbuffer[y, x])
                f.write(self._screenbuffer_attributes[y, x])

    def load_state(self, f, state_version):
        if 2 <= state_version < 11:
            # Dummy reads to align scanline parameters. See LCD instead
            for y in range(ROWS):
                f.read()
                f.read()
                f.read()
                f.read()
                if state_version > 3:
                    f.read()

        if state_version >= 6:
            for y in range(ROWS):
                for x in range(COLS):
                    self._screenbuffer[y, x] = f.read_32bit()
                    if state_version >= 10:
                        self._screenbuffer_attributes[y, x] = f.read()

        self.clear_cache()

    ####################################
    #
    #  ██████   ██████   ██████
    # ██       ██        ██   ██
    # ██       ██   ███  ██████
    # ██       ██    ██  ██   ██
    #  ██████   ██████   ██████
    #

    def _cgb_get_background_map_attributes(self, lcd, i):
        tile_num = lcd.VRAM1[i]
        palette = tile_num & 0b111
        vbank = (tile_num >> 3) & 1
        horiflip = (tile_num >> 5) & 1
        vertflip = (tile_num >> 6) & 1
        bg_priority = (tile_num >> 7) & 1

        return palette, vbank, horiflip, vertflip, bg_priority

    def _cgb_get_tile(self, y, x, offset, lcd):
        tile, yy, tile_addr = self._get_tile(y, x, offset, lcd)

        palette, vbank, horiflip, vertflip, bg_priority = self._cgb_get_background_map_attributes(lcd, tile_addr)

        bg_priority_apply = 0
        if bg_priority:
            # We hide extra rendering information in the lower 8 bits (A) of the 32-bit RGBA format
            bg_priority_apply = BG_PRIORITY_FLAG

        if vertflip:
            yy = 8 * tile + (7 - (y) % 8)

        return tile, yy, palette, horiflip, bg_priority_apply, vbank

    def cgb_scanline_window(self, y, _x, wx, wy, cols, lcd):
        bg_priority_apply = 0
        for x in range(_x, _x + cols):
            xx = (x - wx) % 8
            if xx == 0 or x == _x:
                wt, yy, w_palette, w_horiflip, bg_priority_apply, vbank = self._cgb_get_tile(
                    self.ly_window, x - wx, lcd._LCDC.windowmap_offset, lcd
                )
                self.update_tilecache(vbank, lcd, wt, vbank)

            if w_horiflip:
                xx = 7 - xx

            pixel = lcd.bcpd.getcolor(w_palette, self._tilecache[vbank, yy, xx])
            self._pixel(vbank, pixel, x, y, xx, yy, bg_priority_apply)
        return cols

    def cgb_scanline_background(self, y, _x, bx, by, cols, lcd):
        for x in range(_x, _x + cols):
            # bx mask used for the half tile at the left side when scrolling
            xx = (x + (bx & 0b111)) % 8
            if xx == 0 or x == 0:
                bt, yy, b_palette, b_horiflip, bg_priority_apply, vbank = self._cgb_get_tile(
                    y + by, x + bx, lcd._LCDC.backgroundmap_offset, lcd
                )
                self.update_tilecache(vbank, lcd, bt, vbank)

            if b_horiflip:
                xx = 7 - xx

            pixel = lcd.bcpd.getcolor(b_palette, self._tilecache[vbank, yy, xx])
            self._pixel(vbank, pixel, x, y, xx, yy, bg_priority_apply)
        return cols

    def cgb_scanline(self, y):
        if self.lcd.disable_renderer:
            return

        bx, by = self.lcd.getviewport()
        wx, wy = self.lcd.getwindowpos()

        x = 0
        if self.lcd._LCDC.window_enable and self.wy_activated_frame and wy <= y and wx < COLS:
            # Window has it's own internal line counter. It's only incremented whenever the window is drawing something on the screen.
            self.ly_window += 1

            # Before window
            if wx > x:
                x += self.cgb_scanline_background(y, x, bx, by, wx, self.lcd)

            # Window hit
            self.cgb_scanline_window(y, x, wx, wy, COLS - x, self.lcd)
        else:  # background_enable doesn't exist for CGB. It works as master priority instead
            # No window
            self.cgb_scanline_background(y, x, bx, by, COLS, self.lcd)

        if y == 143:
            # Reset at the end of a frame. We set it to -1, so it will be 0 after the first increment
            self.ly_window = -1


