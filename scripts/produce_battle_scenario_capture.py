"""Bounded capture budget, atomic publication, and the capture driver.

Split out of ``scripts/produce_battle_scenario.py`` for the #122 file-size
contract with no behavior change: the code below is copied verbatim except that
calls to facade-owned, monkeypatch-patched entry points resolve through
``_entry`` so attribute patches on the loaded producer module stay visible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.produce_battle_scenario as _entry
from scripts.produce_battle_scenario_catalog import (
    _positive_int,
    sha1_file,
    validate_bounds,
    validate_scenario_metadata,
)
from scripts.produce_battle_scenario_model import (
    _BUTTON_NAMES,
    _CAPTURE_MESSAGE,
    _CAPTURE_PRODUCER,
    _DEFAULT_PRESS_DURATION,
    _DEFAULT_STEP_FRAMES,
    _LINK_RECEPTION_TILE,
    _LINK_STATE_CODES,
    _SUPPORTED_BOUNDARY,
    _UNMEASURED_RUNTIME,
    CaptureBoundsExceeded,
    CaptureNotAvailable,
    CapturePreconditionFailed,
    ScenarioRefusal,
    _note,
)


@dataclass(frozen=True)
class _InputStep:
    """One replayable capture input: a button press, or an idle frame run."""

    button: str | None
    duration: int
    frames: int

    @property
    def emulated_frames(self) -> int:
        """Frames this step advances the emulator.

        ``PyBoy.button`` only schedules a release; the emulator advances when
        the session ticks, so a press advances exactly ``frames`` frames and
        ``duration`` merely bounds the hold inside them.
        """
        return self.frames

    def render(self) -> str:
        if self.button is None:
            return f"step:{self.frames}"
        return f"press:{self.button}:{self.duration}:{self.frames}"


class _CaptureBudget:
    """Fail-closed frame, input, and wall-clock budget for one bounded drive.

    Every emulator advance is charged against the declared bounds *before* it
    happens, so a drive can never overshoot ``max_frames`` or ``max_inputs``,
    and the wall-clock deadline is re-checked around every advance.
    """

    def __init__(
        self,
        bounds: dict[str, Any],
        *,
        clock: Callable[[], float],
        deadline: float | None = None,
    ) -> None:
        if not isinstance(bounds, dict):
            raise ScenarioRefusal("capture bounds must be a declared object")
        # The effective bounds are validated here, not merely the scenario's:
        # a caller that overrides the declared bounds must not be able to disable
        # the deadline with a non-finite value.
        self.bounds = validate_bounds(
            max_frames=bounds.get("max_frames"),
            max_wall_seconds=bounds.get("max_wall_seconds"),
            max_inputs=bounds.get("max_inputs"),
        )
        self._max_frames = self.bounds["max_frames"]
        self._max_inputs = self.bounds["max_inputs"]
        self._clock = clock
        if deadline is None:
            self._deadline = clock() + self.bounds["max_wall_seconds"]
        else:
            # A caller may carry one absolute deadline across the whole promised
            # operation; an unusable value is refused rather than accepted as an
            # unreachable bound.
            if not math.isfinite(float(deadline)):
                raise ScenarioRefusal("capture deadline must be finite")
            self._deadline = float(deadline)
        self.frames = 0
        self.inputs = 0
        self.history: list[_InputStep] = []

    def check_wall(self, phase: str) -> None:
        """Refuse when the wall-clock deadline has already passed."""
        self._check_wall(phase)

    def _check_wall(self, phase: str) -> None:
        if self._clock() >= self._deadline:
            raise CaptureBoundsExceeded(
                f"capture exceeded max_wall_seconds during {phase}; "
                "the declared bound may only be raised with a recorded justification"
            )

    def _reserve_frames(self, frames: int) -> None:
        if self.frames + frames > self._max_frames:
            raise CaptureBoundsExceeded(
                f"capture would exceed max_frames ({self._max_frames}); "
                "aborted before advancing the emulator"
            )
        self.frames += frames

    def step(self, session: Any, frames: int) -> None:
        """Advance ``frames`` idle frames, charged against the bounds."""
        _positive_int(frames, "step frames")
        self._check_wall("idle step")
        step = _InputStep(None, 0, frames)
        self._reserve_frames(step.emulated_frames)
        session.step(frames)
        self.history.append(step)
        self._check_wall("idle step")

    def press(self, session: Any, button: str, *, duration: int, frames: int) -> None:
        """Press ``button`` and advance ``frames``, charged against the bounds."""
        if button not in _BUTTON_NAMES:
            raise ScenarioRefusal(f"unknown capture button {button!r}")
        _positive_int(duration, "press duration")
        _positive_int(frames, "press frames")
        if duration > frames:
            raise ScenarioRefusal(f"press duration {duration} exceeds the ticked frames {frames}")
        if self.inputs + 1 > self._max_inputs:
            raise CaptureBoundsExceeded(
                f"capture would exceed max_inputs ({self._max_inputs}); "
                "aborted before sending another input"
            )
        self._check_wall("input")
        step = _InputStep(button, duration, frames)
        self._reserve_frames(step.emulated_frames)
        session.press(button, duration=duration)
        # A press schedules the release; the advance is what must remain inside
        # the deadline, so re-check after a potentially slow ``press`` call.
        self._check_wall("input advance")
        session.step(frames)
        self.inputs += 1
        self.history.append(step)
        self._check_wall("input")


def _withdraw_all(exc: BaseException, *publications: _OwnedPublication | None) -> None:
    """Undo every owned publication, attaching cleanup failures to ``exc``.

    The primary error always stays primary: a cleanup that cannot complete is
    reported as a note instead of replacing the failure a caller must act on.
    """
    for publication in publications:
        if publication is None:
            continue
        error = publication.withdraw()
        if error is not None:
            _note(exc, error)


@dataclass(frozen=True)
class _OwnedPublication:
    """A directory entry this capture published, identified by its inode.

    The identity is retained from the *staging* entry immediately before the
    publish, so it outlives the removal of that entry and is never re-derived
    from a pathname a competing writer may have replaced in the meantime.

    ``displaced`` holds the bytes of an entry this publication deliberately
    replaced, so a rollback can put the earlier state back rather than destroy
    it.
    """

    path: Path
    inode: int
    device: int
    displaced: bytes | None = None

    def is_owned(self) -> bool:
        """Whether ``path`` still names exactly the entry this capture published.

        ``lstat`` is deliberate: a competing writer's symlink pointing at this
        capture's own inode is a *different* directory entry owned by that
        writer, so following the link would delete somebody else's file.
        """
        try:
            entry = os.lstat(self.path)
        except OSError:
            return False
        return (entry.st_ino, entry.st_dev) == (self.inode, self.device)

    def withdraw(self) -> str | None:
        """Undo this publication; return a description of any cleanup failure.

        A publication that is no longer ours is left exactly as it is, so a
        competing writer's replacement always survives this capture's rollback.
        """
        if not self.is_owned():
            return None
        if self.displaced is None:
            try:
                os.unlink(self.path)
            except OSError as exc:
                return f"owned output {self.path} could not be removed: {exc!r}"
            return None
        try:
            _write_bytes_atomically(self.path, self.displaced)
        except OSError as exc:
            with contextlib.suppress(OSError):
                if self.is_owned():
                    os.unlink(self.path)
            return f"replaced entry {self.path} could not be restored: {exc!r}"
        return None


def _entry_identity(path: Path) -> tuple[int, int]:
    """Return the ``(inode, device)`` of a directory entry, without following it."""
    entry = os.lstat(path)
    return entry.st_ino, entry.st_dev


def _write_bytes_atomically(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` through an fsynced private sibling."""
    staged = path.with_name(f".{path.name}.partial-{os.getpid()}-{time.monotonic_ns()}")
    try:
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged)


