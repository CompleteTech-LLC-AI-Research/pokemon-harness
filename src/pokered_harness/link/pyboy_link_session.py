"""``PyBoyLinkSession`` — public surface for linking PyBoy instances.

Milestone 4 of the design doc (:doc:`docs/pyboy_serial_overhaul_design.md`).
Given one or two PyBoy instances, this wires a
:class:`~pokered_harness.link.serial_coordinator.CoordinatedBackend` (or
:class:`~pokered_harness.link.network_backend.NetworkBackend`) onto the
``backend`` attribute of each ``pyboy.mb.serial`` so the already-installed
bit-accurate :class:`pyboy.core.serial.Serial` instance exchanges bits
with its peer. No class swap is performed — the existing PyBoy Serial
instance is reused.

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
    # Drive both emulators through the interleaved scheduler so serial-heavy
    # ROM routines stay near-aligned:
    while not done:
        link.step(frames=1)

    link.detach_all()

Design notes
------------

* ``attach`` sets ``pyboy.mb.serial.backend`` rather than replacing the
  whole ``mb.serial`` object. The bit-accurate shift-register logic
  lives in PyBoy's native Serial class (upstream fork); the harness
  only chooses which peer (null / local / network) provides the bits.
* Once *both* sides attach, a :class:`LockstepCoordinator` is
  created, wiring each core's backend to the other. Until then, the
  first side's core keeps its default :class:`NullBackend` and behaves
  like a disconnected cable — matching the "not yet connected" phase
  in Pokémon's own link-cable code.
* ``detach`` restores each serial's original ``backend`` (normally
  :class:`NullBackend`); PyBoy returns to disconnected-cable behavior.
* Both local stepping methods use one persistent physical-time instruction
  scheduler. LCD markers never freeze one side while its peer continues.
  Paired loads and independent managed Session stepping require detachment;
  network stepping retains its separate bounded frame-turn protocol.

This class is the natural extension point for the network backend
(milestone 8): :meth:`listen` / :meth:`connect` classmethods would
pair a single local core with a TCP-backed remote endpoint instead
of a second local core.
"""

from __future__ import annotations

# ``time`` is retained as a module attribute: callers and tests patch
# ``pyboy_link_session.time.monotonic`` to control the wall-clock deadline.
import threading
import time  # noqa: F401

from pokered_harness.link._pyboy_link_session_lifecycle_mixin import _PyBoyLinkLifecycleMixin
from pokered_harness.link._pyboy_link_session_network_mixin import _PyBoyLinkNetworkMixin
from pokered_harness.link._pyboy_link_session_stepping_mixin import _PyBoyLinkSteppingMixin
from pokered_harness.link._pyboy_link_session_support import PairedSessionOperationError
from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.serial_coordinator import (
    LockstepCoordinator,
    SerialOperationGate,
)


