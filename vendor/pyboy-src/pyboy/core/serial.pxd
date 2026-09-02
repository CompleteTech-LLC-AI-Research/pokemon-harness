#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#

from libc.stdint cimport int64_t, uint8_t, uint16_t, uint32_t, uint64_t

import cython
from cython cimport final

from pyboy.logging.logging cimport Logger

# Constants live in serial.py as plain Python ints; cdef methods that
# need nogil access use local cdef copies at method entry.
cdef Logger logger

@final
cdef class Serial:
    # @final on the class ensures cpdef methods monomorphize to direct C
    # calls (so mb.py can keep its nogil sites) while still being Python-
    # callable for unit tests and the harness coordinator.
    cdef public uint64_t SB, SC
    cdef public bint cgb_mode
    cdef public int64_t _cycles_to_interrupt
    cdef public uint64_t last_cycles, clock, clock_target
    cdef public bint transfer_enabled, double_speed, internal_clock
    cdef public uint8_t cpu_speed_shift

    # Bit-accurate shift-register state.
    cdef public uint8_t _shift_register
    cdef public uint8_t _bits_remaining

    # Peer-side coupling. Default is NullBackend (disconnected cable);
    # the link-cable harness swaps in a NetworkBackend after start.
    cdef public object backend

    # Optional owner-thread pump installed by the link harness. The network
    # reader only queues work; this callback is invoked from the native
    # serial tick on the emulator owner thread while an external transfer is
    # armed, so applying an incoming edge cannot race motherboard execution.
    cdef public object owner_dispatch_callback
    cdef public bint owner_dispatch_enabled

    # Cython 3.0.12 emits cpdef vtable slots for ``unsigned long long``.
    # Using the spelling it uses for the callable ABI avoids a platform
    # typedef mismatch (uint64_t is unsigned long on LP64) while preserving
    # the 64-bit cycle value at the Python/C boundary.
    cpdef bint tick(self, unsigned long long) noexcept nogil

    cpdef void set_SB(self, uint8_t) noexcept nogil
    cpdef void set_SC(self, uint8_t) noexcept nogil

    # Slave-mode / coordinator-driven edges. Python-callable so the
    # coordinator can drive pairs in lockstep. Returns True iff this
    # edge completed the transfer (byte finished, IRQ ready); False
    # otherwise. Raises RuntimeError when called without an armed
    # transfer or on a master-mode core.
    cpdef bint apply_external_edge(self, uint8_t) except *

    # Accept ``object`` rather than the strict ``IntIOInterface`` type so
    # harness unit tests can pass duck-typed streams (see
    # ``tests/test_serial_core.py::_FakeStream``). mb.py always passes
    # the real ``IntIOWrapper``, so there's no runtime cost.
    cpdef int save_state(self, object) except -1
    cpdef int load_state(self, object, int) except -1
