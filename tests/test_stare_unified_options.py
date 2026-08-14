"""The two STARE drivers select behaviour through options; they do not reimplement it.

``bin/utils/tiled_pipeline.py`` now holds the method, and the four-process fan-out and the
in-process oracle are drivers over it. The two places they genuinely differed are options:

  * ``coarse_max_dim`` — estimate the anchor on a thumbnail and lift it back to
    full-resolution pixels, or estimate at native resolution (the lift is then an identity);
  * ``lazy_tiles`` — read only each tile's own window and moving crop (what ships), or
    rigid-pre-warp the whole moving plane once and slice out of it.

Why this file matters: before the unification, ``register_slide`` had NO analogue of either,
so every test built on it ran past the decimate-and-lift step and past the tile-boundary
offset arithmetic (``source_region`` + ``warp_image(out_origin=…, src_origin=…)``) — the two
places a real STARE bug is most likely to hide. Selecting the production options here puts
those code paths under the in-process tests too.

The equivalence is asserted with a tolerance, not exactly: the lazy path reads its reference
window at float32 (what a zarr region read of a uint16 slide gives, and what
``TILED_REG_TILE`` really does) while the eager path slices a float64 plane. That is a
deliberate property of the option, not slack in the test — a wrong offset moves a tile by
whole pixels, which is orders of magnitude outside the tolerance below.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
pytest.importorskip("skimage")
pytest.importorskip("scipy")

from coarse_align import estimate_rigid, scale_transform_to_full_res  # noqa: E402
from tiled_pipeline import ArraySource, TileWindows, register_slide  # noqa: E402


def _textured(seed=0, n=256):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    img = gaussian_filter(rng.uniform(0, 1, size=(n, n)), 2.0)
    return ((img - img.min()) / (img.ptp() + 1e-9)).astype(np.float32)


def _shifted(ref, dx=9.0, dy=-6.0, rotation_deg=1.5):
    from skimage.transform import EuclideanTransform
    from skimage.transform import warp as sk_warp

    tform = EuclideanTransform(rotation=np.deg2rad(rotation_deg), translation=(dx, dy))
    return sk_warp(ref, tform, order=1, mode="reflect").astype(np.float32), tform


def test_lazy_and_eager_tile_windows_measure_the_same_residuals():
    """The production windowing and the whole-slide pre-warp must agree tile by tile."""
    ref = _textured(seed=7, n=256)
    mov, tform = _shifted(ref)
    m0 = np.asarray(tform.params, dtype=float)

    ref_src = ArraySource(np.asarray(ref, dtype=float))
    mov_src = ArraySource(np.asarray(mov, dtype=float))
    lazy = TileWindows(ref_src, mov_src, m0, lazy=True)
    eager = TileWindows(ref_src, mov_src, m0, lazy=False)

    # an interior tile, well clear of any edge, so no clamping is involved
    box = (64, 64, 192, 192)
    ref_l, mov_l = lazy(box)
    ref_e, mov_e = eager(box)

    assert ref_l.shape == ref_e.shape == (128, 128)
    np.testing.assert_allclose(ref_l, ref_e, rtol=0, atol=1e-3)
    # the moving tile is where the offset arithmetic lives: out_origin shifts the output
    # window, src_origin shifts the crop's own frame. Getting either wrong moves the tile
    # by whole pixels.
    np.testing.assert_allclose(mov_l, mov_e, rtol=0, atol=1e-6)


def test_the_lazy_window_reads_only_the_region_the_tile_needs():
    """The property the production mode exists for — and that no in-process test could see."""
    ref = _textured(seed=8, n=256)
    mov, tform = _shifted(ref)
    m0 = np.asarray(tform.params, dtype=float)

    reads = []

    class _Spy(ArraySource):
        def __getitem__(self, key):
            _c, ys, xs = key
            reads.append((ys.stop - ys.start) * (xs.stop - xs.start))
            return super().__getitem__(key)

    windows = TileWindows(
        _Spy(np.asarray(ref, dtype=float)), _Spy(np.asarray(mov, dtype=float)), m0
    )
    windows((64, 64, 192, 192))  # a tile covering 1/4 of the frame

    assert reads, "expected at least one region read"
    full = ref.shape[0] * ref.shape[1]
    assert max(reads) < 0.5 * full, (
        f"largest region read is {max(reads)}px of a {full}px frame -- the lazy window is "
        "not bounded by the tile"
    )


def test_an_edge_tile_whose_source_falls_outside_the_slide_yields_zeros():
    """The clamping branch, reachable from the in-process driver for the first time."""
    ref = _textured(seed=9, n=64)
    far = np.asarray([[1.0, 0.0, 10_000.0], [0.0, 1.0, 10_000.0], [0.0, 0.0, 1.0]])
    windows = TileWindows(
        ArraySource(np.asarray(ref, dtype=float)),
        ArraySource(np.asarray(ref, dtype=float)),
        far,
    )
    _ref_tile, mov_tile = windows((0, 0, 32, 32))
    assert mov_tile.shape == (32, 32)
    assert np.array_equal(mov_tile, np.zeros((32, 32)))


def test_register_slide_agrees_with_itself_under_either_windowing():
    """Selecting the production windowing must not change the registration."""
    ref = _textured(seed=10, n=256)
    mov, _tform = _shifted(ref)
    kw = dict(tile=64, halo=32, gate_tre=0.5)

    eager = register_slide(ref, mov, lazy_tiles=False, **kw)
    lazy = register_slide(ref, mov, lazy_tiles=True, **kw)

    # the anchor does not depend on the windowing at all
    np.testing.assert_allclose(
        np.asarray(lazy["M0"], dtype=float),
        np.asarray(eager["M0"], dtype=float),
        rtol=0,
        atol=0,
    )
    assert (lazy["entry"]["mesh"] is None) == (eager["entry"]["mesh"] is None)
    np.testing.assert_allclose(lazy["tre_px"], eager["tre_px"], atol=5e-3)
    np.testing.assert_allclose(lazy["registered"], eager["registered"], atol=5e-3)


def test_the_coarse_anchor_option_decimates_and_lifts_back_to_full_resolution():
    """``coarse_max_dim`` is the fan-out's thumbnail knob, now available to both drivers."""
    n = 256
    ref = _textured(seed=11, n=n)
    mov, tform = _shifted(ref, dx=12.0, dy=-8.0, rotation_deg=1.0)

    native = register_slide(ref, mov, tile=64, halo=32, gate_tre=0.5)
    thumb = register_slide(
        ref, mov, tile=64, halo=32, gate_tre=0.5, coarse_max_dim=n // 2
    )

    # the anchor really was estimated on a 1/2 thumbnail and lifted...
    expected_m0, _tre, _n = _lifted_oracle(ref, mov, factor=2)
    np.testing.assert_allclose(
        np.asarray(thumb["M0"], dtype=float), expected_m0, rtol=0, atol=0
    )
    # ...and the lift means both anchors describe the SAME full-resolution translation,
    # to within the accuracy the thumbnail can support.
    a = np.asarray(thumb["M0"], dtype=float)
    b = np.asarray(native["M0"], dtype=float)
    assert a[0, 2] == pytest.approx(b[0, 2], abs=1.5)
    assert a[1, 2] == pytest.approx(b[1, 2], abs=1.5)
    assert a[0, 2] == pytest.approx(12.0, abs=1.5)
    assert a[1, 2] == pytest.approx(-8.0, abs=1.5)


def _lifted_oracle(ref, mov, factor):
    """estimate on the strided planes, then lift — the contract, spelled out independently."""
    m0_ds, tre_ds, n_inliers = estimate_rigid(
        np.asarray(ref, dtype=float)[::factor, ::factor],
        np.asarray(mov, dtype=float)[::factor, ::factor],
    )
    return scale_transform_to_full_res(m0_ds, factor), float(tre_ds) * factor, n_inliers


def test_the_default_options_are_the_pre_unification_behaviour():
    """The in-process driver's defaults must stay native-resolution + whole-slide pre-warp.

    Flipping either default would silently change every existing test's meaning, and the
    benchmark oracle's, without any of them failing.
    """
    import inspect

    sig = inspect.signature(register_slide)
    assert sig.parameters["coarse_max_dim"].default == 0
    assert sig.parameters["lazy_tiles"].default is False
