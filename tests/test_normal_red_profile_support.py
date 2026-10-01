"""Asset-free controls for opt-in profiling; no ROM or reservation claims."""

import asyncio
import io
import json
import threading
from types import SimpleNamespace

import pytest

from pokered_harness import qualification_profile as telemetry
from scripts import profile_mcp_owner as owner
from scripts import profile_normal_red_link as driver


def test_actual_loaded_runtime_matches_requested_mode():
    import pyboy.pyboy

    mode = "source" if pyboy.pyboy.__file__.endswith(".py") else "cython"
    measured = telemetry.runtime_identity(driver.ROOT, mode)
    assert measured["pyboy_revision"] == "fd765b1808ac9cb192b42ae971987158ff36ae48"
    assert measured["pid"] > 0
    assert all(
        row["kind"] == ("source" if mode == "source" else "extension")
        for row in measured["modules"].values()
    )
    with pytest.raises(ValueError):
        telemetry.runtime_identity(driver.ROOT, "cython" if mode == "source" else "source")


@pytest.mark.parametrize("origin", [None, "unknown.bin"])
def test_unknown_runtime_origin_declines(tmp_path, origin):
    path = tmp_path / "unknown.bin"
    path.write_bytes(b"test")
    with pytest.raises(ValueError):
        telemetry.module_identity(SimpleNamespace(__file__=None if origin is None else str(path)))


def test_external_directory_rejects_checkout_alias(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError):
        telemetry.external_directory(repo / "evidence", repo)
    assert telemetry.external_directory(tmp_path / "outside", repo) == tmp_path / "outside"


@pytest.mark.parametrize(
    "failure", [None, RuntimeError("original"), asyncio.CancelledError("cancel")]
)
def test_recorder_returns_or_raises_exact_original(monkeypatch, failure):
    monkeypatch.setattr(
        telemetry, "owner_cpu", lambda pid: {"status": "measured", "process_start_ticks": 42}
    )
    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"primary_tick": 7, "peer_tick": 8, "state": "PRIVATE"}),
            }
        ]
    }
    stream = io.StringIO()
    recorder = telemetry.CallRecorder(stream, "local-pair", 123)

    async def original(method, params):
        if failure is not None:
            raise failure
        return result

    if failure is None:
        assert (
            asyncio.run(
                recorder.request(
                    original,
                    "tools/call",
                    {"name": "link_step", "arguments": {"count": 2, "state": "PRIVATE"}},
                )
            )
            is result
        )
    else:
        with pytest.raises(type(failure)) as caught:
            asyncio.run(recorder.request(original, "tools/call", {"name": "link_step"}))
        assert caught.value is failure
    row = json.loads(stream.getvalue())
    assert row["cpu_scope"] == "pair-process"
    assert row["cpu_accounting_key"] == 123
    assert row["cpu_sample_status"] == "measured"
    assert "PRIVATE" not in stream.getvalue()
    if failure is None:
        assert row["step_result"]["returned_fields"] == {"primary_tick": 7, "peer_tick": 8}


@pytest.mark.parametrize(
    "samples",
    [
        ({"status": "unknown"}, {"status": "unknown"}),
        (
            {"status": "measured", "process_start_ticks": 1},
            {"status": "measured", "process_start_ticks": 2},
        ),
    ],
)
def test_unknown_or_reused_process_cpu_never_measured(monkeypatch, samples):
    rows = iter(samples)
    monkeypatch.setattr(telemetry, "owner_cpu", lambda pid: next(rows))
    stream = io.StringIO()

    async def original(*args):
        return {}

    asyncio.run(
        telemetry.CallRecorder(stream, "tcp-owner-0", 1).request(original, "tools/list", {})
    )
    assert json.loads(stream.getvalue())["cpu_sample_status"] == "unknown"


def test_evidence_error_preserves_original_failure():
    class Broken(io.StringIO):
        def write(self, value):
            raise OSError("disk")

    failure = RuntimeError("original")

    async def original(*args):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            telemetry.CallRecorder(Broken(), "tcp-owner-0", 1).request(original, "tools/list", {})
        )
    assert caught.value is failure
    assert "profile evidence" in failure.__notes__[0]


