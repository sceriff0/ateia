"""bin/utils/ome_io.py is the only module under bin/ that may write a TIFF.

WHY A CLOSED RULE AND NOT AN ALLOWLIST. Ten scripts called tifffile's writers
directly, and the OME metadata dict -- axes, channel names, PhysicalSizeX/Y and
their unit -- was rebuilt independently in four of them, plus a fifth
hand-written XML template that had no callers at all. Each copy was free to
drift in unit spelling, key order, or which keys it omitted, and a drift shows
up as a header a downstream reader misinterprets rather than as a failure. One
owner closes that; a rule with exceptions reopens it one exception at a time.

WHY ast AND NOT grep. This is a NEGATIVE rule ("this must not appear"), and
tests/ci_actions.py's note on positive vs negative rules says a negative rule
wants a whole-line-only stripper so a trailing comment makes the guard NOISY
rather than blind. That reasoning is about TEXT matching. An AST walk is exact:
`tifffile.imwrite` written inside a docstring is genuinely not a call, and
bin/convert_image.py's own docstring names it three times while describing the
writer it replaced -- deliberately, since that prose is the record of why the
streaming write exists. A text guard would have to choose between deleting that
prose and carrying an exception for the file; the AST has no such problem.

WHAT COUNTS AS A WRITE. Any call to `imwrite` or `TiffWriter` whose receiver is
`tifffile` (or that is a bare name imported from it). `cv2.imwrite` in
bin/utils/qc.py is deliberately NOT matched: it writes a display PNG, not a
pipeline artifact, and routing it through a TIFF module would be worse, not
better. The receiver check is what makes that distinction structural instead of
a special case.

WATCHED FAILING. Reintroducing a bare tifffile.imwrite into bin/segment.py makes
this red; see the break-it step in the plan that introduced it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"

#: The one module allowed to name tifffile's writers.
THE_OWNER = BIN_DIR / "utils" / "ome_io.py"

#: Vendored third-party code under bin/, held to its upstream style rather than ours
#: (the same exclusion ruff.toml carries). A vendored copy is not this repo's code and
#: is kept byte-identical to upstream on purpose.
VENDORED = (BIN_DIR / "utils" / "cse",)

WRITER_NAMES = ("imwrite", "TiffWriter")


def _candidate_files():
    out = []
    for path in sorted(BIN_DIR.rglob("*.py")):
        if any(vendor in path.parents for vendor in VENDORED):
            continue
        out.append(path)
    return out


def _tifffile_writer_calls(path: Path):
    """(lineno, rendered_call) for every tifffile write call in `path`.

    Matches two spellings: `tifffile.imwrite(...)` / `tifffile.TiffWriter(...)`, and a
    bare `imwrite(...)` / `TiffWriter(...)` that a `from tifffile import ...` brought
    into scope. The bare form is looked for only when that import is present, so a
    same-named function belonging to something else is not swept up.
    """
    tree = ast.parse(path.read_text())

    bare_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tifffile":
            for alias in node.names:
                if alias.name in WRITER_NAMES:
                    bare_names.add(alias.asname or alias.name)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            receiver = fn.value
            is_tifffile = isinstance(receiver, ast.Name) and receiver.id == "tifffile"
            if is_tifffile and fn.attr in WRITER_NAMES:
                found.append((node.lineno, f"tifffile.{fn.attr}(...)"))
        elif isinstance(fn, ast.Name) and fn.id in bare_names:
            found.append((node.lineno, f"{fn.id}(...)  [from tifffile import ...]"))
    return found


def test_the_scan_actually_reads_files():
    """Three ways this guard could pass while checking nothing: an empty candidate
    list, a bin/ that is not this repo's, or an owner that no longer writes."""
    candidates = _candidate_files()
    assert len(candidates) >= 20, (
        f"only {len(candidates)} Python file(s) under {BIN_DIR} -- the scan is "
        "mis-rooted, not the tree"
    )
    assert THE_OWNER in candidates, f"{THE_OWNER} is not in the scanned set"


def test_the_owner_really_is_a_writer():
    """A guard whose sole exemption writes nothing is exempting nothing, and every
    'no writer here' answer below would then be vacuous."""
    calls = _tifffile_writer_calls(THE_OWNER)
    assert calls, (
        f"{THE_OWNER.relative_to(REPO_ROOT)} contains no tifffile write call at all. "
        "It is the module every other writer delegates to; if the write moved, this "
        "guard is now pointing at the wrong owner."
    )


@pytest.mark.parametrize(
    "path", [p for p in _candidate_files() if p != THE_OWNER], ids=lambda p: p.name
)
def test_no_file_under_bin_writes_a_tiff_except_ome_io(path):
    calls = _tifffile_writer_calls(path)
    rel = path.relative_to(REPO_ROOT)
    assert not calls, (
        f"{rel} calls tifffile's writer directly at "
        + ", ".join(f"line {ln} ({what})" for ln, what in calls)
        + ". bin/utils/ome_io.py is the only module allowed to: an OME slide goes "
        "through ome_io.write_ome_tiff (which owns the metadata dict, the tiling and "
        "the BigTIFF threshold), a multi-write through ome_io.ome_tiff_writer, and "
        "anything non-OME through ome_io.write_tiff, which forwards every keyword "
        "verbatim so a migrated call keeps its exact behaviour."
    )


def test_a_docstring_mentioning_the_writer_is_not_a_call():
    """The property that makes the AST walk the right tool rather than a stricter grep.

    bin/convert_image.py's module and function docstrings name `tifffile.imwrite`
    while explaining the eager writer the streaming one replaced -- prose that is
    the record of a real design decision and must survive. A text-matching guard
    would force a choice between deleting it and carrying a file exception.
    """
    source = "\n".join(
        [
            "def f():",
            '    """Docstring naming tifffile.imwrite and TiffWriter in prose."""',
            "    # tifffile.imwrite(path, data) -- the call this replaced",
            "    return 1",
        ]
    )
    tree = ast.parse(source)
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in WRITER_NAMES
    ]
    assert not found, "the AST walk must not see prose as a call"


def test_cv2_imwrite_is_not_swept_up():
    """bin/utils/qc.py writes a display PNG with cv2.imwrite. It is not a TIFF and
    not a pipeline artifact; routing it through a TIFF module would be worse. The
    receiver check is what keeps that structural rather than an exception."""
    tree = ast.parse("import cv2\ncv2.imwrite('x.png', d)\n")
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "tifffile"
    ]
    assert not found
