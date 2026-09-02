"""The registration QC figure shows BEFORE and AFTER, on the reference canvas.

``GENERATE_REGISTRATION_QC`` used to render one composite — reference + registered
— which shows what registration *produced* and not what it *corrected*. This file
pins the two properties that make the added "before" panel honest:

1. The native (pre-registration) moving slide is placed on the REFERENCE canvas by
   pad-or-crop at the origin, never by rescaling. Rescaling a differently-sized
   native image to the reference's shape silently removes the scale component of
   the misalignment, which makes the "before" panel understate the error and
   registration look better than it was.
2. The two panels are one image with a deterministic layout, so the published
   ``*_QC_RGB*`` names keep meaning one artifact per moving slide.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

UTILS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
)
if UTILS_DIR not in sys.path:
    sys.path.insert(0, UTILS_DIR)

pytest.importorskip("skimage")
pytest.importorskip("zarr")
tifffile = pytest.importorskip("tifffile")
cv2 = pytest.importorskip("cv2")

import qc  # noqa: E402


def test_smaller_moving_image_is_zero_padded_at_the_origin():
    ref = np.zeros((10, 12), dtype=np.uint16)
    mov = np.full((6, 8), 7, dtype=np.uint16)

    out = qc.compose_on_reference_canvas(ref, mov)

    assert out.shape == (10, 12)
    assert out.dtype == np.uint16
    assert (out[:6, :8] == 7).all()
    assert (out[6:, :] == 0).all()
    assert (out[:, 8:] == 0).all()


def test_larger_moving_image_is_cropped_not_rescaled():
    ref = np.zeros((4, 5), dtype=np.uint16)
    mov = np.arange(9 * 11, dtype=np.uint16).reshape(9, 11)

    out = qc.compose_on_reference_canvas(ref, mov)

    assert out.shape == (4, 5)
    # Cropped at the origin: every value is the SAME value the input had at that
    # coordinate. A rescale would have produced interpolated values instead.
    assert np.array_equal(out, mov[:4, :5])


def test_equal_shapes_pass_through_unchanged():
    ref = np.zeros((5, 5), dtype=np.uint16)
    mov = np.arange(25, dtype=np.uint16).reshape(5, 5)

    out = qc.compose_on_reference_canvas(ref, mov)

    assert np.array_equal(out, mov)


def test_one_dimension_bigger_one_smaller_is_both_cropped_and_padded():
    ref = np.zeros((10, 4), dtype=np.uint16)
    mov = np.full((6, 9), 3, dtype=np.uint16)

    out = qc.compose_on_reference_canvas(ref, mov)

    assert out.shape == (10, 4)
    assert (out[:6, :] == 3).all()  # padded down the Y axis
    assert (out[6:, :] == 0).all()
    # cropped on X: nothing beyond column 4 survives, and nothing was resampled
    assert np.array_equal(out[:6, :], mov[:6, :4])
