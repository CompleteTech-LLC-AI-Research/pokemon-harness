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
    # Compatibility field used by motherboard state migration and older
    # integrations.  New deadlines remain in raw CPU-cycle units.
    cdef public uint8_t cpu_speed_shift

    # Bit-accurate shift-register state.
    cdef public uint8_t _shift_register
    cdef public uint8_t _bits_remaining

    # Peer-side coupling. Default is NullBackend (disconnected cable);
    # the link-cable harness swaps in a NetworkBackend after start.
    cdef public object backend
    # Compatibility surface retained for the original PyBoy link owner.
    # New owner-pump APIs remain available alongside these fields.
    cdef public object owner_dispatch_callback
    cdef public bint owner_dispatch_enabled
    cdef readonly bint backend_failed
    cdef object _backend_error
    cpdef void check_error(self) except *
    cdef object _owner_pump, _pump_thread
    cdef object _owner_pump_claim, _pump_binding_lock
    cdef object _owner_pre_metadata, _owner_post, _owner_time_mapper
    cdef object _owner_callback_thread, _owner_boundary_pending
    cdef readonly bint owner_pump_active
    cdef readonly bint owner_poll_enabled
    cdef readonly uint64_t _boundary_seq
    cdef readonly uint64_t transfer_generation
    cpdef void set_owner_pump(self, object, bint poll=*) except *
    cpdef object claim_owner_pump(self, object, bint poll=*)
    cpdef void release_owner_pump(self, object) except *
    cpdef void dispatch_owner(self) except *
    cpdef void check_execution_allowed(self) except *
    cpdef void set_owner_boundary_callbacks(self, object pre_metadata=*, object post=*) except *
    cpdef void set_owner_time_mapper(self, object mapper=*) except *
    cpdef object _owner_pending_edge_deadlines(self) with gil
    cdef bint owner_boundary(self, int, uint64_t, int, int) noexcept nogil
    cdef bint owner_boundary_post(self, uint64_t, bint committed=*) noexcept nogil
    cdef bint owner_boundary_abort(self, uint64_t) noexcept nogil

    # Keep the exception edge through native owner/backend callbacks. Spell
    # the argument as unsigned long long for Cython 3's cpdef ABI on LP64.
    cpdef bint tick(self, unsigned long long) except * nogil

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
    cpdef int load_state(self, object, int, object legacy_timing=*) except -1
