#!/usr/bin/env python3
"""Guard: no Groovy statement is continued by a line that STARTS with a binary operator.

THE HAZARD, reproduced. Groovy has no line-continuation rule -- a statement ends when it
can. So this, inside an `assertAll(...)` closure list:

    { assert vlines.size() == 2, "versions.yml is not two lines: "
        + vlines },

is not one assert with a two-part message. It is `assert vlines.size() == 2, "..."`
followed by a *separate statement* `+ vlines`, which Groovy compiles as the unary-plus
operator and dispatches as `vlines.positive()`. Verified against the Groovy that ships
inside nextflow 25.04.7:

    groovy.lang.MissingMethodException: No signature of method:
    java.lang.String.positive() is applicable for argument types: () values: []

That throws on EVERY run, whatever the assertion says -- the test can never pass and can
never report the thing it was written to report. It is the exact inverse of a hollow
guard, and it has a characteristic bad fix: because the failure message names a type and
not the assertion, the reflex is to weaken or delete the assertion rather than to move
the `+`. Splitting the SAME message the other way is always safe:

    { assert vlines.size() == 2, "versions.yml is not two lines: " +
        vlines },

because a line ending in `+` cannot be a complete statement, so Groovy keeps reading.

SCOPE. Every Groovy-parsed file in the repo: `.nf`, `.nf.test`, `.groovy`, `.config`.
The bug is not specific to `assert`, or to tests -- any statement whose continuation
starts with an operator is silently re-parsed the same way. `assert` is merely where it
is least visible, because an assert that always throws still looks like a failing test
rather than a broken one.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("tests", "lib", "subworkflows", "workflows", "modules", "conf")
SCAN_SUFFIXES = (".nf", ".test", ".groovy", ".config")

# A continuation line beginning with a binary operator Groovy also accepts as a UNARY
# prefix (`+`, `-`) or as the start of a fresh expression. `+`/`-` are the dangerous
# pair: they compile and then fail at runtime as .positive()/.negative(). Operators with
# no unary form (`*`, `&&`, `||`, `==`) are compile errors instead -- loud, and therefore
# not what this guard is for, but they are equally never intended, so they are included.
LEADING_OPERATOR_RE = re.compile(r"^\s*(\+|-|\*|/|&&|\|\||==|!=)(?![-+=>])\s*\S")

# Lines that legitimately begin with one of those characters and are not continuations.
BENIGN_PREFIXES = (
    "--",  # CLI flags in a shell heredoc / doc block
    "++",
    "*",  # javadoc/groovydoc continuation ` * text`
    "//",
    "/*",
    "*/",
)


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        root = ROOT / d
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and (p.suffix in SCAN_SUFFIXES or p.name.endswith(".nf.test")):
                files.append(p)
    return files


def _strip_block_comments(lines: list[str]) -> list[str]:
    """Blank out lines inside /* ... */ so groovydoc bullets are not scanned."""
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if in_block:
            out.append("")
            if "*/" in line:
                in_block = False
            continue
        if stripped.startswith("/*") and "*/" not in stripped:
            in_block = True
            out.append("")
            continue
        out.append(line)
    return out


def offending_lines(lines: list[str]) -> list[tuple[int, str]]:
    """(1-based line number, text) for every continuation line starting with an operator.

    Only lines whose PREVIOUS non-blank line looks like a complete statement are
    reported: a genuine multi-line expression whose previous line ends in `(`, `,`, `+`,
    `[` etc. cannot terminate there, so Groovy keeps reading and the operator is parsed
    as the binary operator it looks like.
    """
    scanned = _strip_block_comments(lines)
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(scanned):
        stripped = line.strip()
        if not stripped or stripped.startswith(BENIGN_PREFIXES):
            continue
        if not LEADING_OPERATOR_RE.match(stripped):
            continue
        prev = ""
        for j in range(i - 1, -1, -1):
            if scanned[j].strip():
                prev = scanned[j].strip()
                break
        if not prev:
            continue
        # A previous line that cannot end a statement means this really is a
        # continuation of one expression -- fine.
        if prev.endswith(("(", ",", "[", "{", "+", "-", "*", "/", "=", "?", ":", "&&", "||", "\\")):
            continue
        hits.append((i + 1, stripped))
    return hits


def test_the_sweep_actually_reaches_files():
    """The sweep must SCAN something, not merely fail to find anything.

    This is the half the two plant tests below do NOT cover: they call
    `offending_lines()` on literal string lists, so they prove the DETECTOR works and
    say nothing about the REACH. And this sweep was green on its very first run -- so
    file reach is the only thing standing between "the tree is clean" and "the guard
    scans nothing", which is precisely the scope-glob failure this repo keeps hitting.

    `p.suffix in (".nf", ".test", ".groovy", ".config")` LOOKS right for a `.nf.test`
    file (its suffix really is `.test`), but that is a reading of pathlib, not a test of
    it. Each scanned suffix and each scanned directory is pinned to a file known to
    exist, so narrowing either fails here loudly instead of silently emptying the
    haystack.

    Watched fail, each narrowing applied and reverted, counts measured not guessed
    (sweep currently reaches 127 files: 56 `.test`, 50 `.nf`, 13 `.groovy`, 8 `.config`):

      drop "tests" from SCAN_DIRS     127 -> 62,  tests/lib_probe.nf.test unreachable
      drop ".nf"    from SCAN_SUFFIXES 127 -> 77,  subworkflows/local/seg_qc.nf unreachable
      drop ".groovy"                   127 -> 114, lib/Layout.groovy unreachable
      drop ".config"                   127 -> 119, conf/modules.config unreachable

    One narrowing that does NOT break reach, recorded so the next reader does not
    "simplify" the wrong half: dropping `".test"` from SCAN_SUFFIXES still reaches all
    56 `.nf.test` files, because `_candidate_files` also matches
    `p.name.endswith(".nf.test")` explicitly. The two conditions are deliberately
    redundant for the one file type this repo's blocking CI gate runs.
    """
    files = _candidate_files()
    assert files, "the sweep reached no files at all -- SCAN_DIRS/SCAN_SUFFIXES is wrong"

    names = {str(p.relative_to(ROOT)) for p in files}

    # One known-existing file per scanned suffix, so a narrowed suffix list cannot
    # quietly shrink the haystack to a subset that happens to be clean.
    for expected in (
        "tests/lib_probe.nf.test",          # .nf.test -- the shape most at risk
        "tests/lib_probe.nf",               # .nf
        "lib/Layout.groovy",                # .groovy
        "conf/modules.config",              # .config
        "subworkflows/local/seg_qc.nf",     # a scanned dir other than tests/
    ):
        assert expected in names, (
            f"{expected} exists but the sweep did not reach it: SCAN_DIRS/SCAN_SUFFIXES "
            f"is narrower than it looks. Reached {len(files)} files."
        )

    nf_tests = [p for p in files if p.name.endswith(".nf.test")]
    assert len(nf_tests) > 40, (
        f"only {len(nf_tests)} .nf.test files reached; the suite has ~56, so the suffix "
        "match is not doing what it appears to"
    )
    # A plausibility floor on the whole sweep, not just the interesting suffix: a
    # SCAN_DIRS typo resolving to one directory would still pass the checks above if
    # that directory happened to hold all five named files. Floors, not equalities --
    # this must not fail every time somebody adds a module.
    assert len(files) > 100, f"the sweep reached only {len(files)} files"


def test_no_statement_is_continued_by_a_leading_operator():
    offenders: list[str] = []
    for p in _candidate_files():
        lines = p.read_text(encoding="utf-8", errors="ignore").split("\n")
        for ln, text in offending_lines(lines):
            offenders.append(f"{p.relative_to(ROOT)}:{ln}: {text}")
    assert not offenders, (
        "Groovy ends a statement wherever it can, so a continuation line starting with "
        "an operator is a SEPARATE statement -- `+ x` becomes `x.positive()` and throws "
        "at runtime regardless of what the line above asserts. Move the operator to the "
        "end of the previous line:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_fires_on_the_shape_it_was_written_for():
    """Plants the exact `assertAll` shape, and the safe rewrite next to it.

    Without this, a detector whose regex or previous-line heuristic quietly matched
    nothing would leave the test above green forever on a clean tree -- which is what a
    clean tree looks like either way.
    """
    broken = [
        "assertAll(",
        '    { assert vlines.size() == 2, "versions.yml is not two lines: "',
        "        + vlines },",
        ")",
    ]
    assert offending_lines(broken) == [(3, "+ vlines },")], offending_lines(broken)

    safe = [
        "assertAll(",
        '    { assert vlines.size() == 2, "versions.yml is not two lines: " +',
        "        vlines },",
        ")",
    ]
    assert offending_lines(safe) == [], offending_lines(safe)


def test_the_detector_does_not_fire_on_groovydoc_or_flags():
    """The two shapes that look like the bug and are not."""
    groovydoc = [
        "/*",
        " * A doc block whose bullets",
        " * start with an asterisk.",
        " */",
        "def x = 1",
    ]
    assert offending_lines(groovydoc) == []

    shell_flags = [
        "    my_script.py \\",
        "        --input in.tif \\",
        "        --output out.tif",
    ]
    assert offending_lines(shell_flags) == []
