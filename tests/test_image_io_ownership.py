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
4. ``test_foreign_image_writer_cannot_write_a_tiff`` -- no non-tifffile image writer
   under ``bin/`` may write a TIFF. See "The blind spot" below.
5. ``test_detector_flags_a_planted_raw_write`` / ``test_detector_ignores_cv2_imwrite``
   and the ``test_foreign_detector_*`` set -- positive and negative controls on both
   AST matchers, so a matcher that silently stopped matching anything (or started
   matching PNG writes) cannot pass as clean.

``tifffile.memmap`` is included in the write set even though nothing calls it today:
given a ``shape``/``dtype`` it *creates* a new TIFF, so it is a live route to a
non-bigtiff file. If a read-only use is ever wanted, the owner should grow a read
entry point rather than this guard growing an exemption.

The blind spot a tifffile-only matcher would have, and how it is closed
----------------------------------------------------------------------
``tifffile`` is not the only library under ``bin/`` that can write a TIFF.
``bin/generate_preprocess_qc.py`` already imports ``skimage.io.imsave``, and
``bin/utils/qc.py`` calls ``cv2.imwrite``. Both write PNG *today*, and PNG is
legitimately out of scope -- it has no 4 GB offset problem and no bigtiff concept.
But the same ``imsave`` call with a ``.tif`` path produces a classic TIFF that a
tifffile-only scan would never see: a brand-new guard shipping with a known hole is
how a guard ends up checking nothing.

So ``test_foreign_image_writer_cannot_write_a_tiff`` applies a different rule to those
libraries: a non-tifffile image write must have a **statically provable non-TIFF
extension**. ``_static_suffix`` resolves the path argument through the small set of
forms this repo actually uses (string literal, ``str(x)``, ``Path / f"...png"``,
``.with_suffix(".png")``, and one hop through a local assignment). A ``.tif``/``.tiff``
suffix fails; so does a suffix that cannot be resolved at all, because "a foreign
writer whose format nobody can see" is precisely the case this exists to catch.
Allowlisting the two known PNG sites by name was rejected: an allowlist entry is
exactly the kind of thing that outlives the reason for it.

PIL is handled by import: no ``bin/`` module imports it today, and its only use here
would be image I/O, so any ``.save(...)`` call in a file that imports PIL is flagged.
That is a zero-false-positive rule now and a closed door later.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
OWNER = BIN_DIR / "utils" / "image_io.py"

# tifffile callables that can CREATE a TIFF file on disk.
TIFF_WRITE_FUNCS = {"imwrite", "imsave", "TiffWriter", "memmap"}

# Image-writing callables from libraries that are NOT tifffile. These may write PNG
# (legitimate, out of scope) but must never write a TIFF -- see the module docstring.
# Keyed by (module-or-package prefix, attribute).
FOREIGN_WRITE_FUNCS = {
    ("skimage.io", "imsave"),
    ("skimage.io", "imwrite"),
    ("cv2", "imwrite"),
}
TIFF_SUFFIXES = {".tif", ".tiff"}

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


