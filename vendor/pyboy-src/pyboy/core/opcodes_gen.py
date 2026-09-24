#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

import re
import sys
from html.parser import HTMLParser
from urllib.request import urlopen

destination = "opcodes.py"
pxd_destination = "opcodes.pxd"

warning = """
# THIS FILE IS AUTO-GENERATED!!!
# DO NOT MODIFY THIS FILE.
# CHANGES TO THE CODE SHOULD BE MADE IN 'opcodes_gen.py'.
"""

imports = """
import array

import pyboy
logger = pyboy.logging.get_logger(__name__)

FLAGC, FLAGH, FLAGN, FLAGZ = range(4, 8)

def BRK(cpu):
    cpu.bail = True
    cpu.mb.breakpoint_singlestep = 1
    cpu.mb.breakpoint_singlestep_latch = 0
    # NOTE: We do not increment PC
    return 0

"""

cimports = """
cimport cython
from libc.stdint cimport uint8_t, uint16_t, uint32_t, int32_t

from pyboy.logging.logging cimport Logger

from . cimport cpu


cdef Logger logger

cdef uint16_t FLAGC, FLAGH, FLAGN, FLAGZ
cdef uint8_t[512] OPCODE_LENGTHS
cdef int execute_opcode(cpu.CPU, uint16_t, uint16_t) except * nogil

cdef uint8_t no_opcode(cpu.CPU) noexcept nogil
cdef uint8_t BRK(cpu.CPU) noexcept nogil
"""


def inline_signed_int8(arg):
    return "(({} ^ 0x80) - 0x80)".format(arg)


opcodes = []


class MyHTMLParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)

        self.counter = 0
        self.tagstack = []

        self.cell_lines = []
        self.stop = False
        self._attrs = None
        self.founddata = False

    def handle_starttag(self, tag, attrs):
        if tag != "br":
            self.founddata = False
            self._attrs = attrs
            self.tagstack.append(tag)

    def handle_endtag(self, tag):
        if not self.founddata and self.tagstack[-1] == "td" and self.counter % 0x100 != 0:
            self.counter += 1
            opcodes.append(None)  # Blank operations
        self.tagstack.pop()

    def handle_data(self, data):
        if self.stop or len(self.tagstack) == 0:
            return

        self.founddata = True

        if self.tagstack[-1] == "td":
            self.cell_lines.append(data)

            if len(self.cell_lines) == 4:
                opcodes.append(
                    self.make_opcode(
                        self.cell_lines, ("bgcolor", "#ccffcc") in self._attrs or ("bgcolor", "#ffcccc") in self._attrs
                    )
                )
                self.counter += 1
                self.cell_lines = []

        if self.counter == 0x200:
            self.stop = True

    def make_opcode(self, lines, bit16):
        opcode = self.counter
        flags = lines.pop()
        cycles = lines.pop()
        length = lines.pop()
        name = lines.pop()

        return OpcodeData(opcode, name, length, cycles, bit16, *flags.split())


class Operand:
    def __init__(self, operand):
        self.postoperation = None
        self.pointer = False
        self.highpointer = False
        self.immediate = False
        self.signed = False
        self.is16bit = False
        self.flag = False
        self.operand = operand
        self.codegen(False)

    @property
    def set(self):
        return self.codegen(True)

    @property
    def get(self):
        return self.codegen(False)

    def codegen(self, assign, operand=None):
        if operand is None:
            operand = self.operand

        if operand == "(C)":
            self.highpointer = True
            if assign:
                return "cpu.mb.setitem_io_ports(0xFF00 | cpu.C, %s)"
            else:
                return "cpu.mb.getitem_io_ports(0xFF00 | cpu.C)"

        elif operand == "SP+r8":
            self.immediate = True
            self.signed = True

            # post operation set in LD handler!
            return "cpu.SP + " + inline_signed_int8("v")

        elif operand.startswith("(") and operand.endswith(")"):
            _operand = re.search(r"\(([a-zA-Z]+\d*)[\+-]?\)", operand).group(1)
            self.pointer = True
            if assign:
                code = "cpu.mb.setitem_io_ports" if self.highpointer else "cpu.mb.setitem"
                code += "(%s" % self.codegen(False, operand=_operand) + ", %s)"
            else:
                code = "cpu.mb.getitem_io_ports" if self.highpointer else "cpu.mb.getitem"
                code += "(%s)" % self.codegen(False, operand=_operand)

            if "-" in operand or "+" in operand:
                # TODO: Replace with opcode 23 (INC HL)?
                self.postoperation = "cpu.HL %s= 1" % operand[-2]

            return code

        # Sadly, there is an overlap between the register 'C' and to
        # check for the carry flag 'C'.
        elif operand in ["A", "F", "B", "C", "D", "E", "SP", "PC", "HL"]:
            if assign:
                return "cpu." + operand + " = %s"
            else:
                return "cpu." + operand

        elif operand == "H":
            if assign:
                return "cpu.HL = (cpu.HL & 0x00FF) | (%s << 8)"
            else:
                return "(cpu.HL >> 8)"

        elif operand == "L":
            if assign:
                return "cpu.HL = (cpu.HL & 0xFF00) | (%s & 0xFF)"
            else:
                return "(cpu.HL & 0xFF)"

        elif operand in ["AF", "BC", "DE"]:
            if assign:
                return "cpu." + operand[0] + " = %s >> 8" + "\n\t" + "cpu." + operand[1] + " = %s & 0x00FF"
            else:
                return "((cpu." + operand[0] + " << 8) + cpu." + operand[1] + ")"

        elif operand in ["Z", "C", "NZ", "NC"]:  # flags
            assert not assign
            self.flag = True

            if "N" in operand:
                return f"((cpu.F & (1 << FLAG{operand[1]})) == 0)"
            else:
                return f"((cpu.F & (1 << FLAG{operand})) != 0)"
            # return "f_" + operand.lower() + "(cpu)"

        elif operand in ["d8", "d16", "a8", "a16", "r8"]:
            assert not assign
            code = "v"
            self.immediate = True

            if operand == "r8":
                code = inline_signed_int8(code)
                self.signed = True
            elif operand == "a8":
                code += " | 0xFF00"
                self.highpointer = True
            return code

        else:
            raise ValueError("Didn't match symbol: %s" % operand)