class PyBoyLinkSession(
    _PyBoyLinkNetworkMixin,
    _PyBoyLinkLifecycleMixin,
    _PyBoyLinkSteppingMixin,
):
    """Pairs up to two PyBoy instances under a bit-accurate serial link."""

    #: Max attached instances. Gen I Pokémon is strictly 2-player.
    MAX_ATTACHED: int = 2
    PHYSICAL_QUANTUM = 70_224 * 2
    # The public local scheduler still advances one instruction at a time,
    # but checking the complete pair epoch after every instruction is
    # needlessly expensive for the bundled PyBoy runtime.  Keep a small
    # bounded CPU-cycle chunk between epoch observations so serial callbacks
    # and hook dispatch retain instruction granularity without making every
    # ordinary overworld frame pay the full validation cost.  Lightweight
    # test doubles continue to use the exact instruction path below.
    _REAL_SCHEDULER_CHUNK_CYCLES = 256
    MAX_FRAME_INSTRUCTIONS = 200_000
    MAX_FRAME_SECONDS = 30.0
    MAX_STALLED_INSTRUCTIONS = 32
    # PyBoy's LCD uses 70224 CPU cycles for a normal DMG frame. This is only
    # a fallback for test doubles or older integrations which do not expose
    # the LCD's next-frame cycle hint; real PyBoy instances use that hint.
    _DEFAULT_LCD_FRAME_CYCLES: int = 70224
    # Keep an individual singlestepped chunk bounded even if a caller
    # supplies an unusually large ``chunk_cycles`` value.
    # A versioned network peer must be identified before native serial
    # startup. This is deliberately finite so attach cannot retain an
    # emulator or reader thread forever when the remote endpoint disappears.
    _NETWORK_HELLO_TIMEOUT_SECONDS: float = 10.0

    def __init__(
        self,
        network_backend: NetworkBackend | None = None,
        *,
        network_is_internal_clock: bool | None = None,
        local_rom_version: str | None = None,
        view: bool = False,
    ) -> None:
        self._pyboys: list[object] = []
        # Tracks each attached PyBoy's serial instance (``pyboy.mb.serial``).
        # Kept under the historical ``_cores`` name so callers relying on
        # :attr:`cores` keep working.
        self._cores: list[object] = []
        # Per-attach backend we installed; saved so detach() can restore
        # whatever was on ``mb.serial.backend`` before we touched it.
        self._prev_backends: list[object] = []
        # When attaching to a legacy/no-backend serial object, we promote
        # it to a SerialCore and keep the original here so detach() can put
        # the motherboard back exactly as it was.
        self._prev_serials: list[object | None] = []
        self._coord: LockstepCoordinator | None = None
        self._network_backend: NetworkBackend | None = network_backend
        # The gate covers one complete PyBoy frame and owner-side native
        # serial dispatch. The per-frame boundary is installed below so a
        # multi-frame public tick cannot starve a queued peer edge.
        self._serial_gate = SerialOperationGate()
        # Attach/detach and local stepping mutate the same core/backend graph.
        # Serialize those lifecycle transitions so a concurrent caller cannot
        # observe or drive a half-paired session.
        self._lifecycle_lock = threading.RLock()
        self._operation_lock = self._lifecycle_lock
        self._cleanup_state = threading.local()
        # Cleanup must be able to publish a cancellation while a network
        # frame owns the provider/lifecycle lock.  This tiny independent
        # generation lock carries that signal without waiting on emulator
        # ownership; the active operation checks it when its native call
        # returns and fails closed before publishing a successful result.
        self._network_cancel_lock = threading.Lock()
        self._network_cancel_generation = 0
        self._network_tick_active = False
        self._owners = []
        self._step_active = False
        self._peer_progress_active = False
        self._clear_local_epoch()
        self._original_ticks: dict[int, tuple[str, object]] = {}
        # The native serial core may already have an owner-dispatch callback
        # installed by another integration. Keep the exact callback and
        # enabled state, plus our replacement callback, so detach restores
        # only state still owned by this session.
        self._previous_owner_dispatch: list[tuple[object | None, object, object] | None] = []
        self._network_is_internal_clock = network_is_internal_clock
        self._network_frame_barrier = False
        self._local_rom_version = local_rom_version
        # When True, per-frame stepping keeps the LCD renderer on and calls
        # each PyBoy's _post_tick (via pyboy.tick(0, True, False)) so the
        # SDL2 window actually flips and pumps events. Without this the
        # visible windows stay blank because _interleave_one_frame otherwise
        # drives mb.tick directly and skips the plugin manager.
        self._view: bool = view

    # --- construction --------------------------------------------------

    @classmethod
    def local(cls, *, view: bool = False) -> PyBoyLinkSession:
        """Create a local two-instance session.

        Both PyBoys attach into the same process; they're paired via
        a :class:`LockstepCoordinator`.
        """
        return cls(view=view)

    @classmethod
    def listen(
        cls,
        port: int,
        *,
        host: str = "127.0.0.1",
        local_rom_version: str | None = None,
        accept_timeout_s: float = 10.0,
        cancel_event: threading.Event | None = None,
    ) -> PyBoyLinkSession:
        """Bind ``(host, port)``, accept one peer, return the session.

        Single-instance mode: the session holds a :class:`NetworkBackend`;
        :meth:`attach` wires that backend onto the local PyBoy's
        ``mb.serial.backend``. The peer process is expected to have
        used :meth:`connect` and to be driving its own PyBoy.

        Blocks until a peer connects, the bounded accept deadline expires, or
        ``cancel_event`` is set. The bounded default prevents a forgotten
        listener from retaining a thread and socket forever.
        """
        backend, _listener = NetworkBackend.listen(
            port,
            host=host,
            local_rom_version=local_rom_version,
            accept_timeout_s=accept_timeout_s,
            cancel_event=cancel_event,
        )
        # Close the listener — we only accept one connection.
        try:
            _listener.close()
        except OSError:
            pass
        return cls(
            network_backend=backend,
            network_is_internal_clock=True,
            local_rom_version=local_rom_version,
        )

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        local_rom_version: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> PyBoyLinkSession:
        """Connect to a peer running :meth:`listen` on ``(host, port)``.

        Returns a single-instance network-mode session; call
        :meth:`attach` to wire the session's NetworkBackend onto the
        local PyBoy's ``mb.serial.backend``.
        """
        backend = NetworkBackend.connect(
            host,
            port,
            timeout_s=timeout_s,
            local_rom_version=local_rom_version,
            cancel_event=cancel_event,
        )
        return cls(
            network_backend=backend,
            network_is_internal_clock=False,
            local_rom_version=local_rom_version,
        )


__all__ = [
    "PairedSessionOperationError",
    "PyBoyLinkSession",
]
