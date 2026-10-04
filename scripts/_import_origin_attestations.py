"""RECORD and .pth evidence used by the import-origin guard.

Each filesystem and hashing operation stays fail closed. The facade API is
passed explicitly where a helper calls another guard seam, so monkeypatches
on ``scripts.check_import_origins`` continue to affect the same lookup.
"""

from __future__ import annotations

import base64
import csv
import hashlib
from pathlib import Path
from typing import Any

OriginApi = dict[str, Any]

# ``shake_128`` and ``shake_256`` are extendable-output functions: ``hashlib``
# guarantees them, so ``RECORD`` may legitimately name either, but their
# ``digest()`` requires an output length where every fixed-size algorithm takes
# none.  Detecting them by name keeps ``_file_digest`` from probing with a
# length it would then have to catch an exception to undo.
_VARIABLE_LENGTH_ALGORITHMS = frozenset({"shake_128", "shake_256"})

# SHAKE output is unbounded by definition, so no cap derived from the algorithm
# set would bound it; this one is a deliberate refusal to size an allocation off
# untrusted text.  1024 characters is 768 decoded bytes, far above any fixed
# digest pip writes -- its ``RECORD`` writer emits ``sha256`` only -- so the
# bound costs no realistic install anything while capping what a planted record
# can ask the guard to allocate.
_MAX_RECORDED_DIGEST_LENGTH = 1024

# ``_file_digest`` encodes URL-safe base64, so the alphabet here is the URL-safe
# one: a digest can legitimately contain ``-`` and ``_``, and rejecting them
# would refuse the very rows this length exists to honour.
_BASE64_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _decoded_digest_length(digest: str) -> int | None:
    """Return how many bytes an unpadded base64 ``digest`` encodes, or ``None``.

    The length is derived arithmetically rather than by decoding, so an
    attacker-sized claim is never materialised in memory.  ``None`` means the
    text is not a well-formed unpadded base64 digest of a plausible size, and
    the caller treats that as a claim it cannot verify.
    """

    if not digest or len(digest) > _MAX_RECORDED_DIGEST_LENGTH:
        return None
    stripped = digest.rstrip("=")
    if not stripped or any(character not in _BASE64_ALPHABET for character in stripped):
        return None
    remainder = len(stripped) % 4
    if remainder == 1:
        # A single leftover base64 character cannot encode any whole byte.
        return None
    # Padding may only bring the text up to a multiple of four, and at most two
    # characters of it.
    if len(digest) != len(stripped) and (len(digest) % 4 or len(digest) - len(stripped) > 2):
        return None
    decoded = len(stripped) * 3 // 4
    return decoded if decoded else None