def _remove_staged(path: Path) -> str | None:
    """Remove a staging entry; return a description of any failure."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"staged file {path} could not be removed: {exc!r}"
    return None


def write_report(path: str | Path, payload: Any) -> _OwnedPublication:
    """Write a private capture/replay report atomically.

    The report is staged in a private sibling and moved into place, so a failed
    or partial write never replaces an existing report and never leaves a
    partial file behind.  The returned publication carries the inode this call
    installed, so a caller can withdraw exactly its own report and never a
    competing writer's replacement.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.partial-{os.getpid()}-{time.monotonic_ns()}")
    displaced: bytes | None = None
    publication: _OwnedPublication | None = None
    try:
        if target.exists():
            # A report target is deliberately replaceable; remembering the entry
            # it replaced lets a later rollback restore it instead of losing it.
            with contextlib.suppress(OSError):
                displaced = target.read_bytes()
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Retain the identity before the rename: an interrupt delivered inside
        # ``os.replace`` (after the entry exists, before it returns) must still
        # be recognisable as this capture's own publication.
        publication = _OwnedPublication(target, *_entry_identity(staged), displaced)
        os.replace(staged, target)
        return publication
    except BaseException as exc:
        publication_error = publication.withdraw() if publication is not None else None
        staging_error = _remove_staged(staged)
        if publication_error is not None:
            _note(exc, publication_error)
        if staging_error is not None:
            _note(exc, staging_error)
        raise


