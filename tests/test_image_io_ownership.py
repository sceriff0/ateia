#!/usr/bin/env python3
"""Guard: every TIFF write under ``bin/`` goes through the one owner, which always sets bigtiff.

Why this guard exists
---------------------
Before Task 11 the pipeline had **six mutually distinct TIFF write mechanisms** and
no owner for image I/O at all. Three of them are legitimate, documented variants
(deliberately non-OME single-shot, streamed tile-writer, true multi-resolution
pyramid). One of them was a pure defect class: ``segment.py``, ``segment_cellsam.py``,
``segment_instantseg.py`` and ``extract_mask_series.py`` all wrote **full-resolution
label masks with no ``bigtiff``**, and ``extract_mask_series.py`` had no compression
either, while ``bin/utils/qc.py`` wrote its *full-resolution* composite without
``bigtiff`` and its *downsampled* one with it -- in the same function, under two
different metadata conventions (ImageJ vs OME).

``bin/tiled_stitch.py`` states the stakes in its own words:

    bigtiff is mandatory, not an optimisation: a registered slide is written
    uncompressed at full resolution, so classic TIFF's 32-bit offsets overflow
    (struct.error: 'I' format requires 0 <= number <= 4294967295) the moment the
    output crosses 4 GB -- C x H x W x itemsize, reached by any real WSI.

Five sites repeating the same omission across three sibling segmentation backends is
not five bugs; it is a missing owner. So this guard is structural, not a list of
patches: it makes the defect class *unwritable* rather than merely unwritten today.

What it checks
--------------
1. ``test_no_raw_tiff_write_outside_the_owner`` -- no ``.py`` under ``bin/`` other than
   the owner may call ``tifffile.imwrite`` / ``imsave`` / ``TiffWriter`` / ``memmap``.
2. ``test_owner_always_sets_bigtiff`` -- every write call *inside* the owner passes a
   literal ``bigtiff=True``.
3. ``test_scan_reaches_the_five_known_offender_files`` -- the scope glob provably
   reaches all five files that were the original offenders. A static check whose glob
   quietly excludes the interesting files is this repo's single most recurring defect
   (see CLAUDE.md, "Watch every new guard fail before you trust it"), so the scope is
   itself asserted rather than assumed. This test is the one that keeps holding after
   the fix: it pins the *reach* of the scan, not the presence of the bug.
4. ``test_detector_flags_a_planted_raw_write`` / ``test_detector_ignores_cv2_imwrite``
   -- positive and negative controls on the AST matcher, so a matcher that silently
   stopped matching anything (or started matching PNG writes) cannot pass as clean.

``tifffile.memmap`` is included in the write set even though nothing calls it today:
given a ``shape``/``dtype`` it *creates* a new TIFF, so it is a live route to a
non-bigtiff file. If a read-only use is ever wanted, the owner should grow a read
entry point rather than this guard growing an exemption.

PNG writes (``cv2.imwrite`` in ``bin/utils/qc.py``) are deliberately out of scope: PNG
has no 4 GB offset problem and no bigtiff concept. The negative control below pins
that this is a decision, not a gap in the matcher.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
OWNER = BIN_DIR / "utils" / "image_io.py"

# tifffile callables that can CREATE a TIFF file on disk.
TIFF_WRITE_FUNCS = {"imwrite", "imsave", "TiffWriter", "memmap"}

# The five files that wrote >4 GB-capable data with no bigtiff before Task 11.
# Listed as paths, not as a pattern, so a narrowed glob fails loudly here first.
KNOWN_OFFENDERS = (
    BIN_DIR / "extract_mask_series.py",
    BIN_DIR / "segment.py",
    BIN_DIR / "segment_cellsam.py",
    BIN_DIR / "segment_instantseg.py",
    BIN_DIR / "utils" / "qc.py",
)


def scanned_files() -> list[Path]:
    """Every ``.py`` under ``bin/``. The owner is scanned too (rule 2 applies to it)."""
    return sorted(BIN_DIR.rglob("*.py"))


def _tifffile_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to the ``tifffile`` module, and names bound to its write callables.

    Handles ``import tifffile``, ``import tifffile as tf``, and
    ``from tifffile import imwrite, TiffWriter`` -- including imports made inside a
    function body, which ``bin/export_spatialdata.py`` does deliberately.
    """
    module_aliases: set[str] = set()
    direct_funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tifffile":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "tifffile":
                for alias in node.names:
                    if alias.name in TIFF_WRITE_FUNCS:
                        direct_funcs.add(alias.asname or alias.name)
    return module_aliases, direct_funcs