def _record_attestations(
    root: Path, *, api: OriginApi
) -> dict[str, dict[str, set[tuple[str, str]]]]:
    """Return every ``RECORD`` claim about every file, keyed by attesting owner.

    ``RECORD`` is written at install time and names each installed file with
    the hash of its contents.  Keys are resolved paths, so a caller compares
    resolved paths on both sides and cannot be misled by a relative or
    ``..``-laden spelling of the same file.

    The value is keyed by the attesting *owner* -- the name of the
    ``*.dist-info`` directory the ``RECORD`` lives in -- because proving that
    two files came from one install means knowing which distribution claimed
    each of them, which a bare path key cannot express.

    The value is a *set* because several records may claim the same path.  The
    caller has to satisfy all of them: if one record disagrees about what a
    file contains, the file is not consistently attested by an install, and
    picking whichever claim sorted first would let a planted record decide.

    A missing or unreadable ``RECORD`` contributes nothing rather than
    aborting: an install that cannot be attested is refused, and refusing is
    the safe direction.
    """

    digests: dict[str, dict[str, set[tuple[str, str]]]] = {}
    try:
        records = sorted(root.glob("*.dist-info/RECORD"))
    except (OSError, ValueError):
        return digests
    for record in records:
        owner = record.parent.name
        try:
            text = record.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            # ``RECORD`` is CSV with the shape ``path,sha256=<digest>,size``.
            # The path may legally contain a comma, so this is parsed from the
            # right: the size is the last field, the digest the one before it,
            # and the name is everything remaining.  Splitting on every comma
            # would truncate a name that holds one, and a truncated name
            # resolves to a path no record ever listed -- so a genuinely
            # installed finder whose location contains a comma would lose its
            # provenance and be refused.
            head, _, _size = line.rpartition(",")
            name_field, separator, digest = head.rpartition(",")
            if not separator or not name_field:
                continue
            # The name may be a quoted CSV field, and a quoted field may hold
            # its own escaped quotes.  Leaving them attached builds a path no
            # record ever listed, so the digest would be compared against a
            # file that does not exist and a genuinely installed finder would
            # lose its provenance.  Only an actually-quoted field is decoded,
            # so a name that merely *starts* with a quote is left alone.
            if len(name_field) >= 2 and name_field[0] == '"' and name_field[-1] == '"':
                try:
                    decoded = next(csv.reader([name_field]))
                except (csv.Error, StopIteration):
                    continue
                if not decoded:
                    continue
                name_field = decoded[0]
            algorithm, _, expected = digest.partition("=")
            algorithm = algorithm.lower()
            # A record names the algorithm that produced its digest, so the
            # label is part of the claim rather than decoration.  ``RECORD``
            # permits any algorithm ``hashlib`` guarantees and wheel may ship
            # SHA-512, so insisting on ``sha256`` here refused a genuine
            # install outright.  Storing the pair lets the caller recompute
            # under the algorithm the record actually names, which is both
            # wider than trusting the label blindly and narrower than
            # discarding it.
            if not algorithm or not expected or not name_field:
                continue
            # An empty hash field is how ``RECORD`` marks a file it installed
            # without recording contents (a generated script, a compiled
            # extension).  That is not evidence of provenance, so it
            # contributes no claim and the path stays unattested.
            resolved = api["_safe_resolve"](root / name_field)
            if resolved is not None:
                # Collect *every* claim about a path rather than keeping the
                # first.  Two records may disagree about the same file, and
                # the caller must not be able to win by writing one that
                # happens to sort first.
                digests.setdefault(str(resolved), {}).setdefault(owner, set()).add(
                    (algorithm, expected)
                )
    return digests


def _record_digests(root: Path, *, api: OriginApi) -> dict[str, set[tuple[str, str]]]:
    """Return every ``RECORD`` claim about every file, ignoring which owner made it.

    A projection of ``_record_attestations`` for callers that only need to know
    *whether* the claims agree with the file on disk.  Callers that have to
    prove two files came from the *same* install must use
    ``_record_attestations`` instead, because collapsing the owner away loses
    the one thing that pairing depends on.
    """

    return {
        path: {claim for claims in owners.values() for claim in claims}
        for path, owners in api["_record_attestations"](root).items()
    }


def _file_digest(
    candidate: Path, algorithm: str = "sha256", expected_length: int | None = None
) -> str | None:
    """Return the URL-safe base64 digest of ``candidate``, or ``None``.

    The digest is recomputed under ``algorithm`` rather than a hardcoded
    SHA-256, because ``RECORD`` names the algorithm that produced each claim.
    Returns ``None`` for an unknown or unavailable algorithm, so a record
    naming something this interpreter cannot compute attests nothing.

    ``expected_length`` is the caller's own decoded digest length.  It is
    needed only by ``shake_128``/``shake_256``, whose ``digest()`` requires an
    output size, and is ignored by every fixed-size algorithm.
    """

    try:
        data = candidate.read_bytes()
    except (OSError, ValueError):
        return None
    try:
        hasher = hashlib.new(algorithm)
    except (ValueError, TypeError):
        return None
    try:
        hasher.update(data)
        # ``shake_128`` and ``shake_256`` are the two algorithms ``RECORD``
        # permits that ``hashlib`` guarantees but whose ``digest()`` takes a
        # required output length.  Calling it bare raises ``TypeError``, which
        # the guard would otherwise swallow into ``None`` -- so a perfectly
        # valid ``shake_128=`` row would attest nothing and the file would be
        # refused.  That is the same false refusal of a genuine install this
        # change exists to remove, so the digest is requested at the record's
        # own encoded length.  A length the algorithm rejects still yields
        # ``None``, which fails closed as before.
        if algorithm in _VARIABLE_LENGTH_ALGORITHMS:
            if expected_length is None:
                return None
            raw = hasher.digest(expected_length)
        else:
            raw = hasher.digest()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    except (ValueError, TypeError):
        return None


