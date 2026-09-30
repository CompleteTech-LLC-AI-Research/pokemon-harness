"""Synchronisation and party-validation helpers for the TCP trade peer.

Extracted from ``tests/_tcp_trade_peer.py`` (#127): the party record sizes, the
deadline helper, the owner-dispatch sync boundaries, the post-verdict shutdown
rendezvous, and the party-summary / battle-fixture validators.
"""

from __future__ import annotations

import time
from collections.abc import Callable

PARTY_MON_SIZE = 44
PARTY_OT_SIZE = 11
PARTY_NICK_SIZE = 11


def _deadline_remaining(deadline: float, *, phase: str) -> float:
    """Return setup time remaining, failing closed at the absolute cutoff."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{phase} exceeded the process deadline")
    return remaining


def _hold_at_sync_boundary(
    backend: object,
    *,
    ready_sync_id: int,
    release_sync_id: int,
    timeout: float,
    service_pending_edges: Callable[[], int],
    progress_callback: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Rendezvous at a ROM boundary until both peers have observed release.

    Stable UI milestones can drain already-admitted owner-dispatch edges
    without ticking. Timed ROM phases must provide ``progress_callback`` so
    the owner continues authentic emulation while the peer catches up.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a positive number")
    if timeout <= 0:
        raise ValueError("timeout must be a positive number")

    deadline = monotonic() + timeout

    def wait_for_peer(marker: int, *, phase: str) -> None:
        while True:
            if backend.poll_peer_sync(sync_id=marker):
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError(f"sync boundary {phase} did not converge: marker={marker}")
            if progress_callback is None:
                service_pending_edges()
            else:
                progress_callback()
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(0.001, remaining))

    backend.announce_sync(sync_id=ready_sync_id)
    wait_for_peer(ready_sync_id, phase="ready")
    backend.announce_sync(sync_id=release_sync_id)
    wait_for_peer(release_sync_id, phase="release")


def _peer_frame_shutdown_sync(
    backend,
    *,
    step,
    backend_snapshot,
    ready_sync_id,
    release_sync_id,
    leader,
    timeout,
    monotonic,
    sleep,
):
    """Stop frame admission after both owners have entered shutdown.

    The leader sends release outside a completed frame and starts no further
    turns. A follower may still need to finish a turn admitted before that
    release. It advances only for an already-received FRAME_TICK, so a marker
    arriving between a poll and owner progress cannot strand it waiting for
    a frame the leader will never send.
    """
    deadline = monotonic() + timeout

    def remaining():
        value = deadline - monotonic()
        if value <= 0:
            raise RuntimeError(
                f"peer frame shutdown did not converge: backend={backend_snapshot()}"
            )
        return value

    def progress(*, admit_turn):
        if leader and admit_turn:
            step(1)
        elif not leader:
            stats = backend_snapshot()
            # Only this owner consumes FRAME_TICK. The reader publishes the
            # received counter after queue insertion, and the owner updates
            # ACK accounting before its frame call returns. At this owner
            # boundary a positive difference therefore identifies a queued
            # turn without removing it or waiting for a future turn.
            if stats["frame_ticks_received"] > stats["frame_acks_sent"]:
                step(1)
            else:
                backend.service_pending_edges(max_edges=1)
        else:
            backend.service_pending_edges(max_edges=1)

    def wait_for_peer(marker, *, admit_turn):
        while not backend.poll_peer_sync(sync_id=marker):
            remaining()
            progress(admit_turn=admit_turn)
            sleep(min(0.001, remaining()))

    backend.wait_for_wire_idle(
        timeout=remaining(),
        progress_callback=lambda: step(1),
        stable_checks=8,
    )
    backend.announce_sync(sync_id=ready_sync_id)
    wait_for_peer(ready_sync_id, admit_turn=True)
    # Each ready marker was published outside that peer's frame call. Once
    # both owners are here, a second ticking quiet window could demand more
    # turns after one peer has already finished it. Fence admission instead.
    backend.announce_sync(sync_id=release_sync_id)
    wait_for_peer(release_sync_id, admit_turn=False)
    # FRAME_ACK and the peer's release are ordered on its socket. The leader
    # cannot release before its last ACK; the follower cannot observe that
    # release until any last admitted turn has completed. No new frame can
    # create serial work during this final transport-only check.
    backend.wait_for_wire_idle(
        timeout=remaining(),
        allow_peer_close=True,
        progress_callback=lambda: backend.service_pending_edges(max_edges=1),
        stable_checks=8,
    )


def _peer_shutdown_sync(
    backend,
    *,
    step,
    backend_snapshot,
    ready_sync_id: int,
    timeout: float = 120.0,
    release_sync_id: int | None = None,
    progress_after_marker: Callable[[], None] | None = None,
    frame_leader: bool | None = None,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> None:
    """Close a successful pair only after both peers have drained serial work.

    LinkMenu entry is not itself wire idle: the cartridge can have one final
    externally-clocked edge in flight. The owner therefore continues authentic
    emulation until its local transport observes quiet *before* publishing a
    teardown marker. A frame-paced peer may still need the other owner to
    advance while its own first quiet window is settling, so callers can
    provide ``progress_after_marker`` to keep both native owners live through
    the marker and final quiet-window handshake. The default remains
    transport-only after the marker for lightweight/unit-test callers.
    A negotiated frame-paced caller supplies ``frame_leader`` so shutdown
    fences new turns before the final transport-only check.
    """
    if frame_leader is not None:
        if type(frame_leader) is not bool:
            raise TypeError("frame_leader must be a bool or None")
        if release_sync_id is None:
            raise ValueError("frame shutdown requires a release marker")
        _peer_frame_shutdown_sync(
            backend,
            step=step,
            backend_snapshot=backend_snapshot,
            ready_sync_id=ready_sync_id,
            release_sync_id=release_sync_id,
            leader=frame_leader,
            timeout=timeout,
            monotonic=monotonic,
            sleep=sleep,
        )
        return

    def wait_for_peer_marker(sync_id: int) -> None:
        deadline_at = monotonic() + timeout
        while monotonic() < deadline_at:
            if backend.poll_peer_sync(sync_id=sync_id):
                return
            if progress_after_marker is None:
                # After a local wire-idle observation no new master edge can
                # be created without ticking the emulator. Service only
                # already-admitted slave work for transport-only callers.
                backend.service_pending_edges(max_edges=1)
            else:
                # A negotiated frame barrier can leave the peer inside its
                # own quiet-window tick. Keep both owner clocks moving until
                # the marker is observed; the final quiet-window drain below
                # fences any edge admitted during this handshake.
                progress_after_marker()
            sleep(0.001)
        raise RuntimeError(
            f"peer shutdown sync {sync_id} did not converge: backend={backend_snapshot()}"
        )

    backend.wait_for_wire_idle(
        timeout=timeout,
        progress_callback=lambda: step(1),
        stable_checks=8,
    )
    backend.announce_sync(sync_id=ready_sync_id)
    wait_for_peer_marker(ready_sync_id)
    backend.wait_for_wire_idle(
        timeout=timeout,
        progress_callback=(
            progress_after_marker
            if progress_after_marker is not None
            else lambda: backend.service_pending_edges(max_edges=1)
        ),
        stable_checks=8,
    )
    if release_sync_id is not None:
        # Both peers have now completed a second local quiet observation.
        # Keep the owner clocks live until both release markers are seen so
        # neither process detaches while the other is still inside its final
        # frame-paced drain.
        backend.announce_sync(sync_id=release_sync_id)
        wait_for_peer_marker(release_sync_id)
        backend.wait_for_wire_idle(
            timeout=timeout,
            allow_peer_close=True,
            progress_callback=lambda: backend.service_pending_edges(max_edges=1),
            stable_checks=8,
        )


def _finish_link_menu_phase(goal, *, cooperative_sync, peer_shutdown_sync):
    """Finish only after LinkMenu's native selection exchange has settled."""
    if goal == "link_menu":
        # 125 authenticates LinkMenu's directional selection exchange. Keep
        # teardown markers disjoint so an old selection acknowledgement can
        # never satisfy the passive wire-idle handshake.
        peer_shutdown_sync(ready_sync_id=126, timeout=10.0)
    else:
        cooperative_sync(sync_id=123, timeout=10.0, step_frames=1)


