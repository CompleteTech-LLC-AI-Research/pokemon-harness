"""``PyBoyLinkSession`` — public surface for linking PyBoy instances.

Milestone 4 of the design doc (:doc:`docs/pyboy_serial_overhaul_design.md`).
Given one or two PyBoy instances, this installs a
:class:`~pokered_harness.link.serial_core.SerialCore` onto each in
place of PyBoy's legacy ``pyboy.core.serial.Serial`` and pairs them
under a :class:`~pokered_harness.link.serial_coordinator.LockstepCoordinator`.

Usage
-----

::

    from pyboy import PyBoy
    from pokered_harness.link import PyBoyLinkSession

    a = PyBoy("red.gb", window="null")
    b = PyBoy("blue.gb", window="null")

    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)

    # In-game code on both sides now sees a bit-accurate serial bridge.
    # Drive both emulators in interleaved chunks so their serial edges
    # stay near-aligned:
    while not done:
        link.step(frames=1)

    link.detach_all()

Design notes
------------

* ``attach`` swaps out ``pyboy.mb.serial`` with a fresh
  :class:`SerialCore`. Register state (``SB``/``SC``) is copied across
  so mid-game attaches don't visibly disturb the emulator.
* Once *both* sides attach, a :class:`LockstepCoordinator` is
  created, wiring each core's backend to the other. Until then, the
  first side's core uses a :class:`NullBackend` and behaves like a
  disconnected cable — matching the "not yet connected" phase in
  Pokémon's own link-cable code.
* ``detach`` restores the original ``pyboy.mb.serial`` object; PyBoy
  returns to its legacy behavior.
* ``step`` interleaves the two emulators in small frame chunks. Per-edge
  lockstep (mGBA-style) is a later refinement — per-frame is sufficient
  for Gen I Pokémon because the protocol uses software timing loops
  that tolerate modest skew and the :class:`CoordinatedBackend` falls
  back to pull-up when one side isn't armed yet.

This class is the natural extension point for the network backend
(milestone 8): :meth:`listen` / :meth:`connect` classmethods would
pair a single local core with a TCP-backed remote endpoint instead
of a second local core.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pokered_harness.link.serial_coordinator import LockstepCoordinator
from pokered_harness.link.serial_core import SerialCore


@runtime_checkable
class _PyBoyLike(Protocol):
    """Minimal duck-type the session needs from a PyBoy instance.

    Real :class:`pyboy.PyBoy` satisfies this. A test fake need only
    expose a ``mb`` with a swappable ``serial`` attribute and a
    ``tick`` method.
    """

    mb: object

    def tick(self, count: int = 1, render: bool = True, sound: bool = False) -> bool:
        ...


class PyBoyLinkSession:
    """Pairs up to two PyBoy instances under a bit-accurate serial link."""

    #: Max attached instances. Gen I Pokémon is strictly 2-player.
    MAX_ATTACHED: int = 2

    def __init__(self) -> None:
        self._pyboys: list[object] = []
        self._cores: list[SerialCore] = []
        self._originals: list[object] = []
        self._coord: LockstepCoordinator | None = None

    # --- construction --------------------------------------------------

    @classmethod
    def local(cls) -> "PyBoyLinkSession":
        """Create a local two-instance session.

        (For symmetry with an eventual :meth:`connect`/:meth:`listen` —
        today ``local`` is the only constructor.)
        """
        return cls()

    # --- attach / detach -----------------------------------------------

    def attach(self, pyboy: _PyBoyLike) -> SerialCore:
        """Install a :class:`SerialCore` on ``pyboy.mb``.

        Returns the core so callers can inspect it directly. If this is
        the second attachment the pair becomes paired via a
        :class:`LockstepCoordinator`.

        Raises ``RuntimeError`` if ``pyboy`` is already attached or if
        the session is already at :attr:`MAX_ATTACHED`.
        """
        if pyboy in self._pyboys:
            raise RuntimeError(f"already attached: {pyboy!r}")
        if len(self._pyboys) >= self.MAX_ATTACHED:
            raise RuntimeError(
                f"session is full ({self.MAX_ATTACHED} instances max)"
            )

        mb = pyboy.mb
        original = mb.serial
        core = SerialCore()
        # Preserve the legacy serial's current register state so the
        # motherboard's next read of SB/SC sees the same byte it would
        # have seen without the swap. Legacy Serial also has these
        # attributes.
        core.SB = int(getattr(original, "SB", 0xFF)) & 0xFF
        core.SC = int(getattr(original, "SC", 0x00)) & 0xFF
        core.last_cycles = int(getattr(original, "last_cycles", 0))
        core.clock = int(getattr(original, "clock", 0))

        mb.serial = core
        self._pyboys.append(pyboy)
        self._cores.append(core)
        self._originals.append(original)

        # When the second side comes in, wire up the coordinator with
        # IRQ callbacks pointing at each motherboard's CPU so slave-side
        # transfer completion (which happens via the peer driving edges,
        # bypassing our own mb.tick path) still wakes halted code.
        if len(self._cores) == 2:
            self._coord = LockstepCoordinator(
                self._cores[0],
                self._cores[1],
                on_a_transfer_complete=self._make_serial_irq_raiser(
                    self._pyboys[0]
                ),
                on_b_transfer_complete=self._make_serial_irq_raiser(
                    self._pyboys[1]
                ),
            )

        return core

    @staticmethod
    def _make_serial_irq_raiser(pyboy):
        """Return a zero-arg closure that raises INTR_SERIAL on
        ``pyboy``'s CPU. Looked up lazily per call so nothing breaks
        if the CPU object gets reconstructed (unusual but possible
        after load_state).

        Defensive: if the attached object is a test fake without a
        ``mb.cpu.set_interruptflag``, the callback no-ops. Real PyBoy
        always has both.
        """
        INTR_SERIAL = 0x08  # IF bit 3, same constant PyBoy uses

        def _raise():
            cpu = getattr(getattr(pyboy, "mb", None), "cpu", None)
            if cpu is not None and hasattr(cpu, "set_interruptflag"):
                cpu.set_interruptflag(INTR_SERIAL)

        return _raise

    def detach(self, pyboy: _PyBoyLike) -> None:
        """Restore ``pyboy``'s original ``mb.serial`` and (if paired)
        tear down the coordinator. No-op if ``pyboy`` isn't attached."""
        if pyboy not in self._pyboys:
            return
        # Tearing down the coordinator first ensures neither remaining
        # core keeps a stale CoordinatedBackend pointing at the
        # detached peer.
        if self._coord is not None:
            self._coord.detach()
            self._coord = None
        idx = self._pyboys.index(pyboy)
        original = self._originals[idx]
        pyboy.mb.serial = original
        self._pyboys.pop(idx)
        self._cores.pop(idx)
        self._originals.pop(idx)

    def detach_all(self) -> None:
        """Detach every attached PyBoy in reverse order."""
        for pyboy in list(reversed(self._pyboys)):
            self.detach(pyboy)

    # --- accessors -----------------------------------------------------

    @property
    def attached(self) -> tuple[object, ...]:
        return tuple(self._pyboys)

    @property
    def cores(self) -> tuple[SerialCore, ...]:
        return tuple(self._cores)

    @property
    def coordinator(self) -> LockstepCoordinator | None:
        """``None`` until both sides attach."""
        return self._coord

    @property
    def paired(self) -> bool:
        return self._coord is not None

    # --- step ----------------------------------------------------------

    def step(self, frames: int = 1, render: bool = False) -> None:
        """Advance both attached PyBoy instances by ``frames`` frames.

        Interleaves one frame at a time on each side. Sufficient for
        phases where the game isn't in a tight serial-sync loop —
        preamble handshakes, dialog advancement, overworld movement.
        For the tight nibble-exchange loop in
        ``Serial_SyncAndExchangeNybble`` use :meth:`step_interleaved`
        instead; per-frame granularity is too coarse there because
        each side can complete ~17 full-byte serial transfers within
        a single frame while the peer is frozen.

        Raises ``RuntimeError`` if fewer than 2 instances are attached.
        """
        if len(self._pyboys) != self.MAX_ATTACHED:
            raise RuntimeError(
                f"step() requires {self.MAX_ATTACHED} attached instances, "
                f"have {len(self._pyboys)}"
            )
        for _ in range(frames):
            for pyboy in self._pyboys:
                pyboy.tick(1, render)

    def step_interleaved(
        self, frames: int = 1, *, chunk_cycles: int = 256
    ) -> None:
        """Advance both PyBoys by ``frames`` frames with sub-frame
        interleaving for tight serial-sync phases.

        Instead of ticking one whole frame on each side, this alternates
        ~``chunk_cycles`` CPU cycles per side. That keeps the two CPUs
        close enough that a Pokémon serial-sync loop — which oscillates
        a side between SC=0x80 (slave) and SC=0x81 (master) several
        times per byte — sees its peer in the matching role. Per-frame
        interleaving (``step``) is too coarse for this because each
        frame fits ~17 full-byte transfers, so one side can burn its
        whole sync-loop iteration while the peer is frozen.

        Implementation detail: uses PyBoy's ``breakpoint_singlestep``
        mode to force ``mb.tick`` to return after every CPU instruction,
        then batches instructions into chunks. This mode is only
        available on the non-Cython PyBoy build.
        """
        if len(self._pyboys) != self.MAX_ATTACHED:
            raise RuntimeError(
                f"step_interleaved() requires {self.MAX_ATTACHED} "
                f"attached instances, have {len(self._pyboys)}"
            )
        # ~7 cycles per single-stepped mb.tick call (empirical).
        ticks_per_chunk = max(1, chunk_cycles // 7)
        a, b = self._pyboys[0], self._pyboys[1]
        for _ in range(frames):
            self._interleave_one_frame(a, b, ticks_per_chunk)

    @staticmethod
    def _interleave_one_frame(a, b, ticks_per_chunk: int) -> None:
        """Drive ``a`` and ``b`` through one frame each, interleaved."""
        # Per-frame setup mirrors what pyboy._tick does.
        for p in (a, b):
            p._handle_events(p.events)
            p.mb.lcd.frame_done = False
            p.mb.lcd.disable_renderer = True
            p.mb.sound.disable_sampling = True
            p.mb.sound.clear_buffer()

        # Drive both through mb.tick with singlestep on. Replicates the
        # hook-firing logic from pyboy._tick's inner while-loop so our
        # test-side hook counters still fire.
        def _step_chunk(p, n: int) -> bool:
            """Advance ``p`` up to ``n`` mb.tick()s or until frame_done.
            Returns True if the frame completed."""
            for _ in range(n):
                if p.mb.lcd.frame_done:
                    return True
                # Re-arm singlestep every iteration so mb.tick returns
                # after a single CPU instruction — breakpoint handling
                # below may clear it.
                p.mb.breakpoint_singlestep = 1
                if p.mb.tick():
                    # Breakpoint/singlestep return. Mirror pyboy._tick's
                    # hook-firing logic (best-effort — skips plugin
                    # manager, which isn't load-bearing for tests).
                    p.mb.breakpoint_reinject()
                    bp = p.mb.breakpoint_reached()
                    if bp != (-1, -1, -1):
                        bank, addr, _ = bp
                        p.mb.breakpoint_remove(bank, addr)
                        p.mb.breakpoint_singlestep_latch = 0
                        p._handle_hooks()
            return p.mb.lcd.frame_done

        a_done = b_done = False
        while not (a_done and b_done):
            if not a_done:
                a_done = _step_chunk(a, ticks_per_chunk)
            if not b_done:
                b_done = _step_chunk(b, ticks_per_chunk)

        for p in (a, b):
            p.mb.breakpoint_singlestep = 0
            p.frame_count += 1
            p._post_handle_events()


__all__ = [
    "PyBoyLinkSession",
]