class Literal:
    def __init__(self, value):
        if isinstance(value, str) and value.find("H") > 0:
            self.value = int(value[:-1], 16)
        else:
            self.value = value
        self.code = str(self.value)
        self.immediate = False

    @property
    def get(self):
        return self.code


class Code:
    def __init__(self, function_name, opcode, name, takes_immediate, length, cycles, branch_op=False):
        self.function_name = function_name
        self.opcode = opcode
        self.name = name
        self.cycles = cycles
        self.takes_immediate = takes_immediate
        self.length = length
        self.lines = []
        self.branch_op = branch_op

    def addline(self, line):
        self.lines.append(line)

    def addlines(self, lines):
        for l in lines:
            self.lines.append(l)

    def getcode(self):
        code = ""
        code += [
            "def %s_%0.2X(cpu): # %0.2X %s" % (self.function_name, self.opcode, self.opcode, self.name),
            "def %s_%0.2X(cpu, v): # %0.2X %s" % (self.function_name, self.opcode, self.opcode, self.name),
        ][self.takes_immediate]
        code += "\n\t"

        if not self.branch_op:
            self.lines.append("cpu.PC += %d" % self.length)
            self.lines.append("cpu.PC &= 0xFFFF")
            self.lines.append("cpu.cycles += " + self.cycles[0])  # Choose the 0th cycle count

        code += "\n\t".join(self.lines)

        pxd = [
            "cdef uint8_t %s_%0.2X(cpu.CPU) except * nogil # %0.2X %s"
            % (self.function_name, self.opcode, self.opcode, self.name),
            # TODO: Differentiate between 16-bit values
            # (01,11,21,31 ops) and 8-bit values for 'v'
            "cdef uint8_t %s_%0.2X(cpu.CPU, int v) except * nogil # %0.2X %s"
            % (self.function_name, self.opcode, self.opcode, self.name),
        ][self.takes_immediate]

        if self.opcode == 0x27:
            pxd = "@cython.locals(flag=uint8_t, t=int, corr=uint16_t)\n" + pxd
        else:
            pxd = "@cython.locals(a=int32_t, b=int32_t, c=int32_t, flag=uint8_t, t=int32_t)\n" + pxd

        return (pxd, code)



if __name__ == "__main__" and not __package__:  # script invocation keeps one module identity
    sys.modules.setdefault("opcodes_gen", sys.modules["__main__"])

try:  # package-relative import when the generator is imported as a module
    from .opcodes_gen_handlers import OpcodeDataHandlers
except ImportError:  # script/standalone import: the generator modules sit side by side
    from opcodes_gen_handlers import OpcodeDataHandlers


