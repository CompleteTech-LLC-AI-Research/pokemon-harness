"""Process-local receive error custody, not recipient ancestry or physical retirement."""

from __future__ import annotations

import array
import os
import socket
import threading
from dataclasses import dataclass
from typing import Any

INT_SIZE = array.array("i").itemsize


@dataclass
class Right:
    fd: int
    state: str = "raw"
    wrapper: Any = None


class ReceiveBook:
    """Unknown records permanently consume their original reservation; no repair API."""

    def __init__(self, transactions: int = 32, rights: int = 256) -> None:
        self.lock = threading.Lock()
        self.transaction_limit, self.right_limit = transactions, rights
        self.records: dict[int, ReceiveRecord] = {}
        self.reserved = self.ordinal = 0

    def reserve(self, worst: int, deadline: float, clock: Any) -> ReceiveRecord:
        with self.lock:
            if (
                worst < 1
                or len(self.records) >= self.transaction_limit
                or self.reserved + worst > self.right_limit
            ):
                raise RuntimeError("listener receive custody capacity is exhausted")
            self.ordinal += 1
            record = ReceiveRecord(self, self.ordinal, worst, deadline, clock)
            self.reserved += worst
            self.records[record.number] = record
        return record

    def finish(self, record: ReceiveRecord) -> bool:
        with self.lock:
            settled = (
                not record.unknown
                and record.control_state in ("none", "closed", "return_issued")
                and all(r.state in ("closed", "return_issued") for r in record.rights)
            )
            if settled and self.records.get(record.number) is record:
                del self.records[record.number]
                self.reserved -= record.worst
            return settled


