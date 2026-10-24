"""Single-pass owner servicing, without harvesting, waiting or releasing leases."""

from __future__ import annotations

from typing import Any

from scripts import production_gate_parallel_job_retirement as retirement
from scripts.production_gate_parallel_capacity import policy_file_problem
from scripts.production_gate_parallel_ownership import (
    holder_loss,
    latch_holder_loss,
    signal_guardians,
)


class OwnerService:
    """Owner-thread-only callback for cooperative I/O checkpoints.

    ``pulse() -> None`` makes one finite pass over registered jobs. It never sleeps,
    admits, finishes or removes a job. Recursive entry raises, rather than pretending
    siblings were serviced. Exceptions propagate after stop requests are broadcast;
    callers must abort their current operation and return to the owner's cleanup.
    ``cancel_reason`` is latched for the outer scheduler to consume. No successful
    return proves cleanup, worker authority or a lease suitable for a linked probe.
    """

    def __init__(
        self,
        running: Any,
        hub: Any,
        budget: Any,
        live: Any,
        deadlines: Any,
        plan: Any,
        interrupt: Any,
        *,
        cleanup_seconds: float,
        clock: Any,
        loss_notice: Any = None,
        handoffs: Any = None,
    ) -> None:
        self.running, self.hub, self.budget = running, hub, budget
        self.live, self.deadlines, self.plan = live, deadlines, plan
        self.interrupt, self.cleanup_seconds, self.clock = (
            interrupt,
            cleanup_seconds,
            clock,
        )
        self.cancel_reason = ""
        self._active = False
        self.loss_notice = loss_notice
        self.handoffs = handoffs
        self.hub_loss_reason = ""
        self._delivering_hub_loss = False
        self._hub_notification_attempted = False

    def request_cancel(self, reason: str, *, deadlines_current: bool = False) -> None:
        """Latch the first reason and monotonically tighten every original bound."""
        first = None
        if not deadlines_current:
            try:
                self.deadlines.pulse()  # original elapsed deadlines precede marking stops
            except Exception as error:  # noqa: BLE001 - still broadcast to positive siblings
                first = error
        self.cancel_reason = self.cancel_reason or reason
        bound = min(
            [self.clock() + self.cleanup_seconds]
            + [
                i.cleanup_deadline
                for i in self.running
                if retirement.active_item(i) and i.cleanup_deadline is not None
            ]
        )
        for item in tuple(self.running):
            if not retirement.active_item(item):
                continue
            item.cleanup_deadline = (
                min(item.cleanup_deadline, bound) if item.cleanup_deadline is not None else bound
            )
            try:
                if retirement.poll_item(item) is None:
                    item.stop_requested = True
                    self.hub.expect_drop(item.job_id)
            except Exception as error:  # noqa: BLE001 - boundary has barred unknown owner
                if first is None:
                    first = error
                elif first is not error:
                    first.add_note(f"secondary cancellation observation: {type(error).__name__}: {error}")
        if first is not None:
            raise first

    def consume_hub_loss(self, *, deadlines_current: bool = False) -> None:
        """Owner delivery only; background callback sets a permanent Event bit.

        Hub admission refusal is immediate. Guardian delivery waits for this finite
        checkpoint; a blocked owner call has no invented wall-clock latency bound.
        """
        if self._delivering_hub_loss:
            return
        reason = (
            retirement.hub_reason(self.hub)
            if self.loss_notice is not None
            else self.hub.closed_reason
        )  # unmanaged compatibility fixtures
        if not reason:
            return
        self._delivering_hub_loss = True
        try:
            self.hub_loss_reason = self.hub_loss_reason or reason
            self.budget.mark_lost(self.hub_loss_reason)
            self.request_cancel(
                f"live arbiter closed admission: {self.hub_loss_reason}",
                deadlines_current=deadlines_current,
            )
            if not self._hub_notification_attempted:
                self._hub_notification_attempted = True
                signal_guardians(
                    self.running, self.deadlines.problems, notified=self.deadlines.notified
                )
        finally:
            self._delivering_hub_loss = False

    def preflight(self) -> None:
        """Original pre-harvest lease/policy checks; outer poll retains precedence."""
        first = None
        try:
            self.hub.check_leases()
            for item in tuple(self.running):
                if not retirement.active_item(item):
                    continue
                pin = self.plan.job(item.job_id).get("policy_sha256")
                if pin and policy_file_problem(item.tree["root"] / "capacity-policy.json", pin):
                    self.hub.lose(
                        f"{item.job_id}: the child capacity policy changed during the run"
                    )
        except Exception as error:  # noqa: BLE001 - preserve original preflight failure
            first = error
        try:
            if self.handoffs is not None:
                self.handoffs.service()
        except Exception as error:  # noqa: BLE001 - first failure wins; pulse still services siblings
            first = first or error
        if first is not None:
            raise first
        self.consume_hub_loss()

    def pulse(self, *, preflight: bool = True, skip: Any = None) -> None:
        """Service siblings once; ``skip`` is only the already-retiring finish owner.

        Future broker I/O must receive the zero-argument bound ``pulse`` callback,
        never ``poll``. The private poll split skips duplicate preflight only after
        it already ran. These keywords do not create a broker route or new budget.
        """
        if self._active:
            raise RuntimeError("recursive owner service")
        self._active = True
        first_error = None

        def attempt(operation: Any, *args: Any) -> None:
            nonlocal first_error
            try:
                operation(*args)
            except Exception as error:  # noqa: BLE001 - finite pass preserves first identity
                if first_error is None:
                    first_error = error
                elif first_error is not error:
                    first_error.add_note(f"secondary owner service: {type(error).__name__}: {error}")

        try:
            try:
                if preflight:
                    self.preflight()
                else:
                    if self.handoffs is not None:
                        self.handoffs.service()
                    self.consume_hub_loss()
                if self.interrupt is not None and self.interrupt.is_set():
                    self.request_cancel("external interrupt")
                if not self.cancel_reason:
                    reason = self.live.check()
                    if reason:
                        self.budget.mark_lost(reason)
                        self.hub.lose(f"capacity loss: {reason}")
                        self.request_cancel(f"capacity loss: {reason}")
            except Exception as exc:  # noqa: BLE001 - still service sibling stops below
                first_error = exc
                attempt(self.request_cancel, f"owner service failed: {type(exc).__name__}")
            for item in tuple(self.running):
                if not retirement.active_item(item) or item is skip:
                    continue
                try:
                    item.owned.observe()
                    loss = None
                    if retirement.poll_item(item) is not None:
                        loss = holder_loss(
                            item.process.guardian_status,
                            {"pid": item.process.pid, "start": item.owned.root_start},
                        )
                    if not item.owned.contained:
                        loss = item.owned.reason or "owned containment lost"
                    if loss:
                        latch_holder_loss(self.budget, self.hub, item.job_id, loss)
                        self.request_cancel(f"capacity loss reported by {item.job_id}: {loss}")
                except Exception as exc:  # noqa: BLE001 - one observation cannot skip siblings
                    first_error = first_error or exc
                    attempt(self.request_cancel, f"owner observation failed: {type(exc).__name__}")
                attempt(self.deadlines.pulse)
            if self.deadlines.fail_fast_reason:
                attempt(self.request_cancel, self.deadlines.fail_fast_reason)
            attempt(self.deadlines.pulse)
            attempt(self.consume_hub_loss)
            if first_error is not None:
                raise first_error
        finally:
            self._active = False