def test_call_bound_precedes_original():
    async def original(*args):
        pytest.fail("must not call")

    with pytest.raises(ValueError):
        asyncio.run(
            telemetry.CallRecorder(io.StringIO(), "local-pair", 1, max_calls=0).request(
                original, "tools/list", {}
            )
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"history_version": True, "operations": [{"operation": "link_up"}]},
        {"history_version": 1, "operations": []},
        {"history_version": 1, "operations": [{"operation": "state", "owner": 0}]},
        {
            "history_version": 1,
            "operations": [
                {"operation": "link_up"},
                {"operation": "press", "owner": True, "button": "a", "duration": 1},
            ],
        },
        {
            "history_version": 1,
            "operations": [{"operation": "link_up"}, {"operation": "step", "count": 121}],
        },
        {"history_version": 1, "operations": [{"operation": "link_up", "ram": 7}]},
        {
            "history_version": 1,
            "operations": [{"operation": "link_up"}, {"operation": "write_memory"}],
        },
        {
            "history_version": 1,
            "operations": [
                {"operation": "link_up"},
                {"operation": "press", "owner": 0, "button": [], "duration": 1},
            ],
        },
    ],
)
def test_history_declines_unsafe_or_unbounded_input(bad):
    with pytest.raises(ValueError):
        driver.validate_history(bad)


def test_replay_preserves_exact_public_history():
    calls = []

    class Pair:
        async def link_up(self, **kwargs):
            calls.append(("link_up", kwargs))

        async def press(self, *args, **kwargs):
            calls.append(("press", args, kwargs))

        async def step(self, count):
            calls.append(("step", count))

        async def state(self, owner):
            calls.append(("state", owner))

    history = [
        {"operation": "link_up"},
        {"operation": "press", "owner": 1, "button": "a", "duration": 3},
        {"operation": "step", "count": 10},
        {"operation": "state", "owner": 0},
    ]
    stream = io.StringIO()
    asyncio.run(driver.replay(Pair(), history, stream))
    assert calls == [
        ("link_up", {"arm_barrier": None}),
        ("press", (1, "a"), {"duration": 3}),
        ("step", 10),
        ("state", 0),
    ]
    assert len(stream.getvalue().splitlines()) == 4


def test_profiles_opt_in_restores_thread_run(tmp_path):
    original = threading.Thread.run
    with owner.profiles(tmp_path, False):
        assert threading.Thread.run is original
    assert not list(tmp_path.glob("*.pstats"))
    with owner.profiles(tmp_path, True) as errors:
        results = []
        thread = threading.Thread(target=lambda: results.append(sum(range(100))))
        thread.start()
        thread.join()
    assert threading.Thread.run is original
    assert errors == []
    assert (tmp_path / "main.pstats").exists()
    assert results == [4950]
    assert not list(tmp_path.glob("thread-*.pstats"))


def test_owner_wrapper_keeps_protocol_stdout_and_original_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(owner, "runtime_identity", lambda *args: {"status": "unit_control"})
    failure = RuntimeError("original")

    def serve():
        print('{"jsonrpc":"2.0"}')
        raise failure

    with pytest.raises(RuntimeError) as caught:
        owner.run_owner(tmp_path / "output", "source", (0, 1), serve=serve)
    assert caught.value is failure
    assert capsys.readouterr().out == '{"jsonrpc":"2.0"}\n'
    assert json.loads((tmp_path / "output/owner-start.json").read_text())["role"] == "local_pair"
    assert json.loads((tmp_path / "output/owner-end.json").read_text())["status"] == "RAISED"


def test_production_owner_requires_commit_before_serve(tmp_path):
    with pytest.raises(ValueError, match="exact source commit"):
        owner.run_owner(tmp_path / "out", "source", (0,))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("transport", ["local", "tcp"])
def test_missing_owner_evidence_remains_unknown(tmp_path, transport):
    result = driver.measurement_report(tmp_path, transport, "a" * 40, {})
    assert result["status"] == "unknown"
    assert result["qualified_profile"] is False
    assert len(result["unknown"]) == (1 if transport == "local" else 2)


def test_complete_local_evidence_has_one_pair_cpu_identity(tmp_path):
    directory = tmp_path / "owners/local-pair"
    directory.mkdir(parents=True)
    runtime = {
        "build_fingerprint": "fixture",
        "requested_mode": "source",
        "pyboy_revision": "fixture",
        "os_executable_status": "measured",
        "os_executable": "/unit/python",
    }
    start = {
        **runtime,
        "owner_ids": [0, 1],
        "pid": 123,
        "source_identity": {"commit": "a" * 40},
        "process_cpu_identity": {"status": "measured", "process_start_ticks": 42},
    }
    (directory / "owner-start.json").write_text(json.dumps(start))
    (directory / "owner-end.json").write_text(
        json.dumps({"runtime_identity_after": start, "status": "RETURNED", "profile_errors": []})
    )
    (tmp_path / "rpc-local-pair.jsonl").write_text(
        json.dumps(
            {
                "pid": 123,
                "cpu_sample_status": "measured",
                "owner_cpu_start": {"process_start_ticks": 42},
            }
        )
        + "\n"
    )
    report = driver.measurement_report(tmp_path, "local", "a" * 40, runtime)
    assert report["status"] == "complete"
    assert report["qualified_profile"] is False
    assert report["owners"] == [
        {
            "role": "local-pair",
            "owner_ids": [0, 1],
            "pid": 123,
            "cpu_scope": "pair-process",
            "build_fingerprint": "fixture",
            "rpc_count": 1,
        }
    ]
    assert driver.measurement_report(tmp_path, "local", "b" * 40, runtime)["status"] == "unknown"