class OpcodeData(OpcodeDataHandlers):
    def __init__(self, opcode, name, length, cycles, bit16, flag_z, flag_n, flag_h, flag_c):
        self.opcode = opcode
        self.name = name
        self.length = int(length)
        self.cycles = tuple(cycles.split("/"))
        self.flag_z = flag_z
        self.flag_n = flag_n
        self.flag_h = flag_h
        self.flag_c = flag_c
        self.flags = tuple(enumerate([self.flag_c, self.flag_h, self.flag_n, self.flag_z]))
        self.is16bit = bit16

        # TODO: There's no need for this to be so explicit
        self.functionhandlers = {
            "NOP": self.NOP,
            "HALT": self.HALT,
            "PREFIX": self.CB,
            "EI": self.EI,
            "DI": self.DI,
            "STOP": self.STOP,
            "LD": self.LD,
            "LDH": self.LDH,
            "ADD": self.ADD,
            "SUB": self.SUB,
            "INC": self.INC,
            "DEC": self.DEC,
            "ADC": self.ADC,
            "SBC": self.SBC,
            "AND": self.AND,
            "OR": self.OR,
            "XOR": self.XOR,
            "CP": self.CP,
            "PUSH": self.PUSH,
            "POP": self.POP,
            "JP": self.JP,
            "JR": self.JR,
            "CALL": self.CALL,
            "RET": self.RET,
            "RETI": self.RETI,
            "RST": self.RST,
            "DAA": self.DAA,
            "SCF": self.SCF,
            "CCF": self.CCF,
            "CPL": self.CPL,
            "RLA": self.RLA,
            "RLCA": self.RLCA,
            "RLC": self.RLC,
            "RL": self.RL,
            "RRA": self.RRA,
            "RRCA": self.RRCA,
            "RRC": self.RRC,
            "RR": self.RR,
            "SLA": self.SLA,
            "SRA": self.SRA,
            "SWAP": self.SWAP,
            "SRL": self.SRL,
            "BIT": self.BIT,
            "RES": self.RES,
            "SET": self.SET,
        }

    def createfunction(self):
        text = self.functionhandlers[self.name.split()[0]]()
        # Compensate for CB operations being "2 bytes long"
        if self.opcode > 0xFF:
            self.length -= 1
        return (self.length, "%s_%0.2X" % (self.name.split()[0], self.opcode), self.name), text

    # Special carry and half-carry for E8 and F8:
    # http://forums.nesdev.com/viewtopic.php?p=42138
    # Blargg: "Both of these set carry and half-carry based on the low
    # byte of SP added to the UNSIGNED immediate byte. The Negative
    # and Zero flags are always cleared. They also calculate SP +
    # SIGNED immediate byte and put the result into SP or HL,
    # respectively."
    def handleflags16bit_E8_F8(self, r0, r1, op, carry=False):
        flagmask = sum(map(lambda nf: (nf[1] == "-") << (nf[0] + 4), self.flags))

        # Only in case we do a dynamic operation, do we include the
        # following calculations
        if flagmask == 0b11110000:
            return ["# No flag operations"]

        lines = []
        # Sets the flags that always get set by operation
        lines.append("flag = " + format(sum(map(lambda nf: (nf[1] == "1") << (nf[0] + 4), self.flags)), "#010b"))

        # flag |= (((cpu.SP & 0xF) + (v & 0xF)) > 0xF) << FLAGH
        if self.flag_h == "H":
            c = " %s ((cpu.F & (1 << FLAGC)) != 0)" % op if carry else ""
            lines.append("flag |= (((%s & 0xF) %s (%s & 0xF)%s) > 0xF) << FLAGH" % (r0, op, r1, c))

        # flag |= (((cpu.SP & 0xFF) + (v & 0xFF)) > 0xFF) << FLAGC
        if self.flag_c == "C":
            lines.append("flag |= (((%s & 0xFF) %s (%s & 0xFF)%s) > 0xFF) << FLAGC" % (r0, op, r1, c))

        # Clears all flags affected by the operation
        lines.append("cpu.F = 0b00000000")  # E8 and F8 clears N and Z. The rest are dynamic
        lines.append("cpu.F |= flag")
        return lines

    def handleflags16bit(self, r0, r1, op, carry=False):
        flagmask = sum(map(lambda nf: (nf[1] == "-") << (nf[0] + 4), self.flags))

        # Only in case we do a dynamic operation, do we include the
        # following calculations
        if flagmask == 0b11110000:
            return ["# No flag operations"]

        lines = []
        # Sets the ones that always get set by operation
        lines.append("flag = " + format(sum(map(lambda nf: (nf[1] == "1") << (nf[0] + 4), self.flags)), "#010b"))

        if self.flag_h == "H":
            c = " %s ((cpu.F & (1 << FLAGC)) != 0)" % op if carry else ""
            lines.append("flag |= (((%s & 0xFFF) %s (%s & 0xFFF)%s) > 0xFFF) << FLAGH" % (r0, op, r1, c))

        if self.flag_c == "C":
            lines.append("flag |= (t > 0xFFFF) << FLAGC")

        # Clears all flags affected by the operation
        lines.append("cpu.F &= " + format(flagmask, "#010b"))
        lines.append("cpu.F |= flag")
        return lines

    def handleflags8bit(self, r0, r1, op, carry=False):
        flagmask = sum(map(lambda nf: (nf[1] == "-") << (nf[0] + 4), self.flags))

        # Only in case we do a dynamic operation, do we include the
        # following calculations
        if flagmask == 0b11110000:
            return ["# No flag operations"]

        lines = []
        # Sets the ones that always get set by operation
        lines.append("flag = " + format(sum(map(lambda nf: (nf[1] == "1") << (nf[0] + 4), self.flags)), "#010b"))

        if self.flag_z == "Z":
            lines.append("flag |= ((t & 0xFF) == 0) << FLAGZ")

        if self.flag_h == "H" and op == "-":
            c = " %s ((cpu.F & (1 << FLAGC)) != 0)" % op if carry else ""
            lines.append("flag |= (((%s & 0xF) %s (%s & 0xF)%s) < 0) << FLAGH" % (r0, op, r1, c))
        elif self.flag_h == "H":
            c = " %s ((cpu.F & (1 << FLAGC)) != 0)" % op if carry else ""
            lines.append("flag |= (((%s & 0xF) %s (%s & 0xF)%s) > 0xF) << FLAGH" % (r0, op, r1, c))

        if self.flag_c == "C" and op == "-":
            lines.append("flag |= (t < 0) << FLAGC")
        elif self.flag_c == "C":
            lines.append("flag |= (t > 0xFF) << FLAGC")

        # Clears all flags affected by the operation
        lines.append("cpu.F &= " + format(flagmask, "#010b"))
        lines.append("cpu.F |= flag")
        return lines

    def handleflagsrotateshift(self, r0, r1, op, carry=False):
        flagmask = sum(map(lambda nf: (nf[1] == "-") << (nf[0] + 4), self.flags))

        # Only in case we do a dynamic operation, do we include the
        # following calculations
        if flagmask == 0b11110000:
            return ["# No flag operations"]

        lines = []

        assert all(f != "1" for pos, f in self.flags)  # Assert no unconditional flags set
        lines.append("flag = 0b00000000")

        if self.flag_z == "Z":
            lines.append("flag |= ((t & 0xFF) == 0) << FLAGZ")
        if self.flag_c == "C":
            lines.append(f"flag |= ({r0} & 1) << FLAGC")

        # Clears all flags affected by the operation
        lines.append("cpu.F &= " + format(flagmask, "#010b"))
        lines.append("cpu.F |= flag")
        return lines



