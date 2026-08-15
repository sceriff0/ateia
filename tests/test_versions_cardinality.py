#!/usr/bin/env python3
"""Guard: `versions` carries ONE cardinality under one name.

THE RULE, in one sentence: `<X>.out.versions` is followed by `.first()` if and only if
`X` is a PROCESS.

A process emits one `versions.yml` per TASK -- one per patient, per slide, per tile --
and the QC report wants one row per tool, so the stream is de-duplicated with `.first()`.
A subworkflow's `versions` emit is the union of its processes' already-de-duplicated
streams, so it is one row per process ALREADY, and `.first()` applied to it does not
de-duplicate anything: it keeps the first row and DISCARDS every other process's version.
That is silent data loss, not a tidiness question -- `SEG_QC.out.versions.first()` would
publish SEG_QC_SEGMENT's row and drop SEG_QC_GEOJSON's and WARP_SEG_QC's.

WHY A RULE AND NOT A COMMENT. Both conventions were in use within a dozen lines of each
other in subworkflows/local/postprocess.nf: `.first()` on the three process emits, none
on the two subworkflow emits -- and subworkflows/local/registration.nf carried a comment
*explaining* the asymmetry ("SEG_QC.out.versions is already de-duplicated per process
(.first() applied inside)"). A caller had to know whether the name it was mixing belonged
to a process or a subworkflow, and the only way to find out was to read the callee. The
comments are gone; this test is what replaces them.

DE-DUPLICATION HAPPENS AT THE EMITTING BOUNDARY, which is why the rule can be checked at
the point of use at all. `.first()` is written where the process's output is first named,
never later through a variable: `ch_warp_versions = WARP_SEG_QC.out.versions` followed by
`.mix(ch_warp_versions.first())` twelve lines down is the same thing semantically and
invisible to any check that does not follow assignments. Keeping the two adjacent is what
makes "read the line" sufficient.

Collecting-boundary de-duplication is not an option, incidentally, and that is worth
recording so it is not proposed again: a collector cannot `.first()` a subworkflow's
versions stream without dropping rows (see above), so "the collector always de-duplicates"
is unimplementable for the subworkflow half. Emitting-boundary is the only convention that
can be applied uniformly.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("subworkflows", "workflows")

COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)
COMMENT_LINE_RE = re.compile(r"//.*$", re.M)

# `include { NAME } from './x'` / `include { NAME as ALIAS } from '../modules/local/x'`
INCLUDE_RE = re.compile(
    r"include\s*\{\s*([\w\s;]+?)\s*\}\s*from\s*['\"]([^'\"]+)['\"]"
)
# Each entry inside the braces: `NAME` or `NAME as ALIAS`
INCLUDE_ENTRY_RE = re.compile(r"(\w+)(?:\s+as\s+(\w+))?")

VERSIONS_USE_RE = re.compile(r"\b(\w+)\.out\.versions(\.first\(\))?")


def _strip_comments(text: str) -> str:
    text = COMMENT_BLOCK_RE.sub("", text)
    return COMMENT_LINE_RE.sub("", text)


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.nf")):
            files.append(p)
    return files


def _include_kinds(nf: Path, text: str) -> dict[str, str]:
    """Local name (alias if aliased) -> 'process' | 'subworkflow'.

    Decided by where the include resolves: `modules/` is one process per file (CLAUDE.md's
    module convention), anything under `subworkflows/` is a subworkflow.
    """
    kinds: dict[str, str] = {}
    for names, src in INCLUDE_RE.findall(text):
        target = (nf.parent / src).resolve()
        try:
            rel = target.relative_to(ROOT)
        except ValueError:
            continue
        kind = "process" if rel.parts[0] == "modules" else "subworkflow"
        for name, alias in INCLUDE_ENTRY_RE.findall(names):
            if not name or name == "as":
                continue
            kinds[alias or name] = kind
    return kinds


def violations() -> list[tuple[str, int, str, str, str]]:
    """(file, line, name, kind, 'missing .first()' | 'stray .first()')."""
    out: list[tuple[str, int, str, str, str]] = []
    for nf in _scan_files():
        raw = nf.read_text(encoding="utf-8")
        text = _strip_comments(raw)
        kinds = _include_kinds(nf, text)
        for m in VERSIONS_USE_RE.finditer(text):
            name, first = m.group(1), bool(m.group(2))
            kind = kinds.get(name)
            if kind is None:
                continue  # not an include of this file (e.g. a local variable)
            line = text[: m.start()].count("\n") + 1
            if kind == "process" and not first:
                out.append((str(nf.relative_to(ROOT)), line, name, kind, "missing .first()"))
            if kind == "subworkflow" and first:
                out.append((str(nf.relative_to(ROOT)), line, name, kind, "stray .first()"))
    return out


def test_versions_first_iff_process():
    bad = violations()
    assert not bad, (
        "`<X>.out.versions` must carry `.first()` if and only if X is a PROCESS.\n"
        + "\n".join(
            f"  {f}:{ln}  {name} is a {kind} -> {why}" for f, ln, name, kind, why in bad
        )
        + "\nA process emits one versions.yml per task and must be de-duplicated; a "
        "subworkflow's emit is already one row per process, and .first() on it DROPS "
        "every process but one."
    )


def test_the_comments_that_explained_the_asymmetry_are_gone():
    """The trap is removed, so the notes describing how to survive it must be too.

    Each of these sentences existed only to tell a caller that some emits are
    pre-de-duplicated and others are not. Leaving them behind after the rule is uniform
    would re-teach the reader that the two cases differ.
    """
    stale = [
        "already applied `.first()` internally",
        "already de-duplicated per process",
        "versions pre-.first()",
    ]
    offenders = []
    for nf in _scan_files():
        text = nf.read_text(encoding="utf-8")
        for s in stale:
            if s in text:
                offenders.append((str(nf.relative_to(ROOT)), s))
    assert not offenders, (
        f"stale asymmetry notes still present: {offenders}. The convention is uniform "
        "now (see tests/test_versions_cardinality.py); a comment explaining a trap that "
        "no longer exists is worse than no comment."
    )


def test_the_checker_can_see_both_kinds():
    """Proof the include resolver is not classifying everything as one kind.

    A resolver that returned `None` for every name -- a broken relative-path join, say --
    would make `violations()` empty and this whole file vacuous. These two files between
    them include a process and a subworkflow under known names.
    """
    postprocess = ROOT / "subworkflows" / "local" / "postprocess.nf"
    kinds = _include_kinds(postprocess, _strip_comments(postprocess.read_text()))
    assert kinds.get("SPLIT_CHANNELS") == "process", kinds
    assert kinds.get("CHECKPOINT_WRITER") == "subworkflow", kinds
    assert kinds.get("QUANTIFY_MARKERS") == "subworkflow", kinds

    seg_qc = ROOT / "subworkflows" / "local" / "seg_qc.nf"
    kinds = _include_kinds(seg_qc, _strip_comments(seg_qc.read_text()))
    # aliased include: the ALIAS is the local name, and it is still a process
    assert kinds.get("SEG_QC_SEGMENT") == "process", kinds


def test_a_commented_out_violation_is_not_reported():
    """Comments quoting the wrong form must not fail the guard.

    The docstrings in this repo quote both shapes on purpose. If the scanner read
    comments, documenting the rule would violate it.
    """
    planted = (
        "include { FOO } from '../../modules/local/foo'\n"
        "// FOO.out.versions\n"
        "/* FOO.out.versions */\n"
    )
    stripped = _strip_comments(planted)
    assert "FOO.out.versions" not in stripped
