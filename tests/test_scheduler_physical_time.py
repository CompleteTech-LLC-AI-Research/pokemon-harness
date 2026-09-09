"""Physical-time selection invariants independent of ROM/controller input."""
import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from tests.test_local_scheduler_candidate import Endpoint

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


class PhysicalEndpoint(Endpoint):
    def __init__(self, *args, double=False, physical_origin=0, **kwargs):
        super().__init__(*args, cgb=True, **kwargs)
        self.double = double
        self.physical = physical_origin
        self.generation = 0
        self.mb.get_physical_clock = lambda: (self.generation, self.physical)

    def instruction(self):
        self.physical += self.cycles * (1 if self.double else 2)
        return super().instruction()


def attach(a, b):
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)
    # Tiny fakes should not execute a production-sized LCD quantum.
    link.PHYSICAL_QUANTUM = 32
    return link


@pytest.mark.parametrize("double_a,double_b,expected", [
    (False, False, ["a", "b", "a", "b"]),
    (True, True, ["a", "b", "a", "b"]),
    (False, True, ["a", "b", "b", "a"]),
    (True, False, ["a", "b", "a", "a"]),
])
def test_selection_uses_elapsed_physical_time(double_a, double_b, expected):
    trace = []
    a = PhysicalEndpoint("a", trace, origin=10000, physical_origin=77, double=double_a, instructions=8)
    b = PhysicalEndpoint("b", trace, origin=7, physical_origin=99999, double=double_b, instructions=8)
    link = attach(a, b)
    link.step()
    assert [name for name, _ in trace[:4]] == expected
    origins = link._physical_origins
    link.step_interleaved()
    assert link._physical_origins == origins == (77, 99999)
    link.detach_all()


@pytest.mark.parametrize("double", [False, True])
def test_transition_inside_instruction_uses_runtime_split_accounting(double):
    trace = []
    a = PhysicalEndpoint("a", trace, double=double, instructions=8)
    b = PhysicalEndpoint("b", trace, instructions=8)
    original = a.mb.tick
    switched = False
    def transition():
        nonlocal switched
        if not switched:
            # An instruction straddles two runtime-accounted segments; the
            # scheduler must consume exact reported time, not post-rate*delta.
            a.physical += 2 * (1 if a.double else 2)
            a.double = not a.double
            a.physical += 2 * (1 if a.double else 2)
            a.mb.serial.clock += 4
            a.progress += 1
            trace.append(("a", a.mb.serial.clock))
            switched = True
            return True
        return original()
    a.mb.tick = transition
    link = attach(a, b)
    link.step()
    # The quantum ends at physical time 32; the final instruction is allowed
    # to cross that horizon, and its split-rate accounting must remain exact.
    assert a.physical == (34 if not double else 38)
    link.detach_all()


def test_nested_peer_progress_is_accounted_once_at_mixed_speed():
    trace = []
    a = PhysicalEndpoint("a", trace, instructions=4)
    b = PhysicalEndpoint("b", trace, double=True, instructions=4)
    link = attach(a, b)
    progress = link._make_owned_peer_progressor(b)
    called = []
    a.on_instruction = lambda: called.append(progress()) if not called else None
    link.step()
    assert called == [True]
    # Both endpoints advance to the same physical horizon even when their CPU
    # rates differ; nested progress is one bounded instruction, not a second
    # public frame.
    assert a.physical == 32 and b.physical == 32
    assert a.progress == 4 and b.progress == 8
    link.detach_all()


@pytest.mark.parametrize("mutation", ["physical", "generation"])
def test_external_time_change_or_same_clock_load_quarantines(mutation):
    trace = []
    a, b = PhysicalEndpoint("a", trace), PhysicalEndpoint("b", trace)
    link = attach(a, b)
    if mutation == "physical":
        a.physical += 2
    else:
        a.generation += 1
    with pytest.raises(RuntimeError, match="physical clock"):
        link.step()
    assert trace == []
    link.detach_all()


@pytest.mark.parametrize("field", ["physical", "generation"])
def test_metadata_cannot_exceed_runtime_uint64_contract(field):
    endpoint = PhysicalEndpoint("a", [])
    setattr(endpoint, field, 1 << 64)
    with pytest.raises(RuntimeError, match="invalid runtime physical clock metadata"):
        PyBoyLinkSession._read_physical_clock(endpoint)
