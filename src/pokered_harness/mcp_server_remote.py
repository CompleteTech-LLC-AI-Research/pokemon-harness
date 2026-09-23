"""Remote endpoint lifecycle: accept, cleanup and disconnect.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from pokered_harness.mcp_server_model import (
    _DEFAULT_CLEANUP_TIMEOUT_S,
    _DIRECT_LINK_HOOKS,
    LinkState,
    McpHarnessError,
    _ListenerCancelled,
)
from pokered_harness.mcp_server_remote_contract import _negotiate_network_clock_role, _remaining
from pokered_harness.session import Session, locked_sessions


def _track_unpublished_remote_resource(
    link: LinkState,
    generation: int,
    *,
    transport: Any | None = None,
    endpoint: _entry.RemoteLinkEndpoint | None = None,
    network_session: _entry.PyBoyLinkSession | None = None,
) -> None:
    """Publish setup ownership before a remote handshake completes.

    A listener/connect worker can block in HELLO while ``link_disconnect`` is
    already trying to cancel it. Keeping these handles on ``LinkState`` gives
    teardown a way to close the transport and detach the backend immediately;
    the worker still owns the local variables and performs idempotent cleanup
    when it unwinds.
    """
    with link.state():
        if link._generation != generation and not link._disconnecting:
            raise McpHarnessError("link_cancelled", "remote connection cancelled")
        if transport is not None:
            link._pending_remote_link = transport
        if endpoint is not None:
            link._pending_remote_endpoint = endpoint
        if network_session is not None:
            link._pending_network_session = network_session


def _accept_remote(
    link: LinkState,
    session: Session,
    listener: socket.socket,
    cancel: threading.Event,
    generation: int,
    rom_version: str,
    timeout_s: float,
    expected_peer_rom_version: str | None,
) -> None:
    try:
        while not cancel.is_set():
            transport: Any | None = None
            network_session: _entry.PyBoyLinkSession | None = None
            endpoint: _entry.RemoteLinkEndpoint | None = None
            accepted_conn: socket.socket | None = None
            try:
                accepted_conn, _peer_addr = listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if cancel.is_set():
                    return
                raise McpHarnessError("listen_failed", str(exc)) from exc

            try:
                accepted_conn.settimeout(None)
                native_supports = _entry._supports_bit_accurate_network(session)
                if native_supports:
                    transport = _entry.NetworkBackend(accepted_conn, local_rom_version=rom_version)
                    accepted_conn = None
                    _track_unpublished_remote_resource(link, generation, transport=transport)
                    network_session = _entry._attach_network_backend(
                        session,
                        transport,
                        is_internal_clock=True,
                        local_rom_version=rom_version,
                        network_hello_timeout_s=timeout_s,
                    )
                    _track_unpublished_remote_resource(
                        link, generation, network_session=network_session
                    )
                    _entry._wait_for_network_hello(
                        transport,
                        cancel,
                        timeout_s,
                        expected_peer_rom_version=expected_peer_rom_version,
                    )
                    _negotiate_network_clock_role(
                        session,
                        network_session,
                        transport.peer_rom_version,
                        timeout_s=timeout_s,
                    )
                else:  # pragma: no cover - dispatcher admission is fail-closed
                    raise McpHarnessError(
                        "unsupported_runtime",
                        "primary PyBoy lost the pinned serial contract while "
                        "accepting a remote connection",
                    )
                with link.state():
                    if cancel.is_set() or link._disconnecting or link._generation != generation:
                        raise _ListenerCancelled
                    link.remote_link = transport
                    link.remote_endpoint = endpoint
                    link.network_session = network_session
                    if link._pending_remote_link is transport:
                        link._pending_remote_link = None
                    if link._pending_remote_endpoint is endpoint:
                        link._pending_remote_endpoint = None
                    if link._pending_network_session is network_session:
                        link._pending_network_session = None
                    link.remote_mode = "connected"
                    link.remote_role = "listener"
                    link.remote_bind_port = None
                    link._listener_socket = None
                    link._listener_error = None
                    link._remote_error = None
                # Ownership moved into LinkState; the finalizer below must
                # not detach or close the live transport.
                transport = None
                network_session = None
                endpoint = None
                return
            except _ListenerCancelled:
                return
            except Exception as exc:  # noqa: BLE001
                if cancel.is_set():
                    return
                # A bad peer must not take down a listener that can still
                # accept a valid peer. Record the failure for diagnostics,
                # clean only this connection, then continue accepting.
                with link.state():
                    if link._generation == generation:
                        link._listener_error = exc
            finally:
                # The accepted connection is not published to LinkState until
                # HELLO succeeds.  Therefore link_disconnect can cancel this
                # worker while it is waiting for HELLO, leaving these locals
                # as the only owners.  Always release them here, including
                # the cancellation path, or the transport reader can outlive
                # the MCP link and retain the peer socket indefinitely.
                if transport is not None or endpoint is not None or network_session is not None:
                    cleanup_errors = _cleanup_unpublished_remote(
                        session,
                        network_session,
                        transport,
                        link=link,
                        endpoint=endpoint,
                        generation=generation,
                    )
                    if cleanup_errors:
                        cancel.set()
                if accepted_conn is not None:
                    try:
                        accepted_conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        accepted_conn.close()
                    except OSError:
                        pass
    except _ListenerCancelled:
        return
    except Exception as exc:  # noqa: BLE001
        if not cancel.is_set():
            with link.state():
                if link._generation == generation:
                    link._listener_error = exc
                    link.remote_mode = "idle"
                    link.remote_role = None
                    link.remote_bind_port = None
    finally:
        try:
            listener.close()
        except OSError:
            pass
        with link.state():
            if link._listener_socket is listener:
                link._listener_socket = None


def _close_serial_link(link: Any, *, timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    close_ok = True
    try:
        if isinstance(link, _entry.NetworkBackend):
            close_ok = bool(link.close(timeout_s=max(0.0, deadline - time.monotonic())))
        else:
            link.close()
    except Exception:  # noqa: BLE001 - cleanup must report failure, not mask it
        close_ok = False
    workers: list[threading.Thread] = []
    seen_workers: set[int] = set()
    for attr in ("_reader", "_edge_worker"):
        worker = getattr(link, attr, None)
        if not isinstance(worker, threading.Thread):
            continue
        if id(worker) in seen_workers:
            continue
        seen_workers.add(id(worker))
        workers.append(worker)
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return close_ok and not any(worker.is_alive() for worker in workers)


def _uninstall_remote_endpoint(
    session: Session,
    endpoint: Any,
    *,
    timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S,
) -> list[Exception]:
    """Release an endpoint's owned callbacks during transport teardown.

    The MCP lifecycle retains a legacy symbol-based cleanup fallback for
    endpoint implementations that predate ``RemoteLinkEndpoint.uninstall``.
    Calling the owned teardown first lets current endpoints clear both their
    physical hooks and their endpoint/session ownership records.
    """
    uninstall = getattr(endpoint, "uninstall", None)
    if not callable(uninstall):
        return []
    try:
        # RemoteLinkEndpoint.uninstall() calls deactivate_hooks_at once per
        # owned symbol. Acquire the Session lock once with the caller's
        # remaining deadline so those re-entrant calls cannot each consume a
        # fresh five-second lock timeout. ``allow_closed`` is intentional:
        # hook guards must still be released during terminal cleanup.
        with session.locked(timeout_s=max(0.0, timeout_s), allow_closed=True):
            uninstall()
    except Exception as exc:  # noqa: BLE001 - cleanup must continue
        return [exc]
    return []


def _cleanup_unpublished_remote(
    session: Session,
    network_session: _entry.PyBoyLinkSession | None,
    transport: Any | None,
    *,
    link: LinkState | None = None,
    endpoint: _entry.RemoteLinkEndpoint | None = None,
    generation: int | None = None,
) -> list[Exception]:
    """Release resources created before a remote connection was published.

    Connection setup has several failure points after the socket and native
    serial backend exist. Cleanup must be best-effort and independent: a
    failing detach must not prevent the transport from closing, and a close
    failure must not leave raw link hooks active. The original connection
    exception remains the client-visible failure.
    """
    cleanup_deadline = time.monotonic() + _entry._DEFAULT_CLEANUP_TIMEOUT_S
    cleanup_errors: list[Exception] = []
    transport_close_failed = False
    network_detach_failed = False
    hook_cleanup_failed = False
    if network_session is not None:
        try:
            with session.locked(timeout_s=_remaining(cleanup_deadline)):
                network_session.detach_all()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
            network_detach_failed = True
            cleanup_errors.append(exc)
    if transport is not None:
        try:
            transport_closed = _close_serial_link(
                transport,
                timeout_s=_remaining(cleanup_deadline),
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
            transport_closed = False
            cleanup_errors.append(exc)
        if not transport_closed:
            transport_close_failed = True
            cleanup_errors.append(
                TimeoutError(
                    "unpublished remote transport did not close before the cleanup deadline"
                )
            )
    if endpoint is not None:
        endpoint_errors = _uninstall_remote_endpoint(
            session,
            endpoint,
            timeout_s=_remaining(cleanup_deadline),
        )
        if endpoint_errors:
            hook_cleanup_failed = True
            cleanup_errors.extend(endpoint_errors)
    try:
        hook_errors = _entry._deactivate_link_hooks(
            session,
            timeout_s=_remaining(cleanup_deadline),
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
        hook_errors = [exc]
    if hook_errors:
        hook_cleanup_failed = True
        cleanup_errors.extend(hook_errors)

    if link is not None:
        with link.state():
            # A disconnect may have advanced the generation while this worker
            # was unwinding. Its teardown finalizer still preserves pending
            # handles published during that disconnect, but never attach an
            # old worker to a newly started lifecycle.
            same_lifecycle = link._generation == generation
            if link.remote_link is None and (same_lifecycle or link._disconnecting):
                if link._pending_remote_link is transport and not transport_close_failed:
                    link._pending_remote_link = None
                if link._pending_remote_endpoint is endpoint and not hook_cleanup_failed:
                    link._pending_remote_endpoint = None
                if (
                    link._pending_network_session is network_session
                    and not network_detach_failed
                    and not hook_cleanup_failed
                ):
                    link._pending_network_session = None
            if (
                cleanup_errors
                and link.remote_link is None
                and (same_lifecycle or link._disconnecting)
            ):
                if transport_close_failed and transport is not None:
                    link._pending_remote_link = transport
                if hook_cleanup_failed and endpoint is not None:
                    link._pending_remote_endpoint = endpoint
                if (network_detach_failed or hook_cleanup_failed) and network_session is not None:
                    link._pending_network_session = network_session
                link.remote_mode = "disconnecting"
                link.remote_role = None
                link.remote_bind_port = None
                link._remote_error = cleanup_errors[0]
    return cleanup_errors


def _detach_local_link_session(
    link: LinkState,
    session: Session,
    local_link_session: _entry.PyBoyLinkSession,
    *,
    timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S,
) -> None:
    """Detach a local native link while both owned emulators are locked."""
    peer = link.peer_session
    # Acquire emulator owners before the provider/pair lock.  This keeps
    # local stepping, detach, and raw provider callbacks on one order even
    # when the primary/peer objects were supplied in reverse identity order.
    targets = [session]
    if peer is not None and peer is not session:
        targets.append(peer)
    with (
        locked_sessions(
            *targets,
            allow_closed=True,
            timeout_s=max(0.0, timeout_s),
        ),
        link._pair_lock,
    ):
        local_link_session.detach_all()


def _deactivate_link_hooks(
    session: Session,
    peer: Session | None = None,
    *,
    timeout_s: float = _DEFAULT_CLEANUP_TIMEOUT_S,
) -> list[Exception]:
    """Disable and deregister link callbacks within one total deadline."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    errors: list[Exception] = []
    sessions: list[Session] = [session]
    if peer is not None and peer is not session:
        sessions.append(peer)
    try:
        # Acquire all emulator owners canonically before deactivating any
        # provider hook.  Nested Session calls are re-entrant, so this single
        # deadline also prevents primary/peer cleanup from observing a half
        # detached pair.
        with locked_sessions(
            *sessions,
            allow_closed=True,
            timeout_s=_remaining(deadline),
        ):
            for target in sessions:
                try:
                    target.deactivate_serial_hooks(timeout_s=0.0)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                # A few legacy endpoint hooks are installed directly on
                # PyBoy and cannot be reached through Session.serial_hook.
                # Remove those by symbol where the runtime supports
                # hook_deregister.
                for symbol_name in _DIRECT_LINK_HOOKS:
                    try:
                        target.deactivate_hooks_at(symbol_name, timeout_s=0.0)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        errors.append(exc)
    return errors


