"""Authored opcode-shaped data; not a ROM or a PyBoy gameplay fixture."""


def make_sources(count=512):
    source = """# Authored compiler/loader fixture, not actual Game Boy instructions.
import array
FLAGC, FLAGH, FLAGN, FLAGZ = range(4, 8)

def BRK(cpu):
    cpu.bail = True
    return 0

"""
    declarations = """cimport cython
from libc.stdint cimport uint8_t, uint16_t, uint64_t
from . cimport cpu
cdef uint16_t FLAGC, FLAGH, FLAGN, FLAGZ
cdef uint8_t[512] OPCODE_LENGTHS
cdef int execute_opcode(cpu.CPU, uint16_t, uint16_t) except * nogil
cdef uint8_t BRK(cpu.CPU) noexcept nogil
cdef uint8_t no_opcode(cpu.CPU) noexcept nogil
"""
    for opcode in range(count):
        name = f"OP_{opcode:02X}"
        source += f"""def {name}(cpu, v):
    cpu.A = (cpu.A + v + {opcode}) & 0xFF
    cpu.F = (cpu.A == 0) << FLAGZ
    cpu.PC = (cpu.PC + 2) & 0xFFFF
    cpu.cycles += 4


"""
        declarations += (
            "@cython.locals(v=int)\n" + f"cdef uint8_t {name}(cpu.CPU, int v) except * nogil\n"
        )
    source += """def no_opcode(cpu):
    return 0


def execute_opcode(cpu, opcode, v):

"""
    for opcode in range(512):
        instruction = f"OP_{opcode % count:02X}(cpu, v)"
        if opcode == 0xDB:
            instruction = "BRK(cpu)"
        elif opcode == 0xD3:
            instruction = "no_opcode(cpu)"
        source += (
            "    "
            + ("if" if opcode == 0 else "elif")
            + f" opcode == 0x{opcode:02X}:\n        return {instruction}\n"
        )
    source += (
        '\n\nOPCODE_LENGTHS = array.array("B", ['
        + ", ".join(["2"] * 512)
        + "])\n\nCPU_COMMANDS = [\n"
        + "".join(f'    "fixture {opcode}",\n' for opcode in range(512))
        + "]\n"
    )
    return source.encode(), declarations.encode()
