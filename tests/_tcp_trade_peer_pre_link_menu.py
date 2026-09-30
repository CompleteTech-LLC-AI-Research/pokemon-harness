"""Pre-LinkMenu observation schema for the TCP trade peer.

Extracted from ``tests/_tcp_trade_peer.py`` (#127): the bounded-history constant
table plus the :class:`_PreLinkMenuConfig` / :class:`_PreLinkMenuHistory` pair
that owns the observation buffer and its cleanup accounting.  The pinned profile
table and :func:`_resolve_pre_link_menu` stay on the facade so a
``monkeypatch.setattr`` on that table still reaches the resolver's read site.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from threading import get_ident
from typing import NamedTuple

_PRE_LINK_MENU_SCHEMA_VERSION = 1
_PRE_LINK_MENU_HISTORY_LIMIT = 32
_PRE_LINK_MENU_ERROR_LIMIT = 8
_PRE_LINK_MENU_FIELDS = (
    ("hSerialConnectionStatus", 1),
    ("hSerialSendData", 1),
    ("hSerialReceiveData", 1),
    ("hSerialReceivedNewData", 1),
    ("wSerialExchangeNybbleSendData", 1),
    ("wSerialExchangeNybbleTempReceiveData", 1),
    ("wSerialExchangeNybbleReceiveData", 1),
    ("wSerialSyncAndExchangeNybbleReceiveData", 1),
    ("wUnknownSerialCounter", 2),
    ("wUnknownSerialCounter2", 2),
    ("wLinkTimeoutCounter", 1),
)
_PRE_LINK_MENU_REPORT_FIELDS = (
    "schema_version",
    "enabled",
    "available",
    "reason",
    "role",
    "version",
    "pins",
    "observed_pins",
    "sites",
    "limit",
    "total",
    "counts",
    "first",
    "recent",
    "recent_dropped",
    "recent_truncated",
    "hooks",
    "cleanup_pending",
    "error_limit",
    "error_count",
    "errors",
    "errors_dropped",
    "errors_truncated",
)


class _PreLinkMenuConfig(NamedTuple):
    enabled: bool
    role: str
    version: str
    pins: tuple[tuple[str, str], ...]
    observed_pins: tuple[tuple[str, str], ...]
    sites: tuple[tuple[str, int, int], ...]
    limit: int


class _PreLinkMenuHistory:
    """Owner-thread observations with bounded retention and owned-hook cleanup."""

    def __init__(
        self,
        *,
        enabled,
        role,
        version,
        pins=(),
        observed_pins=(),
        sites=(),
        limit=_PRE_LINK_MENU_HISTORY_LIMIT,
    ):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a bool")
        if type(limit) is not int or limit != _PRE_LINK_MENU_HISTORY_LIMIT:
            raise ValueError("limit must be exactly 32")
        if type(role) is not str or type(version) is not str:
            raise ValueError("role and version must be strings")

        def freeze_pins(values):
            rows = tuple(values)
            if any(type(row) not in (tuple, list) for row in rows):
                raise ValueError("pin rows must be tuples or lists")
            rows = tuple(tuple(row) for row in rows)
            if any(len(row) != 2 or any(type(v) is not str for v in row) for row in rows):
                raise ValueError("pins must contain name/digest string pairs")
            if len({row[0] for row in rows}) != len(rows):
                raise ValueError("duplicate pin name")
            return tuple(sorted(rows))

        site_rows = tuple(sites)
        if any(type(row) not in (tuple, list) for row in site_rows):
            raise ValueError("site rows must be tuples or lists")
        frozen_sites = tuple(tuple(row) for row in site_rows)
        if any(
            len(row) != 3
            or type(row[0]) is not str
            or type(row[1]) is not int
            or type(row[2]) is not int
            or row[1] < 0
            or not 0 <= row[2] <= 0xFFFF
            for row in frozen_sites
        ):
            raise ValueError("sites must contain event/bank/address triples")
        if len({row[0] for row in frozen_sites}) != len(frozen_sites):
            raise ValueError("duplicate site event")
        self.config = _PreLinkMenuConfig(
            enabled,
            role,
            version,
            freeze_pins(pins),
            freeze_pins(observed_pins),
            frozen_sites,
            limit,
        )
        self.available = False
        expected = dict(self.config.pins)
        observed = dict(self.config.observed_pins)
        mismatch = next(
            (
                name
                for name in sorted(expected.keys() | observed.keys())
                if expected.get(name) != observed.get(name)
            ),
            None,
        )
        self.reason = (
            "disabled"
            if not enabled
            else f"pin_mismatch:{mismatch}"
            if mismatch is not None
            else "pins_unverified"
            if not expected
            else "hooks_not_installed"
        )
        self.total = 0
        self.counts = dict.fromkeys((row[0] for row in frozen_sites), 0)
        self.first = {}
        self.recent = deque(maxlen=limit)
        self.hooks = {}
        self.cleanup_pending = []
        self.error_count = 0
        self.errors = deque(maxlen=_PRE_LINK_MENU_ERROR_LIMIT)
        self._session = None
        self._owner = None
        self._owned = []
        self._field_addresses = {}
        self._attempted = False

    def _error(self, event, stage, exc):
        self.error_count += 1
        self.errors.append({"event": event, "stage": stage, "error": type(exc).__name__[:128]})

    def _read(self, event, name, getter, maximum):
        try:
            value = getter()
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("invalid observed integer")
            return value
        except BaseException as exc:  # noqa: BLE001
            self._error(event, name, exc)
            return None

    def _observe(self, event):
        if get_ident() != self._owner:
            self._error(event, "owner_thread", RuntimeError())
            return
        self.total += 1
        self.counts[event] += 1
        sample = {
            "event": event,
            "sequence": self.total,
            "frame": None,
            "cpu_cycles": None,
            "registers": {},
            "fields": {},
        }
        sample["frame"] = self._read(event, "frame", self._session.current_tick, (1 << 63) - 1)
        sample["cpu_cycles"] = self._read(
            event,
            "cpu_cycles",
            lambda: self._session._pyboy.mb.cpu.cycles,
            (1 << 63) - 1,
        )
        for name in ("PC", "SP", "A", "F"):
            sample["registers"][name] = self._read(
                event,
                name,
                lambda name=name: getattr(self._session._pyboy.mb.cpu, name),
                0xFFFF if name in ("PC", "SP") else 0xFF,
            )
        for name, length in _PRE_LINK_MENU_FIELDS:
            address = self._field_addresses[name]
            values = [
                self._read(
                    event,
                    f"{name}[{index}]",
                    lambda address=address, index=index: self._session._pyboy.memory[
                        address + index
                    ],
                    0xFF,
                )
                for index in range(length)
            ]
            sample["fields"][name] = values[0] if length == 1 else values
        self.first.setdefault(event, sample)
        self.recent.append(sample)

    def install(self, session):
        if self._attempted or not self.config.enabled or self.reason != "hooks_not_installed":
            return
        self._owner = get_ident()
        self._session = session
        self._attempted = True
        sites = tuple(
            row
            for row in self.config.sites
            if row[0] not in ("LinkMenu", "Serial_SyncAndExchangeNybble")
        )
        self.hooks = {
            event: {"bank": bank, "address": address, "status": "pending"}
            for event, bank, address in sites
        }
        self.hooks["LinkMenu"] = {"status": "external_owner"}
        self.hooks["Serial_SyncAndExchangeNybble"] = {"status": "external_pending"}
        try:
            if len(sites) != 15 or len({(b, a) for _, b, a in sites}) != 15:
                raise ValueError("expected fifteen distinct owned sites")
            for name, length in _PRE_LINK_MENU_FIELDS:
                bank, address = session.symbols.bank_addr(name)
                if type(bank) is not int or bank != 0 or type(address) is not int:
                    raise ValueError("invalid field address")
                if not (
                    0xC000 <= address
                    and address + length <= 0xE000
                    or 0xFF80 <= address
                    and address + length <= 0xFFFF
                ):
                    raise ValueError("field is outside WRAM/HRAM")
                self._field_addresses[name] = address
        except BaseException as exc:  # noqa: BLE001
            self.reason = "site_or_field_resolution_error:" + type(exc).__name__[:128]
            self._error("install", "resolve", exc)
            return
        for event, bank, address in sites:

            def callback(_context, event=event):
                try:
                    self._observe(event)
                except BaseException as exc:  # noqa: BLE001
                    self._error(event, "callback", exc)

            try:
                session._pyboy.hook_register(bank, address, callback, None)
            except BaseException as exc:  # noqa: BLE001
                self.hooks[event]["status"] = "registration_failed"
                self._error(event, "register", exc)
                self.reason = "hook_registration_error:" + event
                self._cleanup()
                return
            self._owned.append((event, bank, address))
            self.hooks[event]["status"] = "registered"
        self.reason = "external_pending"

    def external_registered(self):
        if get_ident() != self._owner or self.reason != "external_pending":
            return
        self.hooks["Serial_SyncAndExchangeNybble"]["status"] = "external_registered"
        self.available = len(self._owned) == 15
        self.reason = None if self.available else "owned_hooks_incomplete"

    def external_failed(self, exc):
        if get_ident() != self._owner or self.reason != "external_pending":
            return
        self.hooks["Serial_SyncAndExchangeNybble"]["status"] = "external_failed"
        self._error("Serial_SyncAndExchangeNybble", "external_register", exc)
        self.reason = "external_registration_failed"
        self._cleanup()

    def _cleanup(self):
        self.available = False
        if self._session is None or not self._owned:
            self.cleanup_pending = [event for event, _, _ in self._owned]
            return
        pending = []
        for event, bank, address in reversed(self._owned):
            try:
                self._session._pyboy.hook_deregister(bank, address)
            except BaseException as exc:  # noqa: BLE001
                self.hooks[event]["status"] = "cleanup_pending"
                self._error(event, "deregister", exc)
                pending.append((event, bank, address))
            else:
                self.hooks[event]["status"] = "removed"
        self._owned = list(reversed(pending))
        self.cleanup_pending = [event for event, _, _ in self._owned]

    def close(self):
        self.available = False
        if not self._attempted:
            # Keep disabled/asset-validation reasons in the emitted result.
            return
        if self._owner is not None and get_ident() != self._owner:
            self._error("close", "owner_thread", RuntimeError())
            self.cleanup_pending = [event for event, _, _ in self._owned]
            self.reason = "cleanup_wrong_thread"
            return
        self._cleanup()
        self.reason = "cleanup_pending" if self.cleanup_pending else "closed"

    def snapshot(self):
        return deepcopy(
            {
                "schema_version": _PRE_LINK_MENU_SCHEMA_VERSION,
                "enabled": self.config.enabled,
                "available": self.available,
                "reason": self.reason,
                "role": self.config.role,
                "version": self.config.version,
                "pins": dict(self.config.pins),
                "observed_pins": dict(self.config.observed_pins),
                "sites": {
                    event: {"bank": bank, "address": address}
                    for event, bank, address in self.config.sites
                },
                "limit": self.config.limit,
                "total": self.total,
                "counts": self.counts,
                "first": self.first,
                "recent": list(self.recent),
                "recent_dropped": max(0, self.total - len(self.recent)),
                "recent_truncated": self.total > len(self.recent),
                "hooks": self.hooks,
                "cleanup_pending": self.cleanup_pending,
                "error_limit": _PRE_LINK_MENU_ERROR_LIMIT,
                "error_count": self.error_count,
                "errors": list(self.errors),
                "errors_dropped": max(0, self.error_count - len(self.errors)),
                "errors_truncated": self.error_count > len(self.errors),
            }
        )