def update():
    response = urlopen("http://pastraiser.com/cpu/gameboy/gameboy_opcodes.html")
    html = response.read().replace(b"&nbsp;", b"<br>").decode()

    parser = MyHTMLParser()
    parser.feed(html)

    opcodefunctions = map(lambda x: (None, None) if x is None else x.createfunction(), opcodes)

    with open(destination, "w") as f, open(pxd_destination, "w") as f_pxd:
        f.write(warning)
        f.write(imports)
        f_pxd.write(warning)
        f_pxd.write(cimports)
        lookuplist = []
        for lookuptuple, code in opcodefunctions:
            lookuplist.append(lookuptuple)

            if code is None:
                continue

            (pxd, functiontext) = code

            # breakpoint()
            f_pxd.write(pxd + "\n")
            f.write(functiontext.replace("\t", " " * 4) + "\n\n\n")

        # We create a new opcode to use as a software breakpoint instruction.
        # I hope the irony of the opcode number is not lost.
        lookuplist[0xDB] = (1, "BRK", "Breakpoint/Illegal opcode")

        f.write("def no_opcode(cpu):\n    return 0\n\n\n")

        f.write(
            """
def execute_opcode(cpu, opcode, v):

"""
        )

        indent = 4
        for i, t in enumerate(lookuplist):
            t = t if t is not None else (0, "no_opcode", "")
            f.write(
                " " * indent
                + ("if" if i == 0 else "elif")
                + " opcode == 0x%0.2X:\n" % i
                + " " * (indent + 4)
                + "return "
                + str(t[1]).replace("'", "")
                + ("(cpu)" if t[0] <= 1 else "(cpu, v)")
                + "\n"
            )
        f.write("\n\n")

        f.write('OPCODE_LENGTHS = array.array("B", [\n    ')
        for i, t in enumerate(lookuplist):
            t = t if t is not None else (0, "no_opcode", "")
            f.write(str(t[0]).replace("'", "") + ",")
            if (i + 1) % 16 == 0:
                f.write("\n" + " " * 4)
            else:
                f.write(" ")

        f.write("])\n")

        f.write("\n\n")
        f.write("CPU_COMMANDS = [\n    ")
        for _, t in enumerate(lookuplist):
            t = t if t is not None else (0, "no_opcode", "")
            f.write(f'"{t[2]}",\n' + " " * 4)

        f.write("]\n")


def load():
    # if os.path.exists(destination):
    #     return
    update()


if __name__ == "__main__":
    load()
