"""One retained adoption owner; child membership is not a linked resource lease."""

from __future__ import annotations

from typing import Any

from scripts.production_gate_parallel_root_wait_partition import RootCustodyError, WaitPartition

# Retention predicates refer to the existing session/run roots. Production removes
# those roots only after checked disposition; a predicate supplies no authority.
_ROOTS: list[RootContext] = []


class RootContext:
    """Owner-thread-only; background notification never enters this context."""

    def __init__(self) -> None:
        self.adoption: Any = None
        self.retained: Any = None
        self.standalone = False
        self.claimed = False
        self.closed = False
        self.disposing = False
        self.failure = ""
        self.runs: list[dict[str, Any]] = []
        self.scopes: list[Any] = []
        self.scope_proofs: list[tuple[Any, ...]] = []
        self.partition = WaitPartition(self)
        self._servicing = False
        self._finish_owner: Any = None
        self.probe_service: Any = None

    def refuse(self, reason: str) -> None:
        self.failure = self.failure or reason

    def claim(self, adoption: Any, retained: Any, *, standalone: bool = False) -> None:
        if self.claimed or self.closed:
            raise RootCustodyError("root adoption context cannot be re-entered")
        if any(not old.closed and old.retained() for old in _ROOTS):
            raise RootCustodyError("prior root adoption context remains unresolved")
        _ROOTS.clear()  # only discharged existing retention roots can reach here
        self.adoption, self.retained, self.standalone = adoption, retained, standalone
        _ROOTS.append(self)  # BEFORE adoption mutation, including no-return enable
        self.claimed = True

    def require_admission(self) -> None:
        unresolved = any(
            record["ledger"].failure or record["ledger"].errors
            or any((slot.failure or slot.errors)
                   and not (slot.no_spawn_eligible and slot.local_disposed)
                   for slot in record["ledger"].slots.values())
            or (record["service"] is not None and record["service"].cancel_reason)
            for record in self.runs
        ) or any(scope.failures or (scope.owner is not None and scope.owner.custody.failures)
                 for scope in self.scopes)
        if not self.claimed or self.closed or self.disposing or self.failure or unresolved:
            raise RootCustodyError("root admission withheld by retained custody")

    def attach_run(self, ledger: Any, job_ids: Any) -> None:
        self.require_admission()
        if self.runs or ledger.root_context is not None:
            raise RootCustodyError("one original run per root context")
        keys = tuple(job_ids)
        if not keys or len(set(keys)) != len(keys):
            raise RootCustodyError("root run requires fixed distinct job keys")
        self.runs.append({
            "ledger": ledger, "keys": keys, "service": None,
            "settled": False, "proof": None,
        })
        ledger.root_context = self
        ledger.adoption, ledger.owns_adoption = self.adoption, False

    def run_record(self, ledger: Any) -> dict[str, Any]:
        return next(record for record in self.runs if record["ledger"] is ledger)

    def prepare_job(self, ledger: Any, slot: Any) -> None:
        self.require_admission()
        record = self.run_record(ledger)
        if (record["settled"] or record["service"] is None
                or slot.job_id not in record["keys"]):
            raise RootCustodyError("job is outside the original active run")
        slot.root_context = self
        self.partition.prepare(slot, "job")

    def attach_service(self, ledger: Any, service: Any) -> None:
        self.require_admission()
        record = self.run_record(ledger)
        if record["service"] is not None or record["settled"]:
            raise RootCustodyError("original owner service cannot be replaced")
        record["service"] = service  # exact OwnerService, before the first spawn

    def prepare_scope(self, scope: Any) -> None:
        self.require_admission()
        if len(self.scopes) >= 4 or len(self.scope_proofs) >= 4096:
            raise RootCustodyError("root scope membership quota reached")
        if any(old is scope for old in self.scopes):
            raise RootCustodyError("scope cannot be registered twice")
        self.scopes.append(scope)
        scope.root_context = self
        self.partition.prepare(scope, "probe")

    def settle_run(self, ledger: Any, proof: tuple[Any, ...]) -> None:
        record = self.run_record(ledger)
        if record["settled"]:
            if record["proof"] != proof:
                self.refuse("original run terminal proof changed")
                raise RootCustodyError(self.failure)
            return
        if self.partition.owner_unknown(ledger.slots.values()):
            raise RootCustodyError("run wait custody remains unproven")
        record["proof"] = proof  # immutable local facts BEFORE caller removes ledger
        record["settled"] = True

    def settle_scope(self, scope: Any, proof: tuple[Any, ...]) -> None:
        if not any(old is scope for old in self.scopes):
            raise RootCustodyError("scope membership is not original")
        if self.partition.owner_unknown((scope,)):
            raise RootCustodyError("scope wait custody remains unproven")
        self.scope_proofs.append(proof)  # BEFORE broker removes the original scope
        self.scopes.remove(scope)
        self.partition.settled(scope)

    def children_quiescent(self) -> bool:
        return not self.failure and not self.scopes and all(r["settled"] for r in self.runs)

    def during_finish(self, item: Any, operation: Any) -> Any:
        if self.closed or self.disposing or self._finish_owner is not None:
            raise RootCustodyError("recursive or disposed finish owner")
        self._finish_owner = item
        try:
            return operation()
        finally:
            self._finish_owner = None

    def service(self, *, skip: Any = None) -> None:
        # Composed measurement callbacks can enter here from broker/OwnerService.
        # The outer finite pass alone services siblings; nested passes do no work.
        if self.closed or self.disposing:
            raise RootCustodyError("root active service is barred during disposition")
        if self._servicing:
            return
        if self._finish_owner is not None:
            if skip is not None and skip is not self._finish_owner:
                raise RootCustodyError("checkpoint names a different retiring owner")
            skip = self._finish_owner
        self._servicing = True
        first = None
        try:
            for record in tuple(self.runs):
                if record["settled"]:
                    continue
                owner = record["service"]
                if owner is None:
                    continue  # retained setup still blocks close; never fake delivery
                try:
                    owner.pulse(skip=skip)
                except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - rethrow after siblings
                    if first is None:
                        first = error
                    elif first is not error:
                        first.add_note(f"secondary root service failure: {type(error).__name__}")
            if self.probe_service is not None:
                try:
                    self.probe_service()
                except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - rethrow original
                    if first is None:
                        first = error
                    elif first is not error:
                        first.add_note(f"secondary root service failure: {type(error).__name__}")
            if first is not None:
                raise first
        finally:
            self._servicing = False

    def close_adoption(self, original: BaseException | None = None) -> None:
        if self.closed:
            return
        if not self.children_quiescent():
            raise RootCustodyError("root children remain unresolved")
        self.disposing = True
        self.adoption.__exit__(
            type(original) if original else None, original,
            original.__traceback__ if original else None,
        )
        if self.adoption.restoration_pending:
            return  # only this same owner may retry checked restoration
        self.closed = True
        if self in _ROOTS:
            _ROOTS.remove(self)
