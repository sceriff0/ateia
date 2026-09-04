"""No tracked file may carry the author placeholder.

WHAT THIS IS FOR. Eight author-placeholder markers (the MARKER constant below;
spelled in two halves everywhere in this file so the scan cannot find itself --
which it did, in CI, the first time) survived into a 1.0.0 tree, across
six files: LICENSE, CITATION.cff (twice), README.md (twice), mkdocs.yml (twice)
and nextflow.config. A placeholder in a LICENSE is not a cosmetic defect -- it is
a licence with no grantor -- and one in CITATION.cff propagates into every
citation manager and into the Zenodo record for the tagged release. None of them
had a check, so each was found by reading.

WHY `git ls-files` AND NOT A GLOB. The enumerator has to be the set of files that
SHIP. A `Path.rglob('*')` over the working tree walks .venv-bench/, valis_lib/,
tests/testdata/ and every __pycache__ -- thousands of files nobody publishes -- and
would either be unbearably slow or acquire an exclusion list that is itself a place
for a real file to hide. `git ls-files` is the shipped set, by definition, and it
needs no exclusions.

WHY THE MARKER IS BUILT AT RUNTIME. This file is itself tracked. Written out
literally, the string below would match this module and the guard would need an
exemption for itself -- and a guard with a self-exemption is one refactor away from
exempting everything. Concatenating two fragments means the bytes of this file
never contain the marker, so no exemption exists to widen.

SCOPE NOTE: `TODO(release)` is DELIBERATELY NOT MATCHED. CITATION.cff carries one,
for the DOI, and it is legitimately open until the tagged release is archived. The
two markers mean different things and only one of them is this file's business;
test_the_release_marker_is_not_matched pins that distinction so a later "tidy the
regex" cannot quietly promote it.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Assembled at import time so this module's own bytes never contain it. See the
# docstring: a self-exempting guard is one refactor away from exempting everything.
MARKER = "TODO" + "(author)"


def _tracked_files():
    """Every file git tracks, as absolute paths. The shipped set, by definition."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / name for name in out.split("\0") if name]


def _offenders():
    """(relative path, 1-based line number, line) for every occurrence of MARKER.

    Read as BYTES. `git ls-files` includes PNGs and a favicon; decoding them as
    text would either raise or need a suffix allowlist, and a suffix allowlist is
    the same "a real file can hide behind it" hazard the enumerator was chosen to
    avoid. A byte search over a binary is meaningless but harmless.
    """
    needle = MARKER.encode()
    hits = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        if needle not in data:
            continue
        for lineno, line in enumerate(data.split(b"\n"), start=1):
            if needle in line:
                hits.append(
                    (
                        str(path.relative_to(ROOT)),
                        lineno,
                        line.decode("utf-8", errors="replace").strip(),
                    )
                )
    return hits


def test_no_tracked_file_carries_the_author_placeholder():
    offenders = _offenders()
    assert not offenders, (
        f"{len(offenders)} tracked file location(s) still carry the author "
        "placeholder:\n  "
        + "\n  ".join(f"{p}:{n}: {line}" for p, n, line in offenders)
        + "\n\nThese are the identity of the released artefact: a LICENSE with a "
        "placeholder grantor, a CITATION.cff that propagates into every citation "
        "manager and into the archived release record, a manifest.author read by "
        "`nextflow info`. Supply the real values; there is nothing in the git "
        "history that is a safe substitute for asking."
    )


def test_the_scan_reads_the_whole_shipped_tree():
    """A 'must not find' scan passes perfectly over an empty set. This is what
    makes the check above mean something: the enumerator really enumerates."""
    tracked = _tracked_files()
    assert len(tracked) >= 300, (
        f"`git ls-files` returned only {len(tracked)} path(s) from {ROOT}. The "
        "repository ships several hundred; a number this small means the subprocess "
        "ran outside the work tree and the scan read almost nothing."
    )
    names = {p.name for p in tracked}
    for required in (
        "LICENSE",
        "CITATION.cff",
        "README.md",
        "mkdocs.yml",
        "nextflow.config",
    ):
        assert required in names, (
            f"{required} is not tracked, so this guard is not watching the file it "
            "was written for. Either it was renamed or the enumerator is wrong."
        )


def test_the_detector_actually_detects(tmp_path):
    """The other way a negative guard passes having proved nothing: the needle
    never matches the form it is looking for. Proved on a synthetic file."""
    probe = tmp_path / "probe.md"
    probe.write_text(f"Written by {MARKER}.\n")
    assert MARKER.encode() in probe.read_bytes()


def test_the_release_marker_is_not_matched():
    """`TODO(release)` is a different, legitimately-open marker: CITATION.cff carries
    one for the DOI, which cannot be filled until the tagged release is archived.
    Widening this guard to `TODO(` would make the release phase's own work fail."""
    assert MARKER.encode() not in b"# TODO" + b"(release): add `doi:` here"