def refuse_report_alias(report: str | Path, *protected: str | Path | None) -> None:
    """Refuse a report path that resolves to an input or to the output fixture."""
    report_path = Path(report)
    report_resolved = report_path.resolve(strict=False)
    for other in protected:
        if other is None:
            continue
        other_path = Path(other)
        alias = report_resolved == other_path.resolve(strict=False)
        if not alias and report_path.exists() and other_path.exists():
            try:
                alias = os.path.samefile(report_path, other_path)
            except OSError:  # pragma: no cover - a racing removal is not an alias
                alias = False
        if alias:
            raise ScenarioRefusal(
                f"refusing to write the report to {report_path}, which is the same file as "
                f"{other_path}; the report must not overwrite an input or the output fixture"
            )


def _symbol_byte(session: Any, name: str) -> int:
    """Read one symbol-backed byte through the session's symbol table.

    ``Session`` exposes ``symbols`` publicly but keeps the emulator memory
    private; this reads the same ``symbols``/``memory`` pair the harness's own
    observers use.  An absent symbol is a refusal, never a guessed zero.
    """
    symbols = getattr(session, "symbols", None)
    memory = getattr(getattr(session, "_pyboy", None), "memory", None)
    if symbols is None or memory is None:
        raise CapturePreconditionFailed(
            f"cannot read {name}: the capture session exposes no symbol table/memory"
        )
    if name not in symbols:
        raise CapturePreconditionFailed(
            f"cannot assert the declared boundary: {name} is absent from the loaded .sym table"
        )
    return symbols.read_u8(memory, name)


def _observe_boundary(session: Any) -> dict[str, Any]:
    """Return the observed pre-write boundary used for the precondition check."""
    state = session.read_game_state()
    return {
        "map_id": state.overworld.map_id,
        "x": state.overworld.x,
        "y": state.overworld.y,
        "link_state_raw": _symbol_byte(session, "wLinkState"),
        "party_count": state.party.count,
        "active_slot": state.party.active_slot,
    }


def _assert_boundary(scenario: dict[str, Any], observed: dict[str, Any], scenario_id: str) -> None:
    """Refuse unless the observed state matches the declared capture boundary."""
    boundary = scenario["capture_boundary"]
    if observed["map_id"] != boundary["map_id"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares map_id {boundary['map_id']}, "
            f"observed {observed['map_id']}"
        )
    if (boundary["stage"], boundary["when"]) == _SUPPORTED_BOUNDARY and (
        observed["x"],
        observed["y"],
    ) != _LINK_RECEPTION_TILE:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares the link-reception pre-command boundary, but the "
            f"observed position ({observed['x']}, {observed['y']}) is not the link receptionist "
            f"tile {_LINK_RECEPTION_TILE}"
        )
    label = boundary["link_state"]
    if label not in _LINK_STATE_CODES:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares link_state {label!r}, which has no "
            "verified wLinkState encoding; refusing to assert an unverified value"
        )
    expected_link = _LINK_STATE_CODES[label]
    if observed["link_state_raw"] != expected_link:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares link_state {label!r} "
            f"(wLinkState={expected_link}), observed wLinkState="
            f"{observed['link_state_raw']}"
        )
    party = scenario.get("party") or {}
    if party.get("count") is not None and observed["party_count"] != party["count"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares party.count {party['count']}, "
            f"observed {observed['party_count']}"
        )
    if party.get("active_slot") is not None and observed["active_slot"] != party["active_slot"]:
        raise CapturePreconditionFailed(
            f"scenario {scenario_id!r} declares party.active_slot {party['active_slot']}, "
            f"observed {observed['active_slot']}"
        )


