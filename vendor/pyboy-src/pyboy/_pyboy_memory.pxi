

class PyBoyRegisterFile:
    """
    This class cannot be used directly, but is accessed through `PyBoy.register_file`.

    This class serves the purpose of reading and writing to the CPU registers. It's best used inside the callback of a
    hook, as `PyBoy.tick` doesn't return at a specific point.

    See the [Pan Docs: CPU registers and flags](https://gbdev.io/pandocs/CPU_Registers_and_Flags.html) for a great overview.

    Registers are accessed with the following names: `A, F, B, C, D, E, HL, SP, PC` where the last three are 16-bit and
    the others are 8-bit. Trying to write a number larger than 8 or 16 bits will truncate it.

    Example:
    ```python
    >>> def my_callback(pyboy):
    ...     print("Register A:", pyboy.register_file.A)
    ...     pyboy.memory[0xFF50] = 1 # Example: Disable boot ROM
    ...     pyboy.register_file.A = 0x11 # Modify to the needed value
    ...     pyboy.register_file.PC = 0x100 # Jump past existing code
    >>> pyboy.hook_register(-1, 0xFC, my_callback, pyboy)
    >>> pyboy.tick(120)
    Register A: 1
    True
    ```
    """

    def __init__(self, cpu):
        self.cpu = cpu

    @property
    def A(self):
        return self.cpu.A

    @A.setter
    def A(self, value):
        self.cpu.A = value & 0xFF

    @property
    def F(self):
        return self.cpu.F

    @F.setter
    def F(self, value):
        self.cpu.F = value & 0xF0

    @property
    def B(self):
        return self.cpu.B

    @B.setter
    def B(self, value):
        self.cpu.B = value & 0xFF

    @property
    def C(self):
        return self.cpu.C

    @C.setter
    def C(self, value):
        self.cpu.C = value & 0xFF

    @property
    def D(self):
        return self.cpu.D

    @D.setter
    def D(self, value):
        self.cpu.D = value & 0xFF

    @property
    def E(self):
        return self.cpu.E

    @E.setter
    def E(self, value):
        self.cpu.E = value & 0xFF

    @property
    def HL(self):
        return self.cpu.HL

    @HL.setter
    def HL(self, value):
        self.cpu.HL = value & 0xFFFF

    @property
    def SP(self):
        return self.cpu.SP

    @SP.setter
    def SP(self, value):
        self.cpu.SP = value & 0xFFFF

    @property
    def PC(self):
        return self.cpu.PC

    @PC.setter
    def PC(self, value):
        self.cpu.PC = value & 0xFFFF


