"""The decimate-and-rescale contract of the shipped COARSE anchor (``bin/tiled_coarse.py``).

This is the half of STARE that ``tiled_pipeline.register_slide`` has no analogue of, and that
no test previously touched: the shipped anchor estimates M0 on a **thumbnail** bounded by
``--max-dim`` and then lifts the fit back to full-resolution pixels with
``coarse_align.scale_transform_to_full_res``. Everything downstream — the tile plan, each
tile's ``source_region``, the stitch — is full-resolution, so forgetting the lift
under-translates the whole slide by exactly the decimation factor while still exiting 0.

Three properties are pinned:

  1. **The lift is applied.** The written M0 equals ``scale_transform_to_full_res(M0_ds,
     factor)`` and the written residual equals ``coarse_tre_ds * factor`` — both exact, against
     an oracle that re-reads the same decimated planes through the same primitives.
  2. **Decimation is a cost knob, not a semantic change.** The anchor estimated on a 1/4
     thumbnail and the anchor estimated with decimation disabled (``--max-dim 0``) agree with
     each other, and both recover the known full-resolution translation. This is the contract
     any unified STARE module must meet before it may claim to have absorbed this step.
  3. **Decimation does not leak into the tile plan.** ``ref_h``/``ref_w`` and the tile-plan read
     boxes are full-resolution regardless of the thumbnail size.
"""

from __future__ import annotations

import csv
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
pytest.importorskip("skimage")
pytest.importorskip("scipy")
pytest.importorskip("zarr")
tifffile = pytest.importorskip("tifffile")

import tiled_coarse  # noqa: E402
from coarse_align import estimate_rigid, scale_transform_to_full_res  # noqa: E402
from tiled_io import decimation_factor, open_lazy, read_decimated  # noqa: E402

N = 768
MAX_DIM = 192  # ceil(768 / 192) = 4
EXPECTED_FACTOR = 4
TRANSLATION = (21.0, -13.0)  # full-resolution px, (x, y)
ROTATION_DEG = 1.5


def _textured(seed, n):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    img = gaussian_filter(rng.uniform(0, 1, size=(n, n)), 2.0)
    return (img - img.min()) / (img.ptp() + 1e-9)


@pytest.fixture(scope="module")
def slide_pair(tmp_path_factory):
    """A 2-channel reference/moving OME-TIFF pair related by a known full-resolution rigid map."""
    from skimage.transform import EuclideanTransform
    from skimage.transform import warp as sk_warp

    tmp_path = tmp_path_factory.mktemp("coarse_decimation")
    dapi = _textured(0, N)
    marker = _textured(1, N)
    tform = EuclideanTransform(
        rotation=np.deg2rad(ROTATION_DEG), translation=TRANSLATION
    )
    ref = (np.stack([dapi, marker]) * 60000).astype(np.uint16)
    mov = (
        np.stack(
            [
                sk_warp(dapi, tform, mode="reflect"),
                sk_warp(marker, tform, mode="reflect"),
            ]
        )
        * 60000
    ).astype(np.uint16)

    ref_f = tmp_path / "ref.ome.tiff"
    mov_f = tmp_path / "mov.ome.tiff"
    tifffile.imwrite(str(ref_f), ref, photometric="minisblack")
    tifffile.imwrite(str(mov_f), mov, photometric="minisblack")
    return ref_f, mov_f


def _run_coarse(tmp_path, ref_f, mov_f, max_dim, tag, tile=256, halo=64):
    m0_f = tmp_path / f"{tag}_m0.json"
    tiles_f = tmp_path / f"{tag}_tiles.csv"
    rc = tiled_coarse.main(
        [
            "--reference",
            str(ref_f),
            "--moving",
            str(mov_f),
            "--nuclear-index",
            "0",
            "--tile",
            str(tile),
            "--halo",
            str(halo),
            "--max-dim",
            str(max_dim),
            "--out-m0",
            str(m0_f),
            "--out-tiles",
            str(tiles_f),
        ]
    )
    assert rc == 0
    return json.loads(m0_f.read_text()), tiles_f


def _thumbnail_oracle(ref_f, mov_f, max_dim, nuclear_index=0, model="euclidean"):
    """Re-derive the decimated fit through the same primitives tiled_coarse.py streams through."""
    ref_src, _d, ref_close = open_lazy(ref_f)
    try:
        mov_src, _d2, mov_close = open_lazy(mov_f)
        try:
            _c, h, w = ref_src.shape
            _mc, mh, mw = mov_src.shape
            factor = decimation_factor([(h, w), (mh, mw)], max_dim)
            ref_nuc = read_decimated(ref_src, nuclear_index, factor)
            mov_nuc = read_decimated(mov_src, nuclear_index, factor)
        finally:
            mov_close()
    finally:
        ref_close()
    m0_ds, tre_ds, n_inliers = estimate_rigid(ref_nuc, mov_nuc, model=model)
    return factor, m0_ds, float(tre_ds), int(n_inliers), (h, w)


