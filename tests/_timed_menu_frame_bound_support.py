"""Shared scaffolding for the timed-menu frame-bound tests (#252).

The owner's frame bound must be a statement about ``--frame-limit``, not about
how fast the host is. ``scripts/probe_timed_rom_pair.py::_run_owner`` stops on
whichever comes first: the frame bound or the supplied monotonic ``deadline``,
so driving the loop from an injected clock is what makes the frame bound
exercisable deterministically on any machine.

``FakeClock`` and ``FRAME_BOUND`` live here rather than in either test module
because both modules need them: keeping them in a ``tests/_`` support module
matches the repository's convention for shared test infrastructure
(``_tier_config.py``, ``_probe_timed_rom_pair_support.py``) and avoids the
import cycle that appears if the two test modules each import from the other.
Support modules are never registered as tier modules.
"""

FRAME_BOUND = 300

# Wall time the fake owner "consumes" per observation. ``0.0`` models an
# arbitrarily fast host; a large step an arbitrarily slow one. Neither may
# change how many calls the frame bound authorizes.
INLINE_STEPS = [0.0, 0.001, 0.002, 0.005]
STREAM_STEPS = [0.0, 0.001, 0.005]
SLOW_STEP = 1.0


class FakeClock:
    """Monotonic stand-in that advances a fixed amount per read.

    ``step`` is the wall time the owner "consumes" per observation: ``0``
    models an arbitrarily fast host and a large step an arbitrarily slow one.
    Neither may change how many calls the frame bound authorizes.
    """

    def __init__(self, start=1000.0, step=0.0):
        self.value = start
        self.step = step
        self.reads = 0

    def __call__(self):
        self.reads += 1
        current = self.value
        self.value += self.step
        return current


def assert_not_deadline_truncated(record):
    """A deadline-truncated run must never pass as a retention result.

    The owner loop leaves ``termination`` at its default when it exits on the
    wall clock, so a short call list is indistinguishable from a dropped record
    unless the terminal state is checked. Every scenario that is meant to reach
    the frame bound must therefore assert a non-default terminal state.

    Without this, a run truncated by the deadline reports ``0 == 300`` and
    reads as a retention bug, which is the wrong diagnosis. Carried over from
    the superseded #256.
    """
    assert record["termination"] != "cancelled_or_deadline", record["termination"]