def _foreign_bindings(
    tree: ast.AST,
) -> tuple[dict[str, tuple[str, str]], set[str], bool]:
    """Names bound to a foreign image writer, plus whether the file imports PIL.

    Returns ``(attr_bases, direct_funcs, imports_pil)`` where ``attr_bases`` maps a
    local name to the ``(module, attr)`` pair it stands for (e.g. ``io -> ("skimage.io",
    "*")`` from ``from skimage import io``), and ``direct_funcs`` holds names bound
    straight to a write callable (``from skimage.io import imsave``).
    """
    attr_bases: dict[str, tuple[str, str]] = {}
    direct_funcs: set[str] = set()
    imports_pil = False
    foreign_modules = {m for m, _ in FOREIGN_WRITE_FUNCS}
    foreign_attrs = {a for _, a in FOREIGN_WRITE_FUNCS}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {"PIL"}:
                    imports_pil = True
                if alias.name in foreign_modules:
                    attr_bases[alias.asname or alias.name.split(".")[-1]] = (
                        alias.name,
                        "*",
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "PIL":
                imports_pil = True
            if module in foreign_modules:
                for alias in node.names:
                    if alias.name in foreign_attrs:
                        direct_funcs.add(alias.asname or alias.name)
            # `from skimage import io` -> the submodule name carries the writer
            for alias in node.names:
                dotted = f"{module}.{alias.name}" if module else alias.name
                if dotted in foreign_modules:
                    attr_bases[alias.asname or alias.name] = (dotted, "*")
    return attr_bases, direct_funcs, imports_pil


def _last_assignments(tree: ast.AST) -> dict[str, ast.AST]:
    """Map ``name -> last value assigned to it`` anywhere in the file.

    Deliberately crude: one hop is all ``_static_suffix`` needs for the forms this
    repo writes (``p = out_dir / f"...png"`` then ``imsave(str(p), ...)``), and a real
    dataflow analysis in a guard is a second thing that can silently stop working.
    """
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
    return out


def _trailing_extension(text: str):
    """Trailing ``.ext`` of a string, or None.

    NOT ``Path(text).suffix``: the argument to ``.with_suffix(".png")`` and the tail
    constant of ``f"{name}.tif"`` are bare extensions, and pathlib reads ``".tif"`` as
    a dotfile with no suffix at all -- which silently turned every provable case into
    an unprovable one when this was first written.
    """
    match = re.search(r"(\.[A-Za-z0-9]+)$", text)
    return match.group(1).lower() if match else None


def _static_suffix(node: ast.AST, assigns: dict[str, ast.AST], depth: int = 0):
    """Best-effort static file suffix of a path expression, or None if unprovable.

    Handles exactly the forms `bin/` uses: a string literal, ``str(x)``,
    ``Path / f"...ext"``, ``x.with_suffix(".ext")``, and one hop through a local
    name. None means "cannot prove", which the caller treats as a failure -- an
    unprovable format from a foreign writer is the case this guard exists for.
    """
    if depth > 6:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _trailing_extension(node.value)
    if isinstance(node, ast.JoinedStr):
        for piece in reversed(node.values):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                suffix = _trailing_extension(piece.value)
                if suffix:
                    return suffix
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _static_suffix(node.right, assigns, depth + 1)
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "str" and node.args:
            return _static_suffix(node.args[0], assigns, depth + 1)
        if isinstance(fn, ast.Attribute) and fn.attr in {"with_suffix", "with_name"}:
            if node.args:
                return _static_suffix(node.args[0], assigns, depth + 1)
        return None
    if isinstance(node, ast.Name):
        value = assigns.get(node.id)
        return _static_suffix(value, assigns, depth + 1) if value is not None else None
    return None


def foreign_image_writes(py_file: Path) -> list[tuple[int, str, object]]:
    """``(lineno, rendered_callee, static_suffix_or_None)`` for non-tifffile writes."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return []

    attr_bases, direct_funcs, imports_pil = _foreign_bindings(tree)
    assigns = _last_assignments(tree)
    foreign_attrs = {a for _, a in FOREIGN_WRITE_FUNCS}

    found: list[tuple[int, str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        callee = None
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            base = attr_bases.get(fn.value.id)
            if base is not None and fn.attr in foreign_attrs:
                callee = f"{fn.value.id}.{fn.attr}"
            elif imports_pil and fn.attr == "save":
                callee = f"{fn.value.id}.save"
        elif isinstance(fn, ast.Attribute) and fn.attr == "save" and imports_pil:
            callee = "<expr>.save"
        elif isinstance(fn, ast.Name) and fn.id in direct_funcs:
            callee = fn.id
        if callee is None:
            continue
        path_arg = node.args[0] if node.args else None
        suffix = _static_suffix(path_arg, assigns) if path_arg is not None else None
        found.append((node.lineno, callee, suffix))
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


# ── the non-tifffile blind spot ────────────────────────────────────────────────
# The two files under bin/ that write images with something other than tifffile.
# Named as paths, for the same reason KNOWN_OFFENDERS is: so a narrowed scan fails
# here first rather than passing on an empty set.
KNOWN_FOREIGN_WRITER_FILES = (
    BIN_DIR / "generate_preprocess_qc.py",  # skimage.io.imsave
    BIN_DIR / "utils" / "qc.py",  # cv2.imwrite
)


def test_foreign_image_writer_cannot_write_a_tiff() -> None:
    """A non-tifffile image write must provably not be a TIFF.

    PNG is fine and stays reachable. What is not fine is a foreign writer whose
    output format nobody can see: `bin/generate_preprocess_qc.py` already imports
    `skimage.io.imsave`, and the same call with a `.tif` path would sail past the
    tifffile-only tests above with no bigtiff and no owner.
    """
    offenders: list[str] = []
    for py_file in scanned_files():
        for lineno, callee, suffix in foreign_image_writes(py_file):
            if suffix is None:
                offenders.append(
                    f"  {_rel(py_file)}:{lineno}  {callee}(...)  <-- format not "
                    "statically provable"
                )
            elif suffix in TIFF_SUFFIXES:
                offenders.append(
                    f"  {_rel(py_file)}:{lineno}  {callee}(...)  <-- writes {suffix}"
                )

    assert not offenders, (
        "non-tifffile image write(s) that are, or may be, TIFFs:\n"
        + "\n".join(offenders)
        + "\n\nA TIFF goes through bin/utils/image_io.py, which sets bigtiff. A PNG "
        "is fine, but its extension has to be visible at the call site (a literal, an "
        "f-string, or .with_suffix) -- not hidden behind a value this check cannot "
        "resolve."
    )


@pytest.mark.parametrize(
    "path", KNOWN_FOREIGN_WRITER_FILES, ids=[p.name for p in KNOWN_FOREIGN_WRITER_FILES]
)
def test_foreign_scan_reaches_the_known_foreign_writer_files(path: Path) -> None:
    """Anti-narrow-glob control for the foreign-writer rule.

    Asserts reach *and* that the matcher actually finds the write, so a binding
    resolver that quietly stopped resolving `from skimage.io import imsave` cannot
    leave this rule passing over an empty set.
    """
    assert path.exists(), f"{_rel(path)} no longer exists; update this guard"
    assert path in scanned_files(), f"{_rel(path)} is NOT reached by this guard's scan"
    found = foreign_image_writes(path)
    assert found, (
        f"the foreign-writer matcher found nothing in {_rel(path)}, which is one of "
        "the two files known to call one -- the matcher, not the file, is what "
        "changed."
    )


def test_foreign_detector_flags_a_planted_skimage_tif_write(tmp_path: Path) -> None:
    """Positive control: the exact hole this rule closes."""
    planted = tmp_path / "planted_skimage.py"
    planted.write_text(
        "from skimage.io import imsave\n"
        "def go(a, out_dir, name):\n"
        "    p = out_dir / f'{name}.tif'\n"
        "    imsave(str(p), a, check_contrast=False)\n"
    )
    found = foreign_image_writes(planted)
    assert [(c, sfx) for _, c, sfx in found] == [("imsave", ".tif")], found


def test_foreign_detector_accepts_the_two_png_forms_the_repo_uses(
    tmp_path: Path,
) -> None:
    """Negative control: both real call shapes resolve to .png and must not be flagged.

    `generate_preprocess_qc.py` builds its path as ``dir / f"...png"``; `qc.py` uses
    ``output_path.with_suffix(".png")``. Both then pass it through ``str(...)``.
    """
    planted = tmp_path / "planted_png.py"
    planted.write_text(
        "import cv2\n"
        "from skimage.io import imsave\n"
        "def go(a, out_dir, name, output_path):\n"
        "    p = out_dir / f'{name}_x.png'\n"
        "    imsave(str(p), a)\n"
        "    q = output_path.with_suffix('.png')\n"
        "    cv2.imwrite(str(q), a)\n"
    )
    assert [sfx for _, _, sfx in foreign_image_writes(planted)] == [".png", ".png"]


def test_foreign_detector_flags_an_unprovable_path(tmp_path: Path) -> None:
    """A foreign write whose format cannot be resolved is a failure, not a pass."""
    planted = tmp_path / "planted_opaque.py"
    planted.write_text(
        "from skimage.io import imsave\ndef go(a, path):\n    imsave(path, a)\n"
    )
    assert [sfx for _, _, sfx in foreign_image_writes(planted)] == [None]


def test_foreign_detector_flags_pil_save(tmp_path: Path) -> None:
    """PIL is closed by import: no bin/ module imports it, and its only use here
    would be image I/O, so any .save() in a PIL-importing file is flagged."""
    planted = tmp_path / "planted_pil.py"
    planted.write_text(
        "from PIL import Image\ndef go(a, path):\n    Image.fromarray(a).save(path)\n"
    )
    assert foreign_image_writes(planted), "a PIL .save() must not be invisible"


def test_foreign_detector_ignores_save_outside_a_pil_file(tmp_path: Path) -> None:
    """...and `.save()` on something that is not PIL must not be matched, or the rule
    would fire on every model checkpoint and DataFrame in the repo."""
    planted = tmp_path / "planted_not_pil.py"
    planted.write_text("def go(model, path):\n    model.save(path)\n")
    assert foreign_image_writes(planted) == []
