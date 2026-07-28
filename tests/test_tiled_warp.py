"""Tests for bin/utils/tiled_warp.py — the image warp of the STARE method (the WARP_TILE core).

Warps a moving image into the reference frame through the same transform the QC warper uses: the
global affine ``M0`` plus the smooth mesh residual. Uses the inverse map + bilinear resampling, so
a non-negative image stays non-negative — the guarantee downstream quantification relies on.
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)

from mesh_field import MeshField
from tiled_warp import warp_image


def _translation(tx, ty):
    return np.array([[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]])


def test_identity_transform_returns_the_image_unchanged():
    rng = np.random.default_rng(0)
    img = rng.uniform(0, 100, size=(20, 24))
    out = warp_image(img, np.eye(3), None, (20, 24))
    np.testing.assert_allclose(out, img, atol=1e-9)


def test_rigid_translation_moves_content_by_M0():
    img = np.zeros((20, 20))
    img[5, 7] = 100.0  # (x=7, y=5)
    # M0 forward maps moving -> reference: (7,5) -> (10, 3)
    out = warp_image(img, _translation(3.0, -2.0), None, (20, 20))
    assert np.unravel_index(np.argmax(out), out.shape) == (3, 10)
    assert out.max() == 100.0


def test_mesh_residual_is_applied_on_top_of_the_rigid_warp():
    img = np.zeros((30, 30))
    img[10, 10] = 100.0  # (x=10, y=10)
    # rigid puts it at (13, 12); a constant mesh of (+2, +1) finishes at (15, 13)
    mesh = MeshField(
        np.array([0.0, 100.0]),
        np.array([0.0, 100.0]),
        np.array([[[2.0, 1.0], [2.0, 1.0]], [[2.0, 1.0], [2.0, 1.0]]]),
    )
    out = warp_image(img, _translation(3.0, 2.0), mesh, (30, 30))
    assert np.unravel_index(np.argmax(out), out.shape) == (13, 15)


def test_warp_never_produces_negative_pixels():
    rng = np.random.default_rng(3)
    img = rng.uniform(0.0, 65535.0, size=(40, 40))  # non-negative uint16-range
    mesh = MeshField(
        np.array([0.0, 40.0]),
        np.array([0.0, 40.0]),
        np.array([[[3.0, -2.0], [1.0, 4.0]], [[-4.0, 1.0], [2.0, 2.0]]]),
    )
    out = warp_image(img, _translation(1.5, -0.5), mesh, (40, 40))
    assert out.min() >= 0.0
    assert out.max() <= img.max() + 1e-9


def test_multichannel_is_warped_per_channel():
    img = np.zeros((16, 16, 3))
    img[4, 4, 1] = 50.0  # (x=4, y=4) in channel 1
    out = warp_image(img, _translation(2.0, 3.0), None, (16, 16))
    assert out.shape == (16, 16, 3)
    # channel 1 impulse moved to (x=6, y=7) -> (row=7, col=6)
    assert np.unravel_index(np.argmax(out[..., 1]), out[..., 1].shape) == (7, 6)
