#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""Opcode handler methods for the opcodes generator (#142 split of ``opcodes_gen.py``).

The 48 ``OpcodeData`` operation handlers live here so both generator modules stay inside
the repository's 1000-line file-split bound. ``OpcodeData`` in ``opcodes_gen`` inherits
this mixin; the moved method text is byte-for-byte the pre-split text. The four names
imported below are the facade's helpers, so handler bodies resolve exactly the objects
they resolved before the split.
"""

try:  # package-relative import when the generator is imported as a module
    from .opcodes_gen import Code, Operand, Literal, inline_signed_int8
except ImportError:  # script/standalone import: the generator modules sit side by side
    from opcodes_gen import Code, Operand, Literal, inline_signed_int8


class OpcodeDataHandlers:
    ###################################################################
    #
    # MISC OPERATIONS
    #
    def NOP(self):
        code = Code(self.name.split()[0], self.opcode, self.name, 0, self.length, self.cycles)
        return code.getcode()

    def HALT(self):
        code = Code(self.name.split()[0], self.opcode, self.name, 0, self.length, self.cycles, branch_op=True)

        # TODO: Implement HALT bug.
        code.addlines(
            [
                "cpu.halted = True",
                "cpu.bail = True",
                "cpu.cycles += " + self.cycles[0],
            ]
        )
        return code.getcode()

    def CB(self):
        code = Code(self.name.split()[0], self.opcode, self.name, 0, self.length, self.cycles)
        code.addline("logger.critical('CB cannot be called!')")
        return code.getcode()

    def EI(self):
        code = Code(self.name.split()[0], self.opcode, self.name, 0, self.length, self.cycles)
        code.addlines(
            [
                "cpu.interrupt_master_enable = True",
                "cpu.bail = (cpu.interrupts_flag_register & 0b11111) & (cpu.interrupts_enabled_register & 0b11111)",
            ]
        )
        return code.getcode()

    def DI(self):
        code = Code(self.name.split()[0], self.opcode, self.name, 0, self.length, self.cycles)
        code.addline("cpu.interrupt_master_enable = False")
        return code.getcode()

    def STOP(self):
        code = Code(self.name.split()[0], self.opcode, self.name, True, self.length, self.cycles)
        code.addlines(
            [
                "if cpu.mb.cgb:",
                "    cpu.mb.switch_speed()",
                "    cpu.mb.setitem(0xFF04, 0)",
            ]
        )
        # code.addLine("raise Exception('STOP not implemented!')")
        return code.getcode()

    def DAA(self):
        left = Operand("A")
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)

        # http://stackoverflow.com/a/29990058/3831206
        # http://forums.nesdev.com/viewtopic.php?t=9088
        code.addlines(
            [
                "t = %s" % left.get,
                "corr = 0",
                "corr |= 0x06 if ((cpu.F & (1 << FLAGH)) != 0) else 0x00",
                "corr |= 0x60 if ((cpu.F & (1 << FLAGC)) != 0) else 0x00",
                "if (cpu.F & (1 << FLAGN)) != 0:",
                "\tt -= corr",
                "else:",
                "\tcorr |= 0x06 if (t & 0x0F) > 0x09 else 0x00",
                "\tcorr |= 0x60 if t > 0x99 else 0x00",
                "\tt += corr",
                "flag = 0",
                "flag |= ((t & 0xFF) == 0) << FLAGZ",
                "flag |= (corr & 0x60 != 0) << FLAGC",
                "cpu.F &= 0b01000000",
                "cpu.F |= flag",
                "t &= 0xFF",
                left.set % "t",
            ]
        )
        return code.getcode()

    def SCF(self):
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        code.addlines(self.handleflags8bit(None, None, None))
        return code.getcode()

    def CCF(self):
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        code.addlines(
            [
                "flag = (cpu.F & 0b00010000) ^ 0b00010000",
                "cpu.F &= 0b10000000",
                "cpu.F |= flag",
            ]
        )
        return code.getcode()

    def CPL(self):
        left = Operand("A")
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        code.addline(left.set % ("(~%s) & 0xFF" % left.get))
        code.addlines(self.handleflags8bit(None, None, None))
        return code.getcode()

    ###################################################################
    #
    # LOAD OPERATIONS
    #
    def LD(self):
        r0, r1 = self.name.split()[1].split(",")
        left = Operand(r0)
        right = Operand(r1)

        # FIX: There seems to be a wrong opcode length on E2 and F2
        if self.opcode == 0xE2 or self.opcode == 0xF2:
            self.length = 1

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )

        # These opcodes can be observed reading mid-cycle
        if self.opcode == 0x36:
            code.addline("cpu.cycles += 4")
            code.cycles = (str(int(code.cycles[0]) - 4),)
        elif self.opcode == 0xE0:
            code.addline("cpu.cycles += 4")
            code.cycles = (str(int(code.cycles[0]) - 4),)
        elif self.opcode == 0xEA:
            code.addline("cpu.cycles += 8")
            code.cycles = (str(int(code.cycles[0]) - 8),)
        elif self.opcode == 0xF0:
            code.addline("cpu.cycles += 4")
            code.cycles = (str(int(code.cycles[0]) - 4),)
        elif self.opcode == 0xFA:
            code.addline("cpu.cycles += 8")
            code.cycles = (str(int(code.cycles[0]) - 8),)

        if self.is16bit and left.immediate and left.pointer:
            code.addline(left.set % ("%s & 0xFF" % right.get))
            a, b = left.set.split(",")
            code.addline((a + "+1," + b) % ("%s >> 8" % right.get))
        else:
            # Special handling of AF, BC, DE
            # print(left.set, right.get, hex(self.opcode))
            if left.set.count("%") > 1:
                code.addline(left.set % (right.get, right.get))
            else:
                code.addline(left.set % right.get)

        # Special HL-only operations
        if left.postoperation is not None:
            code.addline(left.postoperation)
            code.addline("cpu.HL &= 0xFFFF")
        elif right.postoperation is not None:
            code.addline(right.postoperation)
            code.addline("cpu.HL &= 0xFFFF")
        elif self.opcode == 0xF8:
            # E8 and F8 http://forums.nesdev.com/viewtopic.php?p=42138
            code.addline("t = cpu.HL")
            code.addlines(self.handleflags16bit_E8_F8("cpu.SP", "v", "+", False))
            code.addline("cpu.HL &= 0xFFFF")

        return code.getcode()

    def LDH(self):
        return self.LD()

    ###################################################################
    #
    # ALU OPERATIONS
    #
    def ALU(self, left, right, op, carry=False):
        lines = []

        left.assign = False
        right.assign = False

        lines.append(f"a = {left.get}")
        lines.append(f"b = {right.get}")

        calc = " ".join(["t", "=", "a", op, "b"])
        if carry:
            lines.append("c = ((cpu.F & (1 << FLAGC)) != 0)")
            calc += " " + op + " c"

        lines.append(calc)

        if self.opcode == 0xE8:
            # E8 and F8 http://forums.nesdev.com/viewtopic.php?p=42138
            lines.extend(self.handleflags16bit_E8_F8(left.get, "v", op, carry))
            lines.append("t &= 0xFFFF")
        elif self.is16bit:
            lines.extend(self.handleflags16bit("a", "b", op, carry))
            lines.append("t &= 0xFFFF")
        else:
            lines.extend(self.handleflags8bit("a", "b", op, carry))
            lines.append("t &= 0xFF")

        # HAS TO BE THE LAST INSTRUCTION BECAUSE OF CP!
        if left.set.count("%") > 1:
            lines.append(left.set % ("t", "t"))
        else:
            lines.append(left.set % "t")
        return lines

    def ADD(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "+"))
        return code.getcode()

    def SUB(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "-"))
        return code.getcode()

    def INC(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        right = Literal(1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "+"))

        if self.opcode == 0x34:
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(-1, "cpu.cycles += 4")  # Inject before read
            code.cycles = ("8",)  # 12 - 4

        return code.getcode()

    def DEC(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        right = Literal(1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "-"))

        if self.opcode == 0x35:
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(-1, "cpu.cycles += 4")  # Inject before write
            code.cycles = ("8",)  # 12 - 4

        return code.getcode()

    def ADC(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "+", carry=True))
        return code.getcode()

    def SBC(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "-", carry=True))
        return code.getcode()

    def AND(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "&"))
        return code.getcode()

    def OR(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "|"))
        return code.getcode()

    def XOR(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = Operand("A")
            right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        code.addlines(self.ALU(left, right, "^"))
        return code.getcode()

    def CP(self):
        r1 = self.name.split()[1]
        left = Operand("A")
        right = Operand(r1)

        code = Code(
            self.name.split()[0], self.opcode, self.name, left.immediate or right.immediate, self.length, self.cycles
        )
        # CP is equal to SUB, but without saving the result.
        # Therefore; we discard the last instruction.
        code.addlines(self.ALU(left, right, "-")[:-1])
        return code.getcode()

    ###################################################################
    #
    # PUSH/POP OPERATIONS
    #
    def PUSH(self):
        r0 = self.name.split()[1]
        left = Operand(r0)

        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        if "HL" in left.get:
            code.addlines(
                [
                    "cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.HL >> 8) # High",
                    "cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.HL & 0xFF) # Low",
                    "cpu.SP -= 2",
                    "cpu.SP &= 0xFFFF",
                ]
            )
        else:
            # A bit of a hack, but you can only push double registers
            code.addline("cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.%s) # High" % left.operand[-2])
            if left.operand == "AF":
                # by taking fx 'A' and 'F' directly, we save calculations
                code.addline("cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.%s & 0xF0) # Low" % left.operand[-1])
            else:
                # by taking fx 'A' and 'F' directly, we save calculations
                code.addline("cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.%s) # Low" % left.operand[-1])
            code.addline("cpu.SP -= 2")
            code.addline("cpu.SP &= 0xFFFF")

        return code.getcode()

    def POP(self):
        r0 = self.name.split()[1]
        left = Operand(r0)

        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        if "HL" in left.get:
            code.addlines(
                [
                    (left.set % "(cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8) + " "cpu.mb.getitem(cpu.SP)") + " # High",
                    "cpu.SP += 2",
                    "cpu.SP &= 0xFFFF",
                ]
            )
        else:
            if left.operand.endswith("F"):  # Catching AF
                fmask = " & 0xF0"
            else:
                fmask = ""
            # See comment from PUSH
            code.addline("cpu.%s = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) # High" % left.operand[-2])
            if left.operand == "AF":
                code.addline("cpu.%s = cpu.mb.getitem(cpu.SP)%s & 0xF0 # Low" % (left.operand[-1], fmask))
            else:
                code.addline("cpu.%s = cpu.mb.getitem(cpu.SP)%s # Low" % (left.operand[-1], fmask))
            code.addline("cpu.SP += 2")
            code.addline("cpu.SP &= 0xFFFF")

        return code.getcode()

    ###################################################################
    #
    # CONTROL FLOW OPERATIONS
    #
    def JP(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = None
            right = Operand(r1)

        r_code = right.get
        if left is not None:
            l_code = left.get
            if l_code.endswith("C") and "NC" not in l_code:
                left.flag = True
                l_code = "((cpu.F & (1 << FLAGC)) != 0)"
            assert left.flag
        elif right.pointer:
            # FIX: Wrongful syntax of "JP (HL)" actually meaning "JP HL"
            right.pointer = False
            r_code = right.codegen(False, operand="HL")
        else:
            assert right.immediate

        code = Code(
            self.name.split()[0], self.opcode, self.name, right.immediate, self.length, self.cycles, branch_op=True
        )
        if left is None:
            code.addlines(["cpu.PC = %s" % ("v" if right.immediate else r_code), "cpu.cycles += " + self.cycles[0]])
        else:
            code.addlines(
                [
                    "if %s:" % l_code,
                    "\tcpu.PC = %s" % ("v" if right.immediate else r_code),
                    "\tcpu.cycles += " + self.cycles[0],
                    "else:",
                    "\tcpu.PC += %s" % self.length,
                    "\tcpu.PC &= 0xFFFF",
                    "\tcpu.cycles += " + self.cycles[1],
                ]
            )

        return code.getcode()

    def JR(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = None
            right = Operand(r1)

        if left is not None:
            l_code = left.get
            if l_code.endswith("C") and "NC" not in l_code:
                left.flag = True
                l_code = "((cpu.F & (1 << FLAGC)) != 0)"
            assert left.flag
        assert right.immediate

        code = Code(
            self.name.split()[0], self.opcode, self.name, right.immediate, self.length, self.cycles, branch_op=True
        )
        if left is None:
            code.addlines(
                [
                    "cpu.PC += %d + " % self.length + inline_signed_int8("v"),
                    "cpu.PC &= 0xFFFF",
                    "cpu.cycles += " + self.cycles[0],
                ]
            )
        else:
            code.addlines(
                [
                    "cpu.PC += %d" % self.length,
                    "if %s:" % l_code,
                    "\tcpu.PC += " + inline_signed_int8("v"),
                    "\tcpu.PC &= 0xFFFF",
                    "\tcpu.cycles += " + self.cycles[0],
                    "else:",
                    "\tcpu.PC &= 0xFFFF",
                    "\tcpu.cycles += " + self.cycles[1],
                ]
            )

        return code.getcode()

    def CALL(self):
        if self.name.find(",") > 0:
            r0, r1 = self.name.split()[1].split(",")
            left = Operand(r0)
            right = Operand(r1)
        else:
            r1 = self.name.split()[1]
            left = None
            right = Operand(r1)

        if left is not None:
            l_code = left.get
            if l_code.endswith("C") and "NC" not in l_code:
                left.flag = True
                l_code = "((cpu.F & (1 << FLAGC)) != 0)"
            assert left.flag
        assert right.immediate

        code = Code(
            self.name.split()[0], self.opcode, self.name, right.immediate, self.length, self.cycles, branch_op=True
        )

        # Taken from PUSH
        code.addlines(
            [
                "cpu.PC += %s" % self.length,
                "cpu.PC &= 0xFFFF",
            ]
        )

        if left is None:
            code.addlines(
                [
                    "cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High",
                    "cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low",
                    "cpu.SP -= 2",
                    "cpu.SP &= 0xFFFF",
                    "cpu.PC = %s" % ("v" if right.immediate else right.get),
                    "cpu.cycles += " + self.cycles[0],
                ]
            )
        else:
            code.addlines(
                [
                    "if %s:" % l_code,
                    "\tcpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High",
                    "\tcpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low",
                    "\tcpu.SP -= 2",
                    "\tcpu.SP &= 0xFFFF",
                    "\tcpu.PC = %s" % ("v" if right.immediate else right.get),
                    "\tcpu.cycles += " + self.cycles[0],
                    "else:",
                    "\tcpu.cycles += " + self.cycles[1],
                ]
            )

        return code.getcode()

    def RET(self):
        if self.name == "RET":
            left = None
        else:
            r0 = self.name.split()[1]
            left = Operand(r0)

            l_code = left.get
            if left is not None:
                if l_code.endswith("C") and "NC" not in l_code:
                    left.flag = True
                    l_code = "((cpu.F & (1 << FLAGC)) != 0)"
                assert left.flag

        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles, branch_op=True)
        if left is None:
            code.addlines(
                [
                    "cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High",
                    "cpu.PC |= cpu.mb.getitem(cpu.SP) # Low",
                    "cpu.SP += 2",
                    "cpu.SP &= 0xFFFF",
                    "cpu.cycles += " + self.cycles[0],
                ]
            )
        else:
            code.addlines(
                [
                    "if %s:" % l_code,
                    "\tcpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High",
                    "\tcpu.PC |= cpu.mb.getitem(cpu.SP) # Low",
                    "\tcpu.SP += 2",
                    "\tcpu.SP &= 0xFFFF",
                    "\tcpu.cycles += " + self.cycles[0],
                    "else:",
                    "\tcpu.PC += %s" % self.length,
                    "\tcpu.PC &= 0xFFFF",
                    "\tcpu.cycles += " + self.cycles[1],
                ]
            )

        return code.getcode()

    def RETI(self):
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles, branch_op=True)
        code.addlines(
            [
                "cpu.interrupt_master_enable = True",
                "cpu.bail = (cpu.interrupts_flag_register & 0b11111) & (cpu.interrupts_enabled_register & 0b11111)",
                "cpu.PC = cpu.mb.getitem((cpu.SP + 1) & 0xFFFF) << 8 # High",
                "cpu.PC |= cpu.mb.getitem(cpu.SP) # Low",
                "cpu.SP += 2",
                "cpu.SP &= 0xFFFF",
                "cpu.cycles += " + self.cycles[0],
            ]
        )

        return code.getcode()

    def RST(self):
        r1 = self.name.split()[1]
        right = Literal(r1)

        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles, branch_op=True)

        # Taken from PUSH and CALL
        code.addlines(
            [
                "cpu.PC += %s" % self.length,
                "cpu.PC &= 0xFFFF",
                "cpu.mb.setitem((cpu.SP-1) & 0xFFFF, cpu.PC >> 8) # High",
                "cpu.mb.setitem((cpu.SP-2) & 0xFFFF, cpu.PC & 0xFF) # Low",
                "cpu.SP -= 2",
                "cpu.SP &= 0xFFFF",
            ]
        )

        code.addlines(
            [
                "cpu.PC = %s" % (right.code),
                "cpu.cycles += " + self.cycles[0],
            ]
        )

        return code.getcode()

    ###################################################################
    #
    # ROTATE/SHIFT OPERATIONS
    #
    def rotateleft(self, name, left, throughcarry=False):
        code = Code(name, self.opcode, self.name, False, self.length, self.cycles)
        left.assign = False

        code.addline(("a = %s" % left.get))

        if throughcarry:
            code.addline(("t = (a << 1)") + " | ((cpu.F & (1 << FLAGC)) != 0)")
        else:
            code.addline("t = (a << 1) | (a >> 7)")
        code.addlines(self.handleflags8bit(left.get, None, None, throughcarry))
        code.addline("t &= 0xFF")
        left.assign = True

        if left.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(left.set % "t")
        return code

    def RLA(self):
        left = Operand("A")
        code = self.rotateleft(self.name.split()[0], left, throughcarry=True)
        return code.getcode()

    def RLCA(self):
        left = Operand("A")
        code = self.rotateleft(self.name.split()[0], left)
        return code.getcode()

    def RLC(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = self.rotateleft(self.name.split()[0], left)
        return code.getcode()

    def RL(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = self.rotateleft(self.name.split()[0], left, throughcarry=True)
        return code.getcode()

    def rotateright(self, name, left, throughcarry=False):
        code = Code(name, self.opcode, self.name, False, self.length, self.cycles)
        left.assign = False

        code.addline(("a = %s" % left.get))

        if throughcarry:
            # Trigger "overflow" for carry flag
            code.addline(("t = (a >> 1)") + " | (((cpu.F & (1 << FLAGC)) != 0) << 7)")
        else:
            # Trigger "overflow" for carry flag
            code.addline("t = (a >> 1) | ((a & 1) << 7)")

        code.addlines(self.handleflagsrotateshift("a", None, None, throughcarry))
        code.addline("t &= 0xFF")

        if left.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(left.set % "t")
        return code

    def RRA(self):
        left = Operand("A")
        code = self.rotateright(self.name.split()[0], left, throughcarry=True)
        return code.getcode()

    def RRCA(self):
        left = Operand("A")
        code = self.rotateright(self.name.split()[0], left)
        return code.getcode()

    def RRC(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = self.rotateright(self.name.split()[0], left)
        return code.getcode()

    def RR(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = self.rotateright(self.name.split()[0], left, throughcarry=True)
        return code.getcode()

    def SLA(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        code.addline("t = (%s << 1)" % left.get)
        code.addlines(self.handleflags8bit(left.get, None, None, False))
        code.addline("t &= 0xFF")

        if left.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(left.set % "t")
        return code.getcode()

    def SRA(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        # FIX: All documentation tells it should have carry enabled
        self.flag_c = "C"
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)

        code.addline(("a = %s" % left.get))

        code.addline("t = ((a >> 1) | (a & 0x80)) | ((a & 1) << 8)")
        code.addlines(self.handleflags8bit(left.get, None, None, False))  # FIX
        code.addline("t &= 0xFF")

        if left.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(left.set % "t")
        return code.getcode()

    def SRL(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)

        code.addline(("a = %s" % left.get))

        code.addline("t = (a >> 1) + ((a & 1) << 8)")
        code.addlines(self.handleflags8bit(left.get, None, None, False))
        code.addline("t &= 0xFF")

        if left.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(left.set % "t")
        return code.getcode()

    def SWAP(self):
        r0 = self.name.split()[1]
        left = Operand(r0)
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)

        code.addline(("a = %s" % left.get))

        code.addline("t = ((a & 0xF0) >> 4) | ((a & 0x0F) << 4)")
        code.addlines(self.handleflags8bit(left.get, None, None, False))
        code.addline("t &= 0xFF")

        if left.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(left.set % "t")
        return code.getcode()

    ###################################################################
    #
    # BIT OPERATIONS
    #
    def BIT(self):
        r0, r1 = self.name.split()[1].split(",")
        left = Literal(r0)
        right = Operand(r1)
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)

        # FIX: Correct cycle count is 12, not 16!
        if right.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 12 - 4

        code.addline("t = %s & (1 << %s)" % (right.get, left.get))
        code.addlines(self.handleflags8bit(left.get, right.get, None, False))

        return code.getcode()

    def RES(self):
        r0, r1 = self.name.split()[1].split(",")
        left = Literal(r0)
        right = Operand(r1)

        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        code.addline("t = %s & ~(1 << %s)" % (right.get, left.get))

        if right.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(right.set % "t")
        return code.getcode()

    def SET(self):
        r0, r1 = self.name.split()[1].split(",")
        left = Literal(r0)
        right = Operand(r1)
        code = Code(self.name.split()[0], self.opcode, self.name, False, self.length, self.cycles)
        code.addline("t = %s | (1 << %s)" % (right.get, left.get))

        if right.operand == "(HL)":
            # HACK: Offset the timing by 4 cycles
            # TODO: Probably should be generalized
            code.lines.insert(0, "cpu.cycles += 4")  # Inject before read
            code.addline("cpu.cycles += 4")
            code.cycles = ("8",)  # 16 - 4 - 4

        code.addline(right.set % "t")
        return code.getcode()
