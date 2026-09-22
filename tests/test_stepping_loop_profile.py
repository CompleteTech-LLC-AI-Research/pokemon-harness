"""ROM-free guardrails for the #109 instruction-stepping profile.

The pytest tier exercises the probe's pure machinery only: it authors its own
cartridge bytes, checks the declared instruction mixes against an independent
cycle table, and shows the compiled-runtime gate rejecting the vendored Python
sources. Nothing here opens a cartridge or needs a compiled build. The
measurement paths run through ``--profile-probe`` against a compiled runtime,
the way ``tests/test_cpu_instruction_counter.py`` runs its native cases.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "stepping_loop_profile.py"
VENDOR_CORE = ROOT / "vendor" / "pyboy-src" / "pyboy" / "core"
WORKFLOW = ROOT / ".github" / "workflows" / "release-hygiene.yml"
RUNNER = ROOT / "scripts" / "run_local_ci.sh"

# Independent opcode/cycle table. Every declared mix must agree with it, so a
# typo in the probe's own table cannot silently move the oracle, and a mnemonic
# missing from this table is reported rather than assumed.
TRUE_CYCLES = {
    "NOP": 4,
    "INC BC": 8,
    "LD A,B": 4,
    "ADD A,B": 4,
    "INC B": 4,
    "JP loop": 16,
    "LDH (0x80),A": 12,
}
DECLARED_PATHS = (
    "production_chunk_loop",
    "minimal_singlestep_loop",
    "compiled_frame_loop",
    "compiled_batched_loop",
)
PROBE_CYCLES = 60_000


def _load_profile():
    spec = importlib.util.spec_from_file_location("stepping_loop_profile_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


profile = _load_profile()


def _source_core_module(name: str):
    """Import one vendored core module from its Python source file."""

    spec = importlib.util.spec_from_file_location(
        f"pyboy.core._profile_{name}", VENDOR_CORE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extension_stub(name: str, *, retired: bool = True):
    module = types.SimpleNamespace(__file__=f"pyboy/core/{name}.cpython-311-x86_64-linux-gnu.so")
    cpu_type = type("CPU", (), {"retired_instructions": 0} if retired else {})
    module.CPU = cpu_type
    return module


def _unverified_mnemonics(mix) -> set[str]:
    return {item.mnemonic for item in mix.instructions if item.mnemonic not in TRUE_CYCLES}


def _sample(path: str, seconds: float, cycles: int, retired: int, calls: int) -> object:
    return profile.Sample(
        path=path,
        seconds=seconds,
        cycles=cycles,
        retired=retired,
        python_calls=calls,
        end_pc=profile.LOOP_BASE,
    )


def test_authored_cartridge_is_a_deterministic_rom_only_image(tmp_path):
    first = profile.author_cartridge(tmp_path)
    second = profile.author_cartridge(tmp_path)
    image = first.read_bytes()

    assert len(image) == profile.CARTRIDGE_BYTES == 0x8000
    assert image[profile.HEADER_TITLE] == b"STEPPRF"
    assert (
        image[profile.HEADER_CHECKSUM]
        == (-sum(image[profile.HEADER_TITLE]) - profile.BOOTROM_CHECKSUM_DELTA) & 0xFF
    )
    # Only the declared title and checksum may differ from an empty image, so no
    # Nintendo, operator, or BYO byte can reach the measurement.
    expected = bytearray(profile.CARTRIDGE_BYTES)
    expected[profile.HEADER_TITLE] = b"STEPPRF"
    expected[profile.HEADER_CHECKSUM] = image[profile.HEADER_CHECKSUM]
    assert image == bytes(expected)
    assert first.read_bytes() == second.read_bytes()
    assert first.parent == tmp_path and second.parent == tmp_path


def test_declared_mixes_match_an_independent_cycle_table():
    assert profile.MIXES, "the probe must declare at least one mix"
    for mix in profile.MIXES:
        assert mix.name
        assert mix.loop_instructions == len(mix.instructions) >= 2
        assert not _unverified_mnemonics(mix), f"{mix.name} uses unverified mnemonics"
        for item in mix.instructions:
            assert item.cycles == TRUE_CYCLES[item.mnemonic], (mix.name, item.mnemonic)
            assert item.opcode, (mix.name, item.mnemonic)
        assert mix.loop_cycles == sum(item.cycles for item in mix.instructions)
        assert mix.body == b"".join(item.opcode for item in mix.instructions)
        assert len(mix.body) <= 0x1000
        # Only the closing jump may leave the fall-through path, and it must
        # return to the authored loop head.
        control_flow = [item for item in mix.instructions if item.opcode[0] in (0xC3, 0x18)]
        assert control_flow == [mix.instructions[-1]]
        assert mix.instructions[-1].mnemonic == "JP loop"
        assert mix.instructions[-1].opcode[1:] == bytes(
            [profile.LOOP_BASE & 0xFF, (profile.LOOP_BASE >> 8) & 0xFF]
        )
        assert mix.min_instruction_cycles == min(item.cycles for item in mix.instructions)
        assert mix.max_instruction_cycles == max(item.cycles for item in mix.instructions)


def test_unverified_mnemonic_is_reported_rather_than_assumed():
    real = profile.MIXES_BY_NAME["reg-only"]
    assert _unverified_mnemonics(real) == set()

    fictional = dataclasses.replace(
        real,
        name="fictional",
        instructions=real.instructions[:-1] + (profile.Instruction("RET", b"\xc9", 16),),
    )
    assert _unverified_mnemonics(fictional) == {"RET"}


def test_expected_cycles_is_exact_and_detects_a_misdeclaration():
    mix = profile.MIXES_BY_NAME["reg-only"]
    size, loop_cycles = mix.loop_instructions, mix.loop_cycles

    assert mix.expected_cycles(0) == 0
    assert mix.expected_cycles(1) == mix.instructions[0].cycles
    assert mix.expected_cycles(size) == loop_cycles
    assert mix.expected_cycles(size + 1) == loop_cycles + mix.instructions[0].cycles
    assert mix.expected_cycles(size * 4) == loop_cycles * 4
    retired_points = list(range(1, size * 3 + 1))
    predicted = [mix.expected_cycles(retired) for retired in retired_points]
    assert predicted == sorted(predicted)

    misdeclared = dataclasses.replace(
        mix,
        instructions=tuple(
            dataclasses.replace(item, cycles=item.cycles + 4) for item in mix.instructions
        ),
    )
    assert misdeclared.body == mix.body
    assert misdeclared.expected_cycles(size) == loop_cycles + 4 * size
    assert misdeclared.expected_cycles(size) != mix.expected_cycles(size)


def test_compiled_gate_rejects_the_vendored_python_sources():
    cpu_source = _source_core_module("cpu")
    mb_source = _source_core_module("mb")

    assert profile.module_is_compiled(cpu_source) is False
    with pytest.raises(profile.ProfileRefused, match="compiled CPU and Motherboard"):
        profile.compiled_runtime_identity(cpu_source, mb_source)


def test_compiled_gate_accepts_extension_identity_and_requires_the_counter():
    identity = profile.compiled_runtime_identity(_extension_stub("cpu"), _extension_stub("mb"))

    assert identity["compiled_cpu"] is True
    assert identity["compiled_motherboard"] is True
    assert identity["has_retired_instructions"] is True
    assert identity["cpu_module"].endswith(".so")
    assert identity["interpreter"].startswith(sys.implementation.name)

    with pytest.raises(profile.ProfileRefused, match="retired_instructions"):
        profile.compiled_runtime_identity(
            _extension_stub("cpu", retired=False), _extension_stub("mb")
        )


def test_declared_path_names_and_cli_defaults_agree():
    assert tuple(name for name, _ in profile.PATHS) == DECLARED_PATHS
    assert all(callable(runner) for _, runner in profile.PATHS)
    args = profile.parse_args([])
    assert [name.strip() for name in args.paths.split(",")] == list(DECLARED_PATHS)
    assert [name.strip() for name in args.mixes.split(",")] == [mix.name for mix in profile.MIXES]


@pytest.mark.parametrize(
    "argv",
    [
        ["--cycles", "0"],
        ["--cycles", "-1"],
        ["--repeats", "0"],
        ["--mixes", "reg-only,nope"],
        ["--paths", "production_chunk_loop,nope"],
        ["--profile-mix", "nope"],
    ],
)
def test_main_rejects_invalid_arguments_before_any_measurement(argv, monkeypatch):
    def _unreachable():
        raise AssertionError("argument validation must run before the runtime gate")

    monkeypatch.setattr(profile, "require_compiled_runtime", _unreachable)
    with pytest.raises(SystemExit):
        profile.main(argv)


def test_main_refusal_is_a_clean_bounded_exit(tmp_path, capsys, monkeypatch):
    def _refuse():
        raise profile.ProfileRefused("requires the compiled CPU and Motherboard")

    monkeypatch.setattr(profile, "require_compiled_runtime", _refuse)
    destination = tmp_path / "refused.json"
    code = profile.main(["--cycles", "1000", "--repeats", "1", "--json", str(destination)])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.err.startswith("REFUSED: ")
    assert "compiled" in captured.err
    assert captured.out == ""
    assert not destination.exists()


def test_run_sample_rejects_an_unknown_path_without_a_runtime(tmp_path):
    mix = profile.MIXES_BY_NAME["reg-only"]
    cartridge = profile.author_cartridge(tmp_path)

    with pytest.raises(profile.ProfileRefused, match="unknown stepping path"):
        profile.run_sample(mix, cartridge, "nope", 1024)


def test_summarize_reports_count_determinism_and_rejects_drift():
    mix = profile.MIXES_BY_NAME["reg-only"]
    retired = mix.loop_instructions * 10
    cycles = mix.expected_cycles(retired)
    stable = [
        _sample("minimal_singlestep_loop", 0.5 + n / 100, cycles, retired, retired)
        for n in range(3)
    ]

    summary = profile.summarize(mix, "minimal_singlestep_loop", stable)
    assert summary["samples"] == 3
    assert summary["seconds_median"] == pytest.approx(0.51)
    assert summary["cycles"] == cycles
    assert summary["retired_instructions"] == retired
    assert summary["cycles_per_instruction"] == pytest.approx(cycles / retired)
    assert summary["python_calls_per_instruction"] == pytest.approx(1.0)
    assert summary["deterministic_counts_agree"] is True

    drifted = stable + [_sample("minimal_singlestep_loop", 0.4, cycles + 4, retired, retired)]
    assert (
        profile.summarize(mix, "minimal_singlestep_loop", drifted)["deterministic_counts_agree"]
        is False
    )


def test_ratios_are_relative_to_the_production_path():
    results = {
        "reg-only": {
            "production_chunk_loop": {
                "retired_instructions_per_second": 500_000.0,
                "python_calls_per_instruction": 0.025,
            },
            "minimal_singlestep_loop": {
                "retired_instructions_per_second": 1_000_000.0,
                "python_calls_per_instruction": 1.0,
            },
            "compiled_frame_loop": {
                "retired_instructions_per_second": 5_000_000.0,
                "python_calls_per_instruction": 0.0001,
            },
        }
    }
    ratios = profile.compute_ratios(results)

    assert ratios["reg-only"]["production_to_minimal_singlestep_loop"] == pytest.approx(0.5)
    assert ratios["reg-only"]["production_to_compiled_frame_loop"] == pytest.approx(0.1)
    assert ratios["reg-only"]["python_calls_per_instruction_median_over_paths"] == pytest.approx(
        0.0001
    )


def test_format_report_states_identity_paths_and_ratios():
    mix = profile.MIXES_BY_NAME["reg-only"]
    retired = mix.loop_instructions * 10
    cycles = mix.expected_cycles(retired)
    samples = [_sample("minimal_singlestep_loop", 0.5, cycles, retired, retired)]
    payload = {
        "runtime": {
            "pyboy_version": "2.7.0",
            "pyboy_harness_revision": "c565df66c3731fad2856169a90f6bbec99925915",
            "interpreter": f"{sys.implementation.name} {sys.version.split()[0]}",
        },
        "host_load_1min": 0.25,
        "results": {
            "reg-only": {
                "minimal_singlestep_loop": profile.summarize(
                    mix, "minimal_singlestep_loop", samples
                )
            }
        },
        "ratios": {"reg-only": {"production_to_minimal_singlestep_loop": 0.5}},
    }
    report = profile.format_report(payload)

    assert "pyboy 2.7.0" in report
    assert "c565df66c3731fad2856169a90f6bbec99925915" in report
    assert "host load1 at measurement: 0.25" in report
    assert mix.description in report
    assert "minimal_singlestep_loop" in report
    assert "production_to_minimal_singlestep_loop = 0.500" in report


def test_json_payload_is_serializable_and_free_of_paths():
    mix = profile.MIXES_BY_NAME["reg-only"]
    retired = mix.loop_instructions * 10
    samples = [_sample("compiled_frame_loop", 0.25, mix.expected_cycles(retired), retired, 1)]
    payload = {
        "runtime": {"cpu_module": "cpu.cpython-311-x86_64-linux-gnu.so"},
        "results": {
            "reg-only": {
                "compiled_frame_loop": profile.summarize(mix, "compiled_frame_loop", samples)
            }
        },
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True)

    assert json.loads(encoded)["results"]["reg-only"]["compiled_frame_loop"]["cycles"] > 0
    assert "/home/" not in encoded and "/tmp/" not in encoded


def test_probe_module_and_its_tier_are_part_of_the_ci_contract():
    from tests._tier_config import classify_test

    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    relative = "scripts/stepping_loop_profile.py"

    # Ruff check and format must both cover it, in the workflow and locally.
    assert workflow.count(relative) >= 2
    assert runner.count(relative) >= 2
    assert classify_test(
        __file__, "test_expected_cycles_is_exact_and_detects_a_misdeclaration"
    ) == (frozenset({"unit"}))


SHORT_CYCLES = 40_000
CHUNK_CYCLES = profile.PyBoyLinkSession._REAL_SCHEDULER_CHUNK_CYCLES


def probe_native_declared_mixes_match_the_oracle():
    with TemporaryDirectory(prefix="stepping-profile-probe-") as directory:
        for mix in profile.MIXES:
            cartridge = profile.author_cartridge(Path(directory))
            for name, _ in profile.PATHS:
                sample = profile.run_sample(mix, cartridge, name, PROBE_CYCLES)
                assert sample.retired > 0, (mix.name, name)
                assert sample.cycles == mix.expected_cycles(sample.retired), (mix.name, name)
                assert profile.LOOP_BASE <= sample.end_pc < profile.LOOP_BASE + len(mix.body)
                if name == "minimal_singlestep_loop":
                    # One Python call per retired instruction, by construction.
                    assert sample.python_calls == sample.retired, (mix.name, name)
                if name == "production_chunk_loop":
                    # Each production call advances at least its cycle budget and
                    # at most one instruction beyond it.
                    assert sample.python_calls * CHUNK_CYCLES <= sample.cycles
                    assert sample.cycles <= sample.python_calls * (
                        CHUNK_CYCLES + mix.max_instruction_cycles
                    ), (mix.name, name)


def probe_native_misdeclared_mix_is_rejected():
    mix = profile.MIXES_BY_NAME["reg-only"]
    liar = dataclasses.replace(
        mix,
        name="reg-only-liar",
        instructions=tuple(
            dataclasses.replace(item, cycles=item.cycles + 4) for item in mix.instructions
        ),
    )
    assert liar.body == mix.body
    with TemporaryDirectory(prefix="stepping-profile-probe-") as directory:
        cartridge = profile.author_cartridge(Path(directory))
        with pytest.raises(profile.ProfileRefused, match="predicts"):
            profile.run_sample(liar, cartridge, "minimal_singlestep_loop", SHORT_CYCLES)


def probe_native_single_step_chunk_is_budget_bounded_across_a_frame():
    mix = profile.MIXES_BY_NAME["reg-only"]
    frames = 1
    with TemporaryDirectory(prefix="stepping-profile-probe-") as directory:
        cartridge = profile.author_cartridge(Path(directory))
        game = profile.open_loop_game(cartridge, mix)
        try:
            frame_cycles = int(game.mb.lcd._cycles_to_frame) or 70224
            start = game.mb.cpu.cycles
            start_retired = game.mb.cpu.retired_instructions
            chunk_calls = 0
            while game.mb.cpu.cycles - start < frame_cycles * frames:
                before = game.mb.cpu.cycles
                profile.PyBoyLinkSession._step_single_step_chunk(
                    game, CHUNK_CYCLES, stop_on_frame=False
                )
                advanced = game.mb.cpu.cycles - before
                assert advanced >= CHUNK_CYCLES, advanced
                assert advanced < CHUNK_CYCLES + mix.max_instruction_cycles, advanced
                chunk_calls += 1
                assert chunk_calls < 4096, "chunk loop failed to make progress"
            retired = game.mb.cpu.retired_instructions - start_retired
            cycles = game.mb.cpu.cycles - start
        finally:
            game.stop(save=False)
    assert cycles >= frame_cycles
    assert retired >= cycles // mix.max_instruction_cycles
    assert cycles == mix.expected_cycles(retired)


def probe_native_repeated_counts_are_identical():
    mix = profile.MIXES_BY_NAME["hram-io"]
    with TemporaryDirectory(prefix="stepping-profile-probe-") as directory:
        cartridge = profile.author_cartridge(Path(directory))
        for name, _ in profile.PATHS:
            samples = [profile.run_sample(mix, cartridge, name, SHORT_CYCLES) for _ in range(3)]
            counts = {(sample.cycles, sample.retired, sample.python_calls) for sample in samples}
            assert len(counts) == 1, (name, counts)
            summary = profile.summarize(mix, name, samples)
            assert summary["deterministic_counts_agree"] is True
            assert summary["samples"] == 3
            assert summary["seconds_median"] > 0


def _run_native_probe():
    cases = (
        ("declared mixes match the oracle", probe_native_declared_mixes_match_the_oracle),
        ("misdeclared mix is rejected", probe_native_misdeclared_mix_is_rejected),
        (
            "chunk budget holds across a frame",
            probe_native_single_step_chunk_is_budget_bounded_across_a_frame,
        ),
        ("repeated counts are identical", probe_native_repeated_counts_are_identical),
    )
    for label, probe in cases:
        probe()
        print(f"PASS {label}")
    print(f"Stepping-loop profile probe: {len(cases)} passed")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-probe", action="store_true")
    parsed = parser.parse_args()
    if parsed.profile_probe:
        _run_native_probe()
    else:
        parser.error("nothing to do; pass --profile-probe")