def test_bad_revision_declines_before_owner_runtime(monkeypatch):
    import pyboy

    monkeypatch.setattr(pyboy, "__pokered_harness_revision__", "incorrect")
    with pytest.raises(ValueError, match="VERSIONS pins"):
        telemetry.runtime_identity(driver.ROOT, "source")


def test_owner_final_measurement_failure_preserves_server_error(monkeypatch, tmp_path):
    count = 0

    def identity(*args):
        nonlocal count
        count += 1
        if count > 1:
            raise ImportError("measurement")
        return {"status": "unit_control"}

    monkeypatch.setattr(owner, "runtime_identity", identity)
    failure = RuntimeError("server failure")

    def serve():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        owner.run_owner(tmp_path / "owner", "source", (0,), serve=serve)
    assert caught.value is failure
    assert "ImportError" in failure.__notes__[0]


def test_unadmitted_runner_never_collects_or_launches(monkeypatch, tmp_path):
    from scripts import qualification_runner_facts, qualification_runner_report

    monkeypatch.setattr(
        qualification_runner_report, "load_declaration", lambda path: (None, "missing")
    )
    monkeypatch.setattr(
        qualification_runner_facts, "collect_facts", lambda *args: pytest.fail("must not collect")
    )
    with pytest.raises(ValueError, match="declaration unavailable"):
        driver.allocation_admission(tmp_path / "missing", tmp_path / "missing-policy", tmp_path)


def test_source_commit_and_dirty_source_decline(monkeypatch, tmp_path):
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        return "b" * 40 if args[-1] == "HEAD" else " M dirty.py"

    monkeypatch.setattr(telemetry.subprocess, "check_output", output)
    with pytest.raises(ValueError, match="commit mismatch"):
        telemetry.source_identity(tmp_path, "a" * 40)
    with pytest.raises(ValueError, match="clean committed"):
        telemetry.source_identity(tmp_path, "b" * 40)
    with pytest.raises(ValueError, match="exact source"):
        telemetry.source_identity(tmp_path, "main")


def test_history_total_frame_bound():
    with pytest.raises(ValueError, match="frame bound"):
        driver.validate_history(
            {
                "history_version": 1,
                "operations": [{"operation": "link_up"}]
                + [{"operation": "step", "count": 120}] * 151,
            }
        )


