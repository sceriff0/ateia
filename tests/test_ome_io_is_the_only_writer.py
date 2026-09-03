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

WHAT COUNTS AS A WRITE. Any call to `imwrite` or `TiffWriter` whose receiver is bound
to the `tifffile` module -- by a plain `import tifffile`, by a MODULE ALIAS
(`import tifffile as tf` -> `tf.imwrite(...)`), or that is a bare name imported from it
(`from tifffile import imwrite`). A module alias is not a style variant to shrug off:
it is the same escape a bare `from tifffile import imwrite` is, just spelled the other
way, and was missed by the first version of this guard (which checked only the literal
name `tifffile`) until a probe file (`import tifffile as tf; tf.imwrite(...)`) proved it
passed all three tests here uncaught. `cv2.imwrite` in bin/utils/qc.py is deliberately
NOT matched: it writes a display PNG, not a pipeline artifact, and routing it through a
TIFF module would be worse, not better. The receiver check is what makes that
distinction structural instead of a special case.

WATCHED FAILING. Reintroducing a bare tifffile.imwrite into bin/segment.py makes
this red; see the break-it step in the plan that introduced it. So does a module-alias
escape (`import tifffile as tf; tf.imwrite(...)`) -- see the same plan's follow-up fix.
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

    Matches three spellings: `tifffile.imwrite(...)` / `tifffile.TiffWriter(...)`; the
    same through a MODULE alias (`import tifffile as tf` -> `tf.imwrite(...)`); and a
    bare `imwrite(...)` / `TiffWriter(...)` that a `from tifffile import ...` brought
    into scope. The bare form is looked for only when that import is present, so a
    same-named function belonging to something else is not swept up. The module-alias
    form is collected the same way: only names actually bound to `tifffile` by an
    `import tifffile` or `import tifffile as X` count as its receiver, so a variable
    that happens to be named `tf` for an unrelated module is not swept up either.
    """
    tree = ast.parse(path.read_text())

    bare_names = set()
    mod_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tifffile":
            for alias in node.names:
                if alias.name in WRITER_NAMES:
                    bare_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tifffile":
                    mod_aliases.add(alias.asname or alias.name)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            receiver = fn.value
            is_tifffile = isinstance(receiver, ast.Name) and receiver.id in mod_aliases
            if is_tifffile and fn.attr in WRITER_NAMES:
                found.append((node.lineno, f"{receiver.id}.{fn.attr}(...)"))
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


def test_a_module_alias_of_tifffile_is_recognized_as_the_receiver(tmp_path):
    """`import tifffile as tf; tf.imwrite(...)` is the same escape as the bare-name
    form, spelled the other way -- and the first version of this guard missed it
    entirely, because it checked the receiver name against the literal string
    "tifffile" rather than against names actually bound to the tifffile module. A
    probe file with exactly this shape passed all three tests in this module
    (test_the_scan_actually_reads_files, test_the_owner_really_is_a_writer,
    test_no_file_under_bin_writes_a_tiff_except_ome_io) -- 113 passed -- before
    _tifffile_writer_calls learned to collect `import tifffile as X` aliases the same
    way it already collected `from tifffile import X` bare names.
    """
    probe = tmp_path / "probe.py"
    probe.write_text("import tifffile as tf\ntf.imwrite('x.tif', data)\n")
    calls = _tifffile_writer_calls(probe)
    assert calls == [(2, "tf.imwrite(...)")], (
        f"expected the aliased call to be caught at line 2, got {calls}"
    )


def test_an_unrelated_name_that_looks_like_an_alias_is_not_swept_up(tmp_path):
    """The alias check must bind to a REAL `import tifffile as ...` statement, not
    just match on the string "tf" -- a local variable that happens to be called `tf`
    for an unrelated object must not be flagged."""
    probe = tmp_path / "probe.py"
    probe.write_text("tf = SomeUnrelatedWriter()\ntf.imwrite('x.tif', data)\n")
    assert _tifffile_writer_calls(probe) == []
