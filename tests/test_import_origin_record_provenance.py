"""Acceptance tests for the #534 import-origin guard."""

import base64
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import sys
import types
from collections import UserDict
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
from scripts.check_import_origins import (
    _file_digest,
    _finder_code_file,
    _is_installation_finder,
    _is_recorded_by_an_install,
    _is_within,
    _record_digests,
    _site_packages_roots,
    check_origins,
)
from tests._bootstrap_admission_test_support import make_recorded_module, write_hashless_claim


def test_source_copied_into_site_packages_does_not_certify_an_execed_finder(tmp_path, monkeypatch):
    """Matching bytes in a site-packages file are not load provenance.

    The content check asks whether the finder's code is *in* the file it names.
    That is necessary, but it is not the claim the guard needs: the running
    finder must have been *loaded from* that file.  Writing the finder's own
    source into site-packages and then running
    ``exec(compile(source, that_path, "exec"))`` satisfies every content check
    while the finder has no provenance there at all.

    This row pins the missing half: the defining module must be an entry the
    import system created for that same file, which a bare ``exec`` never
    produces.  Dropping ``_finder_was_imported_from`` from the trust decision
    leaves the content check satisfied and this row fails.
    """

    root = tmp_path / "root"
    package_dir = root / "copied_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "copied_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "copied_pkg" or name.startswith("copied_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    planted = site_root / "copied_source_finder_row.py"
    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "import importlib.util\n"
        "class CopiedSourceFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'copied_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    planted.write_text(source, encoding="utf-8")
    forged = None
    try:
        namespace = {"importlib": importlib, "__name__": "copied_source_finder_row"}
        # The attack: the file really holds these exact bytes, and the code is
        # compiled with that file as co_filename -- but it is never imported.
        exec(compile(source, str(planted), "exec"), namespace)  # noqa: S102
        forged = namespace["CopiedSourceFinder"]
        forged.__module__ = "copied_source_finder_row"

        code = forged.find_spec.__func__.__code__
        assert code.co_filename == str(planted)
        assert Path(planted).is_file(), "the planted file must really exist"
        assert any(
            _is_within(Path(planted), root_, strict=False) for root_ in _site_packages_roots()
        ), "the planted file must really sit inside site-packages"
        # The premise: content matching succeeds and is still not enough.
        assert origins._code_matches_source(forged.find_spec, Path(planted)), (
            "this row needs the content check to pass so it isolates provenance"
        )
        assert "copied_source_finder_row" not in sys.modules, (
            "a bare exec must not create a module entry"
        )

        assert not _is_installation_finder(forged), (
            "code that merely matches a site-packages file was never loaded from it"
        )
        sys.meta_path.insert(0, forged)
        report = check_origins(root, ("copied_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("copied_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        if forged is not None and forged in sys.meta_path:
            sys.meta_path.remove(forged)
        planted.unlink(missing_ok=True)


def test_a_foreign_portion_that_cannot_be_rendered_is_still_reported(tmp_path, monkeypatch):
    """The FAIL detail is built from the same untrusted data it reports.

    A finding names the foreign ``__path__`` entries it found, and rendering one
    of those calls ``str()`` on an object the interpreter -- not this guard --
    controls.  If that rendering raises, the finding is lost and the traceback
    that replaces it is precisely the failure this module exists to prevent:
    the operator sees a crash instead of the FAIL naming the offending package.

    This row pins the reporting half.  ``_describe`` is the only place that
    coerces untrusted data to text, so the detail must survive an object whose
    ``__str__`` and ``__repr__`` both raise, and the status must still be FAIL.
    """

    root = tmp_path / "root"
    package_dir = root / "hostile_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "hostile_pkg" or name.startswith("hostile_pkg."):
            del sys.modules[name]

    hostile = __import__("hostile_pkg")

    class Unprintable:
        """A path-like that cannot be converted *or* rendered."""

        def __fspath__(self) -> str:
            raise RuntimeError("boom-fspath")

        def __str__(self) -> str:
            raise RuntimeError("boom-str")

        def __repr__(self) -> str:
            raise RuntimeError("boom-repr")

    # A portion the guard cannot resolve *and* cannot render: the reason text
    # is itself built from the object, so this exercises both coercions.
    hostile.__path__ = [Unprintable(), tmp_path / "outside" / "hostile_pkg"]

    report = check_origins(root, ("hostile_pkg",))

    assert report["status"] == "FAIL", report
    detail = report["packages"][0]["detail"]
    assert detail, "a FAIL must carry a detail naming what was wrong"
    # The rendering degrades to a placeholder; the finding is not lost.
    assert "unprintable" in detail or "not a usable path" in detail, detail
    # The report must stay JSON-serializable, since the CLI emits it verbatim.
    json.dumps(report)


def test_a_forged_module_and_spec_do_not_certify_an_uncertified_finder(tmp_path, monkeypatch):
    """``sys.modules[name].__spec__`` is attacker-writable, so it is no evidence.

    The previous attempt to supply load provenance asked the defining module's
    spec to name the file.  Independent review showed the whole pair can be
    written by hand: build a module with ``types.ModuleType``, attach a spec
    from ``importlib.util.spec_from_file_location`` -- which sets
    ``has_location=True`` -- and every check the guard made is satisfied while
    the import system never loaded anything from that file.  The guard reported
    PASS, ``cli_rc`` was 0, and a foreign submodule loaded afterwards.

    This row pins the replacement: provenance comes from install records on disk
    (``RECORD`` hash, or a ``.pth`` that imports the module), which a running
    process cannot rewrite into a different claim.
    """

    root = tmp_path / "root"
    package_dir = root / "forged_spec_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    foreign = tmp_path / "outside" / "forged_spec_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "forged_spec_pkg" or name.startswith("forged_spec_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    planted = site_root / "forged_spec_finder_row.py"
    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "import importlib.util\n"
        "class ForgedSpecFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'forged_spec_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    planted.write_text(source, encoding="utf-8")
    forged = None
    try:
        namespace = {"__name__": "forged_spec_finder_row"}
        exec(compile(source, str(planted), "exec"), namespace)  # noqa: S102
        forged = namespace["ForgedSpecFinder"]

        # The forgery: a module the import system never created, carrying a
        # file-location spec pointing at the planted file.
        planted_module = types.ModuleType("forged_spec_finder_row")
        planted_module.__file__ = str(planted)
        planted_module.__spec__ = importlib.util.spec_from_file_location(
            "forged_spec_finder_row", planted
        )
        sys.modules["forged_spec_finder_row"] = planted_module
        try:
            # The premise: every in-memory signal the earlier check relied on is
            # satisfied, so this row really isolates the new provenance rule.
            assert planted_module.__spec__.has_location is True
            assert planted_module.__spec__.origin == str(planted)
            assert Path(planted).is_file()
            assert any(
                _is_within(Path(planted), root_, strict=False) for root_ in _site_packages_roots()
            ), "the planted file must really sit inside site-packages"
            assert _finder_code_file(forged) is not None, (
                "this row needs the content check to pass so it isolates provenance"
            )

            assert not _is_installation_finder(forged), (
                "a hand-written module and spec are not load provenance"
            )
            sys.meta_path.insert(0, forged)
            report = check_origins(root, ("forged_spec_pkg",))

            assert report["status"] == "FAIL", report
            assert "meta_path" in report["packages"][0]["detail"], report
            json.dumps(report)

            leaked = importlib.import_module("forged_spec_pkg.leaked")
            assert leaked.ORIGIN == "foreign"
        finally:
            sys.modules.pop("forged_spec_finder_row", None)
    finally:
        if forged is not None and forged in sys.meta_path:
            sys.meta_path.remove(forged)
        planted.unlink(missing_ok=True)


def test_a_recorded_file_stops_being_recorded_when_its_bytes_change():
    """``RECORD`` is provenance only while the bytes still hash to what it says.

    The disk-recorded channel is what replaces the forgeable in-memory one, so
    the part of it that actually carries the claim is the *hash*: a file being
    listed in a ``RECORD`` says only that some install laid down that path, not
    that the bytes now there are the ones it laid down.

    Treating mere presence in ``RECORD`` as provenance was measured to survive
    its own mutation: replacing the digest comparison with a membership test
    left every row in the suite green.  This row pins the difference.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_hash_pin_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_hash_pin_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        recorded = dist_info / "RECORD"
        digest = _file_digest(planted)
        assert digest is not None, "the planted file must be readable"
        recorded.write_text(f"pokemon_hash_pin_row_module.py,sha256={digest},6\n", encoding="utf-8")

        assert _is_recorded_by_an_install(planted, root), (
            "a file whose bytes match its RECORD entry is install-recorded"
        )

        planted.write_text("VALUE = 'tampered'\n", encoding="utf-8")
        assert not _is_recorded_by_an_install(planted, root), (
            "the same path with different bytes is no longer what the install wrote"
        )
    finally:
        sys.modules.pop("pokemon_hash_pin_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


@pytest.mark.parametrize("algorithm", ["sha512", "sha384", "blake2b", "md5"])
def test_a_record_naming_a_non_sha256_algorithm_still_establishes_provenance(algorithm):
    """``RECORD`` permits any algorithm ``hashlib`` guarantees, so honour it.

    The guard used to skip every row whose label was not ``sha256``, which
    meant ``_record_digests`` found no claim for the file and
    ``_is_recorded_by_an_install`` refused it.  Wheel may legitimately ship a
    SHA-512 ``RECORD``, so that is a false refusal of a genuine install: the
    origin is well attested and the guard reports it as unproven.

    The label is honoured rather than trusted, so this is wider than believing
    whatever the record says and narrower than discarding the row: the digest
    is recomputed under the named algorithm and must match.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_algo_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_algo_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        digest = _file_digest(planted, algorithm)
        assert digest is not None, f"the planted file must be readable as {algorithm}"
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_algo_row_module.py,{algorithm}={digest},{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert _is_recorded_by_an_install(planted, root), (
            f"a genuine {algorithm} RECORD must establish provenance, not refuse it"
        )

        planted.write_text("VALUE = 'tampered'\n", encoding="utf-8")
        assert not _is_recorded_by_an_install(planted, root), (
            f"a {algorithm} record must still pin the bytes it claims"
        )
    finally:
        sys.modules.pop("pokemon_record_algo_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


@pytest.mark.parametrize(
    ("algorithm", "output_length"), [("shake_128", 16), ("shake_128", 32), ("shake_256", 64)]
)
def test_a_record_naming_a_variable_length_algorithm_still_establishes_provenance(
    algorithm, output_length
):
    """SHAKE is the one ``RECORD`` algorithm that needs an output length.

    ``shake_128`` and ``shake_256`` are extendable-output functions, so
    ``hashlib`` guarantees them and ``RECORD`` may name either -- but their
    ``digest()`` takes a required length where every fixed-size algorithm takes
    none.  Calling it bare raises ``TypeError``, which the guard's own
    ``except`` turns into ``None``: the file then has no claim and is refused.

    That is precisely the false refusal of a genuine install that honouring the
    label exists to remove, so the record's own digest length is what the file
    is hashed at.  Honouring the label is still conditional on recomputing it:
    a mangled digest, and a record whose length disagrees with the one it
    claims, are both still refused.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_shake_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_shake_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        data = planted.read_bytes()
        hasher = hashlib.new(algorithm)
        hasher.update(data)
        digest = base64.urlsafe_b64encode(hasher.digest(output_length)).rstrip(b"=").decode()
        assert len(base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4))) == output_length, (
            "the premise: the record carries a digest of exactly this length"
        )
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_shake_row_module.py,{algorithm}={digest},{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert _is_recorded_by_an_install(planted, root), (
            f"a genuine {algorithm} RECORD must establish provenance at its own length"
        )

        # A record whose digest is the right algorithm at the right length but
        # the wrong *bytes* is still refused: honouring the label never means
        # believing it, only recomputing under it.
        flipped = "B" if digest[0] != "B" else "C"
        other_digest = flipped + digest[1:]
        recorded.write_text(
            f"pokemon_record_shake_row_module.py,{algorithm}={other_digest},"
            f"{planted.stat().st_size}\n",
            encoding="utf-8",
        )
        assert not _is_recorded_by_an_install(planted, root), (
            "a record whose digest does not match the bytes must be refused"
        )
    finally:
        sys.modules.pop("pokemon_record_shake_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


@pytest.mark.parametrize("algorithm", ["shake_128", "sha256"])
def test_a_record_whose_digest_length_is_absurd_is_refused_without_allocating_it(algorithm):
    """A ``RECORD`` may not choose the size of an allocation the guard makes.

    The digest's own length says how large a ``shake_128`` output was, so the
    guard has to read that length before hashing.  Reading it by *decoding* the
    claim is the trap: a planted row naming a gigabyte of output would make the
    decode allocate a gigabyte before anything was compared, turning a refused
    file into an out-of-memory failure.

    This row claims far more output than any digest can be, and requires the
    guard to refuse it.  The bound is textual, so it costs nothing to apply and
    nothing to exceed with a legitimate record.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_huge_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_huge_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        absurd = "A" * 2_000_000
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_huge_row_module.py,{algorithm}={absurd},{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert not _is_recorded_by_an_install(planted, root), (
            "a record claiming an impossible digest must attest nothing"
        )
    finally:
        sys.modules.pop("pokemon_record_huge_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_record_whose_label_does_not_match_its_digest_is_refused():
    """The algorithm label is part of the claim, so a mismatch must fail.

    Honouring the label is only safe because the digest is recomputed under
    it.  A row reading ``sha512=<a sha256 digest>`` is therefore not evidence
    of anything: trusting the label without recomputing would attest it, and
    discarding labelled rows would reintroduce the false refusal above.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_mislabel_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_mislabel_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        sha256_digest = _file_digest(planted, "sha256")
        assert sha256_digest is not None
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_mislabel_row_module.py,sha512={sha256_digest},"
            f"{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert not _is_recorded_by_an_install(planted, root), (
            "a sha256 digest labelled sha512 is not a claim about these bytes"
        )
    finally:
        sys.modules.pop("pokemon_record_mislabel_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_record_naming_an_unknown_algorithm_attests_nothing():
    """A label this interpreter cannot compute must fail the whole set.

    Falling back to a default algorithm would turn an unverifiable claim into
    a passing one, which is exactly the fail-open direction the guard exists to
    prevent.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_unknown_algo_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_unknown_algo_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_unknown_algo_row_module.py,notarealalgo=AAAA,"
            f"{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert _file_digest(planted, "notarealalgo") is None, (
            "the premise: an unknown algorithm has no digest to compare"
        )
        assert not _is_recorded_by_an_install(planted, root), (
            "a claim this interpreter cannot check must not certify the file"
        )
    finally:
        sys.modules.pop("pokemon_record_unknown_algo_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_quoted_record_path_containing_a_comma_still_establishes_provenance():
    """``RECORD`` is CSV, and a quoted field's comma must not truncate its name.

    The file name is parsed by splitting from the right -- size, then digest,
    then everything remaining is the name -- because a name may legally contain
    a comma.  But pip writes such a field *quoted*, so a name that survives the
    split still arrives wrapped in quotes, and joining the raw text onto the
    site-packages root builds a path no record ever listed.  The digest is then
    compared against a file that does not exist, so a genuinely installed
    finder whose location contains a comma silently loses its provenance and is
    refused: a false red, in exchange for no security gain.

    This row pins the decode.  The writer here is ``csv.writer``, the same one
    pip uses, so the bytes under test are bytes pip really emits.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_csv_path_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon,comma_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        digest = _file_digest(planted)
        assert digest is not None
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(
            [planted.name, f"sha256={digest}", str(planted.stat().st_size)]
        )
        (dist_info / "RECORD").write_text(buffer.getvalue(), encoding="utf-8")

        assert '"' in (dist_info / "RECORD").read_text(encoding="utf-8"), (
            "the premise: a comma in the name forces a quoted CSV field"
        )
        assert _is_recorded_by_an_install(planted, root), (
            "an installed file whose name contains a comma keeps its provenance"
        )
    finally:
        sys.modules.pop(planted.stem, None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_recorded_path_that_another_record_contradicts_is_not_attested():
    """Every ``RECORD`` claim about a path must hold, not just the first one.

    ``_record_digests`` maps a path to the digests every record claims for it.
    Keeping only one claim would let an attacker write a record that agrees
    with their own bytes and rely on ordering to win against the install that
    actually laid the file down.  A file whose installs disagree about its
    contents is not consistently attested by any of them, and the safe answer
    is to refuse.

    This row kills a mutant that keeps whichever claim sorts *last*, and pins
    the collection of both claims before the decision is made.  It cannot kill
    a keep-the-first mutant, and that is a property of the rule rather than a
    gap in the row: on a genuine conflict the first-claim mutant also refuses
    here.  Distinguishing those two would need a case where the first claim
    *agrees* with the file and a later one does not, which is the far more
    dangerous ordering and is what the ``forgery`` below deliberately is not.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_conflict_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_conflict_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        digest = _file_digest(planted)
        assert digest is not None
        record = dist_info / "RECORD"
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(
            [planted.name, f"sha256={digest}", str(planted.stat().st_size)]
        )
        record.write_text(buffer.getvalue(), encoding="utf-8")
        assert _is_recorded_by_an_install(planted, root), "the premise: attested"

        # A second, disagreeing record for the same path.  Its digest is chosen
        # to sort *before* the real one, so a keep-only-the-first-claim mutation
        # would pick the forgery and this row would wrongly survive.
        forgery = "0" * 43 + "="
        assert forgery < digest, "the forgery must sort first to isolate the rule"
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(f"{planted.name},sha256={forgery},{planted.stat().st_size}\n")

        # Assert what the rule actually decides, not just the refusal: a
        # keep-only-the-first-claim mutant also refuses here, so asserting only
        # "not recorded" would let that mutant survive this row unchallenged.
        # The premise already established that the real digest *is* claimed,
        # so "one claim matches and one does not" is the state under test.
        claims = _record_digests(root).get(str(planted))
        assert claims is not None and len(claims) == 2, (
            f"the premise: two records disagree about this path, got {claims}"
        )
        assert ("sha256", digest) in claims and ("sha256", forgery) in claims, (
            "both claims must be collected before the decision is made"
        )
        assert not _is_recorded_by_an_install(planted, root), (
            "installs that disagree about a file's bytes attest to nothing"
        )
    finally:
        sys.modules.pop(planted.stem, None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_the_guard_refuses_rather_than_traceback_on_any_escape(tmp_path, monkeypatch):
    """``check_origins`` must always answer, even for a read nobody guarded.

    Thirteen review rounds across #558 and #559 each widened one more
    hostile read, and several of them repaired an *adjacent* read rather
    than the reported one.  Widening read N therefore never proved read N+1
    was safe, and this module cannot enumerate every value an attacker
    controls: ``sys``, ``site``, ``sys.modules``, the meta-path and the
    on-disk install layout all sit outside the process's own control.

    So the entry point carries the guarantee instead: anything escaping the
    analysis becomes the same machine-readable FAIL every other refusal
    produces.  A guard that answers with a traceback is fail-open, because a
    caller gating on ``status`` sees nothing at all.

    ``KeyboardInterrupt`` and ``SystemExit`` must still escape, so an
    operator can always stop the run.
    """

    class Exploding(BaseException):
        pass

    def explode(*_args, **_kwargs):
        raise Exploding("novel read boom")

    monkeypatch.setattr(origins, "_allowed_roots", explode)

    report = check_origins(tmp_path, ("anything",))

    assert report["status"] == "FAIL", report
    assert report["packages"][0]["package"] == "<guard>"
    assert "could not complete" in report["packages"][0]["detail"]

    def raise_interrupt(*_args, **_kwargs):
        raise interrupt

    for interrupt in (KeyboardInterrupt(), SystemExit()):
        monkeypatch.setattr(origins, "_allowed_roots", raise_interrupt)
        with pytest.raises(type(interrupt)):
            check_origins(tmp_path, ("anything",))


def test_source_editable_accepts_only_declared_import_paths(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    module_path = root / "vendor" / "pyboy-src" / "pyboy" / "core" / "serial.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("SERIAL = True\n", encoding="utf-8")
    module = types.ModuleType("pyboy.core.serial")
    module.__file__ = str(module_path)
    site = tmp_path / "site-packages"
    site.mkdir()

    editable_root = [root]

    class EditableDistribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"url": editable_root[0].as_uri(), "dir_info": {"editable": True}})

    monkeypatch.setattr(origins, "_site_packages_roots", lambda: [site])
    monkeypatch.setattr(origins, "_distribution", lambda _name: EditableDistribution())
    expected = {"pyboy.core.serial": module_path}
    report = origins.attest_selected_module_owners(
        UserDict({"pyboy.core.serial": module}),
        UserDict({"pyboy.core.serial": "pokered-harness"}),
        project_root=root,
        editable_module_paths=UserDict(expected),
    )
    assert report["ok"], report
    assert report["modules"]["pyboy.core.serial"]["basis"] == "editable-source-path"

    foreign_path = root / "vendor" / "pyboy-src-copy" / "pyboy" / "core" / "serial.py"
    report = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module},
        {"pyboy.core.serial": "pokered-harness"},
        project_root=root,
        editable_module_paths={"pyboy.core.serial": foreign_path},
    )
    assert not report["ok"], report
    assert "declared editable path" in " ".join(report["errors"])
    editable_root[0] = root / "other-checkout"
    report = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module},
        {"pyboy.core.serial": "pokered-harness"},
        project_root=root,
        editable_module_paths=expected,
    )
    assert not report["ok"], report
    assert "direct_url" in " ".join(report["errors"])
    editable_root[0] = root
    write_hashless_claim(site, "pokered-harness", module)
    report = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module},
        {"pyboy.core.serial": "pokered-harness"},
        project_root=root,
        editable_module_paths=expected,
    )
    assert not report["ok"], report
    assert "hashless or malformed RECORD claim" in " ".join(report["errors"])
    empty = origins.attest_selected_module_owners({}, {}, project_root=root)
    assert not empty["ok"], empty
    mismatched = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module}, {}, project_root=root
    )
    assert not mismatched["ok"], mismatched