def test_thumbnail_fit_is_lifted_to_full_resolution_pixels(tmp_path, slide_pair):
    """The written M0/residual are the thumbnail fit times the decimation factor — exactly."""
    ref_f, mov_f = slide_pair
    doc, _tiles_f = _run_coarse(tmp_path, ref_f, mov_f, MAX_DIM, "decimated")

    factor, m0_ds, tre_ds, n_inliers, (h, w) = _thumbnail_oracle(ref_f, mov_f, MAX_DIM)
    assert factor == EXPECTED_FACTOR, (
        "fixture no longer decimates; the test would be vacuous"
    )
    assert doc["coarse_factor"] == factor

    np.testing.assert_allclose(
        np.asarray(doc["M0"], dtype=float),
        scale_transform_to_full_res(m0_ds, factor),
        rtol=0,
        atol=0,
        err_msg=(
            "M0 was not lifted out of thumbnail pixels: everything downstream (tile plan, "
            "source_region, stitch) is full-resolution, so this under-translates the whole "
            f"slide by 1/{factor} with a clean exit code"
        ),
    )
    assert doc["coarse_tre"] == pytest.approx(tre_ds * factor, abs=0.0), (
        "the residual was not lifted with the map; it is compared against --halo in "
        "full-resolution px"
    )
    assert doc["n_inliers"] == n_inliers
    # the recorded frame is the FULL-resolution reference, not the thumbnail
    assert (doc["ref_h"], doc["ref_w"]) == (h, w) == (N, N)


def test_the_lifted_anchor_recovers_the_known_full_resolution_translation(
    tmp_path, slide_pair
):
    """A thumbnail fit must land the moving slide within a tile halo at FULL resolution."""
    ref_f, mov_f = slide_pair
    doc, _tiles_f = _run_coarse(tmp_path, ref_f, mov_f, MAX_DIM, "truth")
    m0 = np.asarray(doc["M0"], dtype=float)

    assert m0[0, 2] == pytest.approx(TRANSLATION[0], abs=2.0)
    assert m0[1, 2] == pytest.approx(TRANSLATION[1], abs=2.0)
    # the decimation factor is not a rotation change either
    assert np.degrees(np.arctan2(m0[1, 0], m0[0, 0])) == pytest.approx(
        ROTATION_DEG, abs=0.5
    )


def test_decimation_is_a_cost_knob_not_a_semantic_change(tmp_path, slide_pair):
    """``--max-dim 0`` (no decimation) and a 1/4 thumbnail must agree on the same anchor.

    This is the contract a unified STARE module has to meet: whether the anchor is estimated
    on a thumbnail or at native resolution is an option, not a different registration.
    """
    ref_f, mov_f = slide_pair
    decimated, _t1 = _run_coarse(tmp_path, ref_f, mov_f, MAX_DIM, "thumb")
    native, _t2 = _run_coarse(tmp_path, ref_f, mov_f, 0, "native")

    assert native["coarse_factor"] == 1
    assert decimated["coarse_factor"] == EXPECTED_FACTOR

    a = np.asarray(decimated["M0"], dtype=float)
    b = np.asarray(native["M0"], dtype=float)
    assert a[0, 2] == pytest.approx(b[0, 2], abs=2.0)
    assert a[1, 2] == pytest.approx(b[1, 2], abs=2.0)
    np.testing.assert_allclose(a[:2, :2], b[:2, :2], atol=0.02)


def test_the_tile_plan_stays_full_resolution_whatever_the_thumbnail_size(
    tmp_path, slide_pair
):
    """The tile plan is the frame every later step works in, so it must never be decimated."""
    ref_f, mov_f = slide_pair
    _doc, tiles_f = _run_coarse(
        tmp_path, ref_f, mov_f, MAX_DIM, "plan", tile=256, halo=64
    )

    with open(tiles_f, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == (N // 256) ** 2
    assert max(int(r["x1"]) for r in rows) == N
    assert max(int(r["y1"]) for r in rows) == N
    # read boxes are core + halo, clamped — still in full-resolution px
    assert max(int(r["rx1"]) for r in rows) == N
