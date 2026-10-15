"""Private native-local MCP custody; no public provider API changes.

State-lock sections never wait for emulator/pair/provider ownership. A worker
and its admitted cancellation actor jointly fence replacement until both have
released their resource locks. Finite Python unwinding is supported; killing a
process or indefinitely interrupting finalization is not an atomic transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import time
from types import TracebackType

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.mcp_server_model import McpHarnessError
from pokered_harness.session import locked_sessions


@dataclass
class LocalAttempt:
    generation: int
    primary: object
    peer: object
    deadline: float
    provider: object | None = None
    worker_finished: bool = False
    actor: object | None = None
    committed: bool = False
    detach_pending: bool = False
    hooks_pending: bool = False
    actor_errors_pending: bool = False
    phase: str = "reserved"
    primary_error: BaseException | None = None
    primary_traceback: TracebackType | None = None
    secondary: list[BaseException] = field(default_factory=list)

    @property
    def pending(self):
        return self.detach_pending or self.hooks_pending or self.actor_errors_pending


def _entry():
    from pokered_harness import mcp_server

    return mcp_server


def _remember(attempt, error):
    if attempt.primary_error is None:
        attempt.primary_error = error
        attempt.primary_traceback = error.__traceback__
    elif error is not attempt.primary_error:
        attempt.secondary.append(error)
        try:
            attempt.primary_error.add_note(f"local cleanup also failed: {error!r}")
        except BaseException:  # diagnostic formatting must not replace custody
            pass


def _secondary(attempt, error):
    attempt.secondary.append(error)
    if attempt.primary_error is not None:
        try:
            attempt.primary_error.add_note(f"local cleanup also failed: {error!r}")
        except BaseException:
            pass


@contextmanager
def bounded_pair_lock(lock, deadline, secondary=None):
    acquired = lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
    if not acquired:
        raise TimeoutError("local pair lock cleanup deadline expired")
    error = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            lock.release()
        except BaseException as release_error:
            if error is None:
                raise
            if secondary is not None:
                secondary.append(release_error)
            try:
                error.add_note(f"pair lock release also failed: {release_error!r}")
            except BaseException:
                pass


@contextmanager
def provider_deadline(provider, deadline, secondary=None):
    """Bridge the real provider decorator without changing detach_all's API."""
    if not isinstance(provider, PyBoyLinkSession):
        # Historical seeded test adapters retain the no-argument call shape.
        # This is not a preemption guarantee for arbitrary adapter bodies.
        yield
        return
    state = provider._cleanup_state
    missing = object()
    previous = getattr(state, "deadline", missing)
    effective = deadline if previous is missing or previous is None else min(deadline, previous)
    state.deadline = effective
    error = None
    try:
        yield
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            if previous is missing:
                del state.deadline
            else:
                state.deadline = previous
        except BaseException as restore_error:
            if error is None:
                raise
            if secondary is not None:
                secondary.append(restore_error)
            try:
                error.add_note(f"provider deadline restoration also failed: {restore_error!r}")
            except BaseException:
                pass


def detach_local(
    link, session, provider, *, timeout_s, peer=None, deadline=None, secondary=None
):
    """Owners -> timed pair -> provider's own bounded decorator, one budget."""
    if deadline is None:
        deadline = time.monotonic() + max(0.0, timeout_s)
    if peer is None:
        peer = link.peer_session
    targets = [session] if peer is None or peer is session else [session, peer]
    primary_error = None
    primary_traceback = None
    try:
        with locked_sessions(
            *targets, allow_closed=True, timeout_s=max(0.0, deadline - time.monotonic())
        ):
            try:
                with bounded_pair_lock(link._pair_lock, deadline, secondary):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("local provider cleanup deadline expired")
                    with provider_deadline(provider, deadline, secondary):
                        provider.detach_all()
            except BaseException as error:
                # Capture INSIDE the owners: ExitStack may encounter another
                # release failure after actually releasing an owner lock.
                primary_error = error
                primary_traceback = error.__traceback__
                raise
    except BaseException as error:
        if primary_error is not None and error is not primary_error:
            # ExitStack can chain failures from both owner releases. Retain
            # their actual objects, not only the final replacement's text.
            later = error
            seen = set()
            while later is not None and later is not primary_error and id(later) not in seen:
                seen.add(id(later))
                if secondary is not None and not any(later is item for item in secondary):
                    secondary.append(later)
                try:
                    primary_error.add_note(f"owner release also failed: {later!r}")
                except BaseException:
                    pass
                later = later.__context__
            raise primary_error.with_traceback(primary_traceback)
        raise


