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
