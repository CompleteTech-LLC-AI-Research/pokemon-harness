#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

from libc.stdint cimport int64_t, uint8_t, uint16_t, uint32_t, uint64_t

import cython
from cython cimport final

cimport pyboy.core.bootrom
cimport pyboy.core.cartridge.base_mbc
cimport pyboy.core.cpu
cimport pyboy.core.interaction
cimport pyboy.core.lcd
cimport pyboy.core.ram
cimport pyboy.core.serial
cimport pyboy.core.sound
cimport pyboy.core.timer
from pyboy.logging.logging cimport Logger
from pyboy.utils cimport IntIOInterface, WindowEvent


cdef Logger logger

cdef int64_t MAX_CYCLES
cdef uint64_t PHYSICAL_CLOCK_MAX
cdef uint16_t STAT, LY, LYC
cdef int INTR_TIMER, INTR_SERIAL, INTR_HIGHTOLOW
cdef uint16_t OPCODE_BRK
cdef int STATE_VERSION

@final
cdef class Motherboard:
    # ``public`` on each submodule field: the link-cable harness reads
    # cpu (for interrupt flags), lcd (frame_done), sound (disable_sampling),
    # and serial (SerialCore) from Python.
    cdef public pyboy.core.interaction.Interaction interaction
    cdef public pyboy.core.bootrom.BootROM bootrom
    cdef public pyboy.core.ram.RAM ram
    cdef public pyboy.core.lcd.LCD lcd
    cdef public pyboy.core.cpu.CPU cpu
    cdef public pyboy.core.timer.Timer timer
    cdef public pyboy.core.serial.Serial serial
    cdef public pyboy.core.sound.Sound sound
    cdef public pyboy.core.cartridge.base_mbc.BaseMBC cartridge
    cdef bint bootrom_enabled
    cdef char[1024] serialbuffer
    cdef uint16_t serialbuffer_count

    # CGB
    cdef HDMA hdma
    cdef uint8_t key0, key1, wram_select
    cdef uint8_t[4] cgb_undocumented
    cdef readonly bint double_speed
    cdef uint64_t _physical_clock, _physical_last_cycles, _physical_clock_epoch
    cdef bint _physical_clock_fault, _physical_load_incomplete
    cdef object _serial_time_segments, _serial_raw_offset
    cdef readonly bint cgb, cgb_mode

    # Optional execution governor; callbacks are installed only by the setter.
    cdef readonly object execution_before, execution_after
    cdef bint _execution_governor_active, _execution_governor_enabled
    cdef readonly uint64_t speed_transition_count
    cdef readonly int64_t speed_transition_clock
    cdef readonly bint speed_transition_double_speed
    cpdef void set_execution_governor(self, object before, object after) except * with gil

    # Box snapshots before subtraction so raw-clock/count differences cannot
    # overflow signed C arithmetic at native integer boundaries.
    @cython.locals(
        start_raw=object, end_raw=object, start_count=object,
        transition_count=object, actual_delta=object,
        start_transition_clock=object, transition_clock=object,
    )
    cpdef void _execution_step(self) except * with gil

    cdef dict breakpoints
    # public: harness lockstep coordinator drives tick/breakpoint loop per peer
    cdef public bint breakpoint_singlestep
    cdef public bint breakpoint_singlestep_latch
    cdef int64_t breakpoint_waiting
    cpdef int64_t breakpoint_add(self, int64_t, int64_t) except -1 with gil
    cpdef int64_t breakpoint_remove(self, int64_t, int64_t) except -1 with gil
    cpdef tuple breakpoint_reached(self) with gil
    cpdef void breakpoint_reinject(self) noexcept with gil

    cdef void buttonevent(self, WindowEvent) noexcept
    cdef void stop(self, bint, object, object) noexcept
    @cython.locals(cycles=int64_t, cycles_target=int64_t, mode0_cycles=int64_t, breakpoint_index=int64_t)
    # Serial/network callbacks can raise from Serial.tick or the owner
    # dispatch hook. Keep the exception edge all the way through the
    # motherboard instead of converting it into an unraisable exception.
    cpdef bint tick(self) except * with gil
    cpdef bint service_serial_irq(self, object max_instructions) except * with gil

    cdef void switch_speed(self) noexcept nogil
    @cython.locals(cycles=uint64_t, delta=uint64_t, rate=uint64_t)
    cdef void _sync_physical_clock(self) noexcept nogil
    cpdef void _prune_serial_time_segments(self) with gil
    cpdef tuple _map_serial_boundary_time(self, int, uint64_t, uint64_t) with gil
    cpdef tuple get_physical_clock(self) with gil

    cdef uint8_t getitem(self, uint16_t) except * nogil
    @final
    cdef void setitem(self, uint16_t, uint8_t) except * nogil
    cdef uint8_t getitem_io_ports(self, uint16_t) except * nogil
    cdef void setitem_io_ports(self, uint16_t, uint8_t) except * nogil

    @cython.locals(offset=cython.int, dst=cython.int, n=cython.int)
    cdef void transfer_DMA(self, uint8_t) except * nogil
    cdef int save_state(self, IntIOInterface) except -1
    cdef int load_state(self, IntIOInterface) except -1

@final
cdef class HDMA:
    cdef uint8_t hdma1
    cdef uint8_t hdma2
    cdef uint8_t hdma3
    cdef uint8_t hdma4
    cdef uint8_t hdma5
    cdef uint8_t _hdma5

    cdef bint transfer_active
    cdef uint16_t curr_src
    cdef uint16_t curr_dst

    cdef void set_hdma5(self, uint8_t, Motherboard) except * nogil
    cdef int tick(self, Motherboard) except * nogil

    cdef int save_state(self, IntIOInterface) except -1
    cdef int load_state(self, IntIOInterface, int) except -1