def _is_recorded_by_an_install(source_file: Path, root: Path, *, api: OriginApi) -> bool:
    """Return whether an installed distribution recorded this exact file.

    The ``RECORD`` hash is what makes the check a statement about provenance
    rather than about location: an attacker who plants a file in
    site-packages has to also match a hash recorded by an install, and the
    record is what the installer wrote when it laid the file down.

    Every recorded claim must match, so an extra record asserting different
    content for the same path cannot be ignored: keeping only one claim --
    whichever sorted first -- would let a planted record decide.  Each claim
    is recomputed with the algorithm it names, so a row reading
    ``sha512=<a sha256 digest>`` fails rather than being quietly honoured.
    """

    expected = api["_record_digests"](root).get(str(source_file))
    if not expected:
        return False
    for algorithm, digest in expected:
        # ``shake_128``/``shake_256`` need an output length, and the record
        # states it implicitly by how long the digest it carries is.  The
        # length is derived from that claim rather than assumed, so the file is
        # hashed at exactly the size the record says it was hashed at, and a
        # record whose digest is too short for its algorithm simply fails to
        # match instead of being honoured at some other size.
        #
        # The length is computed from the base64 *text*, never by decoding it.
        # A ``RECORD`` is attacker-writable, and decoding the claim first would
        # allocate whatever the claim asked for -- a row naming a gigabyte of
        # output would exhaust memory before the guard ever compared anything.
        # A digest string also bounds itself: nothing legitimate is longer
        # than the largest fixed digest plus its encoding, so the cap is not a
        # policy choice but a refusal to size an allocation off untrusted text.
        recorded_length = api["_decoded_digest_length"](digest)
        actual = api["_file_digest"](source_file, algorithm, recorded_length)
        if actual is None or actual != digest:
            return False
    return True


def _is_imported_by_a_pth(source_file: Path, root: Path, *, api: OriginApi) -> bool:
    """Return whether a ``.pth`` file in ``root`` imports the defining module.

    ``_virtualenv`` and the ``__editable__`` shim are dropped into
    site-packages and activated by a ``.pth`` file that imports them by name,
    so that pairing is what a real environment shim looks like.

    A ``.pth`` naming the module is not on its own evidence of anything: an
    attacker who can write into site-packages writes both halves.  The pair is
    only trustworthy when one distribution vouches for both of them, so the
    ``.pth`` and the module have to be attested by the *same* ``RECORD``.  An
    editable install records exactly that -- ``pip install -e`` lists the
    ``__editable__.<dist>-<ver>.pth`` and its finder module in one dist-info
    RECORD -- whereas a planted pair is owned by nothing.

    Two earlier rounds of this function were rejected in review, and each is
    why one of these rules exists.  Attesting only the ``.pth`` let an
    attacker plant just the module beside a ``.pth`` a genuine install already
    recorded, so the attacker never had to plant the ``.pth`` itself.
    Consulting only *which* owner claimed the ``.pth`` then let that recorded
    file be rewritten after its install: a ``RECORD`` entry is a historical
    claim, so the bytes change while the record does not, and the file starts
    importing whatever the attacker chose.  So the shared owner has to match
    *both* files as they are on disk now, which is the same rule
    ``_is_recorded_by_an_install`` applies to a single file.
    """

    if source_file.name == "__init__.py":
        stem = source_file.parent.name
    else:
        stem = source_file.stem
    if not stem or not stem.isidentifier():
        return False
    try:
        entries = sorted(root.glob("*.pth"))
    except (OSError, ValueError):
        return False
    needle = f"import {stem}"
    owners = api["_record_attestations"](root)
    module_owners = owners.get(str(source_file))
    if not module_owners:
        # No install ever claimed the module itself, so no owner can vouch for
        # the pairing whatever the ``.pth`` says.
        return False
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if needle not in text:
            continue
        entry_owners = owners.get(str(entry))
        if not entry_owners:
            continue
        if not any(owner in entry_owners for owner in module_owners):
            continue
        # The shared owner has to describe the bytes that are there now, for
        # both halves.  A rewritten ``.pth`` keeps its owner but loses the
        # match, and the pairing stops reading as attested.
        if api["_is_recorded_by_an_install"](entry, root) and api["_is_recorded_by_an_install"](
            source_file, root
        ):
            return True
    return False
