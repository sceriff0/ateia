#!/usr/bin/env python3
"""Single-ownership guards for the per-patient checkpoint fragment.

A checkpoint is now written at two granularities: `<outdir>/csv/<step>.csv` (the
aggregate, written by `collectFile()` when the last patient finishes) and
`<outdir>/csv/<step>.parts/<patient_id>.csv` (one patient's rows, published by
WRITE_CHECKPOINT_FRAGMENT the moment that patient finishes). Two writers for one
artifact is exactly the shape that lets a second, drifting schema grow — the defect
lib/Checkpoint.groovy was created to end, when the header lived in five places.

So these assert the two properties that keep it one artifact:

  1. The fragment's CONTENT comes from lib/Checkpoint.groovy, in both the `script:`
     and the `stub:` block, and the module names no column of its own.
  2. The fragment's PATH comes from lib/Layout.groovy, and conf/modules.config's
     publishDir — which cannot import Layout, and so must restate it — restates
     exactly what Layout says.

What the fragments actually contain is asserted for real by
tests/checkpoint_manifest.nf.test (aggregate and fragments carry the same header and
the same rows) and tests/checkpoint_durability.nf.test (they are on disk before the
run ends). These run in the plain pytest job — no Nextflow, no container engine — so
the ownership properties hold even when the nf-test suite is skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYOUT = ROOT / "lib" / "Layout.groovy"
CHECKPOINT = ROOT / "lib" / "Checkpoint.groovy"
MODULES_CONFIG = ROOT / "conf" / "modules.config"
FRAGMENT_MODULE = ROOT / "modules" / "local" / "write_checkpoint_fragment.nf"
WRITER = ROOT / "subworkflows" / "local" / "checkpoint_writer.nf"


def _groovy_string_const(text: str, name: str) -> str:
    m = re.search(rf"static final String {name}\s*=\s*'([^']*)'", text)
    assert m, f"{name} not found — did the constant move or change quoting?"
    return m.group(1)


def test_the_fragment_writer_exists():
    """The capability itself. Without it every assertion below is vacuous, and the
    checkpoint reverts to being written only when the LAST patient finishes."""
    assert FRAGMENT_MODULE.exists(), (
        "modules/local/write_checkpoint_fragment.nf is gone — the per-patient half of "
        "every checkpoint is unwritten, and a run that loses its driver mid-way leaves "
        "no record of the patients it had already finished"
    )
    assert WRITER.exists(), (
        "subworkflows/local/checkpoint_writer.nf is gone — the one sink every "
        "checkpoint CSV goes through"
    )


def test_both_script_and_stub_render_the_fragment_from_checkpoint():
    """`-stub` never evaluates a `script:` block, and CI's blocking gate is a stub run.

    So the stub block is the ONLY one CI observes, and a separately-written stub block
    would be a second implementation of the file format that no gate can see diverge
    from the real one. Both must render from the same Checkpoint call — the same rule
    lib/ProcessEnvelope.groovy applies to the versions.yml heredoc.
    """
    text = FRAGMENT_MODULE.read_text()
    script_half, _, stub_half = text.partition("\n    stub:\n")
    assert stub_half, "no stub: block — a stub run would produce no fragment at all"
    for half, name in ((script_half, "script:"), (stub_half, "stub:")):
        assert "Checkpoint.fragment(" in half, (
            f"the {name} block does not build its fragment with Checkpoint.fragment() "
            "— the header and row grammar would then have a second owner"
        )


def test_the_fragment_writer_names_no_checkpoint_column():
    """The module writes a file; it does not know what is in it.

    A column name appearing here would mean the fragment's schema is stated somewhere
    other than lib/Checkpoint.groovy's STEPS table — which is the arrangement that let
    a writer and a reader disagree silently before that table existed.
    """
    columns = set(re.findall(r"'([a-z_]+)'", re.search(
        r"static final List<Map> STEPS = \[(.*?)\n    \]\.asImmutable\(\)",
        CHECKPOINT.read_text(),
        re.S,
    ).group(1)))
    columns -= {"preprocessed", "registered", "segmented", "postprocessed"}  # step names
    # `patient_id` is excluded, and it is the only exclusion: it is not merely a column
    # of these four CSVs, it is the pipeline's grouping vocabulary — the meta key, the
    # process's `val(patient_id)` input, and the fragment's own FILENAME
    # (Layout.checkpointFragmentName). A module that keys a per-patient file on the
    # patient is not restating a schema. Every other name here is a column and nothing
    # else, so its appearance in the writer would mean the header is being built twice.
    columns -= {"patient_id"}
    assert columns, "no checkpoint columns parsed — is Checkpoint.STEPS still a literal?"
    assert "is_reference" in columns and "cell_mask" in columns, (
        "the parsed column set lost its schema-only members — the check below would "
        "then have nothing with teeth left to look for"
    )

    text = FRAGMENT_MODULE.read_text()
    named = sorted(c for c in columns if re.search(rf"\b{c}\b", text))
    assert not named, (
        f"modules/local/write_checkpoint_fragment.nf names checkpoint column(s) {named}. "
        "The schema belongs to lib/Checkpoint.groovy; the module only writes the bytes."
    )


def _fragment_publish_path() -> str:
    """The `path:` closure of conf/modules.config's WRITE_CHECKPOINT_FRAGMENT block."""
    config = MODULES_CONFIG.read_text()
    m = re.search(
        r"withName:\s*'WRITE_CHECKPOINT_FRAGMENT'\s*\{(.*?)\n    \}", config, re.S
    )
    assert m, (
        "conf/modules.config has no withName: 'WRITE_CHECKPOINT_FRAGMENT' block, so the "
        "fragments fall through to the _UNROUTED_PUBLISH default and land nowhere a "
        "restart would look"
    )
    p = re.search(r'path:\s*\{\s*"([^"]+)"\s*\}', m.group(1))
    assert p, "WRITE_CHECKPOINT_FRAGMENT's publishDir has no literal path: closure"
    return p.group(1)