def _settle_locked(link, attempt):
    if link._local_setup_attempt is not attempt:
        return
    if not attempt.worker_finished or attempt.actor is not None:
        link._local_pair_in_progress = True
        link._local_setup_done.clear()
        return
    link._local_pair_in_progress = False
    link._local_setup_deadline = None
    if attempt.pending:
        if not attempt.committed and link.local_link_session is attempt.provider:
            link.local_link_session = None
        attempt.phase = "pending_cleanup"
        link._pending_local_link_session = attempt.provider if attempt.detach_pending else None
    else:
        attempt.phase = "published" if attempt.committed else "rolled_back"
        if link._pending_local_link_session is attempt.provider:
            link._pending_local_link_session = None
        if not attempt.committed and link.local_link_session is attempt.provider:
            link.local_link_session = None
        if not attempt.committed:
            link._local_setup_attempt = None
    link._local_setup_done.set()


def _cleanup(link, attempt):
    entry = _entry()
    failures = []
    if attempt.detach_pending:
        try:
            entry._detach_local_link_session(
                link, attempt.primary, attempt.provider,
                peer=attempt.peer, deadline=attempt.deadline, secondary=attempt.secondary,
                timeout_s=max(0.0, attempt.deadline - time.monotonic()),
            )
        except BaseException as exc:
            failures.append(exc)
            _remember(attempt, exc)
        else:
            attempt.detach_pending = False
    if attempt.hooks_pending:
        try:
            errors = entry._deactivate_link_hooks(
                attempt.primary, attempt.peer,
                timeout_s=max(0.0, attempt.deadline - time.monotonic()),
                catch_base_exceptions=True, deadline=attempt.deadline,
            )
        except BaseException as exc:
            failures.append(exc)
            _remember(attempt, exc)
        else:
            # Historical facade observation wrappers may omit their return.
            errors = errors or []
            failures.extend(errors)
            for error in errors:
                _remember(attempt, error)
            if not errors:
                attempt.hooks_pending = False

    return failures


def pair_native(link, primary, peer):
    entry = _entry()
    with link.state():
        generation = link._local_generation + 1
        attempt = LocalAttempt(
            generation, primary, peer, time.monotonic() + entry._DEFAULT_CLEANUP_TIMEOUT_S
        )
        link._local_generation = generation
        link._local_setup_attempt = attempt
        link._local_pair_in_progress = True
        link._local_setup_deadline = attempt.deadline
        link._local_setup_done.clear()
    cleanup_attempted = False
    try:
        attempt.phase = "constructing"
        attempt.provider = entry.PyBoyLinkSession.local()
        attempt.detach_pending = True
        with link.state():
            link._pending_local_link_session = attempt.provider
        with locked_sessions(
            primary, peer, timeout_s=max(0.0, attempt.deadline - time.monotonic())
        ), link._pair_lock:
            try:
                attempt.phase = "attaching"
                attempt.provider.attach(primary._pyboy)
                attempt.provider.attach(peer._pyboy)
                with link.state():
                    if link._local_generation != generation:
                        raise McpHarnessError("link_cancelled", "local link pairing cancelled")
            except BaseException as exc:
                _remember(attempt, exc)
                attempt.hooks_pending = True
                cleanup_attempted = True
                _cleanup(link, attempt)
                raise
        with link.state():
            if link._local_generation != generation:
                raise McpHarnessError("link_cancelled", "local link pairing cancelled")
            attempt.phase = "publishing"
            link.local_link_session = attempt.provider
            if link._pending_local_link_session is attempt.provider:
                link._pending_local_link_session = None
            attempt.committed = True
            attempt.detach_pending = False
        return {
            "paired": True,
            "primary_version": link.primary_version,
            "peer_version": link.peer_version,
        }
    except BaseException as exc:
        _remember(attempt, exc)
        if not attempt.committed and not cleanup_attempted:
            attempt.hooks_pending = True
            _cleanup(link, attempt)
        # A body failure captured before __exit__ remains primary, even if
        # the lock adapter raised a different process-control exception.
        error = attempt.primary_error
        if error is exc:
            raise
        raise error.with_traceback(attempt.primary_traceback)
    finally:
        with link.state():
            attempt.worker_finished = True
            _settle_locked(link, attempt)