def _party_summary(session) -> dict[str, object]:
    """Return the game-owned party arrays for post-trade verification."""
    memory = session._pyboy.memory
    addr_of = session.symbols.addr_of
    count = int(memory[addr_of("wPartyCount")])
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")
    return {
        "count": count,
        "species": [int(memory[species_addr + i]) for i in range(count + 1)],
        "mon_species": [int(memory[mons_addr + i * PARTY_MON_SIZE]) for i in range(count)],
        "mon_records": [
            bytes(
                memory[mons_addr + i * PARTY_MON_SIZE + offset] for offset in range(PARTY_MON_SIZE)
            ).hex()
            for i in range(count)
        ],
    }


def _validate_battle_party_fixture(session) -> str:
    """Validate that the saved battle fixture is already Colosseum-legal."""
    pb = session._pyboy
    addr_of = session.symbols.addr_of
    count_addr = addr_of("wPartyCount")
    species_addr = addr_of("wPartySpecies")
    mons_addr = addr_of("wPartyMons")

    count = pb.memory[count_addr]
    if count < 3:
        raise RuntimeError(f"battle fixture party count is {count}, expected >=3")
    if pb.memory[species_addr + count] != 0xFF:
        raise RuntimeError("battle fixture party species list is not FF-terminated")
    species = []
    for slot in range(count):
        slot_species = pb.memory[species_addr + slot]
        mon_addr = mons_addr + slot * PARTY_MON_SIZE
        mon_species = pb.memory[mon_addr]
        hp = (pb.memory[mon_addr + 1] << 8) | pb.memory[mon_addr + 2]
        if slot_species in (0, 0xFF) or mon_species != slot_species:
            raise RuntimeError(
                "battle fixture has invalid party slot "
                f"{slot}: species={slot_species} mon_species={mon_species}"
            )
        if hp <= 0:
            raise RuntimeError(f"battle fixture party slot {slot} has no HP")
        for move_idx in range(4):
            move_id = pb.memory[mon_addr + 8 + move_idx]
            pp = pb.memory[mon_addr + 29 + move_idx] & 0x3F
            if move_id != 0 and pp <= 0:
                raise RuntimeError(f"battle fixture party slot {slot} move {move_idx} has zero PP")
        species.append(int(slot_species))
    return f"party_count={int(count)} species={species}"
