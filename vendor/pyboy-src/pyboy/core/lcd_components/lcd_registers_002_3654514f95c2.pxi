class PaletteRegister:
    def __init__(self, value, palette):
        self.value = 0
        self.lookup = [0] * 4

        self.set_palette_colors(palette)
        self.set(value)

    def set(self, value):
        # Pokemon Blue continuously sets this without changing the value
        if self.value == value:
            return False

        self.value = value
        self._update_lookup()
        return True

    def set_palette_colors(self, palette):
        self.palette_mem_rgb = [(rgb_to_bgr(c)) for c in palette]
        self._update_lookup()

    def _update_lookup(self):
        for x in range(4):
            self.lookup[x] = self.palette_mem_rgb[(self.value >> x * 2) & 0b11]

    def get(self):
        return self.value

    def getcolor(self, i):
        return self.lookup[i]


class STATRegister:
    def __init__(self):
        self.value = 0b1000_0000
        self._mode = 0

    def set(self, value):
        value &= 0b0111_1000  # Bit 7 is always set, and bit 0-2 are read-only
        self.value &= 0b1000_0111  # Preserve read-only bits and clear the rest
        self.value |= value  # Combine the two

    def update_LYC(self, LYC, LY):
        # WARNING: Only call when LCD is enabled!
        if LYC == LY:
            self.value |= 0b100  # Sets the LYC flag
            if self.value & 0b0100_0000:  # LYC interrupt enabled flag
                return INTR_LCDC
        else:
            # Clear LYC flag
            self.value &= 0b1111_1011
        return 0

    def set_mode(self, mode):
        if self._mode == mode:
            # Mode already set
            return 0

        self._mode = mode
        self.value &= 0b11111100  # Clearing 2 LSB
        self.value |= mode  # Apply mode to LSB

        # Check if interrupt is enabled for this mode
        # Mode "3" is not interruptable
        if mode != 3 and self.value & (1 << (mode + 3)):
            return INTR_LCDC
        return 0

    def save_state(self, f):
        f.write(self.value)

    def load_state(self, f, state_version):
        value = f.read()
        self.value = value
        self._mode = value & 0b11


class LCDCRegister:
    def __init__(self, value):
        self.set(value)

    def set(self, value):
        self.value = value

        # No need to convert to bool. Any non-zero value is true.
        # fmt: off
        self.lcd_enable           = value & (1 << 7)
        self.windowmap_select     = value & (1 << 6)
        self.window_enable        = value & (1 << 5)
        self.tiledata_select      = value & (1 << 4)
        self.backgroundmap_select = value & (1 << 3)
        self.sprite_height        = value & (1 << 2)
        self.sprite_enable        = value & (1 << 1)
        self.background_enable    = value & (1 << 0)
        self.cgb_master_priority  = self.background_enable # Different meaning on CGB
        # fmt: on

        # All VRAM addresses are offset by 0x8000
        # Following addresses are 0x9800 and 0x9C00
        self.backgroundmap_offset = 0x1800 if self.backgroundmap_select == 0 else 0x1C00
        self.windowmap_offset = 0x1800 if self.windowmap_select == 0 else 0x1C00


COL0_FLAG = 0b01
BG_PRIORITY_FLAG = 0b10


