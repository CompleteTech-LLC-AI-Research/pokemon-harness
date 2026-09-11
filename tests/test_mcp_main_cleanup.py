"""Exercise real main() cleanup without ROMs, emulator construction, or stdio."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from pokered_harness import config, mcp_server
from pokered_harness.mcp_server import McpHarnessError


class OperationFailure(RuntimeError):
    pass


class PeerCloseFailure(RuntimeError):
    pass


class PrimaryCloseFailure(RuntimeError):
    pass


def _configure_main(
    monkeypatch,
    tmp_path,
    *,
    fail_at=None,
    operation_error=None,
    peer_error=None,
    primary_error=None,
    with_peer=True,
):
    calls = []
    primary_rom, primary_sym = tmp_path / "primary.gb", tmp_path / "primary.sym"
    peer_rom, peer_sym = tmp_path / "peer.gb", tmp_path / "peer.sym"
    primary_env = config.SessionEnv(str(primary_rom), str(primary_sym), "1" * 40, "red")
    peer_env = config.SessionEnv(
        str(peer_rom) if with_peer else None,
        str(peer_sym) if with_peer else None,
        "2" * 40 if with_peer else None,
        "blue",
    )
    versions = config.VersionsConfig(
        rom_sha1="1" * 40,
        pyboy_version="2.7.0",
        pyboy_revision="c" * 40,
        symbol_sha1_by_path=(("primary.sym", "3" * 40), ("peer.sym", "4" * 40)),
    )
    monkeypatch.delenv("POKERED_SKIP_SHA1", raising=False)
    # ``main`` reads explicit symbol pins directly from ``os.environ`` rather
    # than through the mocked config loaders.  Clear operator/gate overrides
    # so this helper exercises the mocked per-path VersionsConfig pins below.
    # The production entry point still accepts explicit symbol overrides; this
    # is test-fixture isolation, not a relaxation of its strict pin checks.
    monkeypatch.delenv("POKERED_SYM_SHA1", raising=False)
    monkeypatch.delenv("POKERED_PEER_SYM_SHA1", raising=False)
    monkeypatch.setattr(config, "load_primary_env", lambda: primary_env)
    monkeypatch.setattr(config, "load_peer_env", lambda: peer_env)
    monkeypatch.setattr(config, "load_versions", lambda: versions)

    def close(name, error):
        calls.append(f"close:{name}")
        if error is not None:
            raise error

    primary = SimpleNamespace(
        name="primary", close=lambda **_kwargs: close("primary", primary_error)
    )
    peer = SimpleNamespace(
        name="peer", close=lambda **_kwargs: close("peer", peer_error)
    )

    def from_files(_cls, rom, sym, **kwargs):
        name = "primary" if rom == primary_rom else "peer"
        assert (rom, sym) == (
            (primary_rom, primary_sym) if name == "primary" else (peer_rom, peer_sym)
        )
        assert kwargs["expected_rom_sha1"] == ("1" if name == "primary" else "2") * 40
        assert kwargs["expected_symbol_sha1"] == ("3" if name == "primary" else "4") * 40
        assert kwargs["expected_pyboy_version"] == "2.7.0"
        calls.append(f"construct:{name}")
        if fail_at == f"construct:{name}":
            raise operation_error
        return primary if name == "primary" else peer

    def register_hooks(session):
        calls.append(f"hooks:{session.name}")
        if fail_at == f"hooks:{session.name}":
            raise operation_error
        return []

    async def serve(session, **kwargs):
        assert session is primary
        assert kwargs == {
            "peer_session": peer if with_peer else None,
            "primary_version": "red",
            "peer_version": "blue",
        }
        calls.append("serve")
        if fail_at == "serve":
            raise operation_error

    monkeypatch.setattr(mcp_server.Session, "from_files", classmethod(from_files))
    monkeypatch.setattr(mcp_server, "register_default_hooks", register_hooks)
    monkeypatch.setattr(mcp_server, "serve_stdio", serve)
    return calls


def _closes(calls):
    return [call for call in calls if call.startswith("close:")]


def _assert_cleanup_result(raised, operation_error, *error_names):
    if operation_error is None:
        assert isinstance(raised.value, McpHarnessError)
        assert raised.value.code == "server_cleanup_failed"
        for name in error_names:
            assert name in str(raised.value)
    else:
        assert raised.value is operation_error
        assert any(
            "MCP session cleanup failed" in note
            for note in getattr(raised.value, "__notes__", ())
        )


def test_configure_main_isolates_inherited_symbol_pin_overrides(
    monkeypatch, tmp_path
):
    """The fixture must not inherit unrelated primary/peer symbol pins."""
    monkeypatch.setenv("POKERED_SYM_SHA1", "a" * 40)
    monkeypatch.setenv("POKERED_PEER_SYM_SHA1", "b" * 40)

    calls = _configure_main(monkeypatch, tmp_path, with_peer=True)

    assert os.environ.get("POKERED_SYM_SHA1") is None
    assert os.environ.get("POKERED_PEER_SYM_SHA1") is None
    mcp_server.main()
    assert _closes(calls) == ["close:peer", "close:primary"]


@pytest.mark.parametrize("with_peer", [False, True])
def test_main_success_closes_each_constructed_session(monkeypatch, tmp_path, with_peer):
    calls = _configure_main(monkeypatch, tmp_path, with_peer=with_peer)
    mcp_server.main()
    assert "serve" in calls
    assert _closes(calls) == (["close:peer"] if with_peer else []) + ["close:primary"]


@pytest.mark.parametrize(
    ("fail_at", "expected_closes"),
    [
        ("construct:primary", []),
        ("hooks:primary", ["close:primary"]),
        ("construct:peer", ["close:primary"]),
        ("hooks:peer", ["close:peer", "close:primary"]),
        ("serve", ["close:peer", "close:primary"]),
    ],
)
def test_main_operation_failure_closes_only_constructed_sessions(
    monkeypatch, tmp_path, fail_at, expected_closes
):
    error = OperationFailure(fail_at)
    calls = _configure_main(monkeypatch, tmp_path, fail_at=fail_at, operation_error=error)
    with pytest.raises(OperationFailure) as raised:
        mcp_server.main()
    assert raised.value is error
    assert _closes(calls) == expected_closes
    assert ("serve" in calls) == (fail_at == "serve")


@pytest.mark.parametrize("fail_at", [None, "hooks:peer", "serve"])
def test_peer_close_failure_still_closes_primary_and_preserves_operation_context(
    monkeypatch, tmp_path, fail_at
):
    operation_error = OperationFailure(fail_at) if fail_at is not None else None
    peer_error = PeerCloseFailure("peer stop failed")
    calls = _configure_main(
        monkeypatch, tmp_path, fail_at=fail_at, operation_error=operation_error,
        peer_error=peer_error,
    )
    with pytest.raises(Exception) as raised:
        mcp_server.main()
    assert _closes(calls) == ["close:peer", "close:primary"]
    _assert_cleanup_result(raised, operation_error, "peer PeerCloseFailure")


@pytest.mark.parametrize("fail_at", [None, "hooks:peer", "serve"])
def test_both_close_failures_preserve_peer_and_operation_exception_context(
    monkeypatch, tmp_path, fail_at
):
    operation_error = OperationFailure(fail_at) if fail_at is not None else None
    peer_error = PeerCloseFailure("peer stop failed")
    primary_error = PrimaryCloseFailure("primary stop failed")
    calls = _configure_main(
        monkeypatch, tmp_path, fail_at=fail_at, operation_error=operation_error,
        peer_error=peer_error, primary_error=primary_error,
    )
    with pytest.raises(Exception) as raised:
        mcp_server.main()
    assert _closes(calls) == ["close:peer", "close:primary"]
    _assert_cleanup_result(
        raised,
        operation_error,
        "peer PeerCloseFailure",
        "primary PrimaryCloseFailure",
    )


def test_primary_close_failure_preserves_serve_failure_without_peer(monkeypatch, tmp_path):
    operation_error = OperationFailure("serve failed")
    primary_error = PrimaryCloseFailure("primary stop failed")
    calls = _configure_main(
        monkeypatch, tmp_path, fail_at="serve", operation_error=operation_error,
        primary_error=primary_error, with_peer=False,
    )
    with pytest.raises(Exception) as raised:
        mcp_server.main()
    assert _closes(calls) == ["close:primary"]
    _assert_cleanup_result(raised, operation_error, "primary PrimaryCloseFailure")
