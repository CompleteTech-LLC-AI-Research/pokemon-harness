"""ROM-free oracle and synthetic-TCP tests for the MCP stdio trade module.

Split from ``tests/test_mcp_trade_records_rom.py`` (#208) with no behavior change.
These tests are re-exported by the facade and collected there under their
original node IDs.  ``_patch_synthetic_tcp_launch`` patches module attributes by
name, so every caller and callee of that seam stays in this one namespace."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests._mcp_trade_records_rom_drivers_support import (
    _compacted_records,
)
from tests._mcp_trade_records_rom_oracle_support import (
    _distinct_party,
    _record,
    _record_payload,
    _skipped_copy_run,
    _survivor_run,
    _wrong_slot_run,
    assert_paired_exchange,
)
from tests._mcp_trade_records_rom_support import (
    _TCP_DRIVER_TOOL_NAMES,
    TcpPair,
    TradeRun,
    _assert_invoking_runtime,
    _child_runtime_mode,
    _launch_server,
    _server_env,
    _stage_assets,
)


def test_paired_exchange_oracle_rejects_identical_skipped_copy():
    """The byte-identical orientation must not pass on unchanged records.

    This is the review's control verbatim: menus and completion milestones are
    present, both parties are byte-identical to the fixture read, and the
    ROM-owned copy marker never fired.  Before the marker was required this
    observation passed, because a Red/Red exchange is a no-op at the byte level.
    """
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "a" * 64)]
    run = _skipped_copy_run(primary_before, peer_before)
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="identical-skip"
        )


def test_paired_exchange_oracle_rejects_distinct_skipped_copy():
    """A distinct-record orientation must reject a run that copied nothing."""
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "b" * 64)]
    run = _skipped_copy_run(primary_before, peer_before)
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="distinct-skip"
        )


def test_paired_exchange_oracle_rejects_faked_marker_without_exchange():
    """A fired marker is not enough: the records must match the copy result."""
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "b" * 64)]
    run = _skipped_copy_run(primary_before, peer_before, received=(1, 1))
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="faked-marker"
        )


def test_paired_exchange_oracle_rejects_late_corruption():
    """A correct copy snapshot does not excuse a party that changed later."""
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "b" * 64)]
    run = TradeRun(
        final_records=[
            _record_payload([_record(0, "c" * 64)]),
            _record_payload([_record(0, "a" * 64)], source="peer-party-records"),
        ],
        copy_records=[
            _record_payload([_record(0, "b" * 64)]),
            _record_payload([_record(0, "a" * 64)], source="peer-party-records"),
        ],
        copy_states=[],
        final_states=[],
        post_frames=0,
        evolution=[1, 1],
        offered={0: 0, 1: 0},
        back_outs=0,
        received=[1, 1],
    )
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="late-corruption"
        )


def test_paired_exchange_oracle_accepts_identical_completion():
    """Positive control: the identical-record oracle is not vacuous in reverse.

    The same byte-identical observation that the skipped-copy control must
    reject has to be *accepted* once the ROM-owned append marker is present.
    Without this control a marker requirement that simply failed closed could not
    be told apart from an oracle that always raises.
    """
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "a" * 64)]
    run = _skipped_copy_run(primary_before, peer_before, received=(1, 1))
    verdict = assert_paired_exchange(
        run, primary_before=primary_before, peer_before=peer_before, label="identical-accept"
    )
    assert verdict["distinct_records"] is False, verdict


def test_paired_exchange_oracle_rejects_wrong_slot_copy():
    """The review's wrong-slot mutation must fail on pairwise-distinct records.

    Six same-species records per owner make every slot digest distinct, so a
    response that copied slots ``(0, 1)`` yields a different compaction result
    than the required ``(2, 0)`` and the exact-exchange assertion rejects it.
    With the earlier six *identical* records per owner this mutation satisfied
    every assertion; the control fails closed only once the records differ.
    """
    primary_before = _distinct_party("p")
    peer_before = _distinct_party("q")
    run = _wrong_slot_run(
        primary_before,
        peer_before,
        copied_primary_slot=0,
        copied_peer_slot=1,
        claimed_offered={0: 2, 1: 0},
    )
    assert run.offered == {0: 2, 1: 0}, run.offered
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run,
            primary_before=primary_before,
            peer_before=peer_before,
            primary_slot=2,
            peer_slot=0,
            label="wrong-slot",
        )


def test_paired_exchange_oracle_accepts_the_required_slot_copy():
    """Positive control: the oracle accepts the *required* slot copy.

    Without this control a wrong-slot test that raised for every input could not
    be told apart from an oracle that also accepts the correct exchange.
    """
    primary_before = _distinct_party("p")
    peer_before = _distinct_party("q")
    run = _wrong_slot_run(
        primary_before,
        peer_before,
        copied_primary_slot=2,
        copied_peer_slot=0,
        claimed_offered={0: 2, 1: 0},
    )
    verdict = assert_paired_exchange(
        run,
        primary_before=primary_before,
        peer_before=peer_before,
        primary_slot=2,
        peer_slot=0,
        label="required-slot",
    )
    assert verdict["distinct_records"] is True, verdict


def test_paired_exchange_oracle_rejects_reordered_or_substituted_survivors():
    """Survivor mutations must fail once the six records are pairwise distinct.

    With six identical records per owner the compaction oracle could not see a
    reordering or a substitution among the survivors: every permutation produced
    the same digest list.  Distinct records make each survivor identifiable, so
    the exact list comparison rejects both mutations while the ROM-owned append
    marker and the copy-snapshot equality still hold.
    """
    primary_before = _distinct_party("p")
    peer_before = _distinct_party("q")
    required = _compacted_records(primary_before, 2, peer_before[0])
    swapped = [required[0], required[1], required[3], required[2], required[4], required[5]]
    substituted = [required[0], required[1], required[2], required[0], required[4], required[5]]
    for label, mutated in (("reordered", swapped), ("substituted", substituted)):
        run = _survivor_run(mutated, peer_before, primary_before)
        with pytest.raises(AssertionError):
            assert_paired_exchange(
                run,
                primary_before=primary_before,
                peer_before=peer_before,
                primary_slot=2,
                peer_slot=0,
                label=f"survivor-{label}",
            )
    # Positive control: the unmutated required list is accepted, so the two
    # rejections above are not an oracle that simply raises for every input.
    verdict = assert_paired_exchange(
        _survivor_run(required, peer_before, primary_before),
        primary_before=primary_before,
        peer_before=peer_before,
        primary_slot=2,
        peer_slot=0,
        label="survivor-required",
    )
    assert verdict["distinct_records"] is True, verdict


class _SyntheticTcpClient:
    """Minimal single-session MCP surface for the TCP driver's own controls.

    Answers ``tools/list``/``resources/list`` from an explicit surface, records
    every ``tools/call`` it receives, and refuses a name that surface never
    published -- the fail-closed behaviour an installed client relies on.  No
    emulator, ROM, or child process is involved, so these controls cost nothing
    and can probe failure paths the real rows cannot be made to hit.
    """

    _RESOURCE_URIS = (
        "pokered://game-state",
        "pokered://party-records",
        "pokered://events",
    )

    def __init__(self, tools, *, cleanup_failure=None):
        self.tools = frozenset(tools)
        self.calls = []
        self.cleanups = 0
        self.cleanup_failure = cleanup_failure

    async def request(self, method, params=None):
        if method == "tools/list":
            return {"tools": [{"name": name} for name in sorted(self.tools)]}
        if method == "resources/list":
            return {"resources": [{"uri": uri} for uri in self._RESOURCE_URIS]}
        raise AssertionError(f"unexpected request {method!r}")

    async def tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        assert name in self.tools, f"undiscovered tool {name!r} in {sorted(self.tools)}"
        return self._reply(name)

    @staticmethod
    def _reply(name):
        if name == "load_state":
            return {"ok": True}
        if name == "link_listen":
            return {"remote_mode": "listening"}
        if name in {"link_connect", "link_status"}:
            return {"remote_mode": "connected"}
        if name == "link_frame_barrier":
            return {
                "enabled": True,
                "network_frame_barrier": True,
                "remote_mode": "connected",
            }
        return {"ok": True}

    def called(self):
        return {name for name, _ in self.calls}

    async def cleanup(self):
        self.cleanups += 1
        if self.cleanup_failure is not None:
            raise self.cleanup_failure


def _patch_synthetic_tcp_launch(monkeypatch, clients):
    """Replace the two real subprocess launches with synthetic clients."""
    launches = list(clients)

    monkeypatch.setitem(
        globals(), "_stage_assets", lambda directory, primary, peer: (primary, peer)
    )
    monkeypatch.setitem(globals(), "_assert_invoking_runtime", lambda runtime_mode: None)

    async def launch(env):
        assert env, env
        return launches.pop(0)

    monkeypatch.setitem(globals(), "_launch_server", launch)


def _synthetic_assets():
    pins = {"expected_rom_sha1": "0" * 40, "expected_symbol_sha1": "1" * 40}
    return [
        {
            "rom": Path("primary.gb"),
            "sym": Path("primary.sym"),
            "family": family,
            "pins": dict(pins),
            "state": b"\x00\x01",
        }
        for family in ("red", "blue")
    ]


@asynccontextmanager
async def _tcp_pair_servers(tmp_path, primary_asset, peer_asset):
    """Launch two independent installed servers for one TCP trade row.

    Each process owns exactly one session and is configured through the same
    documented ``POKERED_*`` launch contract as the local rows, but with no peer
    session: the pair is formed later through the public
    ``link_listen``/``link_connect`` tools.  A failed body keeps its own
    exception as the primary error and attaches any cleanup failure as a note.
    The ``rom_version``/``peer_rom_version`` handshake arguments use each
    asset's canonical family (``red``/``blue``/``yellow``), the value the entry
    point validates, rather than the color-profile parameter id.
    """
    directory = tmp_path / "servers"
    directory.mkdir()
    primary, peer = _stage_assets(directory, primary_asset, peer_asset)
    runtime_mode = _child_runtime_mode()
    _assert_invoking_runtime(runtime_mode)
    clients = []
    original_failure = None
    try:
        for asset in (primary, peer):
            clients.append(await _launch_server(_server_env(asset, None, runtime_mode, None)))
        assert len(clients) == 2, clients
        yield TcpPair(clients, (primary["family"], peer["family"]), runtime_mode)
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        # Attempt every owned client's cleanup exactly once.  A cleanup failure
        # must never leave a sibling process running: when the body already
        # failed, that failure stays primary and each cleanup error is attached
        # to it as a note; when the body succeeded, the cleanup errors are
        # aggregated and raised only after every attempt has been made.
        cleanup_errors = []
        for client in clients:
            try:
                await client.cleanup()
            # A cleanup attempt must also survive cancellation, so the blind
            # catch is deliberate here.
            except BaseException as cleanup_error:  # noqa: BLE001
                cleanup_errors.append(cleanup_error)
        if original_failure is not None:
            for cleanup_error in cleanup_errors:
                original_failure.add_note(f"MCP_TRADE_RECORDS_CLEANUP_ERROR {cleanup_error!r}")
        elif cleanup_errors:
            primary = cleanup_errors[0]
            for extra in cleanup_errors[1:]:
                primary.add_note(f"MCP_TRADE_RECORDS_CLEANUP_ERROR {extra!r}")
            raise primary


@pytest.mark.asyncio
async def test_tcp_driver_invokes_only_tools_its_own_server_advertises():
    """Finding 1: every tool the TCP driver calls must be in that process's list.

    ``_tool_specs(has_peer=False)`` is the surface a single-session installed
    server publishes; it drops the in-process ``link_step`` because there is no
    second session to drive, so the advertised equivalent is ``step``.  Reading
    the production surface rather than restating it keeps the control honest if
    the surface changes later.  The clients refuse any name they did not
    publish, so a driver that reached for ``link_step`` -- as the reviewed head
    did -- fails here instead of passing on permissive SDK dispatch.
    """
    from pokered_harness.mcp_server import _tool_specs

    advertised = sorted(spec.name for spec in _tool_specs(has_peer=False))
    assert "link_step" not in advertised, advertised
    clients = [_SyntheticTcpClient(advertised) for _ in range(2)]
    pair = TcpPair(clients, ("red", "blue"), "source")

    await pair.assert_surface()
    await pair.load_fixture({"state": b"\x00\x01"}, owner=0)
    await pair.link_up()
    await pair.arm_network_frame_barrier()
    await pair.press(0, "a", duration=1)
    await pair.step(2)

    invoked = {name for client in clients for name in client.called()}
    assert invoked == {
        "load_state",
        "step",
        "press",
        "link_listen",
        "link_connect",
        "link_status",
        "link_frame_barrier",
    }, sorted(invoked)
    assert invoked <= set(advertised), sorted(invoked)
    assert set(_TCP_DRIVER_TOOL_NAMES) == invoked, sorted(_TCP_DRIVER_TOOL_NAMES)
    # Each owner was stepped twice, once per requested frame, through the
    # advertised tool the pair actually selected.
    assert [sum(1 for name, _ in client.calls if name == "step") for client in clients] == [2, 2]


@pytest.mark.asyncio
async def test_tcp_surface_requirement_rejects_a_process_missing_an_invoked_tool():
    """Finding 1: the discovery requirement is load-bearing, not decorative.

    The declared driver set is only a contract if a surface that omits one of
    its names is rejected.  Here every published name but the pacing control is
    advertised, so ``assert_surface`` must fail on the missing
    ``link_frame_barrier`` rather than let a row arm a control its own process
    never offered.
    """
    surface = sorted(
        (set(_TCP_DRIVER_TOOL_NAMES) | {"release", "link_disconnect"}) - {"link_frame_barrier"}
    )
    clients = [_SyntheticTcpClient(surface) for _ in range(2)]
    pair = TcpPair(clients, ("red", "blue"), "source")

    with pytest.raises(AssertionError) as failure:
        await pair.assert_surface()
    assert "link_frame_barrier" in str(failure.value), failure.value


@pytest.mark.asyncio
async def test_tcp_pair_servers_attempt_every_cleanup_when_the_body_succeeds(monkeypatch, tmp_path):
    """Finding 3: one failing cleanup must not strand the sibling server.

    The unarmed-barrier control ends its expected-failure body without
    ``pair.eof()`` and relies on this finalizer to terminate both live servers,
    so every owned client has to be attempted even after an earlier attempt
    raises.  Both synthetic cleanups raise here: both must still be attempted,
    and the first failure must surface only after the loop, carrying the second
    as a note.
    """
    clients = [
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("first")),
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("second")),
    ]
    primary, peer = _synthetic_assets()
    _patch_synthetic_tcp_launch(monkeypatch, clients)

    with pytest.raises(TimeoutError) as failure:
        async with _tcp_pair_servers(tmp_path, primary, peer) as pair:
            assert isinstance(pair, TcpPair), pair

    assert [client.cleanups for client in clients] == [1, 1]
    assert "first" in str(failure.value), failure.value
    notes = getattr(failure.value, "__notes__", [])
    assert any("MCP_TRADE_RECORDS_CLEANUP_ERROR" in note and "second" in note for note in notes), (
        notes
    )


@pytest.mark.asyncio
async def test_tcp_pair_servers_keep_the_body_failure_and_note_every_cleanup_failure(
    monkeypatch, tmp_path
):
    """Finding 3: cleanup errors must not replace a failed body.

    The body failure stays primary and both cleanup failures are attached to it,
    so a row that already failed for its own reason still reports why the owned
    processes could not be settled.  This pins the behaviour the aggregation
    above must preserve (it already held before the correction).
    """
    clients = [
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("first")),
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("second")),
    ]
    primary, peer = _synthetic_assets()
    _patch_synthetic_tcp_launch(monkeypatch, clients)
    body_failure = RuntimeError("trade row failed")

    with pytest.raises(RuntimeError) as failure:
        async with _tcp_pair_servers(tmp_path, primary, peer):
            raise body_failure

    assert failure.value is body_failure, failure.value
    assert [client.cleanups for client in clients] == [1, 1]
    notes = getattr(failure.value, "__notes__", [])
    assert sum(1 for note in notes if "MCP_TRADE_RECORDS_CLEANUP_ERROR" in note) == 2, notes
