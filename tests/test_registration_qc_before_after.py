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


def _plane(seed, h, w):
    return np.random.default_rng(seed).integers(0, 65536, size=(h, w), dtype=np.uint16)


def test_render_before_after_is_two_panels_wide_plus_the_gap():
    ref = _plane(1, 20, 30)
    native = _plane(2, 20, 30)
    registered = _plane(3, 20, 30)

    out = qc.render_before_after(ref, native, registered, scale_factor=1.0, gap=8)

    assert out.dtype == np.uint8
    assert out.shape == (3, 20, 30 * 2 + 8)


def test_render_before_after_puts_registered_on_the_right_and_native_on_the_left():
    """Red is the MOVING slide in both panels; green is the reference in both.
    A native and a registered plane that differ must therefore produce panels
    whose red channels differ, while their green channels are identical.

    ``native`` is a constant-zero plane and ``registered`` is a plane with real
    variance, NOT a second constant. ``create_nuclear_overlay`` autoscales each
    plane independently with a min-max stretch, and a min-max stretch of ANY
    constant-valued plane collapses to all-zero regardless of the constant's
    value (``(x - x) / eps == 0`` everywhere) -- so two different constants
    would produce identical (all-zero) red channels and the panels would be
    indistinguishable for a reason that has nothing to do with
    ``render_before_after`` itself.
    """
    ref = _plane(1, 16, 16)
    native = np.zeros((16, 16), dtype=np.uint16)
    registered = _plane(3, 16, 16)

    out = qc.render_before_after(ref, native, registered, scale_factor=1.0, gap=4)

    left = out[:, :, :16]
    right = out[:, :, 16 + 4 :]

    # Green (index 1) is the reference in both panels -> identical.
    assert np.array_equal(left[1], right[1])
    # Red (index 0) is the moving slide -> a constant-zero native and a
    # variance-carrying registered plane cannot produce the same red channel.
    assert not np.array_equal(left[0], right[0])
    assert left[0].max() == 0
    assert right[0].max() > 0


def test_render_before_after_marks_the_panel_boundary_in_blue():
    ref = _plane(1, 8, 9)
    out = qc.render_before_after(
        ref, _plane(2, 8, 9), _plane(3, 8, 9), scale_factor=1.0, gap=5
    )

    band = out[:, :, 9 : 9 + 5]
    assert (band[2] == 255).all(), "the separator band must be blue 255"
    assert (band[0] == 0).all() and (band[1] == 0).all(), (
        "the separator must not carry red or green, or it would read as signal"
    )
    # Blue is the annotation channel and is unused everywhere else.
    assert out[2, :, :9].max() == 0
    assert out[2, :, 9 + 5 :].max() == 0


def test_render_before_after_without_a_native_is_a_single_panel():
    """The script stays runnable by hand against a pair of images. With no
    native there is no 'before' to draw, and the output degenerates to exactly
    the one-panel composite this process produced before the change."""
    ref = _plane(1, 12, 14)
    registered = _plane(3, 12, 14)

    out = qc.render_before_after(ref, None, registered, scale_factor=1.0, gap=8)

    assert out.shape == (3, 12, 14)
    assert out[2].max() == 0


def test_render_before_after_pads_a_smaller_native_onto_the_reference_canvas():
    """The panels must be the same height and width even when the native slide
    is a different size — that is what compose_on_reference_canvas is for."""
    ref = _plane(1, 20, 24)
    native = _plane(2, 11, 13)  # smaller in both axes
    registered = _plane(3, 20, 24)

    out = qc.render_before_after(ref, native, registered, scale_factor=1.0, gap=6)

    assert out.shape == (3, 20, 24 * 2 + 6)


def test_render_before_after_honours_the_scale_factor():
    ref = _plane(1, 40, 40)
    out = qc.render_before_after(
        ref, _plane(2, 40, 40), _plane(3, 40, 40), scale_factor=0.5, gap=4
    )

    # Each panel is 20 wide after a 0.5 rescale; the gap is not rescaled.
    assert out.shape == (3, 20, 20 * 2 + 4)


def test_cyx_to_bgr_reorders_for_opencv():
    cyx = np.zeros((3, 2, 2), dtype=np.uint8)
    cyx[0] = 10  # red
    cyx[1] = 20  # green
    cyx[2] = 30  # blue

    bgr = qc.cyx_to_bgr(cyx)

    assert bgr.shape == (2, 2, 3)
    assert (bgr[:, :, 0] == 30).all()  # OpenCV channel 0 is blue
    assert (bgr[:, :, 1] == 20).all()
    assert (bgr[:, :, 2] == 10).all()