class ReceiveRecord:
    def __init__(self, book: ReceiveBook, number: int, worst: int, deadline: float, clock: Any):
        self.book, self.number, self.worst = book, number, worst
        self.deadline, self.clock = deadline, clock
        self.control: Any = None
        self.control_state = "none"
        self.raw_tuple: Any = None
        self.rights: list[Right] = []
        self.unknown = ""
        self.phase = "reserved"
        self.first_error: BaseException | None = None
        self.ancillary_index = self.payload_offset = 0

    def remaining(self) -> float:
        now = self.clock()  # never under the bookkeeping lock
        if now >= self.deadline:
            raise TimeoutError("original listener handoff local bound expired")
        return self.deadline - now

    def uncertain(self, reason: str) -> None:
        with self.book.lock:
            self.unknown = self.unknown or reason

    def effect(self, method: str, *args: Any) -> Any:
        with self.book.lock:
            usable = (
                self.control_state == "open"
                and not self.unknown
                and self.book.records.get(self.number) is self
            )
        if not usable:
            raise RuntimeError("listener control is not usable")
        self.control.settimeout(self.remaining())
        self.remaining()
        return getattr(self.control, method)(*args)

    def send(self, data: bytes) -> None:
        sent = self.effect("send", data)
        if sent != len(data):
            raise OSError("short listener handoff frame; no continuation allowed")

    def classify(self) -> bytes:
        with self.book.lock:
            usable = (
                self.book.records.get(self.number) is self
                and self.phase == "raw_returned"
                and not self.unknown
            )
            if usable:
                self.phase = "classifying"
        if not usable:
            raise RuntimeError("listener ancillary cannot be reparsed")
        try:
            data, ancillary, flags, _address = self.raw_tuple
            unexpected = False
            for index, (level, kind, payload) in enumerate(ancillary):
                self.ancillary_index, self.payload_offset = index, 0
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    unexpected = True
                    self.uncertain("unsupported listener ancillary ownership")
                    continue
                # Parse one native integer at a time; the whole original tuple remains held.
                for offset in range(0, len(payload) - len(payload) % INT_SIZE, INT_SIZE):
                    self.payload_offset = offset
                    value = array.array("i")
                    value.frombytes(payload[offset : offset + INT_SIZE])
                    fd = value[0]
                    if fd < 0:
                        raise ValueError("negative received descriptor")
                    duplicate = next((r for r in self.rights if r.fd == fd), None)
                    if duplicate is not None:
                        duplicate.state = "unknown"
                        raise ValueError("duplicate received descriptor ownership")
                    if len(self.rights) >= self.worst:
                        raise ValueError("returned ancillary exceeds reserved capacity")
                    self.rights.append(Right(fd))  # nonnegative 0/1/2 are valid known rights
                if len(payload) % INT_SIZE:
                    raise ValueError("unaligned received rights payload")
            self.phase = "rights_classified"
            if flags & socket.MSG_CTRUNC:
                self.uncertain("truncated ancillary inventory")
            if unexpected or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                raise ValueError("unexpected or truncated listener ancillary")
            if len(self.rights) != 1:
                raise ValueError("listener reply requires exactly one descriptor")
            return data
        except BaseException:
            if self.phase != "rights_classified":
                self.uncertain("listener ancillary classification unproven")
            raise

    def wrap(self, constructor: Any) -> Any:
        with self.book.lock:
            usable = (
                self.book.records.get(self.number) is self
                and not self.unknown
                and len(self.rights) == 1
                and self.rights[0].state == "raw"
                and self.phase == "rights_classified"
            )
        if not usable:
            raise RuntimeError("listener wrapper transfer cannot be repeated")
        self.remaining()
        right = self.rights[0]
        right.state, self.phase = "transfer_attempted", "wrap_attempted"
        try:
            right.wrapper = constructor(fileno=right.fd)
            right.state, self.phase = "wrapped", "wrapper_returned"
        except BaseException:
            right.state = "unknown"
            self.uncertain("listener wrapper transfer unproven")
            raise
        return right.wrapper

    def close_right(self, right: Right) -> None:
        with self.book.lock:
            state = right.state
            if state not in ("raw", "wrapped"):
                return
            right.state = "closing"
        try:
            self.remaining()
            if state == "raw":
                os.close(right.fd)
            else:
                right.wrapper.close()
        except BaseException:
            right.state = "unknown"
            self.uncertain("received listener close unproven")
            raise
        right.state = "closed"

    def close_control(self) -> None:
        with self.book.lock:
            if self.control_state != "open":
                return
            self.control_state = "closing"
        try:
            self.remaining()
            self.control.close()
        except BaseException:
            self.control_state = "unknown"
            self.uncertain("listener control close unproven")
            raise
        self.control_state = "closed"

    def fail(self, primary: BaseException, *, nack: bool = False) -> None:
        self.first_error = self.first_error or primary
        if self.raw_tuple is not None and self.phase in ("recv_attempted", "raw_returned"):
            self.uncertain("listener returned ancillary not yet classified")
        if self.control_state == "constructing":
            self.uncertain("listener control constructor unproven")
        for right in self.rights:
            try:
                self.close_right(right)
            except BaseException as secondary:  # noqa: BLE001 - preserve active primary identity
                primary.add_note("listener disposition also escaped: " + type(secondary).__name__)
        if nack and not self.unknown and all(r.state == "closed" for r in self.rights):
            try:
                self.send(b'{"op":"nack"}')
            except BaseException as secondary:  # noqa: BLE001 - preserve active primary identity
                primary.add_note("listener nack also escaped: " + type(secondary).__name__)
        try:
            self.close_control()
        except BaseException as secondary:  # noqa: BLE001 - preserve active primary identity
            primary.add_note("control disposition also escaped: " + type(secondary).__name__)
        self.book.finish(self)

    def transfer_control(self) -> Any:
        # Ordinary raw API return, not checked caller publication or physical retirement.
        if self.control_state != "open" or self.unknown:
            raise RuntimeError("listener control cannot be transferred")
        self.control_state = "return_issued"
        self.book.finish(self)
        return self.control

    def transfer_listener(self) -> Any:
        if self.control_state != "closed" or self.unknown or self.phase != "acknowledged":
            raise RuntimeError("internal control custody remains unresolved")
        right = self.rights[0]
        if right.state != "wrapped":
            raise RuntimeError("listener wrapper is not transferable")
        right.state, self.phase = "return_issued", "return_issued"
        self.book.finish(self)
        return right.wrapper
