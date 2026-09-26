class VBKregister:
    def __init__(self, value=0):
        self.active_bank = value

    def set(self, value):
        # when writing to VBK, bit 0 indicates which bank to switch to
        bank = value & 1
        self.active_bank = bank

    def get(self):
        # reading from this register returns current VRAM bank in bit 0, other bits = 1
        return self.active_bank | 0xFE


class PaletteIndexRegister:
    def __init__(self, val=0):
        self.value = val
        self.auto_inc = 0
        self.index = 0
        self.hl = 0

    def set(self, val):
        if self.value == val:
            return
        self.value = val
        self.hl = val & 0b1
        self.index = (val >> 1) & 0b11111
        self.auto_inc = (val >> 7) & 0b1

    def get(self):
        return self.value

    def getindex(self):
        return self.index

    def shouldincrement(self):
        if self.auto_inc:
            # ensure autoinc also set for new val
            new_val = 0x80 | (self.value + 1)
            self.set(new_val)

    def save_state(self, f):
        f.write(self.value)
        f.write(self.auto_inc)
        f.write(self.index)
        f.write(self.hl)

    def load_state(self, f, state_version):
        self.value = f.read()
        self.auto_inc = f.read()
        self.index = f.read()
        self.hl = f.read()


CGB_NUM_PALETTES = 8


class PaletteColorRegister:
    def __init__(self, i_reg):
        # 8 palettes of 4 colors each 2 bytes
        self.palette_mem = array("I", [0xFFFF] * CGB_NUM_PALETTES * 4)
        self.palette_mem_rgb = array("L", [0] * CGB_NUM_PALETTES * 4)
        self.index_reg = i_reg

        # Init with some colors -- TODO: What are real defaults?
        for n in range(0, len(self.palette_mem), 4):
            c = [0x1CE7, 0x1E19, 0x7E31, 0x217B]
            for m in range(4):
                self.palette_mem[n + m] = c[m]
                self.palette_mem_rgb[n + m] = self.cgb_to_rgb(c[m], m)

    def cgb_to_rgb(self, cgb_color, index):
        alpha = 0xFF
        red = (cgb_color & 0x1F) << 3
        green = ((cgb_color >> 5) & 0x1F) << 3
        blue = ((cgb_color >> 10) & 0x1F) << 3
        # NOTE: Actually BGR, not RGB
        rgb_color = (alpha << 24) | (blue << 16) | (green << 8) | red
        return rgb_color

    def set(self, val):
        i_val = self.palette_mem[self.index_reg.getindex()]
        if self.index_reg.hl:
            self.palette_mem[self.index_reg.getindex()] = (i_val & 0x00FF) | (val << 8)
        else:
            self.palette_mem[self.index_reg.getindex()] = (i_val & 0xFF00) | val

        cgb_color = self.palette_mem[self.index_reg.getindex()] & 0x7FFF
        self.palette_mem_rgb[self.index_reg.getindex()] = self.cgb_to_rgb(cgb_color, self.index_reg.getindex())

        # check for autoincrement after write
        self.index_reg.shouldincrement()

    def get(self):
        if self.index_reg.hl:
            return (self.palette_mem[self.index_reg.getindex()] & 0xFF00) >> 8
        else:
            return self.palette_mem[self.index_reg.getindex()] & 0x00FF

    def getcolor(self, paletteindex, colorindex):
        # Each palette = 8 bytes or 4 colors of 2 bytes
        return self.palette_mem_rgb[paletteindex * 4 + colorindex]

    def save_state(self, f):
        for n in range(CGB_NUM_PALETTES * 4):
            f.write_16bit(self.palette_mem[n])

    def load_state(self, f, state_version):
        for n in range(CGB_NUM_PALETTES * 4):
            self.palette_mem[n] = f.read_16bit()
            self.palette_mem_rgb[n] = self.cgb_to_rgb(self.palette_mem[n], n % 4)