def tiff_write_calls(py_file: Path) -> list[tuple[int, str, ast.Call]]:
    """``(lineno, rendered_callee, node)`` for every tifffile write call in the file."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return []

    module_aliases, direct_funcs = _tifffile_bindings(tree)
    found: list[tuple[int, str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr in TIFF_WRITE_FUNCS
            and isinstance(fn.value, ast.Name)
            and fn.value.id in module_aliases
        ):
            found.append((node.lineno, f"{fn.value.id}.{fn.attr}", node))
        elif isinstance(fn, ast.Name) and fn.id in direct_funcs:
            found.append((node.lineno, fn.id, node))
    return found


def _has_literal_bigtiff_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "bigtiff":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT))


# ── the guard ──────────────────────────────────────────────────────────────────
def test_no_raw_tiff_write_outside_the_owner() -> None:
    """All TIFF writing lives in bin/utils/image_io.py."""
    offenders: list[str] = []
    for py_file in scanned_files():
        if py_file == OWNER:
            continue
        for lineno, callee, call in tiff_write_calls(py_file):
            flag = "" if _has_literal_bigtiff_true(call) else "  <-- NO BIGTIFF"
            offenders.append(f"  {_rel(py_file)}:{lineno}  {callee}(...){flag}")

    assert not offenders, (
        "raw tifffile write(s) outside bin/utils/image_io.py:\n"
        + "\n".join(offenders)
        + "\n\nRoute them through the owner (write_ome_tiff / write_mask_tiff / "
        "write_plain_tiff / open_tiff_writer). See that module's docstring for which "
        "entry point each documented variant maps to."
    )


def test_no_tiff_write_lacks_bigtiff() -> None:
    """No TIFF write anywhere under bin/ may omit bigtiff.

    Kept separate from the ownership rule so the *defect class* has its own name in
    the failure output: the ownership test says "this is in the wrong place", this one
    says "this file can overflow 32-bit TIFF offsets at 4 GB".
    """
    offenders: list[str] = []
    for py_file in scanned_files():
        for lineno, callee, call in tiff_write_calls(py_file):
            if not _has_literal_bigtiff_true(call):
                offenders.append(f"  {_rel(py_file)}:{lineno}  {callee}(...)")

    assert not offenders, (
        "TIFF write(s) with no bigtiff=True -- these overflow at 4 GB:\n"
        + "\n".join(offenders)
    )


def test_owner_always_sets_bigtiff() -> None:
    """Every write inside the owner passes a literal bigtiff=True.

    Literal, not a variable or a default parameter: a caller-supplied ``bigtiff``
    would put the decision back where it was, which is the whole thing this task
    removed.
    """
    assert OWNER.exists(), f"the image-write owner is missing: {_rel(OWNER)}"
    calls = tiff_write_calls(OWNER)
    assert calls, (
        f"{_rel(OWNER)} contains no tifffile write calls -- either the owner moved or "
        "this guard's matcher has stopped matching, which would make every other "
        "assertion here vacuous."
    )
    bad = [
        f"  {_rel(OWNER)}:{lineno}  {callee}(...)"
        for lineno, callee, call in calls
        if not _has_literal_bigtiff_true(call)
    ]
    assert not bad, "owner write(s) without a literal bigtiff=True:\n" + "\n".join(bad)


# ── controls on the guard itself ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "offender", KNOWN_OFFENDERS, ids=[p.name for p in KNOWN_OFFENDERS]
)
def test_scan_reaches_the_five_known_offender_files(offender: Path) -> None:
    """The scope glob provably reaches each of the five original offenders.

    This is the anti-narrow-glob control. It asserts *reach*, not defect, so it keeps
    holding after the fix -- and it fails the moment someone narrows the glob (e.g. to
    ``bin/*.py``, which would silently drop ``bin/utils/qc.py``).
    """
    assert offender.exists(), f"{_rel(offender)} no longer exists; update this guard"
    assert offender in scanned_files(), (
        f"{_rel(offender)} is NOT reached by this guard's scan -- the scope glob "
        "excludes a file that was a known offender."
    )


def test_detector_flags_a_planted_raw_write(tmp_path: Path) -> None:
    """Positive control: the matcher really does fire on a raw write."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import tifffile as tf\n"
        "import numpy as np\n"
        "def go(a):\n"
        "    tf.imwrite('x.tif', a)\n"
        "    with tf.TiffWriter('y.tif') as w:\n"
        "        w.write(a)\n"
    )
    found = tiff_write_calls(planted)
    assert [c for _, c, _ in found] == ["tf.imwrite", "tf.TiffWriter"], found
    assert not any(_has_literal_bigtiff_true(call) for _, _, call in found)


def test_detector_flags_a_from_import_write(tmp_path: Path) -> None:
    """Positive control: ``from tifffile import imwrite`` is not a way around the guard."""
    planted = tmp_path / "planted_from.py"
    planted.write_text(
        "from tifffile import imwrite\ndef go(a):\n    imwrite('x.tif', a)\n"
    )
    assert [c for _, c, _ in tiff_write_calls(planted)] == ["imwrite"]


def test_detector_ignores_cv2_imwrite(tmp_path: Path) -> None:
    """Negative control: PNG writes are out of scope, and same-named calls on other
    modules must not be matched. ``bin/utils/qc.py`` genuinely calls ``cv2.imwrite``."""
    planted = tmp_path / "planted_cv2.py"
    planted.write_text("import cv2\ndef go(a):\n    cv2.imwrite('x.png', a)\n")
    assert tiff_write_calls(planted) == []
