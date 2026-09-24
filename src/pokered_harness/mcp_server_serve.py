"""Server construction, default hooks, stdio serving and CLI main.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.server import Server
from mcp.shared.exceptions import McpError

# Direct names this module calls; cyclic back-edges go through _entry.
from pokered_harness.mcp_server_model import (
    _LOCAL_LINK_TOOL_NAMES,
    _MAX_STATE_BYTES,
    _TIMED_UNSUPPORTED_TOOL_NAMES,
    _URI_LINK_STATUS,
    _URI_LINK_TRANSPORT,
    _URI_PEER_EVENTS,
    _URI_PEER_GAME_STATE,
    _URI_PEER_PARTY_RECORDS,
    DEFAULT_HOOKS,
    LinkState,
    McpHarnessError,
    _error_payload,
    _error_reply,
    _text_reply,
)
from pokered_harness.mcp_server_resources import _resource_specs
from pokered_harness.mcp_server_timed import (
    _await_blocking_task,
    _await_owner_request,
    _close_sessions_independently,
    _McpTaskRegistry,
    _shutdown_timed_owner,
    _timed_status,
    _timed_status_payload,
    _timed_tool_request,
    load_timed_policy_from_env,
)
from pokered_harness.mcp_server_tools import _tool_specs
from pokered_harness.mcp_timed_owner import TimedOwner, TimedOwnerPolicy
from pokered_harness.session import Session


def build_server(
    session: Session,
    *,
    peer_session: Session | None = None,
    primary_version: str = "red",
    peer_version: str = "red",
    link: LinkState | None = None,
    timed_policy: TimedOwnerPolicy | None = None,
    timed_owner: TimedOwner | None = None,
) -> Server:
    """Construct an MCP ``Server`` bound to ``session``.

    When ``peer_session`` (or a pre-built ``link`` container) is provided,
    the server additionally exposes the ``link_*`` tools and peer
    resources. The pair itself is constructed lazily on the first
    ``link_pair`` call.

    The server stays un-run — caller invokes ``server.run`` via an
    appropriate transport. This makes the wiring itself testable without
    spawning stdio pipes.

    ``timed_policy`` explicitly selects single-session timed remote mode.
    Every live Session operation then runs on ``_pokered_timed_owner``;
    status is a cached observation. The caller must close that owner (which
    closes its Session) or use ``serve_stdio`` for managed shutdown. A false
    owner close result retains ownership and must never trigger foreign
    Session teardown. Supplying a local peer or active legacy link is invalid.
    """
    if link is None:
        link = LinkState(
            peer_session=peer_session,
            primary_version=primary_version,
            peer_version=peer_version,
        )

    if timed_owner is not None and timed_policy is None:
        raise McpHarnessError("invalid_timed_configuration", "timed_owner requires timed_policy")
    if timed_policy is not None and (link.peer_session is not None or peer_session is not None):
        raise McpHarnessError(
            "invalid_timed_configuration", "timed transport requires a single local session"
        )
    if timed_policy is not None and (
        link.remote_mode != "idle"
        or link.pair is not None
        or link.local_link_session is not None
        or link._pending_local_link_session is not None
        or link._local_pair_in_progress
        or link.remote_link is not None
        or link.remote_endpoint is not None
        or link.network_session is not None
        or link._pending_remote_link is not None
        or link._pending_remote_endpoint is not None
        or link._pending_network_session is not None
        or link._pending_listener_socket is not None
        or link._listener_socket is not None
        or link._listener_start_in_progress
        or link._connect_in_progress
        or link._disconnecting
    ):
        raise McpHarnessError(
            "invalid_timed_configuration", "timed transport requires an idle legacy link"
        )
    if timed_owner is not None and (
        timed_owner.session is not session or timed_owner.policy != timed_policy
    ):
        raise McpHarnessError(
            "invalid_timed_configuration", "timed owner session or policy does not match server"
        )
    if timed_policy is not None:
        timed_owner = timed_owner or TimedOwner(session, timed_policy)

    server: Server = Server("pokered-harness")
    request_tasks = _McpTaskRegistry()
    # ``serve_stdio`` uses this private handle during transport EOF cleanup.
    # Keeping it on the server also makes the ownership explicit for callers
    # that construct a server first and select their transport later.
    server._pokered_request_tasks = request_tasks  # type: ignore[attr-defined]
    server._pokered_timed_owner = timed_owner  # type: ignore[attr-defined]

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        if timed_owner is not None:
            specs = [
                spec
                for spec in _tool_specs(has_peer=True)
                if spec.name not in _TIMED_UNSUPPORTED_TOOL_NAMES
                and (spec.name == "link_step" or spec.name not in _LOCAL_LINK_TOOL_NAMES)
            ]
            for spec in specs:
                if spec.name in {"link_listen", "link_connect"}:
                    spec.inputSchema["properties"]["timeout_s"].pop("default", None)
                    spec.description = (
                        "Bind timed listener and return when ready; poll link_status for connection."
                        if spec.name == "link_listen"
                        else "Connect timed transport within the explicit setup deadline."
                    )
                elif spec.name == "link_step":
                    spec.description = (
                        "Advance this session by count ticks through its timed endpoint."
                    )
            return specs
        return _tool_specs(has_peer=link.peer_session is not None)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> mcp_types.CallToolResult:
        args = arguments or {}
        if timed_owner is not None:
            try:
                if name == "link_status":
                    return _text_reply(_timed_status(timed_owner))
                request = _timed_tool_request(timed_owner, session, name, args, link)
                result = await _await_owner_request(request, ready=name == "link_listen")
                if name in {"link_listen", "link_connect", "link_disconnect"}:
                    result = (
                        _timed_status_payload(result)
                        if name == "link_listen"
                        else _timed_status(timed_owner)
                    )
                return _text_reply(result)
            except Exception as exc:  # noqa: BLE001
                return _error_reply(exc)
        # ``asyncio.to_thread`` cannot cancel a Python worker that is already
        # executing. Keep an explicit Task and shield it so a cancelled MCP
        # request does not orphan an emulator operation. For link-related
        # work, close the remote transport from a second thread to wake a
        # serial hook or an in-progress connect before waiting for the worker
        # to settle.
        worker = request_tasks.start(lambda: _entry.dispatch_tool(session, name, args, link))
        try:
            # The emulator is not asyncio-aware. Run the blocking operation
            # off the event loop while the Session/LinkState locks preserve
            # single-emulator ordering. This also leaves the loop responsive
            # to status/resource requests while a bounded TCP call waits.
            result = await _await_blocking_task(
                worker,
                task_registry=request_tasks,
                cancel_cleanup=(
                    None
                    if name == "link_status"
                    else lambda: _entry._disconnect_remote(link, session)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return _error_reply(exc)
        return _text_reply(result)

    @server.list_resources()
    async def _list_resources() -> list[mcp_types.Resource]:
        return _resource_specs(has_peer=link.peer_session is not None)

    @server.read_resource()
    async def _read_resource(uri: Any) -> str:
        resource_uri = str(uri)
        if timed_owner is not None:
            try:
                if resource_uri == _URI_LINK_STATUS:
                    return json.dumps(_timed_status(timed_owner))
                if resource_uri in {
                    _URI_PEER_GAME_STATE,
                    _URI_PEER_PARTY_RECORDS,
                    _URI_PEER_EVENTS,
                    _URI_LINK_TRANSPORT,
                }:
                    raise McpHarnessError(
                        "timed_unsupported_resource", "timed remote transport has no local peer"
                    )
                return await _await_owner_request(
                    timed_owner.submit(
                        lambda owned: _entry.read_resource(owned, resource_uri, link)
                    )
                )
            except Exception as exc:
                raise McpError(
                    mcp_types.ErrorData(
                        code=0, message=str(exc) or type(exc).__name__, data=_error_payload(exc)
                    )
                ) from exc
        worker = request_tasks.start(lambda: _entry.read_resource(session, resource_uri, link))
        try:
            return await _await_blocking_task(
                worker,
                task_registry=request_tasks,
                # A resource read may be waiting on a remote endpoint or a
                # session lock.  The same cancellation wake-up used by tools
                # is required so EOF cannot strand a thread outside server
                # cleanup.
                cancel_cleanup=lambda: _entry._disconnect_remote(link, session),
            )
        except Exception as exc:
            # The low-level MCP server turns McpError into a protocol
            # ErrorData response. Without this boundary, a resource failure
            # becomes an untyped JSON-RPC error even though tool failures
            # already expose a stable machine-readable envelope.
            raise McpError(
                mcp_types.ErrorData(
                    code=0,
                    message=str(exc) or type(exc).__name__,
                    data=_error_payload(exc),
                )
            ) from exc

    return server


def register_default_hooks(session: Session) -> list[str]:
    """Register the ADR's canonical event hooks. Returns the event names
    that were wired up (skipping any whose symbol is missing)."""
    registered: list[str] = []
    for symbol_name, event_name in DEFAULT_HOOKS:
        if symbol_name not in session.symbols:
            continue
        session.register_hook(symbol_name, event_name)
        registered.append(event_name)
    return registered


# -- entry point -------------------------------------------------------------


async def serve_stdio(
    session: Session,
    *,
    peer_session: Session | None = None,
    primary_version: str = "red",
    peer_version: str = "red",
    link: LinkState | None = None,
    timed_policy: TimedOwnerPolicy | None = None,
    timed_owner: TimedOwner | None = None,
) -> None:
    """Serve MCP; timed mode transfers Session cleanup to its persistent owner."""
    owned_link = link or LinkState(
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
    )
    timed_options: dict[str, Any] = {}
    if timed_policy is not None or timed_owner is not None:
        timed_options = {"timed_policy": timed_policy, "timed_owner": timed_owner}
    server = _entry.build_server(
        session,
        peer_session=peer_session,
        primary_version=primary_version,
        peer_version=peer_version,
        link=owned_link,
        **timed_options,
    )
    owner = getattr(server, "_pokered_timed_owner", None)
    request_tasks = getattr(server, "_pokered_request_tasks", None)
    if owner is not None:
        try:
            async with _entry.stdio_server() as (read_stream, write_stream):
                with contextlib.redirect_stdout(sys.stderr):
                    await server.run(
                        read_stream, write_stream, server.create_initialization_options()
                    )
        finally:
            active_exception = sys.exc_info()[1]
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    await _shutdown_timed_owner(owner, timed_policy, request_tasks)
            except Exception as exc:
                if active_exception is not None:
                    active_exception.add_note(f"MCP timed cleanup failed: {exc}")
                else:
                    raise
        return
    try:
        async with _entry.stdio_server() as (read_stream, write_stream):
            # stdio_server captures the real stdout file descriptor before
            # entering this block. Redirecting the Python stream only after
            # that capture keeps JSON-RPC on the original stdout while
            # routing emulator/library diagnostics printed during requests to
            # stderr as well as during boot.
            with contextlib.redirect_stdout(sys.stderr):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
    finally:
        active_exception = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        with contextlib.redirect_stdout(sys.stderr):
            try:
                _entry._disconnect_remote(owned_link, session)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            # A stdio client can terminate while either link mode is active.
            # Restore native serial backends before the owning Sessions are
            # closed by main().
            try:
                # ``dispatch_tool`` normally waits for the operation guard
                # without a deadline.  During stdio EOF cleanup, a request
                # that is still inside a link operation must not turn that
                # wait into an unbounded server shutdown.
                with owned_link.operation(timeout_s=_entry._DEFAULT_CLEANUP_TIMEOUT_S):
                    _entry.dispatch_tool(session, "link_unpair", {}, link=owned_link)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            if request_tasks is not None:
                try:
                    drained = await request_tasks.wait_until(
                        asyncio.get_running_loop().time() + _entry._DEFAULT_CLEANUP_TIMEOUT_S
                    )
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
                else:
                    if not drained:
                        cleanup_errors.append(
                            TimeoutError(
                                "MCP request worker did not stop before the "
                                f"{_entry._DEFAULT_CLEANUP_TIMEOUT_S:g}s shutdown deadline"
                            )
                        )
        if cleanup_errors:
            details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in cleanup_errors)
            if active_exception is not None:
                active_exception.add_note(f"MCP cleanup failed: {details}")
            else:
                raise McpHarnessError("server_cleanup_failed", details)


_PEER_STATE_SHA1_ENV = "POKERED_PEER_STATE_SHA1"


_PEER_STATE_PATH_ENV = "POKERED_PEER_STATE_PATH"


def _peer_startup_state_from_env() -> bytes | None:
    """Read the documented peer-fixture launch contract.

    The public tool surface can only ``load_state`` the primary session, so a
    second session's starting fixture has no supported tool call.  The
    production entry point therefore publishes an explicit launch contract:
    ``POKERED_PEER_STATE_PATH`` names the immutable peer fixture and
    ``POKERED_PEER_STATE_SHA1`` pins its bytes.  Both are required together and
    the pin is verified before the bytes reach the emulator, so a caller cannot
    silently start the peer from unpinned or substituted state.  A checkout
    that does not need a peer fixture leaves both unset.
    """
    raw_path = os.environ.get(_PEER_STATE_PATH_ENV)
    raw_sha1 = os.environ.get(_PEER_STATE_SHA1_ENV)
    path = raw_path.strip() if raw_path is not None else ""
    sha1 = raw_sha1.strip() if raw_sha1 is not None else ""
    if not path and not sha1:
        return None
    if not path or not sha1:
        raise SystemExit(
            f"{_PEER_STATE_PATH_ENV} and {_PEER_STATE_SHA1_ENV} must be set "
            "together; a peer fixture is never loaded without its pin"
        )
    expected = sha1.lower()
    if len(expected) != 40 or any(char not in "0123456789abcdef" for char in expected):
        raise SystemExit(f"{_PEER_STATE_SHA1_ENV} must be a 40-character SHA-1 hex digest")
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SystemExit(
            f"unable to read {_PEER_STATE_PATH_ENV}: {type(exc).__name__}: {exc}"
        ) from exc
    if not data or len(data) > _MAX_STATE_BYTES:
        raise SystemExit(
            f"{_PEER_STATE_PATH_ENV} must hold between 1 and {_MAX_STATE_BYTES} "
            f"bytes; observed {len(data)}"
        )
    observed = hashlib.sha1(data).hexdigest()
    if observed != expected:
        raise SystemExit(
            f"{_PEER_STATE_PATH_ENV} SHA-1 mismatch: expected {expected}, observed {observed}"
        )
    return data


def main() -> None:
    """CLI entry point: ``python -m pokered_harness.mcp_server``.

    Required environment variables:

    * ``POKERED_ROM_PATH`` — path to the primary .gb ROM.
    * ``POKERED_SYM_PATH`` — path to the matching .sym file.

    Optional:

    * ``POKERED_ROM_SHA1`` — explicit pin. When unset, the server reads
      ``VERSIONS.md`` (relative to cwd) and enforces the SHA-1 from it. A
      wheel launched outside a checkout may use explicit primary and peer
      pins without a local ``VERSIONS.md``; the bundled runtime identity is
      still enforced.
    * ``POKERED_SYM_SHA1`` / ``POKERED_PEER_SYM_SHA1`` — explicit symbol-file
      pins for wheel launches without a local ``VERSIONS.md``. In a source
      checkout, matching per-path symbol pins are selected automatically.
    * ``POKERED_SKIP_SHA1=1`` — rejected by this production entry point;
      ad-hoc diagnostics must use a separate, explicitly non-production
      driver.
    * ``POKERED_PEER_ROM_PATH`` / ``POKERED_PEER_SYM_PATH`` /
      ``POKERED_PEER_ROM_SHA1`` — when set, a peer Session is constructed
      at startup and the link-cable tools become usable. The pair is NOT
      auto-paired — invoke ``link_pair`` explicitly.
    * ``POKERED_PEER_STATE_PATH`` / ``POKERED_PEER_STATE_SHA1`` — the
      documented public peer-fixture launch contract. When both are set, this
      entry point verifies the pinned peer save state and loads it before
      serving, so a caller never constructs a Session or calls a private
      ``Session.load_state`` to reproduce the peer's starting board. Both
      variables are required together, the digest is verified before the bytes
      reach the emulator, and the pair must still be linked explicitly.
    * ``POKERED_LINK_TRANSPORT=timed`` — explicitly select timed remote
      transport. Every ``POKERED_TIMED_<FIELD>`` listed by
      :func:`load_timed_policy_from_env` is required; no timing policy is
      qualified or selected implicitly. Local peer configuration is invalid.
    """
    from pokered_harness.config import (
        VersionsConfigError,
        load_peer_env,
        load_primary_env,
        load_versions,
    )

    try:
        timed_policy = load_timed_policy_from_env()
    except McpHarnessError as exc:
        raise SystemExit(f"invalid timed MCP configuration: {exc}") from exc

    if _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "POKERED_SKIP_SHA1 is diagnostic-only and rejected by the MCP "
            "production entry point; unset it and provide the documented "
            "ROM and symbol SHA-1 pins"
        )

    primary_env = load_primary_env()
    peer_env = load_peer_env()
    try:
        primary_env.validate(role="primary session")
        peer_env.validate(role="peer session")
    except VersionsConfigError as exc:
        raise SystemExit(f"invalid POKERED_* configuration: {exc}") from exc
    if primary_env.rom_path is None or primary_env.sym_path is None:
        raise SystemExit("set POKERED_ROM_PATH and POKERED_SYM_PATH before launching")

    primary_rom, primary_sym = primary_env.resolved_paths()
    assert primary_rom is not None and primary_sym is not None
    peer_rom, peer_sym = peer_env.resolved_paths()
    peer_state_configured = any(
        (os.environ.get(name) or "").strip()
        for name in (_PEER_STATE_PATH_ENV, _PEER_STATE_SHA1_ENV)
    )
    if peer_state_configured and (peer_rom is None or peer_sym is None):
        raise SystemExit(
            f"{_PEER_STATE_PATH_ENV} requires a configured peer session; set "
            "POKERED_PEER_ROM_PATH and POKERED_PEER_SYM_PATH"
        )
    peer_state = _peer_startup_state_from_env()
    if timed_policy is not None and peer_rom is not None:
        raise SystemExit(
            "timed MCP transport requires a single local session; unset POKERED_PEER_*"
        )
    peer_symbol_sha_override = os.environ.get("POKERED_PEER_SYM_SHA1")
    if (
        peer_sym is None
        and peer_symbol_sha_override is not None
        and peer_symbol_sha_override.strip()
    ):
        raise SystemExit(
            "POKERED_PEER_SYM_SHA1 requires POKERED_PEER_SYM_PATH and a configured peer ROM"
        )

    versions = None
    if not _env_flag("POKERED_SKIP_SHA1"):
        try:
            versions = load_versions()
        except VersionsConfigError as exc:
            # A wheel-installed server may be launched outside the source
            # checkout, where VERSIONS.md is intentionally not part of the
            # Python package. Explicit primary/peer hashes are sufficient
            # for ROM identity in that deployment; the bundled PyBoy marker
            # below still enforces the runtime contract. If either hash is
            # absent, fail closed rather than silently losing pin coverage.
            missing_explicit_hash = primary_env.rom_sha1 is None or (
                peer_rom is not None and peer_env.rom_sha1 is None
            )
            if missing_explicit_hash:
                raise SystemExit(
                    "unable to load ROM/PyBoy pins; set POKERED_VERSIONS_PATH, "
                    "provide explicit primary and peer ROM SHA-1 values, or "
                    "explicitly set POKERED_SKIP_SHA1=1: "
                    f"{exc}"
                ) from exc

    expected_sha: str | None = primary_env.rom_sha1
    if expected_sha is None and versions is not None:
        expected_sha = versions.sha1_for_path(primary_rom)
        if expected_sha is None:
            raise SystemExit(
                "no ROM SHA-1 pin matches POKERED_ROM_PATH; set POKERED_ROM_SHA1 explicitly"
            )
    if expected_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "set POKERED_ROM_SHA1 or provide a matching per-ROM Path/SHA-1 entry in VERSIONS.md"
        )

    primary_symbol_sha = os.environ.get("POKERED_SYM_SHA1")
    if primary_symbol_sha is not None:
        primary_symbol_sha = primary_symbol_sha.strip() or None
    if primary_symbol_sha is None and versions is not None:
        primary_symbol_sha = versions.symbol_sha1_for_path(primary_sym)
    if primary_symbol_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "set POKERED_SYM_SHA1 or provide a matching symbol-file pin "
            "for POKERED_SYM_PATH in VERSIONS.md"
        )

    peer_expected_sha: str | None = peer_env.rom_sha1
    if peer_rom is not None and peer_expected_sha is None and versions is not None:
        peer_expected_sha = versions.sha1_for_path(peer_rom)
        if peer_expected_sha is None:
            raise SystemExit(
                "no ROM SHA-1 pin matches POKERED_PEER_ROM_PATH; set "
                "POKERED_PEER_ROM_SHA1 explicitly"
            )
    if peer_rom is not None and peer_expected_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
        raise SystemExit(
            "set POKERED_PEER_ROM_SHA1 or provide a matching peer per-ROM "
            "Path/SHA-1 entry in VERSIONS.md"
        )

    peer_symbol_sha: str | None = None
    if peer_sym is not None:
        peer_symbol_sha = os.environ.get("POKERED_PEER_SYM_SHA1")
        if peer_symbol_sha is not None:
            peer_symbol_sha = peer_symbol_sha.strip() or None
        if peer_symbol_sha is None and versions is not None:
            peer_symbol_sha = versions.symbol_sha1_for_path(peer_sym)
        if peer_symbol_sha is None and not _env_flag("POKERED_SKIP_SHA1"):
            raise SystemExit(
                "set POKERED_PEER_SYM_SHA1 or provide a matching peer "
                "symbol-file pin for POKERED_PEER_SYM_PATH in VERSIONS.md"
            )
    if versions is not None:
        expected_pyboy = versions.pyboy_version
        expected_pyboy_revision = versions.pyboy_revision
        if expected_pyboy_revision is None:
            raise SystemExit("VERSIONS.md is missing the pinned PyBoy fork revision")
    elif _env_flag("POKERED_SKIP_SHA1"):
        expected_pyboy = None
        expected_pyboy_revision = None
    else:
        # Keep the runtime identity check active for a wheel launched without
        # a checkout-local VERSIONS.md. The vendored runtime exposes both
        # values; a stock or partially installed PyBoy must not pass as the
        # production runtime merely because its ROM hash was supplied.
        try:
            import pyboy as pyboy_module

            expected_pyboy = getattr(pyboy_module, "__version__", None)
            revision = getattr(pyboy_module, "__pokered_harness_revision__", None)
        except Exception as exc:  # pragma: no cover - environment-specific
            raise SystemExit(
                "unable to identify the bundled PyBoy runtime without "
                f"VERSIONS.md: {type(exc).__name__}: {exc}"
            ) from exc
        if not expected_pyboy or not revision:
            raise SystemExit(
                "installed PyBoy is missing the pokered-harness runtime "
                "identity; provide VERSIONS.md or install the bundled "
                "runtime"
            )
        expected_pyboy_revision = revision

    # CRITICAL: PyBoy's init writes ~70KB of `.sym`-skip warnings to
    # stdout via a logging handler it installs before we get a chance
    # to reconfigure it. MCP's stdio transport uses stdout exclusively
    # for JSON-RPC, so anything the emulator prints would corrupt the
    # stream and wedge the client at ``initialize``. Capture all stdout
    # during boot and re-emit it on stderr so the noise is still
    # visible but out of the RPC channel.
    peer_session: Session | None = None
    session: Session | None = None
    timed_owner: TimedOwner | None = None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            session = Session.from_files(
                primary_rom,
                primary_sym,
                expected_rom_sha1=expected_sha,
                expected_symbol_sha1=primary_symbol_sha,
                expected_pyboy_version=expected_pyboy,
                expected_pyboy_revision=expected_pyboy_revision,
            )
            _entry.register_default_hooks(session)
            session.enable_battle_menu_observation()
            session.enable_battle_resolution_observation()
            session.enable_battle_end_observation()
            if peer_rom is not None and peer_sym is not None:
                peer_session = Session.from_files(
                    peer_rom,
                    peer_sym,
                    expected_rom_sha1=peer_expected_sha,
                    expected_symbol_sha1=peer_symbol_sha,
                    expected_pyboy_version=expected_pyboy,
                    expected_pyboy_revision=expected_pyboy_revision,
                )
                _entry.register_default_hooks(peer_session)
                peer_session.enable_battle_menu_observation()
                peer_session.enable_battle_resolution_observation()
                peer_session.enable_battle_end_observation()
                if peer_state is not None:
                    # The pin was verified before the emulator existed; the
                    # load itself is the same public state entry the primary
                    # session uses through the ``load_state`` tool.
                    peer_session.load_state(peer_state)

        timed_options: dict[str, Any] = {}
        if timed_policy is not None:
            timed_owner = TimedOwner(session, timed_policy)
            timed_options = {"timed_policy": timed_policy, "timed_owner": timed_owner}
        asyncio.run(
            _entry.serve_stdio(
                session,
                peer_session=peer_session,
                primary_version=primary_env.version,
                peer_version=peer_env.version,
                **timed_options,
            )
        )
    finally:
        active_exception = sys.exc_info()[1]
        owner_stopped = timed_owner is None
        owner_error: Exception | None = None
        if timed_owner is not None:
            try:
                owner_stopped = timed_owner.close(timeout=timed_policy.close_timeout)
            except Exception as exc:  # noqa: BLE001 - ownership remains transferred
                owner_error = exc
        cleanup_errors = _close_sessions_independently(
            peer_session, session if timed_owner is None else None
        )
        if not owner_stopped:
            cleanup_errors.append(
                (
                    "primary",
                    owner_error
                    or McpHarnessError(
                        "timed_cleanup_pending",
                        "Session retained by unresolved timed owner; foreign close refused",
                    ),
                )
            )
        if cleanup_errors:
            details = "; ".join(
                f"{role} {type(exc).__name__}: {exc}" for role, exc in cleanup_errors
            )
            if active_exception is not None:
                active_exception.add_note(f"MCP session cleanup failed: {details}")
            else:
                raise McpHarnessError("server_cleanup_failed", details)


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Call-time indirection so facade-level monkeypatches stay visible here.
import pokered_harness.mcp_server as _entry
