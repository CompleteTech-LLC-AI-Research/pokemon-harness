"""Validator identity, the pinned §2 table, and the read-only §9.1 series check. (#84; split from tests/test_timed_frame_admission.py for #122.)

Pure relocation: every test and helper definition is byte-identical at the AST
level; only the module file changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts import timed_frame_admission as admission
from scripts import timed_frame_window as window
from tests._timed_frame_admission_support import (
    ALLOWED_CPUS,
    FakeClock,
)

# --- the named, pinned gap/admission validator ---------------------------


def test_validator_identity_is_pinned_by_name_and_sha256():
    identity = admission.validator_identity()

    assert identity["name"] == "timed-frame-gap-admission-validator"
    assert identity["module"].endswith(".py")
    assert isinstance(identity["bytes"], int) and identity["bytes"] > 0
    assert len(identity["sha256"]) == 64
    assert set(identity["sha256"]) <= set("0123456789abcdef")
    # The digest is computed from the file's own bytes, so re-deriving it is
    # deterministic and a second engineer can re-run the check.
    assert admission.validator_identity()["sha256"] == identity["sha256"]


def test_validator_admits_only_a_continuous_sixty_second_quiet_run():
    clock = FakeClock()
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    samples = [
        admission.HostSample(index, clock.now + index * 5.0, 1.0, 2.0, 0.5) for index in range(13)
    ]

    window = validator.validate(samples)

    assert window.qualifying is True
    assert window.quiet_seconds == pytest.approx(60.0)
    assert max(window.gaps) <= admission.MAX_SAMPLE_GAP_SECONDS
    assert window.final_sample is samples[-1]
    assert window.reasons == ()


def test_validator_rejects_a_quiet_run_whose_gap_exceeds_the_permitted_maximum():
    clock = FakeClock()
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    samples = [
        admission.HostSample(index, clock.now + index * 5.0, 1.0, 2.0, 0.5) for index in range(7)
    ]
    # A 7.4 s gap breaks continuity, so the run must restart from the next
    # sample rather than counting the whole span.
    samples.append(
        admission.HostSample(len(samples), samples[-1].monotonic_seconds + 7.4, 1.0, 2.0, 0.5)
    )

    window = validator.validate(samples)

    assert window.qualifying is False
    assert window.quiet_seconds == 0.0
    assert any("exceeds" in item for item in window.restarts)
    assert any("below the required" in item for item in window.reasons)


def test_validator_treats_the_boundary_as_a_breach_and_just_under_as_quiet():
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)

    # §5: the window qualifies on strictly less than each bound.
    assert validator.is_quiet(admission.HostSample(0, 0.0, 9.999, 9.999, 3.999)) is True
    # §5.1 S3 breaches on >= each bound.
    assert validator.is_quiet(admission.HostSample(0, 0.0, 10.0, 1.0, 0.5)) is False
    assert validator.is_quiet(admission.HostSample(0, 0.0, 1.0, 10.0, 0.5)) is False
    assert validator.is_quiet(admission.HostSample(0, 0.0, 1.0, 1.0, 4.0)) is False

    assert validator.breached(admission.HostSample(0, 0.0, 10.0, 1.0, 0.5))
    assert validator.breached(admission.HostSample(0, 0.0, 1.0, 10.0, 0.5))
    assert validator.breached(admission.HostSample(0, 0.0, 1.0, 1.0, 4.0))
    assert validator.breached(admission.HostSample(0, 0.0, 9.9, 9.9, 3.9)) == []


def test_validator_restarts_the_window_when_pressure_rose_above_a_bound_then_returned():
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    samples = [admission.HostSample(index, index * 5.0, 1.0, 2.0, 0.5) for index in range(6)]
    samples.append(admission.HostSample(6, 30.0, 18.0, 12.0, 9.0))
    samples.extend(
        admission.HostSample(7 + index, 35.0 + index * 5.0, 1.0, 2.0, 0.5) for index in range(6)
    )

    window = validator.validate(samples)

    # Only the post-breach quiet run counts, and it is 25 s: below 60 s.
    assert window.qualifying is False
    assert window.quiet_seconds == pytest.approx(25.0)


def test_validator_never_admits_on_an_unusable_sample():
    validator = admission.GapAdmissionValidator(allowed_cpus=ALLOWED_CPUS)
    broken = [
        admission.HostSample(index, index * 5.0, None, None, None, ("unreadable host",))
        for index in range(20)
    ]

    window = validator.validate(broken)

    assert window.qualifying is False
    assert window.final_sample is None


# --- §9.1's read-only validation procedure over a retained series ---------


def test_validate_retained_series_is_read_only_and_names_its_validator():
    series = [
        item.as_dict()
        for item in (admission.HostSample(index, index * 5.0, 1.0, 2.0, 0.5) for index in range(13))
    ]

    result = admission.validate_retained_series(series, allowed_cpus=ALLOWED_CPUS)

    assert result["validator"]["name"] == "timed-frame-gap-admission-validator"
    assert result["validator"]["sha256"] == admission.validator_identity()["sha256"]
    assert result["result"]["qualifying"] is True
    assert result["result"]["quiet_seconds"] == pytest.approx(60.0)
    # The input series is untouched: this is a read-only validation.
    assert len(series) == 13


def test_validate_retained_series_rejects_a_saturated_series():
    series = [
        admission.HostSample(index, index * 5.0, 30.0, 25.0, 40.0).as_dict() for index in range(50)
    ]

    result = admission.validate_retained_series(series, allowed_cpus=ALLOWED_CPUS)

    assert result["result"]["qualifying"] is False


def test_read_host_sample_reports_an_unreadable_host_explicitly(tmp_path):
    sample = admission.read_host_sample(
        loadavg_path=tmp_path / "absent-loadavg",
        pressure_path=tmp_path / "absent-pressure",
    )

    assert sample.usable is False
    assert len(sample.problems) == 2


# --- the CLI refuses to invent an admission ------------------------------


def test_cli_prints_identity_and_refuses_to_dispatch_without_a_series(capsys):
    assert admission._main(["--identity-only", "--allowed-cpus", str(ALLOWED_CPUS)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "timed-frame-gap-admission-validator"

    with pytest.raises(SystemExit):
        admission._main(["--allowed-cpus", str(ALLOWED_CPUS)])


def test_cli_validates_a_retained_series_without_running_a_row(capsys):
    series = [
        admission.HostSample(index, index * 5.0, 1.0, 2.0, 0.5).as_dict() for index in range(13)
    ]

    code = admission._main(
        [
            "--allowed-cpus",
            str(ALLOWED_CPUS),
            "--validate-series",
            json.dumps(series),
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["qualifying"] is True


def test_doc_pin_table_matches_the_sources_it_pins():
    """Every in-repo row of the §2 pin table must match the file it pins.

    The table is what a row record is checked against, so a stale byte count or
    digest makes the recorded identity unverifiable.  Rows naming files that are
    not tracked here (the external observers) are not checked: they are pinned to
    their retained copies, not to a path in this tree.
    """

    repo_root = Path(__file__).resolve().parents[1]
    document = repo_root / "docs" / "TIMED_FRAME_DEADLINE_PROTOCOL_20260921.md"
    rows = re.findall(
        r"^\|\s*`([^`]+\.py)`\s*\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`",
        document.read_text(),
        re.MULTILINE,
    )

    pinned = {name: (int(size), digest) for name, size, digest in rows}
    assert pinned, "the §2 pin table must declare the validator and the wrapper"

    for name in (
        "scripts/timed_frame_window.py",
        "scripts/timed_frame_admission.py",
        "scripts/timed_frame_runner.py",
    ):
        assert name in pinned, f"{name} must be pinned in §2"

    for name, (size, digest) in pinned.items():
        source = repo_root / name
        if not source.exists():
            continue
        raw = source.read_bytes()
        assert size == len(raw), f"{name}: §2 says {size} bytes, source is {len(raw)}"
        assert digest == hashlib.sha256(raw).hexdigest(), f"{name}: §2 digest is stale"


def test_the_pinned_validator_identity_is_the_validator_actually_used():
    """``validator_identity()`` must describe the code that makes the decision."""

    identity = admission.validator_identity()
    source = Path(window.__file__).resolve()

    assert identity["name"] == window.GapAdmissionValidator.NAME
    assert identity["module"] == "timed_frame_window.py"
    assert identity["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert identity["bytes"] == len(source.read_bytes())
