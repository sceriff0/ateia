"""subworkflows/local/checkpoint_writer.nf is the only subworkflow that writes a manifest.

The same five-step chain -- filter on Checkpoint.writesAtLevel, map through
Checkpoint.row, collectFile with the step's name, seed, sort and storeDir --
appeared four times verbatim, in preprocess.nf, registered_checkpoint.nf,
segmentation.nf and postprocessed_checkpoint.nf. Three of the four carried the
SAME eight-line "sort: true is load-bearing" comment word for word.

Four copies of one mechanism is four chances for one of them to lose the cleanup
gate, or the sort, or the seed -- and each loss is silent. Without the gate, a
run at a cleaning level writes a manifest whose rows name artifacts that were
never published, and --start and add_cycle both OPEN what it names. Without the
sort, two runs of the same commit produce different files. Without the seed, the
manifest has no header and every reader's splitCsv(header: true) misparses it.

TWO PROPERTIES, ASSERTED SEPARATELY, because they can break independently:

1. Only checkpoint_writer.nf may hold a `collectFile(` whose `storeDir:` is
   Layout.checkpointDir. A second one is a second writer.
2. Only checkpoint_writer.nf, among subworkflows/, may read
   `params.cleanup_level`. That is the GATE. A caller reading it again is either
   a second gate that can disagree, or -- worse -- the same gate applied twice,
   which reads as defence in depth and is actually two places to forget.

`params.outdir` is deliberately NOT part of this rule, and the reason is worth
recording. The row builders MUST call
`Layout.publishedPath(params.outdir, <pid>, '<kind>', file)` with params.outdir
and the kind as LITERAL arguments, because tests/test_layout.py's static scan
matches exactly that shape -- a variable there is invisible to it, and the guard
would silently stop covering the checkpoint a `--start registration` run reads.
So params.outdir is read at every row builder by design.

The model is queried through tests.nfmodel rather than a private regex: this file
constructs paths to Nextflow source, so tests/test_nfmodel.py requires it, and
the string-preserving view is the correct one here because every needle
(`storeDir: Layout.checkpointDir`, `params.cleanup_level`) is code rather than a
quoted literal, while a COMMENT naming either must not count.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.nfmodel import strip_comments

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBWORKFLOWS = REPO_ROOT / "subworkflows"

THE_WRITER = SUBWORKFLOWS / "local" / "checkpoint_writer.nf"

#: A collectFile whose storeDir is the checkpoint directory. Matched across the
#: intervening arguments (name:, newLine:, sort:, seed: may appear in any order), which
#: is why this is one regex over the block rather than two independent substring checks
#: -- a file may legitimately hold a collectFile for something else entirely.
_CHECKPOINT_COLLECTFILE = re.compile(
    r"collectFile\((?:[^()]|\([^()]*\))*storeDir\s*:\s*Layout\.checkpointDir",
    re.S,
)

_CLEANUP_LEVEL = re.compile(r"\bparams\.cleanup_level\b")


def _subworkflow_files():
    return sorted(SUBWORKFLOWS.rglob("*.nf"))


def test_the_scan_actually_reads_subworkflows():
    """Three ways this guard could pass while checking nothing: an empty file list,
    a mis-rooted tree, or a writer that no longer writes."""
    files = _subworkflow_files()
    assert len(files) >= 10, (
        f"only {len(files)} subworkflow file(s) under {SUBWORKFLOWS} -- the scan is "
        "mis-rooted, not the tree"
    )
    assert THE_WRITER in files, f"{THE_WRITER} is not in the scanned set"


def test_the_writer_really_writes():
    """The sole exemption must be load-bearing. If checkpoint_writer.nf stopped
    holding the collectFile, every 'no writer here' answer below would be
    vacuously true and the pipeline would write no manifests at all."""
    body = strip_comments(THE_WRITER.read_text())
    assert _CHECKPOINT_COLLECTFILE.search(body), (
        f"{THE_WRITER.relative_to(REPO_ROOT)} holds no collectFile writing into "
        "Layout.checkpointDir. It is the only file allowed to; if the write moved, "
        "this guard is pointing at the wrong owner."
    )
    assert _CLEANUP_LEVEL.search(body), (
        f"{THE_WRITER.relative_to(REPO_ROOT)} no longer reads params.cleanup_level, so "
        "the cleanup gate is gone: a run at a cleaning level would write a manifest "
        "whose every path dangles."
    )


def test_only_checkpoint_writer_collects_a_manifest():
    offenders = []
    for path in _subworkflow_files():
        if path == THE_WRITER:
            continue
        if _CHECKPOINT_COLLECTFILE.search(strip_comments(path.read_text())):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert not offenders, (
        "these subworkflows collectFile a checkpoint manifest themselves: "
        + ", ".join(offenders)
        + ". subworkflows/local/checkpoint_writer.nf is the only writer -- call "
        "CHECKPOINT_WRITER(<step>, <channel of row Maps>) instead. A second copy of "
        "the chain is a second chance to lose the cleanup gate, the sort or the seed, "
        "and every one of those losses is silent."
    )


def test_only_checkpoint_writer_reads_the_cleanup_level():
    offenders = []
    for path in _subworkflow_files():
        if path == THE_WRITER:
            continue
        if _CLEANUP_LEVEL.search(strip_comments(path.read_text())):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert not offenders, (
        "these subworkflows read params.cleanup_level: "
        + ", ".join(offenders)
        + ". The manifest gate lives in checkpoint_writer.nf alone. A second read is "
        "either a gate that can disagree with it, or the same gate applied twice -- "
        "which reads as defence in depth and is actually two places to forget. "
        "(workflows/mirage.nf reads it for a DIFFERENT purpose -- warning about a "
        "--start that re-enters a tree this level does not publish -- and is out of "
        "this rule's scope.)"
    )


def test_the_comment_stripped_view_is_what_is_read():
    """The failure mode CLAUDE.md records six instances of, in one day: a guard
    satisfied (or, for a negative rule like this one, wrongly tripped) by a
    COMMENT. Here the needle is code, so a comment mentioning params.cleanup_level
    -- and preprocess.nf's history is full of such comments -- must not count as a
    read."""
    sample = "\n".join(
        [
            "workflow X {",
            "    // params.cleanup_level is read in checkpoint_writer.nf, not here",
            "    /* collectFile(storeDir: Layout.checkpointDir(params.outdir)) */",
            "    main:",
            "    ch = Channel.empty()",
            "}",
        ]
    )
    body = strip_comments(sample)
    assert not _CLEANUP_LEVEL.search(body)
    assert not _CHECKPOINT_COLLECTFILE.search(body)


def test_every_checkpoint_step_has_exactly_one_commissioning_call_site():
    """The other direction: the rule above forbids a second writer, and this one
    forbids a step quietly losing its only one. A step no file commissions is a
    manifest that is never written, which fails at the NEXT run's launch (--start
    or add_cycle refusing) rather than at this one's."""
    call = re.compile(r"CHECKPOINT_WRITER\s*\(\s*Layout\.(\w+)")
    commissioned = set()
    for path in _subworkflow_files():
        commissioned.update(call.findall(strip_comments(path.read_text())))

    assert commissioned == {
        "PREPROCESSED",
        "REGISTERED",
        "SEGMENTED",
        "POSTPROCESSED",
    }, (
        f"CHECKPOINT_WRITER is commissioned for {sorted(commissioned)}; every one of "
        "the four checkpoint steps must have a call site, and no step outside them."
    )
