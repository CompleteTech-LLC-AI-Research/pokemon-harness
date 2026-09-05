#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#


from libc.stdint cimport int16_t, int64_t, uint8_t, uint16_t, uint64_t

cimport pyboy.core.mb
from pyboy.utils cimport IntIOInterface

from . cimport opcodes

import cython

from pyboy.logging.logging cimport Logger


cdef Logger logger

cdef uint16_t IF_ADDRESS, IE_ADDRESS
cdef int16_t FLAGC, FLAGH, FLAGN, FLAGZ
cdef uint8_t INTR_VBLANK, INTR_LCDC, INTR_TIMER, INTR_SERIAL, INTR_HIGHTOLOW


@cython.final
cdef class CPU:
    cdef bint interrupt_master_enable, interrupt_queued, halted, stopped, bail

    cdef uint8_t interrupts_flag, interrupts_enabled, interrupts_flag_register, interrupts_enabled_register

    # The harness lockstep coordinator reads the emulated CPU clock to keep
    # two native PyBoy instances on one deterministic cycle horizon.  Keep
    # this public in the Cython ABI, matching the source-runtime attribute.
    cdef public int64_t cycles
    cdef readonly uint64_t retired_instructions

    cdef inline int check_interrupts(self) except * nogil

    @cython.final
    cpdef void set_interruptflag(self, int) noexcept nogil
    cdef bint handle_interrupt(self, uint8_t, uint16_t) except * nogil

    @cython.locals(pc1=uint16_t,pc2=uint16_t,pc3=uint16_t, opcode=uint16_t, v=cython.int, a=cython.int, b=cython.int, result=uint8_t, retired_instructions_max=uint64_t)
    cdef inline uint8_t fetch_and_execute(self) except * nogil
    @cython.locals(_cycles0=int64_t)
    cdef int tick(self, int64_t) except * nogil
    cdef int save_state(self, IntIOInterface) except -1
    cdef int load_state(self, IntIOInterface, int) except -1

    # Only char (8-bit) needed, but I'm not sure all intermittent
    # results do not overflow
    cdef int16_t A, F, B, C, D, E

    # Only short (16-bit) needed, but I'm not sure all intermittent
    # results do not overflow
    cdef int HL, SP, PC

    cdef pyboy.core.mb.Motherboard mb

    cdef str dump_state(self, list) with gil