class PyBoyMemoryView:
    """
    This class cannot be used directly, but is accessed through `PyBoy.memory`.

    This class serves four purposes: Reading memory (ROM/RAM), writing memory (RAM), overriding memory (ROM) and special registers.

    See the [Pan Docs: Memory Map](https://gbdev.io/pandocs/Memory_Map.html) for a great overview of the memory space.

    Memory can be accessed as individual bytes (`pyboy.memory[0x00]`) or as slices (`pyboy.memory[0x00:0x10]`). And if
    applicable, a specific ROM/RAM bank can be defined before the address (`pyboy.memory[0, 0x00]` or `pyboy.memory[0, 0x00:0x10]`).

    The boot ROM is accessed using the special `-1` ROM bank.

    The find addresses of interest, either search online for something like: "[game title] RAM map", or find them yourself
    using `PyBoy.memory_scanner`.

    **Read:**

    If you're developing a bot or AI with this API, you're most likely going to be using read the most. This is how you
    would efficiently read the score, time, coins, positions etc. in a game's memory.

    At this point, all reads will return a new list of the values in the given range. The slices will not reference back to the PyBoy memory. This feature might come in the future.

    ```python
    >>> pyboy.memory[0x0000] # Read one byte at address 0x0000
    49
    >>> pyboy.memory[0x0000:0x0010] # Read 16 bytes from 0x0000 to 0x0010 (excluding 0x0010)
    [49, 254, 255, 33, 0, 128, 175, 34, 124, 254, 160, 32, 249, 6, 48, 33]
    >>> pyboy.memory[-1, 0x0000:0x0010] # Read 16 bytes from 0x0000 to 0x0010 (excluding 0x0010) from the boot ROM
    [49, 254, 255, 33, 0, 128, 175, 34, 124, 254, 160, 32, 249, 6, 48, 33]
    >>> pyboy.memory[0, 0x0000:0x0010] # Read 16 bytes from 0x0000 to 0x0010 (excluding 0x0010) from ROM bank 0
    [64, 65, 66, 67, 68, 69, 70, 65, 65, 65, 71, 65, 65, 65, 72, 73]
    >>> pyboy.memory[2, 0xA000] # Read from external RAM on cartridge (if any) from bank 2 at address 0xA000
    0
    ```

    **Write:**

    Writing to Game Boy memory can be complicated because of the limited address space. There's a lot of memory that
    isn't directly accessible, and can be hidden through "memory banking". This means that the same address range
    (for example 0x4000 to 0x8000) can change depending on what state the game is in.

    If you want to change an address in the ROM, then look at override below. Issuing writes to the ROM area actually
    sends commands to the [Memory Bank Controller (MBC)](https://gbdev.io/pandocs/MBCs.html#mbcs) on the cartridge.

    A write is done by assigning to the `PyBoy.memory` object. It's recommended to define the bank to avoid mistakes
    (`pyboy.memory[2, 0xA000]=1`). Without defining the bank, PyBoy will pick the current bank for the given address if
    needed (`pyboy.memory[0xA000]=1`).

    ```python
    >>> pyboy.memory[0xC000] = 123 # Write to WRAM at address 0xC000
    >>> pyboy.memory[0xC000:0xC00A] = [0,1,2,3,4,5,6,7,8,9] # Write to WRAM from address 0xC000 to 0xC00A
    >>> pyboy.memory[0xC010:0xC01A] = 0 # Write to WRAM from address 0xC010 to 0xC01A
    >>> pyboy.memory[0x1000] = 123 # Not writing 123 at address 0x1000! This sends a command to the cartridge's MBC.
    >>> pyboy.memory[2, 0xA000] = 123 # Write to external RAM on cartridge (if any) for bank 2 at address 0xA000
    >>> # Game Boy Color (CGB) only:
    >>> pyboy_cgb.memory[1, 0x8000] = 25 # Write to VRAM bank 1 at address 0xD000 when in CGB mode
    >>> pyboy_cgb.memory[6, 0xD000] = 25 # Write to WRAM bank 6 at address 0xD000 when in CGB mode
    ```

    **Override:**

    Override data at a given memory address of the Game Boy's ROM.

    This can be used to reprogram a game ROM to change its behavior.

    This will not let you override RAM or a special register. This will let you override data in the ROM at any given bank.
    This is the memory allocated at 0x0000 to 0x8000, where 0x4000 to 0x8000 can be changed from the MBC.

    _NOTE_: Any changes here are not saved or loaded to game states! Use this function with caution and reapply
    any overrides when reloading the ROM.

    To override, it's required to provide the ROM-bank you're changing. Otherwise, it'll be considered a regular 'write' as described above.

    ```python
    >>> pyboy.memory[0, 0x0010] = 10 # Override ROM-bank 0 at address 0x0010
    >>> pyboy.memory[0, 0x0010:0x001A] = [0,1,2,3,4,5,6,7,8,9] # Override ROM-bank 0 at address 0x0010 to 0x001A
    >>> pyboy.memory[-1, 0x0010] = 10 # Override boot ROM at address 0x0010
    >>> pyboy.memory[1, 0x6000] = 12 # Override ROM-bank 1 at address 0x6000
    >>> pyboy.memory[0x1000] = 12 # This will not override, as there is no ROM bank assigned!
    ```

    **Special Registers:**

    The Game Boy has a range of memory addresses known as [hardware registers](https://gbdev.io/pandocs/Hardware_Reg_List.html). These control parts of the hardware like LCD,
    Timer, DMA, serial and so on. Even though they might appear as regular RAM addresses, reading/writing these addresses
    often results in special side-effects.

    The [DIV (0xFF04) register](https://gbdev.io/pandocs/Timer_and_Divider_Registers.html#ff04--div-divider-register) for example provides a number that increments 16 thousand times each second. This can be
    used as a source of randomness in games. If you read the value, you'll get a pseudo-random number. But if you write
    *any* value to the register, it'll reset to zero.

    ```python
    >>> pyboy.memory[0xFF04] # DIV register
    231
    >>> pyboy.memory[0xFF04] = 123 # Trying to write to it will always reset it to zero
    >>> pyboy.memory[0xFF04]
    0
    ```

    """

    def __init__(self, mb):
        self.mb = mb

    def _fix_slice(self, addr):
        if addr.start is None:
            return (-1, 0, 0)
        if addr.stop is None:
            return (0, -1, 0)
        start = addr.start
        stop = addr.stop
        if start > stop:
            return (-1, -1, 0)
        if addr.step is None:
            step = 1
        else:
            step = addr.step
        return start, stop, step

    def __len__(self):
        raise PyBoyInvalidOperationException(
            "It's not possible to define the length of the memory space. See instead https://gbdev.io/pandocs/Memory_Map.html"
        )

    def __iter__(self):
        """
        Address space is overlapping, and therefore too complex to return as list or iterator.
        If you want a snapshot, you should request specific memory ranges and banks.
        See https://gbdev.io/pandocs/Memory_Map.html
        """
        raise PyBoyInvalidOperationException("It's not possible to iterate over the memory space.")

    def __getitem__(self, addr):
        is_bank = isinstance(addr, tuple)
        bank = 0
        if is_bank:
            bank, addr = addr
            if not (isinstance(bank, int)):
                raise PyBoyInvalidInputException("Bank has to be integer. Slicing is not supported.")
        is_single = isinstance(addr, int)
        if not is_single:
            start, stop, step = self._fix_slice(addr)
            if not (start >= 0):
                raise PyBoyInvalidInputException("Start address required")
            if not (stop >= 0):
                raise PyBoyInvalidInputException("End address required")
            if not 0 <= start <= 0xFFFF:
                raise PyBoyOutOfBoundsException("Start address out of bounds")
            if not 0 <= stop <= 0x10000:
                raise PyBoyOutOfBoundsException("End address out of bounds")
            if not (start < stop):
                raise PyBoyInvalidInputException("Start address has to come before end address")
            return self.__getitem(start, stop, step, bank, is_single, is_bank)
        else:
            return self.__getitem(addr, 0, 1, bank, is_single, is_bank)

    def __getitem(self, start, stop, step, bank, is_single, is_bank):
        self.mb.serial.check_error()
        slice_length = (stop - start) // step
        if is_bank:
            # Reading a specific bank
            if start < 0x8000:
                if start >= 0x4000:
                    start -= 0x4000
                    stop -= 0x4000
                # Cartridge ROM Banks
                if not (stop < 0x4000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading ROM bank")
                if bank == -1:
                    if not (start <= 0xFF):
                        raise PyBoyOutOfBoundsException("Start address out of range for bootrom")
                    if not (stop <= 0xFF):
                        raise PyBoyOutOfBoundsException("Start address out of range for bootrom")
                    if not is_single:
                        mem_slice = [0] * slice_length
                        for x in range(start, stop, step):
                            mem_slice[(x - start) // step] = self.mb.bootrom.bootrom[x]
                        return mem_slice
                    else:
                        return self.mb.bootrom.bootrom[start]
                else:
                    if not (bank <= self.mb.cartridge.external_rom_count):
                        raise PyBoyOutOfBoundsException("ROM Bank out of range")
                    if not is_single:
                        mem_slice = [0] * slice_length
                        for x in range(start, stop, step):
                            mem_slice[(x - start) // step] = self.mb.cartridge.rombanks[bank, x]
                        return mem_slice
                    else:
                        return self.mb.cartridge.rombanks[bank, start]
            elif start < 0xA000:
                start -= 0x8000
                stop -= 0x8000
                # CGB VRAM Banks
                if not (self.mb.cgb or (bank == 0)):
                    raise PyBoyInvalidInputException("Selecting bank of VRAM is only supported for CGB mode")
                if not (stop < 0x2000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading VRAM bank")
                if not (bank <= 1):
                    raise PyBoyOutOfBoundsException("VRAM Bank out of range")

                if bank == 0:
                    if not is_single:
                        mem_slice = [0] * slice_length
                        for x in range(start, stop, step):
                            mem_slice[(x - start) // step] = self.mb.lcd.VRAM0[x]
                        return mem_slice
                    else:
                        return self.mb.lcd.VRAM0[start]
                else:
                    if not is_single:
                        mem_slice = [0] * slice_length
                        for x in range(start, stop, step):
                            mem_slice[(x - start) // step] = self.mb.lcd.VRAM1[x]
                        return mem_slice
                    else:
                        return self.mb.lcd.VRAM1[start]
            elif start < 0xC000:
                start -= 0xA000
                stop -= 0xA000
                # Cartridge RAM banks
                if not (stop < 0x2000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading cartridge RAM bank")
                if not (bank <= self.mb.cartridge.external_ram_count):
                    raise PyBoyOutOfBoundsException("ROM Bank out of range")
                if not is_single:
                    mem_slice = [0] * slice_length
                    for x in range(start, stop, step):
                        mem_slice[(x - start) // step] = self.mb.cartridge.rambanks[bank, x]
                    return mem_slice
                else:
                    return self.mb.cartridge.rambanks[bank, start]
            elif start < 0xE000:
                start -= 0xC000
                stop -= 0xC000
                if start >= 0x1000:
                    start -= 0x1000
                    stop -= 0x1000
                # CGB VRAM banks
                if not (self.mb.cgb or (bank == 0)):
                    raise PyBoyInvalidInputException("Selecting bank of WRAM is only supported for CGB mode")
                if not (stop < 0x1000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading VRAM bank")
                if not (bank <= 7):
                    raise PyBoyOutOfBoundsException("WRAM Bank out of range")
                if not is_single:
                    mem_slice = [0] * slice_length
                    for x in range(start, stop, step):
                        mem_slice[(x - start) // step] = self.mb.ram.internal_ram0[x + bank * 0x1000]
                    return mem_slice
                else:
                    return self.mb.ram.internal_ram0[start + bank * 0x1000]
            else:
                raise PyBoyInvalidInputException("Invalid memory address for bank")
        elif not is_single:
            # Reading slice of memory space
            mem_slice = [0] * slice_length
            for x in range(start, stop, step):
                mem_slice[(x - start) // step] = self.mb.getitem(x)
                self.mb.serial.check_error()
            return mem_slice
        else:
            # Reading specific address of memory space
            value = self.mb.getitem(start)
            self.mb.serial.check_error()
            return value

    def __setitem__(self, addr, v):
        is_bank = isinstance(addr, tuple)
        bank = 0
        if is_bank:
            bank, addr = addr
            if not (isinstance(bank, int)):
                raise PyBoyInvalidInputException("Bank has to be integer. Slicing is not supported.")
        is_single = isinstance(addr, int)
        if not is_single:
            start, stop, step = self._fix_slice(addr)
            if not (start >= 0):
                raise PyBoyInvalidInputException("Start address required")
            if not (stop >= 0):
                raise PyBoyInvalidInputException("End address required")
            if not 0 <= start <= 0xFFFF:
                raise PyBoyOutOfBoundsException("Start address out of bounds")
            if not 0 <= stop <= 0x10000:
                raise PyBoyOutOfBoundsException("End address out of bounds")
            if not (start < stop):
                raise PyBoyInvalidInputException("Start address has to come before end address")
            self.__setitem(start, stop, step, v, bank, is_single, is_bank)
        else:
            self.__setitem(addr, 0, 0, v, bank, is_single, is_bank)

    def __setitem(self, start, stop, step, v, bank, is_single, is_bank):
        self.mb.serial.check_error()
        if is_bank:
            # Writing a specific bank
            if start < 0x8000:
                """
                Override one byte at a given memory address of the Game Boy's ROM.

                This will let you override data in the ROM at any given bank. This is the memory allocated at 0x0000 to 0x8000, where 0x4000 to 0x8000 can be changed from the MBC.

                __NOTE__: Any changes here are not saved or loaded to game states! Use this function with caution and reapply
                any overrides when reloading the ROM.

                If you need to change a RAM address, see `pyboy.PyBoy.memory`.

                Args:
                    rom_bank (int): ROM bank to do the overwrite in
                    addr (int): Address to write the byte inside the ROM bank
                    value (int): A byte of data
                """
                if start >= 0x4000:
                    start -= 0x4000
                    stop -= 0x4000
                # Cartridge ROM Banks
                if not (stop <= 0x4000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading ROM bank")
                if not (bank <= self.mb.cartridge.external_rom_count):
                    raise PyBoyOutOfBoundsException("ROM Bank out of range")

                if bank == -1:
                    if not (start <= 0xFF):
                        raise PyBoyOutOfBoundsException("Start address out of range for bootrom")
                    if not (stop <= 0x100):
                        raise PyBoyOutOfBoundsException("Start address out of range for bootrom")
                    if not is_single:
                        # Writing slice of memory space
                        if hasattr(v, "__iter__"):
                            if not ((stop - start) // step == len(v)):
                                raise PyBoyInvalidInputException("slice does not match length of data")
                            _v = iter(v)
                            for x in range(start, stop, step):
                                self.mb.bootrom.bootrom[x] = next(_v)
                        else:
                            for x in range(start, stop, step):
                                self.mb.bootrom.bootrom[x] = v
                    else:
                        self.mb.bootrom.bootrom[start] = v
                else:
                    if not is_single:
                        # Writing slice of memory space
                        if hasattr(v, "__iter__"):
                            if not ((stop - start) // step == len(v)):
                                raise PyBoyInvalidInputException("slice does not match length of data")
                            _v = iter(v)
                            for x in range(start, stop, step):
                                self.mb.cartridge.overrideitem(bank, x, next(_v))
                        else:
                            for x in range(start, stop, step):
                                self.mb.cartridge.overrideitem(bank, x, v)
                    else:
                        self.mb.cartridge.overrideitem(bank, start, v)

            elif start < 0xA000:
                start -= 0x8000
                stop -= 0x8000
                # CGB VRAM Banks
                if not (self.mb.cgb or (bank == 0)):
                    raise PyBoyInvalidInputException("Selecting bank of VRAM is only supported for CGB mode")
                if not (stop <= 0x2000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading VRAM bank")
                if not (bank <= 1):
                    raise PyBoyOutOfBoundsException("VRAM Bank out of range")

                if bank == 0:
                    if not is_single:
                        # Writing slice of memory space
                        if hasattr(v, "__iter__"):
                            if not ((stop - start) // step == len(v)):
                                raise PyBoyInvalidInputException("slice does not match length of data")
                            _v = iter(v)
                            for x in range(start, stop, step):
                                self.mb.lcd.VRAM0[x] = next(_v)
                        else:
                            for x in range(start, stop, step):
                                self.mb.lcd.VRAM0[x] = v
                    else:
                        self.mb.lcd.VRAM0[start] = v
                else:
                    if not is_single:
                        # Writing slice of memory space
                        if hasattr(v, "__iter__"):
                            if not ((stop - start) // step == len(v)):
                                raise PyBoyInvalidInputException("slice does not match length of data")
                            _v = iter(v)
                            for x in range(start, stop, step):
                                self.mb.lcd.VRAM1[x] = next(_v)
                        else:
                            for x in range(start, stop, step):
                                self.mb.lcd.VRAM1[x] = v
                    else:
                        self.mb.lcd.VRAM1[start] = v
            elif start < 0xC000:
                start -= 0xA000
                stop -= 0xA000
                # Cartridge RAM banks
                if not (stop <= 0x2000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading cartridge RAM bank")
                if not (bank <= self.mb.cartridge.external_ram_count):
                    raise PyBoyOutOfBoundsException("ROM Bank out of range")
                if not is_single:
                    # Writing slice of memory space
                    if hasattr(v, "__iter__"):
                        if not ((stop - start) // step == len(v)):
                            raise PyBoyInvalidInputException("slice does not match length of data")
                        _v = iter(v)
                        for x in range(start, stop, step):
                            self.mb.cartridge.rambanks[bank, x] = next(_v)
                    else:
                        for x in range(start, stop, step):
                            self.mb.cartridge.rambanks[bank, x] = v
                else:
                    self.mb.cartridge.rambanks[bank, start] = v
            elif start < 0xE000:
                start -= 0xC000
                stop -= 0xC000
                if start >= 0x1000:
                    start -= 0x1000
                    stop -= 0x1000
                # CGB VRAM banks
                if not (self.mb.cgb or (bank == 0)):
                    raise PyBoyInvalidInputException("Selecting bank of WRAM is only supported for CGB mode")
                if not (stop <= 0x1000):
                    raise PyBoyOutOfBoundsException("Out of bounds for reading VRAM bank")
                if not (bank <= 7):
                    raise PyBoyOutOfBoundsException("WRAM Bank out of range")
                if not is_single:
                    # Writing slice of memory space
                    if hasattr(v, "__iter__"):
                        if not ((stop - start) // step == len(v)):
                            raise PyBoyInvalidInputException("slice does not match length of data")
                        _v = iter(v)
                        for x in range(start, stop, step):
                            self.mb.ram.internal_ram0[x + bank * 0x1000] = next(_v)
                    else:
                        for x in range(start, stop, step):
                            self.mb.ram.internal_ram0[x + bank * 0x1000] = v
                else:
                    self.mb.ram.internal_ram0[start + bank * 0x1000] = v
            else:
                raise PyBoyInvalidInputException("Invalid memory address for bank")
        elif not is_single:
            # Writing slice of memory space
            if hasattr(v, "__iter__"):
                if not ((stop - start) // step == len(v)):
                    raise PyBoyInvalidInputException("slice does not match length of data")
                _v = iter(v)
                for x in range(start, stop, step):
                    self.mb.setitem(x, next(_v))
                    self.mb.serial.check_error()
            else:
                for x in range(start, stop, step):
                    self.mb.setitem(x, v)
                    self.mb.serial.check_error()
        else:
            # Writing specific address of memory space
            self.mb.setitem(start, v)
            self.mb.serial.check_error()