@pytest.mark.parametrize("transport", ["local", "tcp"])
def test_opt_in_launch_references_restore_without_launch(monkeypatch, tmp_path, transport):
    from tests import _mcp_trade_records_rom_support as support
    from tests import _mcp_trade_records_rom_tests_support as tcp_support

    original = support._launch_server, tcp_support._launch_server
    with (
        pytest.raises(RuntimeError, match="body"),
        driver.owner_launches(tmp_path, "source", transport, "a" * 40, False),
    ):
        assert support._launch_server is tcp_support._launch_server
        assert support._launch_server is not original[0]
        raise RuntimeError("body")
    assert (support._launch_server, tcp_support._launch_server) == original
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("transport, expected_ids", [("local", ["0,1"]), ("tcp", ["0", "1"])])
def test_launch_forwards_actual_identity_checks_and_bounds(
    monkeypatch, tmp_path, transport, expected_ids
):
    from tests import _mcp_trade_records_rom_support as support
    from tests import test_mcp_timed_rom

    commands = []

    class Client:
        def __init__(self, process):
            self.process = process

        async def request(self, method, params):
            return {"fixture": True}

    async def create(*args, **kwargs):
        commands.append((args, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(test_mcp_timed_rom, "RomClient", Client)
    monkeypatch.setattr(driver.asyncio, "create_subprocess_exec", create)

    async def exercise():
        with driver.owner_launches(tmp_path, "source", transport, "a" * 40, True):
            for _ in expected_ids:
                client = await support._launch_server({"PRIVATE": "preserved"})
                assert await client.request("tools/list", {}) == {"fixture": True}
            with pytest.raises(ValueError, match="launch bound"):
                await support._launch_server({})

    asyncio.run(exercise())
    assert len(commands) == len(expected_ids)
    for (args, kwargs), role in zip(commands, expected_ids, strict=True):
        assert args[args.index("--owners") + 1] == role
        assert args[args.index("--expected-head") + 1] == "a" * 40
        assert args[args.index("--runtime") + 1] == "source"
        assert "--cprofile" in args
        assert kwargs["env"] == {"PRIVATE": "preserved"}
        assert kwargs["start_new_session"] is True
    assert all("PRIVATE" not in path.read_text() for path in tmp_path.glob("rpc-*.jsonl"))


def test_step_result_nonfinite_and_private_fields_excluded():
    payload = {
        "primary_tick": 7,
        "actual_frames": float("nan"),
        "state": "PRIVATE",
        "message": "PRIVATE",
        "code": "timeout",
        "partial": True,
    }
    result = telemetry.step_result(
        {"isError": True, "content": [{"type": "text", "text": json.dumps(payload)}]}
    )
    assert result["is_error"] is True
    assert result["returned_fields"] == {"primary_tick": 7, "code": "timeout", "partial": True}
    assert "PRIVATE" not in json.dumps(result)


def test_replay_cancellation_preserves_original_and_evidence():
    failure = asyncio.CancelledError("original")

    class Pair:
        async def link_up(self, **kwargs):
            raise failure

    stream = io.StringIO()
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(driver.replay(Pair(), [{"operation": "link_up"}], stream))
    assert caught.value is failure
    assert json.loads(stream.getvalue())["error_type"] == "CancelledError"


def test_main_profile_exception_leaves_thread_execution_unchanged(tmp_path):
    original = threading.Thread.run
    failure = RuntimeError("body")
    with pytest.raises(RuntimeError) as caught, owner.profiles(tmp_path, True):
        assert threading.Thread.run is original
        raise failure
    assert caught.value is failure
    assert threading.Thread.run is original
    assert (tmp_path / "main.pstats").exists()


def test_mixed_class_origin_declines_before_owner(tmp_path):
    import importlib

    import pyboy.pyboy

    outside = tmp_path / "foreign.py"
    outside.write_text("# unit control\n")
    foreign = SimpleNamespace(__file__=str(outside))
    original = importlib.import_module("pyboy.core.cpu")
    replacement = SimpleNamespace(
        __file__=original.__file__, CPU=type("CPU", (), {"__module__": "foreign"})
    )

    def importer(name):
        if name == "foreign":
            return foreign
        if name == "pyboy.core.cpu":
            return replacement
        return importlib.import_module(name)

    mode = "source" if pyboy.pyboy.__file__.endswith(".py") else "cython"
    with pytest.raises(ValueError, match="(modules|extensions)/classes"):
        telemetry.runtime_identity(driver.ROOT, mode, importer=importer)


def test_cli_allocation_rejection_precedes_assets_or_owners(monkeypatch, tmp_path):
    import sys

    asset_calls = []
    fake = SimpleNamespace(resolve_normal_red_assets=lambda *args: asset_calls.append(args))
    monkeypatch.setitem(sys.modules, "scripts.normal_red_link_admission", fake)
    monkeypatch.setattr(driver, "baseline_sources", lambda head: {"commit": head})
    monkeypatch.setattr(driver, "runtime_identity", lambda *args: {"status": "unit_control"})

    def reject(*args):
        raise ValueError("reservation denied")

    monkeypatch.setattr(driver, "allocation_admission", reject)
    monkeypatch.setattr(driver, "owner_launches", lambda *args: pytest.fail("must not launch"))
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"history_version": 1, "operations": [{"operation": "link_up"}]}))
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "profile",
            "--rom",
            "unused",
            "--symbols",
            "unused",
            "--fixture-root",
            "unused",
            "--output",
            str(output),
            "--history",
            str(history),
            "--runner-declaration",
            "unused",
            "--capacity-policy",
            "unused",
            "--expected-head",
            "a" * 40,
            "--runtime",
            "source",
            "--transport",
            "local",
        ],
    )
    with pytest.raises(ValueError, match="reservation denied"):
        driver.main()
    assert asset_calls == []
    assert json.loads((output / "profile-receipt.json").read_text())["status"] == "BLOCKED"