def _assert_supported_conditions(scenario: dict[str, Any], scenario_id: str) -> None:
    """Refuse declared preconditions this bounded drive cannot observe.

    Silently driving past a declared inventory, opponent, prior action, or
    per-mon party condition would publish a fixture whose declared
    preconditions were never checked.  Only declarations that assert nothing are
    accepted: an *absent* field (or an explicit ``null``) for the inventory and
    opponent, an empty ``required_prior_actions`` list, and an empty per-mon
    ``party.mons`` list.  A declared inventory or opponent is refused even when
    it is empty, because the field's presence is itself the assertion that this
    drive cannot check.
    """
    if scenario.get("inventory") is not None:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} Scenario {scenario_id!r} declares an inventory precondition "
            "that this bounded drive does not observe; capture it with a producer that asserts "
            "the declared bag, or declare the inventory as null."
        )
    if scenario.get("opponent") is not None:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} Scenario {scenario_id!r} declares an opponent precondition "
            "that this bounded drive does not observe; capture it with a producer that asserts "
            "the declared opponent, or declare the opponent as null."
        )
    prior_actions = scenario.get("required_prior_actions")
    if not isinstance(prior_actions, list):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare required_prior_actions as a list"
        )
    if prior_actions:
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} Scenario {scenario_id!r} declares required prior actions that "
            "this bounded drive does not replay; capture it with a producer that performs and "
            "records them, or declare an empty list."
        )
    party = scenario.get("party") or {}
    if party.get("mons"):
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} Scenario {scenario_id!r} declares per-mon party contents, "
            "which this bounded drive does not assert; refusing to record an unchecked capture."
        )


def _drive_to_link_reception(session: Any, budget: _CaptureBudget) -> None:
    """Drive the supplied source state to the Cable Club link receptionist.

    The input sequence is the documented, replayable route from a Cerulean
    Pokecenter source state: settle, walk up to the counter row, sidestep the
    nurse, cross to the receptionist's adjacent tile, and face it.  No runtime
    RAM, party, PP, RNG, or serial state is mutated.
    """
    budget.step(session, 10)  # let the overworld settle after loading
    for _ in range(4):
        budget.press(session, "up", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES)
    for _ in range(2):
        budget.press(session, "left", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES)
    budget.press(session, "down", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES)
    while session.read_game_state().overworld.x < _LINK_RECEPTION_TILE[0]:
        budget.press(
            session, "right", duration=_DEFAULT_PRESS_DURATION, frames=_DEFAULT_STEP_FRAMES
        )
    budget.press(session, "up", duration=_DEFAULT_PRESS_DURATION, frames=30)
    position = session.read_game_state().overworld
    if (position.x, position.y) != _LINK_RECEPTION_TILE:
        raise CapturePreconditionFailed(
            f"the bounded drive ended at ({position.x}, {position.y}), not the link "
            f"receptionist tile {_LINK_RECEPTION_TILE}"
        )