def test_the_fragment_publish_path_restates_layout_exactly():
    """conf/*.config cannot see lib/*.groovy classes — a class name inside a config
    closure resolves silently against ConfigObject and only surfaces as a
    MissingMethodException when the closure runs. So the fragment's published path is
    necessarily restated by hand in conf/modules.config, and the two copies are kept in
    agreement HERE rather than by eye.

    A drift is not cosmetic: the fragments would be published somewhere
    Layout.checkpointFragment() does not name, so every reader — this repo's tests
    included — would look in the wrong place and find nothing, on a green run.
    """
    layout = LAYOUT.read_text()
    csv_dir = _groovy_string_const(layout, "CSV_DIR")
    suffix = _groovy_string_const(layout, "FRAGMENT_DIR_SUFFIX")

    expected = "${params.outdir}/" + csv_dir + "/${step}" + suffix
    actual = _fragment_publish_path()
    assert actual == expected, (
        f"conf/modules.config publishes checkpoint fragments to {actual!r}, but "
        f"lib/Layout.groovy's checkpointFragmentDir() builds {expected!r}"
    )


def test_layout_builds_the_fragment_path_from_its_own_constants():
    """The other direction: Layout must not hardcode 'csv' or '.parts' a second time
    inside its own fragment methods, which would make the constants above decorative."""
    layout = LAYOUT.read_text()
    body = layout[layout.index("static String checkpointFragmentDirName(") :]
    body = body[: body.index("checkpointFragment(def outdir")]
    assert "FRAGMENT_DIR_SUFFIX" in body, (
        "Layout.checkpointFragmentDirName does not use FRAGMENT_DIR_SUFFIX — the "
        "constant conf/modules.config is checked against would be decorative"
    )
    assert "'.parts'" not in body, (
        "Layout restates the '.parts' suffix as a literal instead of using "
        "FRAGMENT_DIR_SUFFIX"
    )