def _disconnect_remote(link: LinkState, session: Session) -> None:
    """Cancel listener/connect work, close sockets, and join workers.

    A teardown failure is not allowed to look like a clean idle transition.
    Any resource whose close/detach/join did not complete remains owned by the
    ``LinkState`` as a pending cleanup handle, so a later ``link_disconnect``
    can retry it and reconnect cannot overlap a live worker or socket.
    """
    current_thread_id = threading.get_ident()
    cancel_local_setup = False
    retry_local_cleanup = False
    pending_local: _entry.PyBoyLinkSession | None = None
    local_peer: Session | None = None
    with link.state():
        if link._local_pair_in_progress:
            if not link._local_setup_done.is_set():
                # Local setup is owned by the pairing worker and may be
                # holding both emulator locks. Advance its generation and
                # return so the worker performs exactly-once rollback; never
                # dismantle a completed local pair from this path.
                link._local_generation += 1
                cancel_local_setup = True
            elif link._pending_local_link_session is not None:
                # A worker which already returned after a failed rollback
                # leaves its provider published for an explicit retry.
                retry_local_cleanup = True
                pending_local = link._pending_local_link_session
            local_peer = link.peer_session
        elif link._pending_local_link_session is not None:
            retry_local_cleanup = True
            pending_local = link._pending_local_link_session
            local_peer = link.peer_session
        if cancel_local_setup or retry_local_cleanup:
            pass
        elif link._disconnecting:
            done = link._disconnect_done
            owned_by_current_thread = link._disconnect_owner == current_thread_id
        else:
            done = None
            owned_by_current_thread = False
        listener_thread = link._listener_thread
        if isinstance(listener_thread, threading.Thread) and not listener_thread.is_alive():
            link._listener_thread = None
            listener_thread = None
        if cancel_local_setup or retry_local_cleanup:
            pass
        elif link._disconnecting:
            # A second disconnect should not report success while the first
            # caller is still tearing down sockets and callbacks. A
            # re-entrant call from the owner must return to avoid deadlock.
            pass
        elif not (
            link.remote_mode != "idle"
            or link._listener_socket is not None
            or link.remote_link is not None
            or link.remote_endpoint is not None
            or link.network_session is not None
            or link._pending_remote_link is not None
            or link._pending_remote_endpoint is not None
            or link._pending_network_session is not None
            or link._pending_listener_socket is not None
            or link._pending_local_link_session is not None
            or link._connect_in_progress
            or link._listener_start_in_progress
            or listener_thread is not None
        ):
            # In particular, do not deactivate local-pair hooks when the
            # caller uses the remote disconnect tool while no remote link is
            # active. The public contract says the two modes are independent.
            return
        else:
            link._disconnecting = True
            link._disconnect_owner = current_thread_id
            link._disconnect_done.clear()
            link._generation += 1
            listener = link._listener_socket
            if listener is None:
                listener = link._pending_listener_socket
            remote = link.remote_link if link.remote_link is not None else link._pending_remote_link
            remote_endpoint = (
                link.remote_endpoint
                if link.remote_endpoint is not None
                else link._pending_remote_endpoint
            )
            network_session = (
                link.network_session
                if link.network_session is not None
                else link._pending_network_session
            )
            connect_in_progress = link._connect_in_progress
            connect_done = link._connect_done
            link._listener_cancel.set()
            link._connect_cancel.set()
            link._listener_socket = None
            link._pending_listener_socket = None
            link.remote_link = None
            link.remote_endpoint = None
            link.network_session = None
            link._pending_remote_link = None
            link._pending_remote_endpoint = None
            link._pending_network_session = None
            link.remote_mode = "disconnecting"
            link.remote_role = None
            link.remote_bind_port = None

    if cancel_local_setup:
        # Signal cancellation before attempting hook cleanup.  The pairing
        # worker may currently hold both owner locks; this call is bounded by
        # the normal cleanup contract and does not dismantle a published
        # local pair because setup has not completed yet.
        with link.state():
            setup_deadline = link._local_setup_deadline
        hook_timeout = (
            _remaining(setup_deadline)
            if setup_deadline is not None
            else _entry._DEFAULT_CLEANUP_TIMEOUT_S
        )
        _entry._deactivate_link_hooks(session, local_peer, timeout_s=hook_timeout)
        return

    if retry_local_cleanup:
        assert pending_local is not None
        cleanup_deadline = time.monotonic() + _entry._DEFAULT_CLEANUP_TIMEOUT_S
        cleanup_errors: list[Exception] = []
        try:
            _detach_local_link_session(
                link,
                session,
                pending_local,
                timeout_s=_remaining(cleanup_deadline),
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append(exc)
        try:
            cleanup_errors.extend(
                _entry._deactivate_link_hooks(
                    session,
                    local_peer,
                    timeout_s=_remaining(cleanup_deadline),
                )
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_errors.append(exc)
        with link.state():
            if cleanup_errors:
                link._pending_local_link_session = pending_local
            else:
                if link._pending_local_link_session is pending_local:
                    link._pending_local_link_session = None
            link._local_pair_in_progress = False
            link._local_setup_deadline = None
            link._local_setup_done.set()
        if cleanup_errors:
            details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in cleanup_errors)
            raise McpHarnessError("link_teardown_failed", details)
        return

    if done is not None:
        if not owned_by_current_thread:
            if not done.wait(timeout=_entry._DEFAULT_CLEANUP_TIMEOUT_S):
                raise McpHarnessError(
                    "link_teardown_timeout",
                    "another link teardown is still in progress after the "
                    f"{_entry._DEFAULT_CLEANUP_TIMEOUT_S:g}s cleanup deadline",
                )
            with link.state():
                teardown_error = link._remote_error or link._listener_error
            if teardown_error is not None:
                raise McpHarnessError("link_teardown_failed", str(teardown_error))
        return

    cleanup_errors: list[Exception] = []
    cleanup_deadline = time.monotonic() + _entry._DEFAULT_CLEANUP_TIMEOUT_S
    listener_close_failed = False
    remote_close_failed = False
    network_detach_failed = False
    hook_cleanup_failed = False
    try:
        if listener is not None:
            try:
                listener.close()
            except Exception as exc:  # noqa: BLE001 - preserve the handle for retry
                listener_close_failed = True
                cleanup_errors.append(exc)
        if remote is not None:
            try:
                remote_closed = _close_serial_link(
                    remote,
                    timeout_s=max(0.0, cleanup_deadline - time.monotonic()),
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must continue
                remote_closed = False
                cleanup_errors.append(exc)
            if not remote_closed:
                remote_close_failed = True
                cleanup_errors.append(
                    TimeoutError("remote link worker did not stop before cleanup deadline")
                )
        if network_session is not None:
            try:
                with session.locked(timeout_s=max(0.0, cleanup_deadline - time.monotonic())):
                    network_session.detach_all()
            except Exception as exc:  # noqa: BLE001
                network_detach_failed = True
                cleanup_errors.append(exc)
        if (
            isinstance(listener_thread, threading.Thread)
            and listener_thread is not threading.current_thread()
        ):
            listener_thread.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        if connect_in_progress and not connect_done.wait(
            timeout=max(0.0, cleanup_deadline - time.monotonic())
        ):
            cleanup_errors.append(
                TimeoutError("connect worker did not stop before cleanup deadline")
            )
        # A remote endpoint owns raw PyBoy callbacks; a listener that never
        # accepted a peer does not. This conditional also preserves an active
        # in-process pair when link_disconnect is called in its idle state.
        if remote_endpoint is not None or network_session is not None:
            if remote_endpoint is not None:
                endpoint_errors = _uninstall_remote_endpoint(
                    session,
                    remote_endpoint,
                    timeout_s=max(0.0, cleanup_deadline - time.monotonic()),
                )
                if endpoint_errors:
                    hook_cleanup_failed = True
                    cleanup_errors.extend(endpoint_errors)
            hook_errors = _entry._deactivate_link_hooks(
                session,
                timeout_s=max(0.0, cleanup_deadline - time.monotonic()),
            )
            if hook_errors:
                hook_cleanup_failed = True
                cleanup_errors.extend(hook_errors)
    finally:
        with link.state():
            if listener_thread is not None and listener_thread.is_alive():
                cleanup_errors.append(
                    TimeoutError("listener worker did not stop before cleanup deadline")
                )
            pending_listener = link._pending_listener_socket
            pending_remote = link._pending_remote_link
            pending_endpoint = link._pending_remote_endpoint
            pending_network = link._pending_network_session
            if not cleanup_errors and any(
                resource is not None
                for resource in (
                    pending_listener,
                    pending_remote,
                    pending_endpoint,
                    pending_network,
                )
            ):
                # A connect/accept worker may have finished its own cleanup
                # after this teardown captured its first snapshot. Pending
                # handles are still live ownership, not a successful idle
                # transition; force the caller to retry link_disconnect.
                cleanup_errors.append(
                    TimeoutError("remote setup cleanup remained pending after teardown")
                )
            if cleanup_errors:
                link._listener_error = cleanup_errors[0]
                link._remote_error = cleanup_errors[0]
                # Keep every resource that still needs another cleanup pass.
                # The public remote fields stay empty while this state is
                # disconnecting, so no emulator operation can use a resource
                # during teardown.
                link._pending_listener_socket = (
                    listener if listener_close_failed else pending_listener
                )
                link._pending_remote_link = remote if remote_close_failed else pending_remote
                link._pending_remote_endpoint = (
                    remote_endpoint if hook_cleanup_failed else pending_endpoint
                )
                link._pending_network_session = (
                    network_session
                    if network_detach_failed or hook_cleanup_failed
                    else pending_network
                )
            else:
                link._listener_error = None
                link._remote_error = None
                link._pending_listener_socket = None
                link._pending_remote_link = None
                link._pending_remote_endpoint = None
                link._pending_network_session = None
            # Failed cleanup remains visibly non-idle and can be retried by a
            # subsequent link_disconnect. A clean teardown is the only path
            # that returns the lifecycle to idle.
            link.remote_mode = "disconnecting" if cleanup_errors else "idle"
            link.remote_role = None
            link.remote_bind_port = None
            if listener_thread is None or not listener_thread.is_alive():
                link._listener_thread = None
            else:
                # Retain ownership of a worker that outlived the bounded
                # join. _require_remote_idle() blocks a new listener until
                # this handle becomes non-live, avoiding overlap with stale
                # socket/backend cleanup.
                link._listener_thread = listener_thread
            link._listener_cancel = threading.Event()
            if not connect_in_progress or connect_done.is_set():
                link._connect_cancel = threading.Event()
            link._disconnect_owner = None
            link._disconnecting = False
            link._disconnect_done.set()

    if cleanup_errors:
        details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in cleanup_errors)
        raise McpHarnessError("link_teardown_failed", details)


# Call-time indirection so facade-level monkeypatches stay visible here.
import pokered_harness.mcp_server as _entry
