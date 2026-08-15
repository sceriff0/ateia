#!/usr/bin/env python3
"""`bin/utils/qc.py`'s display autoscaler: behaviour, and the branch that never ran.

`autoscale_for_display` had four call sites (bin/utils/qc.py:141,142,333,334), all four
passing `method="minmax"` explicitly, and no test of any kind. Its `"percentile"` branch
was the sole reachable path into `bin/utils/registration_utils.py` -- an 89-line module
holding one function -- so that whole module was live only in the sense that an import
statement named it. `tests/test_no_dead_bin_modules.py` could not see this: the module IS
imported by a production file, which is all that test asks. Deadness one level down, in
an unreachable branch, is invisible to it by construction.

The numeric cases below are not ceremony around a deletion. They are the coverage the
function never had, and they are what makes the deletion safe to assert: removing the
`method` parameter changes the signature of a function whose only remaining behaviour is
these five properties.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin" / "utils"))
QC_PY = REPO_ROOT / "bin" / "utils" / "qc.py"

# `import cv2` used to sit at qc.py's module top level for the benefit of ONE call
# (`cv2.imwrite`, in create_registration_qc). opencv is in neither requirements.txt nor
# CI's pip line, so importing this module raised ModuleNotFoundError everywhere pytest
# runs -- which is why a function with four production call sites had no test at all, and
# why any test written for it would have been a permanent skip rather than coverage. The
# import is now inside the one function that needs it; everything else in qc.py imports
# with the dependencies the test environment actually has.
from qc import autoscale_for_display  # noqa: E402


def _signature_params() -> list[str]:
    """autoscale_for_display's parameter names, read from the source.

    From the AST rather than `inspect`, so this still answers even if the module ever
    becomes unimportable again -- the failure would then be "the signature is wrong",
    not "the test could not run".
    """
    tree = ast.parse(QC_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "autoscale_for_display":
            return [a.arg for a in node.args.args]
    raise AssertionError("autoscale_for_display not found in bin/utils/qc.py")


def test_minmax_spans_the_full_uint8_range():
    img = np.random.rand(64, 64) * 1000
    out = autoscale_for_display(img)
    assert out.dtype == np.uint8
    assert out.min() == 0
    assert out.max() == 255


def test_minmax_is_linear_in_the_input():
    """The midpoint of a linear ramp lands at the midpoint of the output range."""
    img = np.array([[0.0, 50.0, 100.0]])
    out = autoscale_for_display(img)
    assert out.tolist() == [[0, 128, 255]]


def test_rounds_rather_than_truncates():
    """The `np.round` before the uint8 cast is load-bearing.

    Truncation maps the 0.5 midpoint to 127; rounding maps it to 128. A plain
    `.astype(np.uint8)` would silently darken every display image by up to one level.
    """
    img = np.array([[0.0, 0.5, 1.0]])
    assert autoscale_for_display(img).tolist() == [[0, 128, 255]]


def test_a_constant_image_does_not_divide_by_zero():
    """max-min is 0 for a flat field; the 1e-6 floor is what stops a NaN reaching uint8."""
    out = autoscale_for_display(np.full((8, 8), 7.0))
    assert out.dtype == np.uint8
    assert np.all(out == 0)


def test_preserves_shape():
    assert autoscale_for_display(np.random.rand(5, 9) * 10).shape == (5, 9)


def test_takes_no_method_parameter():
    """The dead `"percentile"` branch is gone, and so is the switch that selected it.

    Keeping `method="minmax"` as a parameter after deleting the only other value it
    accepted would leave a knob with one setting -- and the next reader would have to
    re-derive that there is nothing else to pass.
    """
    params = _signature_params()
    assert params == ["img"], (
        f"autoscale_for_display{tuple(params)} still takes a scaling-method switch; the "
        "only branch it selected other than minmax was the unreachable 'percentile' one."
    )


def test_registration_utils_is_deleted():
    """The module the dead branch existed to reach.

    Asserted by absence rather than by import failure so the message says why: this is
    not "the import broke", it is "the module was unreachable and was removed".
    """
    dead = REPO_ROOT / "bin" / "utils" / "registration_utils.py"
    assert not dead.exists(), (
        f"{dead.relative_to(REPO_ROOT)} still exists. Its only caller was "
        "autoscale_for_display's 'percentile' branch, which no call site ever selected."
    )


def test_nothing_still_names_the_deleted_module():
    """A leftover import or docstring reference is the same defect wearing prose.

    bin/utils/__init__.py listed it in a module inventory and bin/utils/qc.py's `See
    Also` block pointed at it; neither is an import, so neither would fail at runtime --
    they would just tell the next reader to go and read a file that is not there.
    """
    offenders = []
    for py in sorted((REPO_ROOT / "bin").rglob("*.py")):
        if "registration_utils" in py.read_text(encoding="utf-8"):
            offenders.append(str(py.relative_to(REPO_ROOT)))
    assert not offenders, f"still reference the deleted module: {offenders}"


def test_percentile_is_no_longer_a_documented_option():
    src = (REPO_ROOT / "bin" / "utils" / "qc.py").read_text(encoding="utf-8")
    assert "percentile" not in src.lower(), (
        "bin/utils/qc.py still advertises percentile scaling. The branch is gone; the "
        "docstring must not keep offering it."
    )


def test_the_deleted_branch_really_was_unreachable():
    """Evidence, not assertion: no call site ever selected anything but minmax.

    Re-derived from the source rather than trusted, because "all four call sites pass
    minmax" is exactly the kind of claim that is true when written and false a year
    later. If a caller ever passes something else, the signature check above fails
    first -- but this states the reason the deletion was safe at all.
    """
    src = (REPO_ROOT / "bin" / "utils" / "qc.py").read_text(encoding="utf-8")
    calls = [line for line in src.splitlines() if "autoscale_for_display(" in line]
    invocations = [c for c in calls if "def autoscale_for_display" not in c]
    assert invocations, "expected autoscale_for_display to still be called somewhere"
    for c in invocations:
        assert "method=" not in c, (
            f"a caller still passes a scaling method: {c.strip()!r}"
        )


def test_accepts_the_dtypes_qc_actually_hands_it():
    """create_registration_qc feeds it raw OME-TIFF planes, not floats in [0, 1]."""
    for dtype in (np.uint16, np.int32, np.float32):
        img = (np.random.rand(16, 16) * 4095).astype(dtype)
        out = autoscale_for_display(img)
        assert out.dtype == np.uint8
        assert out.max() == 255, dtype
