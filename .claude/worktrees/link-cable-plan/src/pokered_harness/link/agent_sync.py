"""Agent-layer rendezvous for two-process link-cable play.

The transport (:mod:`pokered_harness.link.serial_link` +
:mod:`pokered_harness.link.remote`) carries game-initiated serial
exchanges: whenever the ROM's ``Serial_Exchange*`` routine fires, the
endpoint translates it into a symbol-named RPC over TCP. That works
for transport-correct game-code paths.

What the transport **cannot** do alone is coordinate *agent* actions
— two independent MCP policies pressing buttons. Two separate Python
processes each owning a PyBoy have no shared frame clock; their
``step()`` loops drift by milliseconds every second. The in-process
:class:`LinkPair` is effectively coordinated because one thread
advances both sessions; for the true two-agent deployment that option
isn't available.

:class:`AgentSync` is the deployment-layer answer. It piggybacks on
the existing :class:`SerialLink` transport with a dedicated ``kind``
(``agent_sync/<label>``) so no new connection or sidechannel is
needed, and exposes a single blocking :meth:`rendezvous` method that
both peers call before taking a coordinated action:

    my_tick = session.current_tick()
    peer_tick = sync.rendezvous("link_menu_vote", my_tick)
    # Both sides now know each other's current game-tick.
    # Each side advances to an agreed target, then presses A.

That's the whole pattern — a one-message exchange of arbitrary
payloads between the two agents, FIFO-ordered within a label. For
actions that don't need a payload, pass an empty ``bytes()`` and
ignore the return.

Why this lives separate from :class:`RemoteLinkEndpoint`: the endpoint
is the game's view of the cable; the sync is the agent's. Mixing them
would conflate "the ROM asked for a byte exchange" with "my policy
wants to align with the peer." Keeping them separate means agents can
rendezvous at any application-level point — menu entries, warp
completion, trade-completion acknowledgement — without touching the
game-level hooks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pokered_harness.link.serial_link import SerialLink


_KIND_PREFIX = "agent_sync/"


class AgentSync:
    """Application-layer rendezvous over a shared :class:`SerialLink`.

    Usage — same pattern on both agents::

        sync = AgentSync(link)
        # Agent A:
        peer_payload = sync.rendezvous("about_to_press_a", b"my payload")
        # Agent B (same call):
        peer_payload = sync.rendezvous("about_to_press_a", b"my payload")

    Both sides block in ``rendezvous`` until the other arrives. FIFO
    within the label string — two back-to-back rendezvous calls with
    the same label pair up in order.
    """

    def __init__(self, link: "SerialLink") -> None:
        self._link = link

    def rendezvous(
        self,
        label: str,
        payload: bytes = b"",
        *,
        timeout_ms: int = 30000,
    ) -> bytes:
        """Block until the peer issues the matching rendezvous for the
        same label, then return the peer's payload. The label is
        prefixed with ``agent_sync/`` so it can never collide with a
        game-serial RPC kind (which uses ``exchange_bytes/``,
        ``exchange_nybble/``, ``menu_selection/``)."""
        if "/" in label:
            raise ValueError(
                f"label must not contain '/', got {label!r} — the "
                f"namespace prefix is managed by AgentSync."
            )
        return self._link.exchange(
            _KIND_PREFIX + label, bytes(payload), timeout_ms=timeout_ms
        )


__all__ = ["AgentSync"]