def test_output_rejects_other_git_checkout(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").write_text("gitdir: elsewhere\n")
    with pytest.raises(ValueError, match="Git checkouts"):
        telemetry.external_directory(other / "evidence", tmp_path / "own")


def test_capacity_report_failure_is_retained_without_false_completion(monkeypatch, tmp_path):
    import sys
    from contextlib import nullcontext

    fake = SimpleNamespace(
        resolve_normal_red_assets=lambda *args: {
            "state": b"unit control",
            "pins": {},
            "family": "red",
        }
    )
    monkeypatch.setitem(sys.modules, "scripts.normal_red_link_admission", fake)
    monkeypatch.setattr(driver, "baseline_sources", lambda head: {"commit": head})
    monkeypatch.setattr(driver, "runtime_identity", lambda *args: {"status": "unit_control"})
    failure = OSError("capacity evidence")

    def report(**kwargs):
        raise failure

    capacity = SimpleNamespace(
        monitoring=nullcontext,
        admission=SimpleNamespace(release=lambda *args: None),
        telemetry=SimpleNamespace(mark=lambda *args: None),
        report=report,
    )
    monkeypatch.setattr(driver, "allocation_admission", lambda *args: capacity)
    monkeypatch.setattr(driver, "owner_launches", lambda *args: nullcontext())
    monkeypatch.setattr(
        driver,
        "measurement_report",
        lambda *args: {"status": "unknown", "qualified_profile": False},
    )

    async def run(*args):
        return None

    monkeypatch.setattr(driver, "run_history", run)
    history = tmp_path / "history.json"
    history.write_text(json.dumps({"history_version": 1, "operations": [{"operation": "link_up"}]}))
    output = tmp_path / "output"
    monkeypatch.setenv("POKERED_TRADE_RUNTIME_MODE", "original")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "profile",
            "--rom",
            "unused",
            "--symbols",
            "unused",
            "--fixture-root",
            "unused",
            "--output",
            str(output),
            "--history",
            str(history),
            "--runner-declaration",
            "unused",
            "--capacity-policy",
            "unused",
            "--expected-head",
            "a" * 40,
            "--runtime",
            "source",
            "--transport",
            "local",
        ],
    )
    with pytest.raises(OSError) as caught:
        driver.main()
    assert caught.value is failure
    receipt = json.loads((output / "profile-receipt.json").read_text())
    assert receipt["status"] == "FAILED"
    assert receipt["capacity_evidence_status"] == "unknown"
    assert driver.os.environ["POKERED_TRADE_RUNTIME_MODE"] == "original"


@pytest.mark.parametrize("tampered", [False, True])
def test_five_baseline_controller_blob_checks(monkeypatch, tampered):
    import subprocess

    monkeypatch.setattr(driver, "source_identity", lambda root, head: {"commit": head})

    def committed(args, **kwargs):
        relative = args[-1].split(":", 1)[1]
        data = (driver.ROOT / relative).read_bytes()
        return data + b"tampered" if tampered else data

    monkeypatch.setattr(subprocess, "check_output", committed)
    if tampered:
        with pytest.raises(ValueError, match="differs from commit"):
            driver.baseline_sources("a" * 40)
    else:
        result = driver.baseline_sources("a" * 40)
        assert result["commit"] == "a" * 40
        assert set(result["checked_baseline_controller_files"]) == set(driver.BASELINE_FILES)
        assert len(result["checked_baseline_controller_files"]) == 5


def test_rebound_package_constructor_declines(monkeypatch):
    import pyboy

    monkeypatch.setattr(pyboy, "PyBoy", type("Foreign", (), {}))
    with pytest.raises(ValueError, match="constructor differs"):
        telemetry.runtime_identity(driver.ROOT, "source")


def test_profile_input_metadata_and_public_queries_exclude_private_bytes():
    asset = {
        "family": "red",
        "pins": {"expected_rom_sha1": "a" * 40},
        "state": b"PRIVATE_SYNTHETIC_CONTROL",
    }
    identity = driver.asset_identity(asset)
    assert set(identity) == {"family", "pins", "fixture_size", "fixture_sha256"}
    assert identity["fixture_size"] == len(asset["state"])
    assert len(identity["fixture_sha256"]) == 64
    assert "PRIVATE" not in json.dumps(identity)
    assert telemetry.safe_request(
        "resources/read", {"uri": "pokered://game-state", "data": "PRIVATE"}
    ) == {"method": "resources/read", "uri": "pokered://game-state"}
    assert telemetry.safe_request("resources/read", {"uri": "PRIVATE"}) == {
        "method": "resources/read"
    }
    assert telemetry.safe_request(
        "tools/call",
        {"name": "link_frame_barrier", "arguments": {"enabled": True, "data": "PRIVATE"}},
    ) == {"method": "tools/call", "tool": "link_frame_barrier", "enabled": True}
