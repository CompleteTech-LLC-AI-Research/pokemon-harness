"""Readiness-synchronized local TCP pair setup for serial-link tests."""

from __future__ import annotations

import threading

from pokered_harness.link.serial_link import SerialLinkClosed, TcpSerialLink


def make_tcp_pair(port: int, a_rom: str, b_rom: str) -> tuple[TcpSerialLink, TcpSerialLink]:
    """Wait for listener readiness before connecting; clean up failed setup."""
    holder: dict[str, TcpSerialLink] = {}
    errors: list[BaseException] = []
    ready = threading.Event()
    cancel = threading.Event()
    client: TcpSerialLink | None = None

    def listen() -> None:
        try:
            holder["server"] = TcpSerialLink.listen(
                port, a_rom, ready_event=ready, cancel_event=cancel
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            ready.set()

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    try:
        assert ready.wait(timeout=0.5), "TCP listener did not become ready"
        if errors:
            raise errors[0]
        client = TcpSerialLink.connect("127.0.0.1", port, b_rom)
        listener.join(timeout=2.0)
        if errors:
            raise errors[0]
        assert not listener.is_alive(), "TCP listener thread did not finish"
        if "server" not in holder:
            raise AssertionError("TCP listener returned without a link")
        return holder["server"], client
    except BaseException as exc:
        cancel.set()
        if client is not None:
            client.close()
        listener.join(timeout=2.0)
        server = holder.get("server")
        if server is not None:
            server.close()
        if listener.is_alive():
            raise AssertionError("TCP listener thread did not stop") from exc
        listener_error = errors[0] if errors else None
        if (
            listener_error is not None
            and listener_error is not exc
            and not isinstance(listener_error, SerialLinkClosed)
        ):
            raise listener_error from exc
        raise
