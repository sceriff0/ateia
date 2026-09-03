"""`modules/nf-core/basicpy/` is upstream's file, byte for byte. This is what says so.

`modules/nf-core/basicpy/MIRAGE-NOTES.md` opens by asserting that `main.nf` and `meta.yml`
are "byte-for-byte upstream; nothing here is patched", and cites this file as the guard.
For one round that sentence cited a test that did not exist -- which on this branch is a
defect in its own right, not a documentation nit -- so the guard is now real.

WHY A DIGEST RATHER THAN MORE FEATURE ASSERTIONS.
`tests/test_basicpy_defaults_are_deliberate.py` pins the four properties mirage reasons
about: the container tag, the conda refusal in both blocks, the hardcoded version literal
and its topic emit. Those are the things whose meaning is discussed elsewhere in the repo,
and they fail with a message that explains itself. But they are a SAMPLE -- between them
they do not cover the input/output tuple shapes, the `when:` guard, or the `/opt/main.py`
invocation line, and it was the invocation line the review found unpinned. Enumerating
every remaining line as its own assertion would be a hand-maintained second copy of the
file; a digest is the whole file, and it costs one line.

The trade is that the digest fails on any edit at all, including a whitespace change or a
legitimate upstream re-sync -- which is the intended behaviour for a vendored file. When
re-syncing, do it deliberately: re-fetch, re-read the diff, update the digest here, and
re-check MIRAGE-NOTES.md, because the notes describe upstream's behaviour and an upstream
change may have invalidated them.

PROVENANCE, and its limit. The digests below are of the files as vendored on 2026-08-17,
transcribed from

    https://raw.githubusercontent.com/nf-core/modules/master/modules/nf-core/basicpy/main.nf
    https://raw.githubusercontent.com/nf-core/modules/master/modules/nf-core/basicpy/meta.yml

Re-verify with:

    curl -sSL <url> | shasum -a 256

This test pins "unchanged since vendoring". It does NOT and cannot pin "identical to
whatever upstream says today" -- that would need the network, and a guard that silently
skips when offline is worse than no guard (this repo has been bitten by exactly that
shape). The invocation-line assertion below is the readable half: it states in the failure
message what the shape is supposed to be, so a digest mismatch can be triaged without
fetching anything.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent.parent / "modules" / "nf-core" / "basicpy"

# sha256 of the file as vendored. See the provenance note above before changing either.
VENDORED_DIGESTS = {
    "main.nf": "4563de4d07aca2c47f0b0a121dd06e3de4c9b94153a854d7f0636f7d494db025",
    "meta.yml": "836af09031cba3c0a3c922975cd658d31b302b8a4150ccfc38035d6054feaf8a",
}


@pytest.mark.parametrize("name,expected", sorted(VENDORED_DIGESTS.items()))
def test_vendored_file_is_unchanged_since_it_was_vendored(name, expected):
    path = MODULE_DIR / name
    assert path.is_file(), f"{path} is missing -- the module is no longer vendored"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected, (
        f"modules/nf-core/basicpy/{name} has been edited since it was vendored "
        f"(sha256 {actual}, expected {expected}). This directory is upstream's file and "
        "MIRAGE-NOTES.md says so. If this is a deliberate upstream re-sync, read the diff, "
        "update the digest in this test, and re-check MIRAGE-NOTES.md -- the notes "
        "describe upstream's behaviour and an upstream change may have invalidated them. "
        "If it is a local patch, do not: mirage's arguments belong in conf/modules.config, "
        "and its resources in a withName: block."
    )


def test_the_invocation_line_is_exactly_upstreams():
    """The readable half of the digest: what the container is actually told to run.

    Spelled out rather than left to the digest because this is the line the flag-binding
    review was about -- the profile filenames come from `--output-flatfield` /
    `--output-darkfield`, and `subworkflows/local/preprocess.nf` joins BASICPY's output
    back to the tile stack's sidecar on the prefix those two flags set. A digest failure
    here would be triaged against this string.
    """
    text = (MODULE_DIR / "main.nf").read_text()
    assert (
        "/opt/main.py -i $image -o . "
        "--output-flatfield $prefix --output-darkfield $prefix $args"
    ) in text, (
        "the /opt/main.py invocation in the vendored module is not upstream's. mirage "
        "relies on both --output-* flags taking the SAME prefix (meta.id, the tile "
        "stack's simpleName), because subworkflows/local/preprocess.nf keys its join on "
        "it; and on $args being the only place arguments enter, which is what "
        "tests/test_basicpy_defaults_are_deliberate.py guards."
    )


def test_the_output_tuple_is_darkfield_then_flatfield():
    """Upstream's emit order, which APPLY_PROFILES' input tuple has to match.

    `tuple val(meta), path("*-dfp.ome.tif"), path("*-ffp.ome.tif")` -- dfp FIRST. Getting
    this the wrong way round downstream divides by the darkfield and subtracts the
    flatfield, silently, on every pixel. tests/modules/apply_profiles.nf.test pins the CLI
    binding at the other end of the chain, and bin/apply_basic_profiles.py's flatfield
    positivity check catches a swap semantically; this pins the source of the ordering.
    """
    text = (MODULE_DIR / "main.nf").read_text()
    assert (
        'tuple val(meta), path("*-dfp.ome.tif"), path("*-ffp.ome.tif"), emit: profiles'
        in text
    )
    assert text.index("*-dfp.ome.tif") < text.index("*-ffp.ome.tif")