def _write_fixture_exclusive(
    output: Path, payload: bytes, *, guard: Callable[[str], None] | None = None
) -> _OwnedPublication:
    """Publish fixture bytes without ever overwriting, or leaving a partial file.

    The payload is staged in a private sibling and published with ``os.link``,
    which fails if the destination already exists.  Every step — including the
    removal of the staging entry that finalizes the operation — runs inside the
    rollback-protected region, and the published inode identity is retained from
    the staging entry *before* the link.  A failure after the directory entry
    exists (an interrupt inside ``os.link``, or one delivered once the staging
    entry has already gone) therefore still removes this capture's own
    publication, while a pre-existing or competing writer's entry is never
    touched: the check is ``lstat`` on the destination, so a competing symlink
    pointing at this capture's inode is not mistaken for the publication itself.

    ``guard`` is called immediately before the link and again once the entry
    exists, so a caller's deadline is re-checked with the publication in place
    and a late expiry rolls it back instead of admitting it.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{output.name}.partial-{os.getpid()}-{time.monotonic_ns()}")
    publication: _OwnedPublication | None = None
    try:
        descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if guard is not None:
            guard("fixture staging")
        # Retained before the link: once the entry exists the staging path may
        # already be gone, so ownership must not be re-derived from that entry.
        publication = _OwnedPublication(output, *_entry_identity(staged))
        try:
            os.link(staged, output)
        except FileExistsError as exc:
            raise ScenarioRefusal(f"refusing to overwrite existing output: {output}") from exc
        if guard is not None:
            guard("fixture publication")
        # Finalization: with the staging entry gone the operation has committed.
        os.unlink(staged)
        return publication
    except BaseException as exc:
        publication_error = publication.withdraw() if publication is not None else None
        staging_error = _remove_staged(staged)
        if publication_error is not None:
            _note(exc, publication_error)
        if staging_error is not None:
            _note(exc, staging_error)
        raise


def capture_battle_scenario(
    *,
    scenario: dict[str, Any],
    rom: str | Path,
    sym: str | Path,
    input_fixture: str | Path | None,
    output: str | Path,
    role: str,
    bounds: dict[str, Any],
    report: str | Path | None,
    repo_root: str | Path,
    runtime: str,
    python: str | Path | None,
    plan: dict[str, Any],
    session_factory: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
    ownership: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive a pinned ROM to the declared boundary and publish the fixture.

    Returns the private provenance record for the capture.  ``run`` merges it
    into the report it writes, so a caller never has to re-derive the input
    history, the runtime identity, or the output hashes.
    """
    del report
    scenario_id = scenario["scenario_id"]
    validate_scenario_metadata(scenario, scenario_id)
    boundary = scenario["capture_boundary"]
    if (boundary["stage"], boundary["when"]) not in (_SUPPORTED_BOUNDARY,):
        producer = scenario.get("provenance", {}).get("producer", "the documented producer")
        raise CaptureNotAvailable(
            f"{_CAPTURE_MESSAGE} The {boundary['stage']!r}/{boundary['when']!r} boundary "
            f"requires the controlled linked run produced by {producer}; a single-console "
            "bounded drive cannot establish it."
        )
    _assert_supported_conditions(scenario, scenario_id)
    if input_fixture is None:
        raise ScenarioRefusal(
            "capture requires a source state; pass --input-fixture (the documented "
            "producer takes --source) so the drive starts from a known position"
        )

    factory = session_factory or _entry._default_session_factory
    # An injected session is diagnostic: it never inspects the capturing process,
    # so the record must say its runtime was not measured instead of adopting the
    # caller's request as an observation.
    measured = session_factory is None
    if not measured and python is not None and not Path(python).is_file():
        # A diagnostic session is still handed a named interpreter to run; a
        # request that cannot be resolved at all is refused rather than recorded
        # as if it had been used.
        raise ScenarioRefusal(
            f"requested interpreter {python} does not exist; refusing to record an "
            "interpreter this process cannot resolve"
        )
    if measured and isinstance(plan, dict):
        # Measure and enforce the executing identity *before* the emulator opens,
        # so a requested runtime or interpreter is never recorded as observed.
        plan["runtime_identity"] = _entry.measure_runtime_identity(
            runtime=runtime, python=python, repo_root=repo_root
        )
    output_path = Path(output)
    started = clock()
    budget = _CaptureBudget(bounds, clock=clock, deadline=deadline)
    session: Any | None = None
    failure: BaseException | None = None
    try:
        try:
            session = factory(
                rom=rom,
                sym=sym,
                repo_root=repo_root,
                runtime=runtime,
                python=python,
                plan=plan,
            )
        except CaptureNotAvailable:
            raise
        except (ImportError, RuntimeError, OSError) as exc:
            raise CaptureNotAvailable(
                f"{_CAPTURE_MESSAGE} The pinned runtime could not be opened: {exc}"
            ) from exc
        session.load_state(Path(input_fixture).read_bytes())
        _drive_to_link_reception(session, budget)
        observed = _observe_boundary(session)
        budget.check_wall("boundary observation")
        _assert_boundary(scenario, observed, scenario_id)
        budget.check_wall("state save")
        payload = session.save_state()
        budget.check_wall("state save")
    except (CaptureNotAvailable, ScenarioRefusal) as exc:
        failure = exc
        raise
    except Exception as exc:  # fail closed: never publish bytes on an error
        refusal = ScenarioRefusal(
            f"bounded capture aborted before writing ({type(exc).__name__}): {exc}"
        )
        failure = refusal
        raise refusal from exc
    except BaseException as exc:  # an interrupt still owns a session to close
        failure = exc
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as close_exc:
                if failure is None:
                    # An otherwise successful capture whose teardown failed has
                    # not established bounded cleanup, so it must not publish.
                    raise ScenarioRefusal(
                        f"capture teardown failed for {scenario_id!r} and the owned session was "
                        f"not closed; no bytes were written: {close_exc}"
                    ) from close_exc
                _note(failure, f"session.close() also failed: {close_exc!r}")

    effective_bounds = budget.bounds
    runtime_identity = plan.get("runtime_identity") if isinstance(plan, dict) else None
    declared = scenario.get("fixture") or {}
    # Every slow step below is bracketed by a deadline re-check, so a capture that
    # drifts past the declared bound while hashing, looking up the producer
    # revision, or staging the bytes refuses instead of publishing anyway.
    budget.check_wall("fixture digest")
    digest_sha1 = hashlib.sha1(payload).hexdigest()
    digest_sha256 = hashlib.sha256(payload).hexdigest()
    budget.check_wall("fixture digest")
    matches = (
        declared.get("sha1") == digest_sha1
        and declared.get("sha256") == digest_sha256
        and declared.get("size_bytes") == len(payload)
    )
    verified = scenario.get("provenance", {}).get("status") == "verified"
    if verified and not matches:
        raise ScenarioRefusal(
            f"bounded capture did not reproduce the declared fixture for {scenario_id!r}: "
            f"declared sha1 {declared['sha1']} ({declared.get('size_bytes')} bytes), "
            f"produced sha1 {digest_sha1} ({len(payload)} bytes); no bytes were written"
        )

    recorded_runtime = (
        runtime_identity["mode"] if measured and runtime_identity else _UNMEASURED_RUNTIME
    )
    record = {
        "producer": _CAPTURE_PRODUCER,
        "producer_sha1": sha1_file(Path(_entry.__file__)),
        "producer_revision": _entry._producer_revision(repo_root),
        "runtime": recorded_runtime,
        "runtime_measurement": "measured" if measured else "not_measured",
        "runtime_identity": runtime_identity,
        "runtime_request": {
            "runtime": runtime,
            "python": str(python) if python is not None else None,
        },
        "python": str(python) if measured and python is not None else None,
        "role": role,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "wall_seconds": round(clock() - started, 6),
        "declared_bounds": effective_bounds,
        "declared_producer": plan.get("producer_identity") if isinstance(plan, dict) else None,
        "inputs_used": budget.inputs,
        "frames_used": budget.frames,
        "input_sequence": [step.render() for step in budget.history],
        "observed_boundary": observed,
        "rom_sha1": plan.get("rom_sha1"),
        "sym_sha1": plan.get("sym_sha1"),
        "input_fixture_sha1": plan.get("input_fixture_sha1"),
        "output": {
            "path": str(output_path),
            "size_bytes": len(payload),
            "sha1": digest_sha1,
            "sha256": digest_sha256,
        },
        "reproduction": {
            "declared_fixture_sha1": declared.get("sha1"),
            "declared_fixture_sha256": declared.get("sha256"),
            "declared_size_bytes": declared.get("size_bytes"),
            "matches": matches,
        },
    }
    budget.check_wall("record preparation")
    _entry.validate_capture_record(record, scenario_id)
    publication: _OwnedPublication | None = None
    try:
        publication = _write_fixture_exclusive(output_path, payload, guard=budget.check_wall)
        budget.check_wall("fixture publication")
    except BaseException as exc:
        _withdraw_all(exc, publication)
        raise
    # The recorded duration now covers the publication too, and an operation that
    # crossed the declared bound regardless removes its own pair before refusing.
    elapsed = round(clock() - started, 6)
    if elapsed > effective_bounds["max_wall_seconds"]:
        refusal = CaptureBoundsExceeded(
            f"capture exceeded max_wall_seconds ({effective_bounds['max_wall_seconds']}) before "
            f"admitting the fixture; observed {elapsed}s"
        )
        _withdraw_all(refusal, publication)
        raise refusal
    record["wall_seconds"] = elapsed
    if ownership is not None:
        ownership["publication"] = publication
    return record