def _cancel_actor(link, attempt, token):
    """The registration fences replacement through actual owner release."""
    actor_errors = []
    try:
        errors = _entry()._deactivate_link_hooks(
            attempt.primary, attempt.peer,
            timeout_s=max(0.0, attempt.deadline - time.monotonic()),
            catch_base_exceptions=True, custody=(link, attempt, token),
            deadline=attempt.deadline,
        )
        errors = errors or []  # preserve no-return facade observation spies
        actor_errors.extend(errors)
        with link.state():
            for error in errors:
                _secondary(attempt, error)
            if errors:
                attempt.hooks_pending = True
                attempt.actor_errors_pending = True
    except BaseException as exc:
        actor_errors.append(exc)
        with link.state():
            _secondary(attempt, exc)
            attempt.hooks_pending = True
            attempt.actor_errors_pending = True
    finally:
        # This runs AFTER the outer owner context exited, including failure.
        with link.state():
            if link._local_setup_attempt is attempt and attempt.actor is token:
                attempt.actor = None
                _settle_locked(link, attempt)

    if actor_errors:
        error = actor_errors[0]
        if not isinstance(error, Exception):
            raise error.with_traceback(error.__traceback__)
        raise McpHarnessError("link_teardown_failed", "local cancellation hook cleanup failed")


def _retry(link, attempt, token):
    try:
        failures = _cleanup(link, attempt)
    finally:
        with link.state():
            if link._local_setup_attempt is attempt and attempt.actor is token:
                attempt.actor = None
                _settle_locked(link, attempt)
    if failures and not isinstance(failures[0], Exception):
        raise failures[0].with_traceback(failures[0].__traceback__)
    if attempt.pending:
        raise McpHarnessError(
            "link_teardown_failed",
            "; ".join(type(error).__name__ for error in attempt.secondary)
            or "local cleanup remains pending",
        )


def cleanup_local(link, primary, *, disconnect):
    """Return whether native-local custody handled this real consumer."""
    with link.state():
        attempt = link._local_setup_attempt
        if attempt is not None and attempt.committed:
            if disconnect:
                return True  # ordinary remote disconnect protects healthy local pair
            if not attempt.worker_finished or attempt.actor is not None:
                raise McpHarnessError("link_busy", "local setup is still unwinding")
        if attempt is not None and (
            not attempt.worker_finished or attempt.actor is not None
        ):
            if not disconnect:
                raise McpHarnessError("link_busy", "local setup is still unwinding")
            link._local_generation += 1
            if attempt.actor is not None:
                return True  # concurrent and recursive callers coalesce; never self-wait
            token = object()
            attempt.actor = token
            cancel = True
        else:
            provider = link._pending_local_link_session
            if not disconnect and provider is None:
                provider = link.local_link_session
            if attempt is None and provider is None:
                return False
            if attempt is None:
                attempt = LocalAttempt(
                    link._local_generation, primary, link.peer_session,
                    time.monotonic() + _entry()._DEFAULT_CLEANUP_TIMEOUT_S,
                    provider=provider, worker_finished=True, detach_pending=True,
                    hooks_pending=True,
                )
                link._local_setup_attempt = attempt
            else:
                attempt.deadline = time.monotonic() + _entry()._DEFAULT_CLEANUP_TIMEOUT_S
                if attempt.committed:
                    attempt.detach_pending = True
                    attempt.hooks_pending = True
            attempt.hooks_pending = attempt.hooks_pending or attempt.actor_errors_pending
            attempt.actor_errors_pending = False
            token = object()
            attempt.actor = token
            attempt.committed = False
            link._local_pair_in_progress = True
            link._local_setup_done.clear()
            link._local_setup_deadline = attempt.deadline
            cancel = False
    if cancel:
        _cancel_actor(link, attempt, token)
    else:
        _retry(link, attempt, token)
    return True