def test_wheel_record_rejects_overwritten_or_unowned_module(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    module = make_recorded_module(tmp_path, "pyboy.core.serial", "pokered-harness", b"serial core")
    monkeypatch.setattr(origins, "_site_packages_roots", lambda: [site])
    expected = {"pyboy.core.serial": "pokered-harness"}
    report = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module}, expected, project_root=tmp_path
    )
    assert report["ok"], report
    assert report["modules"]["pyboy.core.serial"]["basis"] == "record"

    Path(module.__file__).write_bytes(b"overwritten serial core")
    report = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module}, expected, project_root=tmp_path
    )
    assert not report["ok"], report
    assert "differ from the expected RECORD digest" in " ".join(report["errors"])

    record = site / "pokered-harness-1.0.dist-info" / "RECORD"
    record.unlink()
    write_hashless_claim(site, "pokered-harness", module)
    report = origins.attest_selected_module_owners(
        {"pyboy.core.serial": module}, expected, project_root=tmp_path
    )
    assert not report["ok"], report
    assert "hashless or malformed RECORD claim" in " ".join(report["errors"])


def test_native_requires_fork_records_and_retains_harness_owner(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    names = (
        "pyboy",
        "pyboy.pyboy",
        "pyboy.utils",
        "pyboy.core.mb",
        "pyboy.core.serial",
        "pyboy.link",
    )
    modules = {name: make_recorded_module(tmp_path, name, "pyboy", name.encode()) for name in names}
    modules["pokered_harness"] = make_recorded_module(
        tmp_path, "pokered_harness", "pokered-harness", b"harness"
    )
    owners = {name: "pyboy" for name in names}
    owners["pokered_harness"] = "pokered-harness"
    monkeypatch.setattr(origins, "_site_packages_roots", lambda: [site])

    report = origins.attest_selected_module_owners(modules, owners, project_root=tmp_path)
    assert report["ok"], report
    assert report["modules"]["pyboy.link"]["record_owner"] == "pyboy"
    assert report["modules"]["pokered_harness"]["record_owner"] == "pokered-harness"

    editable_root = tmp_path / "native-editable-checkout"
    editable_modules = {
        name: make_recorded_module(editable_root, name, "pyboy", name.encode()) for name in names
    }
    harness_path = editable_root / "src" / "pokered_harness" / "__init__.py"
    harness_path.parent.mkdir(parents=True)
    harness_path.write_text("NATIVE_HARNESS = True\n", encoding="utf-8")
    harness_module = types.ModuleType("pokered_harness")
    harness_module.__file__ = str(harness_path)
    editable_modules["pokered_harness"] = harness_module

    class EditableHarnessDistribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"url": editable_root.as_uri(), "dir_info": {"editable": True}})

    native_site = editable_root / "site-packages"
    monkeypatch.setattr(origins, "_site_packages_roots", lambda: [native_site])
    monkeypatch.setattr(origins, "_distribution", lambda _name: EditableHarnessDistribution())
    report = origins.attest_selected_module_owners(
        editable_modules,
        owners,
        project_root=editable_root,
        editable_module_paths={"pokered_harness": harness_path},
    )
    assert report["ok"], report
    assert report["modules"]["pokered_harness"]["basis"] == "editable-source-path"

    owners["pokered_harness"] = "pyboy"
    monkeypatch.setattr(origins, "_site_packages_roots", lambda: [site])
    report = origins.attest_selected_module_owners(modules, owners, project_root=tmp_path)
    assert not report["ok"], report
    assert "pokered_harness: module file is recorded by the wrong distribution" in report["errors"]
